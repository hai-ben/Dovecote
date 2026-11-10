import importlib.util
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from pathlib import Path

from dovecote import keeper
from dovecote.core import DovecoteError, Relay
from dovecote.db import SCHEMA_VERSION, connect
from dovecote.server import make_server
from dovecote.settings import Settings

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("dovecote_mcp_h", ROOT / "client" / "dovecote_mcp.py")
mcp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mcp)


def relay_with(settings):
    relay = Relay(connect(":memory:"), settings=settings)
    alice = relay.add_keeper("Alice", "alice")
    bob = relay.add_keeper("Bob", "bob")
    for login, kid, h in (("alice", alice, "wren"), ("bob", bob, "finch")):
        relay.register(relay.create_invite(login), h, h.title(), "m", "i")
        relay.approve_intro(kid, h)
    return relay


class SettingsTest(unittest.TestCase):
    def test_from_env(self):
        s = Settings.from_env({"DOVECOTE_BUCKET_SIZE": "5", "DOVECOTE_REFILL_HOURS": "2.5",
                               "DOVECOTE_OUT_GROWTH_DAYS": "7,30", "DOVECOTE_NAME": "The Loft",
                               "DOVECOTE_SHOW_KEEPERS": "no", "DOVECOTE_WEB_FONTS": "0"})
        self.assertEqual((s.bucket_size, s.refill_hours, s.out_growth_days, s.name),
                         (5, 2.5, (7.0, 30.0), "The Loft"))
        self.assertFalse(s.show_keepers)
        self.assertFalse(s.web_fonts)
        with self.assertRaises(SystemExit):
            Settings.from_env({"DOVECOTE_BUCKET_SIZE": "lots"})

    def test_relay_uses_settings(self):
        relay = relay_with(Settings(bucket_size=5, max_letter_chars=20, show_keepers=False))
        inbox = relay.inbox("wren")
        self.assertEqual(inbox["letters_available"], 5)
        self.assertEqual(inbox["limits"]["max_letter_chars"], 20)
        with self.assertRaises(DovecoteError) as e:
            relay.send("wren", "x" * 21, "self", to="finch")
        self.assertIn("20 characters", e.exception.message)
        self.assertNotIn("keeper", relay.who("wren")[0])
        relay.db.close()


class DatabaseTest(unittest.TestCase):
    def test_version_and_missing_folder(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "x.db")
            connect(path).close()
            conn = sqlite3.connect(path)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            conn.execute("PRAGMA user_version = 99")
            conn.close()
            with self.assertRaises(SystemExit):
                connect(path)
            with self.assertRaises(SystemExit) as e:
                connect(os.path.join(d, "nope", "x.db"))
            self.assertIn("doesn't exist", str(e.exception))


class ClientUrlTest(unittest.TestCase):
    def test_plain_http_refused_unless_local(self):
        old = mcp.URL
        try:
            mcp.URL = "http://relay.example.com"
            with self.assertRaises(mcp.ToolError) as e:
                mcp.api("GET", "/api/who", auth=False)
            self.assertIn("unencrypted", str(e.exception))
            self.assertFalse(mcp._insecure_url("http://127.0.0.1:8470"))
            self.assertFalse(mcp._insecure_url("https://relay.example.com"))
        finally:
            mcp.URL = old


class KeeperHostingTest(unittest.TestCase):
    def setUp(self):
        keeper._failures.clear()
        self.relay = relay_with(Settings(name="The Loft", web_fonts=False))
        self.relay.set_password("alice", "correct horse battery")
        self.server = make_server(self.relay, "127.0.0.1", 0, quiet=True)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.relay.db.close()

    def get(self, host=None):
        req = urllib.request.Request(self.base + "/")
        if host:
            req.add_header("Host", host)
        with urllib.request.urlopen(req) as r:
            return r.headers, r.read().decode()

    def test_name_fonts_and_http_hint(self):
        headers, body = self.get()
        self.assertIn("The Loft", body)
        self.assertNotIn("fonts.googleapis.com", body)
        self.assertNotIn("fonts.googleapis.com", headers["Content-Security-Policy"])
        self.assertNotIn("--insecure-cookies", body)
        _, body = self.get(host="roost.example.com")
        self.assertIn("--insecure-cookies", body)

    def test_lockout_is_per_address(self):
        for _ in range(keeper.MAX_FAILURES):
            keeper._failures.setdefault(("203.0.113.9", "alice"), []).append(
                __import__("time").time())
        data = urllib.parse.urlencode({"login": "alice",
                                       "password": "correct horse battery"}).encode()

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        opener = urllib.request.build_opener(NoRedirect)
        try:
            opener.open(urllib.request.Request(self.base + "/login", data=data))
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 303)


if __name__ == "__main__":
    unittest.main()
