import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from shortener.config import Settings
from shortener.server import ApiHandler, Application


class HttpApiTests(unittest.TestCase):
    def setUp(self):
        self.db_path = Path.cwd() / ".test-data" / "api.sqlite3"
        self.db_path.parent.mkdir(exist_ok=True)
        settings = Settings(database_path=self.db_path, base_url="http://127.0.0.1")
        app = Application(settings)
        handler = type("TestHandler", (ApiHandler,), {"app": app})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        for path in (self.db_path, Path(str(self.db_path) + "-wal"), Path(str(self.db_path) + "-shm")):
            path.unlink(missing_ok=True)

    def test_create_redirect_and_analytics_round_trip(self):
        request = Request(self.base + "/api/links", data=json.dumps({"url": "https://example.com", "custom_code": "round1"}).encode(),
                          headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(request) as response:
            payload = json.loads(response.read())
            self.assertEqual(response.status, 201)
        no_redirect = build_opener(_NoRedirect())
        with self.assertRaises(HTTPError) as redirect:
            no_redirect.open(self.base + "/round1")
        self.assertEqual(redirect.exception.code, 302)
        self.assertEqual(redirect.exception.headers["Location"], "https://example.com")
        with urlopen(self.base + "/api/links/round1/analytics") as response:
            report = json.loads(response.read())
        self.assertEqual(report["click_count"], 1)
        self.assertEqual(payload["code"], "round1")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


if __name__ == "__main__":
    unittest.main()
