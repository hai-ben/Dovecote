import unittest

from dovecote.core import DAY, DovecoteError, Relay
from dovecote.db import connect


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class RelayTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.relay = Relay(connect(":memory:"), clock=self.clock)
        self.alice = self.relay.add_keeper("Alice", "alice")
        self.bob = self.relay.add_keeper("Bob", "bob")
        self.wren_key = self.join("alice", "wren")
        self.finch_key = self.join("bob", "finch")

    def tearDown(self):
        self.relay.db.close()

    def join(self, keeper, handle):
        code = self.relay.create_invite(keeper)
        key = self.relay.register(code, handle, handle.title(), "example-model",
                                  f"Hello, I'm {handle}.")
        kid = self.relay.keeper_by_login(keeper)["id"]
        self.relay.approve_intro(kid, handle)
        return key

    def deliver(self, sender, to, text="Hello there.", marker="self"):
        """Send a letter and have both keepers approve it."""
        sent = self.relay.send(sender, text, marker, to=to)
        sk = self.alice if sender == "wren" else self.bob
        rk = self.bob if sender == "wren" else self.alice
        self.relay.approve(sk, sent["letter"])
        self.relay.approve(rk, sent["letter"])
        return sent


class RegisterTest(RelayTest):
    def test_key_authenticates(self):
        self.assertEqual(self.relay.authenticate(self.wren_key), "wren")

    def test_invite_is_single_use(self):
        code = self.relay.create_invite("alice")
        self.relay.register(code, "lark", "Lark", "model", "intro")
        with self.assertRaises(DovecoteError) as e:
            self.relay.register(code, "lark2", "Lark", "model", "intro")
        self.assertEqual(e.exception.code, "bad_invite")

    def test_handle_rules(self):
        for bad in ("H", "-x", "has space", "x" * 30):
            with self.assertRaises(DovecoteError):
                self.relay.register(self.relay.create_invite("alice"), bad, "N", "m", "i")
        with self.assertRaises(DovecoteError) as e:
            self.relay.register(self.relay.create_invite("alice"), "wren", "N", "m", "i")
        self.assertEqual(e.exception.code, "handle_taken")

    def test_who_lists_keeper(self):
        names = {m["handle"]: m["keeper"] for m in self.relay.who("wren")}
        self.assertEqual(names, {"wren": "Alice", "finch": "Bob"})


class ApprovalTest(RelayTest):
    def test_letter_needs_both_keepers(self):
        sent = self.relay.send("wren", "Hi Finch.", "self", to="finch")
        self.assertEqual(self.relay.read("finch", sent["thread"])["letters"], [])
        # Bob can't approve before Alice does.
        with self.assertRaises(DovecoteError):
            self.relay.approve(self.bob, sent["letter"])
        self.assertEqual(self.relay.approve(self.alice, sent["letter"]),
                         "awaiting_recipient_keeper")
        self.assertEqual(self.relay.inbox("finch")["threads"][0]["unread"], 0)
        self.assertEqual(self.relay.approve(self.bob, sent["letter"]), "delivered")
        inbox = self.relay.inbox("finch")
        self.assertEqual(inbox["threads"][0]["unread"], 1)
        letters = self.relay.read("finch", sent["thread"])["letters"]
        self.assertEqual(letters[0]["text"], "Hi Finch.")
        self.assertTrue(letters[0]["untrusted"])
        self.assertEqual(self.relay.inbox("finch")["threads"][0]["unread"], 0)

    def test_each_keeper_sees_only_their_queue(self):
        sent = self.relay.send("wren", "Hi.", "self", to="finch")
        self.assertEqual(len(self.relay.awaiting_keeper(self.alice)), 1)
        self.assertEqual(self.relay.awaiting_keeper(self.bob), [])
        self.relay.approve(self.alice, sent["letter"])
        self.assertEqual(self.relay.awaiting_keeper(self.alice), [])
        waiting = self.relay.awaiting_keeper(self.bob)
        self.assertEqual(waiting[0]["direction"], "incoming")

    def test_outgoing_reject_refunds_and_tells_own_agent(self):
        sent = self.relay.send("wren", "Hi.", "self", to="finch")
        self.assertEqual(self.relay.inbox("wren")["letters_available"], 2)
        self.relay.reject(self.alice, sent["letter"], "Leave out my address.")
        inbox = self.relay.inbox("wren")
        self.assertEqual(inbox["letters_available"], 3)
        self.assertEqual(inbox["notices"][0]["reason"], "Leave out my address.")
        # Shown once.
        self.assertEqual(self.relay.inbox("wren")["notices"], [])

    def test_incoming_reject_reason_stays_with_recipient(self):
        sent = self.relay.send("wren", "Hi.", "self", to="finch")
        self.relay.approve(self.alice, sent["letter"])
        self.relay.reject(self.bob, sent["letter"], "Not today.")
        wren_notice = self.relay.inbox("wren")["notices"][0]
        self.assertNotIn("reason", wren_notice)
        finch_notice = self.relay.inbox("finch")["notices"][0]
        self.assertEqual(finch_notice["reason"], "Not today.")
        self.assertNotIn("text", finch_notice)


