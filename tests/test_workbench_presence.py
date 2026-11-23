"""Top-right online count and the removed upload kicker label."""
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from audit_core import workbench_server
from audit_core.workbench_server import Handler, WorkbenchCatalog, WorkbenchHTTPServer


class PresenceEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        workbench_server.PRESENCE._seen.clear()
        self.server = WorkbenchHTTPServer(("127.0.0.1", 0), Handler,
                                          catalog=WorkbenchCatalog(self.root))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        workbench_server.PRESENCE._seen.clear()
        self.temporary.cleanup()

    def get(self, path: str) -> dict:
        with self.opener.open(self.base + path, timeout=5) as response:
            return json.load(response)

    def post(self, path: str, payload: dict) -> dict:
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with self.opener.open(request, timeout=5) as response:
            return json.load(response)

    def test_counts_distinct_clients_and_ignores_repeats(self):
        self.assertEqual(self.get("/api/presence")["online"], 0)
        self.assertEqual(self.post("/api/presence", {"client_id": "tab-a"})["online"], 1)
        self.assertEqual(self.post("/api/presence", {"client_id": "tab-b"})["online"], 2)
        self.assertEqual(self.post("/api/presence", {"client_id": "tab-a"})["online"], 2)
        self.assertEqual(self.get("/api/presence")["online"], 2)

    def test_stale_clients_fall_out_of_the_window(self):
        self.post("/api/presence", {"client_id": "tab-a"})
        workbench_server.PRESENCE._seen["tab-old"] = (
            time.monotonic() - workbench_server.PRESENCE.WINDOW_SECONDS - 5)
        document = self.get("/api/presence")
        self.assertEqual(document["online"], 1)
        self.assertNotIn("tab-old", workbench_server.PRESENCE._seen)

    def test_heartbeat_requires_a_client_id(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post("/api/presence", {})
        self.assertEqual(caught.exception.code, 400)

    def test_upload_view_keeps_only_the_upload_panel(self):
        with self.opener.open(self.base + "/", timeout=5) as response:
            page = response.read().decode("utf-8")
        upload = page[page.index('id="as-view-upload"'):]
        view = upload[:upload.index('id="as-upload-form"')]
        self.assertNotIn("资料接入", view)
        self.assertNotIn("资料上传", view)
        self.assertNotIn('class="as-hero"', view)
        self.assertIn("上传核销资料", view)
        self.assertIn('id="as-presence-count"', page)
        self.assertIn("/api/presence", page)


if __name__ == "__main__":
    unittest.main()
