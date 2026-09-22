"""Local stand-in for an asset ingest API, so the add-on has something real to talk to.

    POST /v1/uploads                reserve an upload, get a signed upload_url
    PUT  /v1/uploads/{id}/content   send the GLB bytes (size and sha256 verified)
    POST /v1/uploads/{id}/complete  hand off for processing, get an asset_id
    GET  /v1/assets/{id}            poll until ready or failed

Fault injection reproduces the failures the client is built to survive:

    --fail-first N    first N requests to each route return 503
    --rate-limit N    first N upload reservations return 429 with Retry-After
    --lose-ack        the first reservation succeeds, but its response is replaced with
                      a 502 (the case idempotency keys exist for)

Run:  python server/mock_ingest.py --token dev-token --fail-first 1 --lose-ack
"""

import argparse
import hashlib
import json
import re
import secrets
import sys
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "handoff"))
from core import glb  # noqa: E402  (core is bpy-free, so it imports fine outside Blender)

MAX_BYTES = 200 * 1024 * 1024

ROUTES = [
    ("POST", re.compile(r"^/v1/uploads$"), "create"),
    ("PUT", re.compile(r"^/v1/uploads/(?P<id>[\w-]+)/content$"), "content"),
    ("POST", re.compile(r"^/v1/uploads/(?P<id>[\w-]+)/complete$"), "complete"),
    ("GET", re.compile(r"^/v1/assets/(?P<id>[\w-]+)$"), "asset"),
]


class IngestState:
    def __init__(self, *, token="dev-token", fail_first=0, rate_limit=0, lose_ack=False,
                 processing_delay=1.5, max_triangles=2_000_000, log=print):
        self.token = token
        self.fail_first = fail_first
        self.rate_limit = rate_limit
        self.lose_ack = lose_ack
        self.processing_delay = processing_delay
        self.max_triangles = max_triangles
        self.log = log
        self.lock = threading.Lock()
        self.uploads = {}
        self.assets = {}
        self.idempotency = {}
        self.hits = Counter()
        self.ack_lost = False

    def injected_fault(self, route):
        with self.lock:
            self.hits[route] += 1
            n = self.hits[route]
        if n <= self.fail_first:
            return 503, {"error": "injected: service unavailable"}, {}
        if route == "create" and n - self.fail_first <= self.rate_limit:
            return 429, {"error": "injected: rate limited"}, {"Retry-After": "1"}
        return None