class LimitTest(RelayTest):
    def test_length_and_marker(self):
        with self.assertRaises(DovecoteError) as e:
            self.relay.send("wren", "x" * 4001, "self", to="finch")
        self.assertEqual(e.exception.code, "too_long")
        with self.assertRaises(DovecoteError) as e:
            self.relay.send("wren", "Hi.", "about-me", to="finch")
        self.assertEqual(e.exception.code, "bad_marker")

    def test_bucket_refills(self):
        code = self.relay.create_invite("alice")
        self.relay.register(code, "robin", "Robin", "m", "i")
        self.relay.approve_intro(self.alice, "robin")
        self.relay.send("wren", "1", "self", to="finch")
        self.relay.send("wren", "2", "self", to="finch")
        self.relay.send("wren", "3", "self", to="robin")
        with self.assertRaises(DovecoteError) as e:
            self.relay.send("wren", "4", "self", to="robin")
        self.assertIn(e.exception.code, ("no_allowance", "too_many_out"))
        self.assertEqual(self.relay.inbox("wren")["letters_available"], 0)
        self.clock.advance(8 * 3600)
        self.assertEqual(self.relay.inbox("wren")["letters_available"], 1)

    def test_out_limit_and_expiry(self):
        for i in range(3):
            self.relay.send("wren", str(i), "self", to="finch")
            self.clock.advance(8 * 3600)
        with self.assertRaises(DovecoteError) as e:
            self.relay.send("wren", "4", "self", to="finch")
        self.assertEqual(e.exception.code, "too_many_out")
        self.clock.advance(7 * DAY)
        self.relay.send("wren", "4", "self", to="finch")

    def test_reply_frees_slot(self):
        sent = self.deliver("wren", "finch")
        self.assertEqual(self.relay.inbox("wren")["letters_out"], 1)
        reply = self.relay.send("finch", "Hi back.", "self", thread=sent["thread"])
        # A reply only frees the slot once it's delivered.
        self.assertEqual(self.relay.inbox("wren")["letters_out"], 1)
        self.relay.approve(self.bob, reply["letter"])
        self.relay.approve(self.alice, reply["letter"])
        self.assertEqual(self.relay.inbox("wren")["letters_out"], 0)

    def test_out_limit_grows(self):
        self.clock.advance(84 * DAY)
        self.relay.inbox("wren")
        self.assertEqual(self.relay.inbox("wren")["letters_out_limit"], 6)

    def test_paused_cannot_send(self):
        self.relay.set_paused(self.alice, "wren", True)
        with self.assertRaises(DovecoteError) as e:
            self.relay.send("wren", "Hi.", "self", to="finch")
        self.assertEqual(e.exception.code, "paused")
        with self.assertRaises(DovecoteError):
            self.relay.set_paused(self.bob, "wren", False)


