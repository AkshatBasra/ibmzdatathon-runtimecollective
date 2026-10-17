import json
import os
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from pulseops.engine import PulseOpsEngine  # noqa: E402
from pulseops.server import App, make_handler  # noqa: E402


class TestAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = App(PulseOpsEngine(), tick_interval=0.005)
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.app))
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.app.start()

    @classmethod
    def tearDownClass(cls):
        cls.app.stop()
        cls.srv.shutdown()
        cls.srv.server_close()

    def req(self, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        r = urllib.request.Request(self.base + path, data=data, method="POST" if body is not None else "GET",
                                   headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            finally:
                e.close()

    def test_full_flow_over_http(self):
        self.req("/api/reset", {})
        code, s = self.req("/api/state")
        self.assertEqual((code, s["health"]), (200, "healthy"))
        self.assertTrue(s["simulated"])
        self.assertEqual(self.req("/api/fault", {"kind": "load_spike", "magnitude": 2})[0], 200)
        deadline = time.time() + 10
        inc = None
        while time.time() < deadline:
            inc = self.req("/api/state")[1]["incident"]
            if inc and inc["state"] == "awaiting_approval":
                break
            time.sleep(0.02)
        self.assertIsNotNone(inc)
        self.assertEqual(inc["state"], "awaiting_approval")
        # approving an action that is not pending is rejected
        self.assertEqual(self.req("/api/approve", {"action_id": "A999"})[0], 400)
        code, _ = self.req("/api/approve", {"action_id": inc["pending_action"], "who": "test"})
        self.assertEqual(code, 200)
        while time.time() < deadline + 10:
            inc = self.req("/api/state")[1]["incident"]
            if inc["state"] not in ("verifying",):
                break
            time.sleep(0.02)
        self.assertEqual(inc["state"], "recovered")
        self.assertEqual(self.req("/api/incident/" + inc["id"])[0], 200)
        self.assertEqual(self.req("/api/incident/NOPE")[0], 404)

    def test_bad_input_rejected(self):
        self.assertEqual(self.req("/api/fault", {"kind": "format_disk", "magnitude": 2})[0], 400)
        self.assertEqual(self.req("/api/fault", {"kind": "load_spike", "magnitude": 1000})[0], 400)
        self.assertEqual(self.req("/api/fault", {"kind": "load_spike", "magnitude": True})[0], 400)
        self.assertEqual(self.req("/api/nope", {})[0], 404)
        r = urllib.request.Request(self.base + "/api/fault", data=b"{not json", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(r, timeout=5)
        self.assertEqual(cm.exception.code, 400)

    def test_dashboard_served(self):
        with urllib.request.urlopen(self.base + "/", timeout=5) as r:
            html = r.read().decode()
        self.assertIn("PulseOps", html)
        self.assertNotIn("http://", html.replace("http://www.w3.org", ""))   # no external assets (offline-safe)
        self.assertNotIn("https://", html)


if __name__ == "__main__":
    unittest.main()