def make_handler(st):
    class Handler(BaseHTTPRequestHandler):
        server_version = "mock-ingest/0.1"

        def log_message(self, fmt, *args):
            st.log(f"  {self.command} {self.path.split('?')[0]} -> {args[1] if len(args) > 1 else ''}")

        def do_GET(self):
            self._route()

        def do_POST(self):
            self._route()

        def do_PUT(self):
            self._route()

        def _route(self):
            # Always drain the body first; replying early to a large upload makes some
            # clients see a connection reset instead of the actual error.
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            url = urlsplit(self.path)

            for method, pattern, name in ROUTES:
                match = pattern.match(url.path)
                if match and method == self.command:
                    break
            else:
                return self._reply(404, {"error": f"no route for {self.command} {url.path}"})

            # The content route is authorized by its URL signature, like a presigned URL;
            # everything else needs the bearer token.
            if name != "content" and self.headers.get("Authorization") != f"Bearer {st.token}":
                return self._reply(401, {"error": "missing or invalid bearer token"})

            fault = st.injected_fault(name)
            if fault:
                return self._reply(*fault)
            handler = getattr(self, f"_{name}")
            return handler(body, match.groupdict(), parse_qs(url.query))

        def _create(self, body, params, query):
            try:
                req = json.loads(body)
            except ValueError:
                return self._reply(400, {"error": "body is not JSON"})
            if req.get("content_type") != "model/gltf-binary":
                return self._reply(415, {"error": "only model/gltf-binary (.glb) is accepted"})
            if not isinstance(req.get("bytes"), int) or req["bytes"] <= 0:
                return self._reply(422, {"error": "bytes must be a positive integer"})
            if req["bytes"] > MAX_BYTES:
                return self._reply(413, {"error": f"file exceeds the {MAX_BYTES // 2**20} MB limit"})
            if not re.fullmatch(r"[0-9a-f]{64}", str(req.get("sha256", ""))):
                return self._reply(422, {"error": "sha256 must be 64 lowercase hex characters"})

            key = self.headers.get("Idempotency-Key")
            with st.lock:
                if key and key in st.idempotency:
                    upload = st.uploads[st.idempotency[key]]
                    st.log(f"  idempotent replay: {key[:8]} -> {upload['id']}")
                    return self._reply(200, self._upload_view(upload))
                upload = {
                    "id": "upl_" + secrets.token_hex(6),
                    "sig": secrets.token_urlsafe(16),
                    "filename": req.get("filename", "asset.glb"),
                    "bytes": req["bytes"],
                    "sha256": req["sha256"],
                    "data": None,
                    "asset_id": None,
                }
                st.uploads[upload["id"]] = upload
                if key:
                    st.idempotency[key] = upload["id"]
                drop = st.lose_ack and not st.ack_lost
                st.ack_lost = st.ack_lost or drop
            if drop:
                return self._reply(502, {"error": "injected: upstream dropped the response"})
            return self._reply(201, self._upload_view(upload))

        def _content(self, body, params, query):
            upload = st.uploads.get(params["id"])
            if upload is None:
                return self._reply(404, {"error": "unknown upload"})
            if query.get("sig", [""])[0] != upload["sig"]:
                return self._reply(403, {"error": "bad or missing upload signature"})
            if len(body) != upload["bytes"]:
                return self._reply(400, {"error": f"expected {upload['bytes']} bytes, got {len(body)}"})
            if hashlib.sha256(body).hexdigest() != upload["sha256"]:
                return self._reply(400, {"error": "checksum mismatch: bytes changed in transit"})
            upload["data"] = body
            return self._reply(200, {"received": len(body)})

        def _complete(self, body, params, query):
            upload = st.uploads.get(params["id"])
            if upload is None:
                return self._reply(404, {"error": "unknown upload"})
            if upload["data"] is None:
                return self._reply(409, {"error": "upload has no content yet"})
            with st.lock:
                if upload["asset_id"]:  # completing twice returns the same asset
                    return self._reply(202, {"asset_id": upload["asset_id"], "status": "processing"})
                try:
                    report = glb.inspect(upload["data"])
                    issues = list(report["issues"])
                except glb.GlbError as exc:
                    report, issues = None, [str(exc)]
                if report and report["triangles"] > st.max_triangles:
                    issues.append(f"{report['triangles']:,} triangles exceeds the {st.max_triangles:,} limit")
                manifest = (json.loads(body or b"{}")).get("manifest") or {}
                asset_id = "ast_" + secrets.token_hex(6)
                st.assets[asset_id] = {
                    "asset_id": asset_id,
                    "name": manifest.get("asset", {}).get("name", upload["filename"]),
                    "ready_at": time.monotonic() + st.processing_delay,
                    "report": report,
                    "issues": issues,
                    "manifest_schema": manifest.get("schema"),
                }
                upload["asset_id"] = asset_id
            return self._reply(202, {"asset_id": asset_id, "status": "processing"})

        def _asset(self, body, params, query):
            asset = st.assets.get(params["id"])
            if asset is None:
                return self._reply(404, {"error": "unknown asset"})
            view = {k: v for k, v in asset.items() if k != "ready_at"}
            if time.monotonic() < asset["ready_at"]:
                view["status"] = "processing"
            else:
                view["status"] = "failed" if asset["issues"] else "ready"
            return self._reply(200, view)

        def _upload_view(self, upload):
            host = self.headers.get("Host", "127.0.0.1")
            return {
                "upload_id": upload["id"],
                "upload_url": f"http://{host}/v1/uploads/{upload['id']}/content?sig={upload['sig']}",
                "expires_in": 900,
            }

        def _reply(self, status, payload, headers=None):
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(raw)

    return Handler


def serve_in_thread(st, host="127.0.0.1", port=0):
    """Start a server on a background thread; returns (server, base_url). Used by the tests."""
    server = ThreadingHTTPServer((host, port), make_handler(st))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://{host}:{server.server_address[1]}"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--token", default="dev-token")
    parser.add_argument("--fail-first", type=int, default=0, metavar="N")
    parser.add_argument("--rate-limit", type=int, default=0, metavar="N")
    parser.add_argument("--lose-ack", action="store_true")
    parser.add_argument("--processing-delay", type=float, default=1.5, metavar="SECONDS")
    parser.add_argument("--max-triangles", type=int, default=2_000_000)
    args = parser.parse_args()

    st = IngestState(token=args.token, fail_first=args.fail_first, rate_limit=args.rate_limit,
                     lose_ack=args.lose_ack, processing_delay=args.processing_delay,
                     max_triangles=args.max_triangles)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(st))
    print(f"mock ingest listening on http://127.0.0.1:{args.port}  (token: {args.token})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