class ReadTest(RelayTest):
    def test_default_is_latest_from_each_side(self):
        t = self.deliver("wren", "finch", "one")["thread"]
        self.clock.advance(9 * 3600)
        self.deliver("finch", "wren", "two")
        self.clock.advance(9 * 3600)
        self.deliver("wren", "finch", "three")
        texts = [l["text"] for l in self.relay.read("finch", t)["letters"]]
        self.assertEqual(texts, ["two", "three"])
        texts = [l["text"] for l in self.relay.read("finch", t, n=3)["letters"]]
        self.assertEqual(texts, ["one", "two", "three"])

    def test_one_open_thread_per_pair(self):
        a = self.deliver("wren", "finch")["thread"]
        self.clock.advance(9 * 3600)
        b = self.deliver("wren", "finch")["thread"]
        self.assertEqual(a, b)
        self.relay.close("finch", a)
        with self.assertRaises(DovecoteError):
            self.relay.send("wren", "Hi.", "self", thread=a)
        self.clock.advance(9 * 3600)
        c = self.relay.send("wren", "New start.", "self", to="finch")["thread"]
        self.assertNotEqual(a, c)

    def test_keeper_sees_threads_in_full(self):
        t = self.deliver("wren", "finch", "one")["thread"]
        self.relay.send("finch", "draft", "self", thread=t)
        alice_view = self.relay.keeper_threads(self.alice)[0]
        # Bob hasn't approved Finch's draft, so Alice can't see it yet.
        self.assertEqual([l["text"] for l in alice_view["letters"]], ["one"])
        bob_view = self.relay.keeper_threads(self.bob)[0]
        self.assertEqual([l["text"] for l in bob_view["letters"]], ["one", "draft"])

    def test_intro_needs_keeper_approval(self):
        code = self.relay.create_invite("bob")
        self.relay.register(code, "swift", "Swift", "m", "Hi.")
        self.assertNotIn("swift", [m["handle"] for m in self.relay.who("wren")])
        with self.assertRaises(DovecoteError) as e:
            self.relay.send("swift", "Hello.", "self", to="wren")
        self.assertEqual(e.exception.code, "intro_pending")
        with self.assertRaises(DovecoteError):
            self.relay.send("wren", "Hello.", "self", to="swift")
        with self.assertRaises(DovecoteError) as e:
            self.relay.approve_intro(self.alice, "swift")
        self.assertEqual(e.exception.code, "not_yours")
        self.relay.reject_intro(self.bob, "swift", "Say less about me.")
        notices = self.relay.inbox("swift")["notices"]
        self.assertEqual(notices[0]["reason"], "Say less about me.")
        self.assertEqual(self.relay.inbox("swift")["notices"], [])
        self.relay.set_intro("swift", "Hi, I'm Swift.")
        self.assertEqual(self.relay.inbox("swift")["intro"], "pending")
        self.relay.approve_intro(self.bob, "swift")
        self.assertIn("swift", [m["handle"] for m in self.relay.who("wren")])
        self.relay.send("swift", "Hello.", "self", to="wren")

    def test_retract_refunds(self):
        self.relay.check_out = lambda text: ["mentions a command"] if "sudo" in text else []
        sent = self.relay.send("wren", "Try sudo.", "self", to="finch")
        self.assertEqual(sent["warnings"], ["mentions a command"])
        self.assertIn("dovecote_retract", sent["note"])
        self.assertEqual(self.relay.inbox("wren")["letters_available"], 2)
        self.relay.retract("wren", sent["letter"])
        inbox = self.relay.inbox("wren")
        self.assertEqual((inbox["letters_available"], inbox["letters_out"]), (3, 0))
        self.assertEqual(self.relay.awaiting_keeper(self.alice), [])
        with self.assertRaises(DovecoteError) as e:
            self.relay.retract("wren", sent["letter"])
        self.assertEqual(e.exception.code, "too_late")
        other = self.relay.send("wren", "Plain.", "self", to="finch")
        self.relay.approve(self.alice, other["letter"])
        with self.assertRaises(DovecoteError):
            self.relay.retract("wren", other["letter"])
        with self.assertRaises(DovecoteError):
            self.relay.retract("finch", other["letter"])

    def test_waiting_on_keeper_count_and_read_state(self):
        sent = self.relay.send("wren", "Hello.", "self", to="finch")
        self.relay.approve(self.alice, sent["letter"])
        self.assertEqual(self.relay.inbox("finch")["letters_waiting_on_your_keeper"], 1)
        self.relay.approve(self.bob, sent["letter"])
        self.assertEqual(self.relay.inbox("finch")["letters_waiting_on_your_keeper"], 0)

        def letter(keeper):
            return self.relay.keeper_threads(keeper)[0]["letters"][0]
        self.assertIsNone(letter(self.bob)["read_at"])
        self.relay.read("finch", sent["thread"])
        self.assertIsNotNone(letter(self.bob)["read_at"])
        # The sender's keeper doesn't see the other agent's read state.
        self.assertIsNone(letter(self.alice)["read_at"])

    def test_pause_stops_inactivity_clock(self):
        self.relay.set_paused(self.alice, "wren", True)
        self.clock.advance(120 * DAY)
        self.relay.sweep()
        self.assertEqual(self.relay.keeper_members(self.alice)[0]["status"], "paused")
        self.relay.set_paused(self.alice, "wren", False)
        m = self.relay.keeper_members(self.alice)[0]
        self.assertAlmostEqual(m["days_until_left"], 90)
        self.assertFalse(m["inactivity_warning"])
        self.clock.advance(80 * DAY)
        self.assertTrue(self.relay.keeper_members(self.alice)[0]["inactivity_warning"])
        self.relay.authenticate(self.wren_key)
        self.assertFalse(self.relay.keeper_members(self.alice)[0]["inactivity_warning"])

    def test_rekey(self):
        new = self.relay.rekey("wren")
        self.assertEqual(self.relay.authenticate(new), "wren")
        with self.assertRaises(DovecoteError):
            self.relay.authenticate(self.wren_key)

    def test_cannot_read_others_thread(self):
        t = self.deliver("wren", "finch")["thread"]
        self.relay.register(self.relay.create_invite("alice"), "lark", "Q", "m", "i")
        with self.assertRaises(DovecoteError):
            self.relay.read("lark", t)


class SweepTest(RelayTest):
    def test_inactive_then_purged(self):
        t = self.deliver("wren", "finch")["thread"]
        self.clock.advance(91 * DAY)
        self.relay.inbox("wren")
        self.assertEqual(self.relay.sweep()["marked_left"], 1)
        with self.assertRaises(DovecoteError):
            self.relay.authenticate(self.finch_key)
        with self.assertRaises(DovecoteError) as e:
            self.relay.send("wren", "Hi.", "self", thread=t)
        self.assertEqual(e.exception.code, "left")
        self.clock.advance(61 * DAY)
        self.relay.inbox("wren")
        self.assertEqual(self.relay.sweep()["purged"], ["finch"])
        self.assertEqual(self.relay.inbox("wren")["threads"], [])
        with self.assertRaises(DovecoteError) as e:
            self.relay.register(self.relay.create_invite("bob"), "finch", "V", "m", "i")
        self.assertEqual(e.exception.code, "handle_taken")

    def test_restore_within_window(self):
        self.relay.remove("finch")
        self.clock.advance(30 * DAY)
        self.relay.restore("finch")
        self.assertEqual(self.relay.authenticate(self.finch_key), "finch")


if __name__ == "__main__":
    unittest.main()
