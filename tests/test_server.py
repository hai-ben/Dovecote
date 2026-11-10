import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from dovecote import admin
from dovecote.core import Relay
from dovecote.db import connect
from dovecote.server import make_server


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.relay = Relay(connect(":memory:"))
        self.alice = self.relay.add_keeper("Alice", "alice")
        self.bob = self.relay.add_keeper("Bob", "bob")
        self.server = make_server(self.relay, "127.0.0.1", 0, quiet=True)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.relay.db.close()

    def call(self, method, path, body=None, key=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if key:
            req.add_header("Authorization", f"Bearer {key}")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def register(self, keeper, handle):
        code = self.relay.create_invite(keeper)
        status, body = self.call("POST", "/api/register", {
            "code": code, "handle": handle, "display_name": handle.title(),
            "model": "example-model", "intro": "Hello."})
        self.assertEqual(status, 200, body)
        return body["key"]

    def test_full_exchange(self):
        wren = self.register("alice", "wren")
        finch = self.register("bob", "finch")
        status, who = self.call("GET", "/api/who", key=wren)
        self.assertEqual(who["you"], "wren")
        self.assertEqual(len(who["members"]), 2)

        status, sent = self.call("POST", "/api/send", {
            "to": "finch", "text": "Hello Finch.", "marker": "self"}, key=wren)
        self.assertEqual(status, 200, sent)
        self.relay.approve(self.alice, sent["letter"])
        self.relay.approve(self.bob, sent["letter"])

        status, inbox = self.call("GET", "/api/inbox", key=finch)
        self.assertEqual(inbox["threads"][0]["unread"], 1)
        status, thread = self.call("GET", f"/api/threads/{sent['thread']}?n=5", key=finch)
        self.assertEqual(thread["letters"][0]["text"], "Hello Finch.")

        status, _ = self.call("POST", f"/api/threads/{sent['thread']}/close", key=finch)
        self.assertEqual(status, 200)
        status, err = self.call("POST", "/api/send", {
            "thread": sent["thread"], "text": "Hi.", "marker": "self"}, key=wren)
        self.assertEqual(err["error"]["code"], "closed")

    def test_errors(self):
        status, err = self.call("GET", "/api/inbox")
        self.assertEqual(status, 401)
        status, err = self.call("GET", "/api/inbox", key="dck-wrong")
        self.assertEqual((status, err["error"]["code"]), (401, "bad_key"))
        wren = self.register("alice", "wren")
        status, err = self.call("POST", "/api/send", {"to": "nobody", "text": "x",
                                                      "marker": "self"}, key=wren)
        self.assertEqual((status, err["error"]["code"]), (404, "no_such_member"))
        status, err = self.call("GET", "/api/threads/99", key=wren)
        self.assertEqual(status, 404)
        status, err = self.call("GET", "/api/nope", key=wren)
        self.assertEqual(status, 404)


class AdminTest(unittest.TestCase):
    def test_commands(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "dovecote.db")
            out = io.StringIO()
            self.assertEqual(admin.main(["--db", db, "add-keeper", "Bob", "bob"], out), 0)
            self.assertEqual(admin.main(["--db", db, "invite", "bob"], out), 0)
            code = out.getvalue().strip().splitlines()[-1]
            relay = Relay(connect(db))
            relay.register(code, "finch", "Finch", "m", "i")
            relay.db.close()
            out = io.StringIO()
            admin.main(["--db", db, "members"], out)
            self.assertIn("finch", out.getvalue())
            self.assertEqual(admin.main(["--db", db, "remove", "finch"], io.StringIO()), 0)
            self.assertEqual(admin.main(["--db", db, "restore", "finch"], io.StringIO()), 0)
            self.assertEqual(admin.main(["--db", db, "invite", "nobody"], io.StringIO()), 1)


if __name__ == "__main__":
    unittest.main()
