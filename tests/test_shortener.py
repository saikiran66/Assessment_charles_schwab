import unittest
from pathlib import Path

from shortener.database import Database
from shortener.service import NotFoundError, ShortenerService, ValidationError


class ShortenerServiceTests(unittest.TestCase):
    def setUp(self):
        self.db_path = Path.cwd() / ".test-data" / f"shortener-{self._testMethodName}.sqlite3"
        self.db_path.parent.mkdir(exist_ok=True)
        self.database = Database(self.db_path)
        self.database.initialize()
        self.service = ShortenerService(self.database, "http://localhost:8000")

    def tearDown(self):
        for path in (self.db_path, Path(str(self.db_path) + "-wal"), Path(str(self.db_path) + "-shm")):
            path.unlink(missing_ok=True)

    def test_create_resolve_and_analytics(self):
        created = self.service.create_link("https://example.com/a", "test42")
        self.assertEqual(created["short_url"], "http://localhost:8000/test42")
        self.assertEqual(self.service.resolve("test42", "https://source.example", "test-agent"), "https://example.com/a")
        report = self.service.analytics("test42")
        self.assertEqual(report["click_count"], 1)
        self.assertEqual(sum(day["clicks"] for day in report["daily_clicks"]), 1)

    def test_rejects_unsafe_or_malformed_url(self):
        for url in ("javascript:alert(1)", "//example.com", "https://user:secret@example.com", "https://"):
            with self.subTest(url=url), self.assertRaises(ValidationError):
                self.service.create_link(url)

    def test_rejects_duplicate_code_and_expired_link(self):
        self.service.create_link("https://example.com", "same42")
        with self.assertRaises(ValidationError):
            self.service.create_link("https://other.example", "same42")
        with self.assertRaises(ValidationError):
            self.service.create_link("https://example.com", expires_at="2000-01-01T00:00:00Z")

    def test_unknown_code_is_not_found(self):
        with self.assertRaises(NotFoundError):
            self.service.resolve("missing")


if __name__ == "__main__":
    unittest.main()
