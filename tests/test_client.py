"""End-to-end: the real client against the mock ingest server, with faults injected."""

import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "handoff"), str(ROOT)]

from core.client import Cancelled, IngestClient, IngestError, _origin  # noqa: E402
from server.mock_ingest import IngestState, serve_in_thread  # noqa: E402
from tools.make_sample_glb import build_sample  # noqa: E402

SAMPLE = build_sample()
MANIFEST = {"schema": "handoff.manifest/1", "asset": {"name": "sample"}}


class ClientAgainstMockServer(unittest.TestCase):
    def start(self, **faults):
        self.state = IngestState(token="t0ken", processing_delay=0, log=lambda msg: None, **faults)
        self.server, self.url = serve_in_thread(self.state)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def client(self, token="t0ken", **kwargs):
        self.lines = []
        return IngestClient(self.url, token, sleep=lambda s: None, poll_interval=0,
                            log=self.lines.append, **kwargs)

    def test_happy_path(self):
        self.start()
        client = self.client()
        asset = client.publish(SAMPLE, "sample.glb", MANIFEST)
        self.assertEqual(asset["status"], "ready")
        self.assertEqual(asset["report"]["triangles"], 48 * 24 * 2 - 2 * 48 + 12)
        self.assertEqual(asset["manifest_schema"], "handoff.manifest/1")

    def test_retries_through_503s_and_429(self):
        self.start(fail_first=2, rate_limit=1)
        client = self.client()
        self.assertEqual(client.publish(SAMPLE, "sample.glb", MANIFEST)["status"], "ready")
        self.assertTrue(any("429" in line for line in self.lines))
        self.assertTrue(any("503" in line for line in self.lines))

    def test_lost_ack_does_not_create_a_duplicate(self):
        self.start(lose_ack=True)
        client = self.client()
        self.assertEqual(client.publish(SAMPLE, "sample.glb", MANIFEST)["status"], "ready")
        # The server did the work on the first try; the retry must get that upload back.
        self.assertEqual(len(self.state.uploads), 1)

    def test_bad_token_fails_fast(self):
        self.start()
        client = self.client(token="wrong")
        with self.assertRaises(IngestError) as ctx:
            client.publish(SAMPLE, "sample.glb", MANIFEST)
        self.assertEqual(ctx.exception.status, 401)
        self.assertEqual(client.attempts, 1)

    def test_gives_up_after_max_attempts(self):
        self.start(fail_first=99)
        client = self.client(max_attempts=3)
        with self.assertRaises(IngestError) as ctx:
            client.publish(SAMPLE, "sample.glb", MANIFEST)
        self.assertTrue(ctx.exception.retryable)
        self.assertEqual(client.attempts, 3)

    def test_server_rejection_surfaces_the_reason(self):
        self.start(max_triangles=100)
        with self.assertRaisesRegex(IngestError, "exceeds the 100 limit"):
            self.client().publish(SAMPLE, "sample.glb", MANIFEST)

    def test_corrupt_bytes_are_rejected_by_processing(self):
        self.start()
        corrupt = b"FBX " + SAMPLE[4:]
        with self.assertRaisesRegex(IngestError, "magic"):
            self.client().publish(corrupt, "sample.glb", MANIFEST)

    def test_cancel(self):
        self.start(fail_first=99)
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(Cancelled):
            self.client(cancel=cancel).publish(SAMPLE, "sample.glb", MANIFEST)


class TokenScoping(unittest.TestCase):
    def test_token_only_goes_to_the_api_origin(self):
        api = _origin("https://ingest.example.com/v1")
        self.assertEqual(api, _origin("https://ingest.example.com/v1/uploads"))
        self.assertNotEqual(api, _origin("https://storage.example-cdn.com/bucket/obj?sig=x"))
        self.assertNotEqual(api, _origin("http://ingest.example.com/v1"))


if __name__ == "__main__":
    unittest.main()
