# SPDX-License-Identifier: GPL-3.0-or-later
"""HTTP client for the ingest API.

Stdlib only: Blender's bundled Python has no guaranteed third-party packages, and an
add-on that pip-installs into a user's Blender is a support ticket waiting to happen.

Flow (the same shape as most presigned-upload services):
    POST /v1/uploads               reserve an upload        -> upload_id, upload_url
    PUT  <upload_url>              send the bytes
    POST /v1/uploads/{id}/complete hand off for processing  -> asset_id
    GET  /v1/assets/{id}           poll until ready or failed

Every step is safe to retry. The two POSTs carry an Idempotency-Key, so if the server did
the work but the response got lost, the retry gets the original result back instead of
creating a duplicate. PUT is idempotent by definition.
"""

import hashlib
import json
import random
import time
import uuid
from urllib import error, parse, request

GLB_MIME = "model/gltf-binary"
USER_AGENT = "handoff-blender/0.1"
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_RETRY_AFTER = 60.0


class IngestError(Exception):
    def __init__(self, message, status=None, retryable=False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class Cancelled(Exception):
    """The user pressed Cancel; not a failure."""


class IngestClient:
    def __init__(self, base_url, token, *, timeout=30.0, max_attempts=5, backoff_base=0.5,
                 backoff_cap=8.0, poll_interval=1.0, poll_timeout=180.0, log=None,
                 cancel=None, sleep=None):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout
        self.log = log or (lambda msg: None)
        self.cancel = cancel  # threading.Event, set from the UI thread
        self._sleep = sleep or time.sleep
        self._origin = _origin(self.base_url)
        self.attempts = 0  # total HTTP attempts; useful in support logs and tests

    def publish(self, data, filename, manifest):
        digest = hashlib.sha256(data).hexdigest()
        key = str(uuid.uuid4())
        self.log(f"{filename}: {len(data):,} bytes, sha256 {digest[:12]}")

        upload = self._json("POST", "/v1/uploads", {
            "filename": filename,
            "content_type": GLB_MIME,
            "bytes": len(data),
            "sha256": digest,
        }, idempotency_key=key)
        self.log(f"upload {upload['upload_id']} reserved")

        self._send("PUT", upload["upload_url"], data, {
            "Content-Type": GLB_MIME,
            "X-Content-SHA256": digest,
        })
        self.log("bytes uploaded, checksum accepted")

        job = self._json("POST", f"/v1/uploads/{upload['upload_id']}/complete",
                         {"manifest": manifest}, idempotency_key=f"{key}:complete")
        self.log(f"asset {job['asset_id']} processing")
        return self._poll(job["asset_id"])

    def _poll(self, asset_id):
        deadline = time.monotonic() + self.poll_timeout
        while True:
            asset = self._json("GET", f"/v1/assets/{asset_id}")
            status = asset.get("status")
            if status == "ready":
                self.log(f"asset {asset_id} ready")
                return asset
            if status == "failed":
                reasons = "; ".join(asset.get("issues") or ["no reason given"])
                raise IngestError(f"ingest rejected the asset: {reasons}")
            if time.monotonic() > deadline:
                raise IngestError(f"asset {asset_id} still '{status}' after {self.poll_timeout:.0f}s")
            self._wait(self.poll_interval)

    def _json(self, method, path, payload=None, idempotency_key=None):
        headers = {"Accept": "application/json"}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        raw = self._send(method, self.base_url + path, body, headers)
        try:
            return json.loads(raw or b"{}")
        except ValueError as exc:
            raise IngestError(f"{method} {path}: response was not JSON") from exc

    def _send(self, method, url, body, headers):
        headers = dict(headers, **{"User-Agent": USER_AGENT})
        # Only send the API token to the API itself. A presigned upload_url usually lives
        # on a storage host; forwarding the bearer token there would leak it.
        if self.token and _origin(url) == self._origin:
            headers["Authorization"] = f"Bearer {self.token}"
        label = f"{method} {parse.urlsplit(url).path}"

        for attempt in range(1, self.max_attempts + 1):
            self._check_cancel()
            self.attempts += 1
            req = request.Request(url, data=body, headers=headers, method=method)
            try:
                with request.urlopen(req, timeout=self.timeout) as resp:
                    return resp.read()
            except error.HTTPError as exc:
                detail = _error_detail(exc)
                if exc.code not in RETRYABLE_STATUS:
                    raise IngestError(f"{label} -> {exc.code}: {detail}", status=exc.code) from None
                delay = _retry_after(exc) if exc.code == 429 else None
                if delay is None:
                    delay = self._backoff(attempt)
                problem = f"{exc.code} {detail}"
            except (error.URLError, TimeoutError, ConnectionError) as exc:
                problem = f"network error: {getattr(exc, 'reason', exc)}"
                delay = self._backoff(attempt)

            if attempt == self.max_attempts:
                raise IngestError(f"{label} failed after {attempt} attempts ({problem})", retryable=True)
            self.log(f"{label} -> {problem}; retry {attempt}/{self.max_attempts - 1} in {delay:.1f}s")
            self._wait(delay)

    def _backoff(self, attempt):
        # Exponential with "equal jitter": never less than half the step, so retries from
        # many artists hitting the same outage spread out instead of arriving in waves.
        step = min(self.backoff_cap, self.backoff_base * 2 ** (attempt - 1))
        return step / 2 + random.uniform(0, step / 2)

    def _wait(self, seconds):
        if self.cancel is not None:
            if self.cancel.wait(seconds):
                raise Cancelled()
        else:
            self._sleep(seconds)

    def _check_cancel(self):
        if self.cancel is not None and self.cancel.is_set():
            raise Cancelled()


def _origin(url):
    parts = parse.urlsplit(url)
    return (parts.scheme, parts.hostname, parts.port)


def _retry_after(exc):
    value = exc.headers.get("Retry-After") if exc.headers else None
    try:
        return min(float(value), MAX_RETRY_AFTER)
    except (TypeError, ValueError):
        return None  # absent, or an HTTP-date; fall back to our own backoff


def _error_detail(exc):
    try:
        raw = exc.read()
    except Exception:
        raw = b""
    try:
        body = json.loads(raw)
        return body.get("error") or body.get("message") or str(body)
    except (ValueError, AttributeError):
        text = raw.decode("utf-8", "replace").strip()
        return text[:200] or str(exc.reason)
