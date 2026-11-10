import re
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request

from dovecote import keeper
from dovecote.core import Relay
from dovecote.db import connect
from dovecote.server import make_server


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


class KeeperPageTest(unittest.TestCase):
    def setUp(self):
        keeper._failures.clear()
        keeper._address_failures.clear()
        self.relay = Relay(connect(":memory:"))
        self.alice = self.relay.add_keeper("Alice", "alice")
        self.bob = self.relay.add_keeper("Bob", "bob")
        self.relay.set_password("alice", "correct horse battery")
        self.relay.register(self.relay.create_invite("alice"), "wren", "Wren", "m", "i")
        self.relay.register(self.relay.create_invite("bob"), "finch", "Finch", "m", "i")
        self.relay.approve_intro(self.alice, "wren")
        self.relay.approve_intro(self.bob, "finch")
        self.server = make_server(self.relay, "127.0.0.1", 0, quiet=True,
                                  secure_cookies=False)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.cookie = None

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.relay.db.close()

    def request(self, method, path, form=None):
        data = None if form is None else urllib.parse.urlencode(form).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if self.cookie:
            req.add_header("Cookie", self.cookie)
        try:
            with OPENER.open(req) as r:
                return r.status, r.headers, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read().decode()

    def sign_in(self, password="correct horse battery"):
        status, headers, _ = self.request("POST", "/login",
                                          {"login": "alice", "password": password})
        if status == 303:
            self.cookie = headers["Set-Cookie"].split(";")[0]
        return status

    def csrf(self):
        _, _, body = self.request("GET", "/")
        return re.search(r'name="csrf" value="([^"]+)"', body).group(1)

    def test_login(self):
        _, _, body = self.request("GET", "/")
        self.assertIn("Sign in", body)
        self.assertEqual(self.sign_in("wrong password!"), 401)
        self.assertEqual(self.sign_in(), 303)
        self.assertIn("HttpOnly", self.request("POST", "/login", {
            "login": "alice", "password": "correct horse battery"})[1]["Set-Cookie"])
        _, _, body = self.request("GET", "/")
        self.assertIn("Sign out", body)
        self.assertIn("Alice", body)

    def test_lockout(self):
        for _ in range(10):
            self.sign_in("wrong password!")
        self.assertEqual(self.sign_in(), 429)

    def test_approve_and_reject(self):
        a = self.relay.send("wren", "<script>alert(1)</script>", "self", to="finch")
        self.sign_in()
        _, _, body = self.request("GET", "/?tab=outbox")
        self.assertIn("&lt;script&gt;", body)
        self.assertNotIn("<script>", body)
        status, _, _ = self.request("POST", f"/approve/{a['letter']}", {"csrf": self.csrf()})
        self.assertEqual(status, 303)
        self.assertEqual(self.relay.awaiting_keeper(self.bob)[0]["letter"], a["letter"])

        b = self.relay.send("wren", "Another.", "self", thread=a["thread"])
        self.request("POST", f"/reject/{b['letter']}",
                     {"csrf": self.csrf(), "reason": "Too soon."})
        notice = self.relay.inbox("wren")["notices"][0]
        self.assertEqual(notice["reason"], "Too soon.")

    def test_csrf_required(self):
        a = self.relay.send("wren", "Hi.", "self", to="finch")
        self.sign_in()
        status, _, _ = self.request("POST", f"/approve/{a['letter']}", {"csrf": "nope"})
        self.assertEqual(status, 403)
        self.assertEqual(len(self.relay.awaiting_keeper(self.alice)), 1)

    def test_cannot_act_on_other_keepers_letters(self):
        a = self.relay.send("finch", "Hi.", "self", to="wren")
        self.sign_in()
        status, _, body = self.request("POST", f"/approve/{a['letter']}",
                                       {"csrf": self.csrf()})
        self.assertEqual(status, 400)

    def test_pause_and_logout(self):
        self.sign_in()
        self.request("POST", "/pause/wren", {"csrf": self.csrf()})
        self.assertEqual(self.relay.inbox("wren")["status"], "paused")
        self.request("POST", "/resume/wren", {"csrf": self.csrf()})
        self.assertEqual(self.relay.inbox("wren")["status"], "active")
        self.request("POST", "/logout", {"csrf": self.csrf()})
        _, _, body = self.request("GET", "/")
        self.assertIn("Sign in", body)

    def test_tabs_show_the_right_letters(self):
        out = self.relay.send("wren", "Going out.", "about-keeper", to="finch")
        came = self.relay.send("finch", "Coming in, see https://example.com", "self",
                               to="wren")
        self.relay.check_in = lambda text: ["contains a link: \u201chttps://example.com\u201d"]
        self.relay.approve(self.bob, came["letter"])
        self.sign_in()
        _, _, inbox = self.request("GET", "/?agent=wren&tab=inbox")
        self.assertIn("Coming in", inbox)
        self.assertNotIn("Going out", inbox)
        self.assertIn("Seal and deliver to Wren", inbox)
        self.assertIn("<mark>https://example.com</mark>", inbox)
        _, _, outbox = self.request("GET", "/?agent=wren&tab=outbox")
        self.assertIn("Going out", outbox)
        self.assertIn("Seal and pass to Bob", outbox)
        self.assertIn("it mentions you", outbox)
        self.request("POST", f"/approve/{came['letter']}",
                     {"csrf": self.csrf(), "back": "/?agent=wren&tab=inbox"})
        self.relay.read("wren", came["thread"])
        _, _, threads = self.request("GET", "/?agent=wren&tab=threads")
        self.assertIn("read by Wren", threads)

    def test_roost_intro_and_rekey(self):
        self.relay.set_intro("wren", "A new intro, please seal.")
        self.sign_in()
        _, _, body = self.request("GET", "/?tab=roost")
        self.assertIn("Seal the introduction", body)
        self.request("POST", "/intro/wren/reject", {"csrf": self.csrf(), "reason": "Shorter."})
        self.assertEqual(self.relay.inbox("wren")["notices"][-1]["reason"], "Shorter.")
        self.request("POST", "/intro/wren/approve", {"csrf": self.csrf()})
        self.assertIn("wren", [m["handle"] for m in self.relay.who("finch")])
        status, _, body = self.request("POST", "/rekey/wren", {"csrf": self.csrf()})
        self.assertEqual(status, 200)
        key = re.search(r"dck-[A-Za-z0-9_-]+", body).group(0)
        self.assertEqual(self.relay.authenticate(key), "wren")
        status, _, _ = self.request("POST", "/rekey/finch", {"csrf": self.csrf()})
        self.assertEqual(status, 400)

    def test_agent_menu_and_safe_back(self):
        self.relay.register(self.relay.create_invite("alice"), "robin", "Robin",
                            "m", "i")
        self.sign_in()
        _, _, body = self.request("GET", "/?agent=robin&tab=roost")
        self.assertIn('<option value="robin" selected>', body)
        self.assertIn("Seal the introduction", body)
        status, headers, _ = self.request("POST", "/pause/robin",
                                          {"csrf": self.csrf(),
                                           "back": "https://evil.example/"})
        self.assertEqual(headers["Location"], "/")

    def test_no_compose(self):
        self.sign_in()
        status, _, _ = self.request("POST", "/send", {"csrf": self.csrf(), "text": "hi"})
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
