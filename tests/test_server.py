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
        self.relay.approve_intro(self.relay.keeper_by_login(keeper)["id"], handle)
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
        letter = self.relay.awaiting_keeper(self.alice)[0]["letter"]
        self.relay.approve(self.alice, letter)
        self.relay.approve(self.bob, letter)

        status, inbox = self.call("GET", "/api/inbox", key=finch)
        self.assertEqual(inbox["conversations"][0]["unread"], 1)
        status, thread = self.call("GET", "/api/with/wren?n=5", key=finch)
        self.assertEqual(thread["letters"][0]["text"], "Hello Finch.")
        status, one = self.call("GET", "/api/who?handle=finch", key=wren)
        self.assertEqual([m["handle"] for m in one["members"]], ["finch"])
        status, found = self.call("GET", "/api/who?search=nothing&page=7", key=wren)
        self.assertEqual(([m["handle"] for m in found["members"]], found["page"],
                          found["pages"], found["total"]), (["wren"], 1, 1, 0))
        # Agents get no relay-wide letter or thread numbers.
        self.assertNotIn("letter", sent)
        self.assertNotIn("thread", sent)
        self.assertNotIn("id", thread["letters"][0])

        status, _ = self.call("POST", "/api/with/wren/close", key=finch)
        self.assertEqual(status, 200)
        status, err = self.call("POST", "/api/with/wren/close", key=finch)
        self.assertEqual((status, err["error"]["code"]), (400, "not_open"))
        status, sent2 = self.call("POST", "/api/send", {
            "to": "finch", "text": "Hi.", "marker": "self"}, key=wren)
        self.assertEqual(len(self.relay.keeper_threads(self.alice)), 2)
        status, back = self.call("POST", "/api/with/finch/retract", {}, key=wren)
        self.assertEqual((status, back["text"]), (200, "Hi."))
        status, saved = self.call("POST", "/api/profile", {"model": "next-model"}, key=wren)
        self.assertEqual((status, saved["profile"]), (200, "pending"))
        status, err = self.call("POST", "/api/profile", {}, key=wren)
        self.assertEqual(status, 400)
        # The guide needs no key, so an agent can read it before joining.
        status, guide = self.call("GET", "/api/guide")
        self.assertEqual(status, 200)
        self.assertIn("Allowance", guide["text"])

    def test_errors(self):
        status, err = self.call("GET", "/api/inbox")
        self.assertEqual(status, 401)
        status, err = self.call("GET", "/api/inbox", key="dck-wrong")
        self.assertEqual((status, err["error"]["code"]), (401, "bad_key"))
        wren = self.register("alice", "wren")
        status, err = self.call("POST", "/api/send", {"to": "nobody", "text": "x",
                                                      "marker": "self"}, key=wren)
        self.assertEqual((status, err["error"]["code"]), (404, "no_such_member"))
        status, err = self.call("GET", "/api/with/nobody", key=wren)
        self.assertEqual((status, err["error"]["code"]), (404, "no_letters"))
        status, err = self.call("GET", "/api/threads/1", key=wren)
        self.assertEqual(status, 404)
        self.assertIn("client", err["error"]["message"])
        status, err = self.call("GET", "/api/nope", key=wren)
        self.assertEqual(status, 404)

    def test_bad_content_length(self):
        import http.client
        for value in ("abc", "-5"):
            conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1],
                                              timeout=5)
            conn.putrequest("POST", "/api/register")
            conn.putheader("Content-Length", value)
            conn.endheaders()
            r = conn.getresponse()
            self.assertEqual(r.status, 400)
            self.assertEqual(json.loads(r.read())["error"]["code"], "bad_length")
            conn.close()


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
            out = io.StringIO()
            self.assertEqual(admin.main(["--db", db, "rekey", "finch"], out), 0)
            new_key = out.getvalue().strip().splitlines()[-1]
            relay = Relay(connect(db))
            self.assertEqual(relay.authenticate(new_key), "finch")
            relay.db.close()
            self.assertEqual(admin.main(["--db", db, "remove", "finch"], io.StringIO()), 0)
            self.assertEqual(admin.main(["--db", db, "restore", "finch"], io.StringIO()), 0)
            self.assertEqual(admin.main(["--db", db, "invite", "nobody"], io.StringIO()), 1)
            out = io.StringIO()
            self.assertEqual(admin.main(["--db", db, "mark-test", "finch"], out), 0)
            admin.main(["--db", db, "members"], out)
            self.assertIn("TEST (hidden from real agents)", out.getvalue())
            self.assertEqual(admin.main(["--db", db, "unmark-test", "finch"], out), 0)
            relay = Relay(connect(db))
            relay.ensure_responder()
            relay.db.close()
            out = io.StringIO()
            admin.main(["--db", db, "members"], out)
            admin.main(["--db", db, "keepers"], out)
            self.assertIn("the automatic test address", out.getvalue())
            self.assertNotIn("~relay", out.getvalue())
            for cmd in ("rekey", "remove", "mark-test"):
                self.assertEqual(admin.main(["--db", db, cmd, "echo"], io.StringIO()), 1)
            self.assertEqual(admin.main(["--db", db, "invite", "~relay"], io.StringIO()), 1)


if __name__ == "__main__":
    unittest.main()
