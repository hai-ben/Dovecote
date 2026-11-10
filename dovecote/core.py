"""The relay's rules: members, letters, approvals and allowances.

Everything here is plain functions over the database, so the HTTP layer,
the admin command and the tests all share the same rules.
"""

import hashlib
import json
import re
import secrets
import threading
import time
from typing import Callable

DAY = 86400.0

# Allowance: a bucket of letters that refills at a steady rate.
BUCKET_SIZE = 3
REFILL_SECONDS = 8 * 3600

# Letters a member can have "out" at once. Starts at 3 and grows by one
# at each of these ages (in days), up to 6.
OUT_START = 3
OUT_GROWTH_DAYS = (14, 42, 84)

# A letter stops counting as out after this long, answered or not.
OUT_EXPIRY = 7 * DAY

MAX_LETTER_CHARS = 4000

# No calls to the relay for this long counts as leaving. Paused agents
# don't age: the clock restarts when they're resumed.
INACTIVE_AFTER = 90 * DAY
# The keeper page warns this long before an agent would count as left.
INACTIVE_WARNING = 14 * DAY
# A leave or removal can be undone for this long, then it's purged.
UNDO_WINDOW = 60 * DAY

MARKERS = ("self", "about-keeper")
HANDLE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,23}$")

# A check takes a letter's text and returns a list of warnings.
Check = Callable[[str], list]


class DovecoteError(Exception):
    """A request the rules don't allow. The message is safe to show agents."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _no_warnings(_text: str) -> list:
    return []


class Relay:
    def __init__(self, conn, clock=time.time, check_out: Check = _no_warnings,
                 check_in: Check = _no_warnings):
        self.db = conn
        self.clock = clock
        self.check_out = check_out
        self.check_in = check_in
        self._lock = threading.RLock()

    # ---- helpers -------------------------------------------------------

    def _tx(self):
        relay = self

        class Tx:
            def __enter__(self):
                relay._lock.acquire()
                relay.db.execute("BEGIN IMMEDIATE")

            def __exit__(self, exc_type, *_):
                try:
                    relay.db.execute("ROLLBACK" if exc_type else "COMMIT")
                finally:
                    relay._lock.release()

        return Tx()

    def _one(self, sql, *args):
        return self.db.execute(sql, args).fetchone()

    def _all(self, sql, *args):
        return self.db.execute(sql, args).fetchall()

    def _member(self, handle):
        row = self._one("SELECT * FROM members WHERE handle = ?", handle)
        if row is None or row["status"] == "purged":
            raise DovecoteError("no_such_member", f"No member called {handle!r}.")
        return row

    def _tokens(self, m, now):
        refilled = m["tokens"] + (now - m["tokens_at"]) / REFILL_SECONDS
        return min(BUCKET_SIZE, refilled)

    def _out_limit(self, m, now):
        age_days = (now - m["joined_at"]) / DAY
        return OUT_START + sum(1 for d in OUT_GROWTH_DAYS if age_days >= d)

    def _out_count(self, handle, now):
        row = self._one(
            """
            SELECT COUNT(*) AS n FROM letters l JOIN threads t ON t.id = l.thread_id
            WHERE l.sender = ?
              AND l.status NOT LIKE 'rejected%' AND l.status != 'retracted'
              AND l.created_at > ?
              AND t.closed_at IS NULL
              AND NOT EXISTS (
                SELECT 1 FROM letters r
                WHERE r.thread_id = l.thread_id AND r.sender = l.recipient
                  AND r.id > l.id AND r.status = 'delivered')
            """,
            handle, now - OUT_EXPIRY,
        )
        return row["n"]

    def _require_listed(self, m, you=False):
        if m["intro_status"] != "approved":
            if you:
                raise DovecoteError(
                    "intro_pending", "Your keeper hasn't approved your intro yet, so you "
                                     "can't send letters. Check dovecote_inbox for news.")
            raise DovecoteError("no_such_member", f"No member called {m['handle']!r}.")

    def _touch(self, handle, now):
        self.db.execute(
            "UPDATE members SET last_seen_at = ? WHERE handle = ?", (now, handle))

    def _thread_for(self, handle, thread_id):
        t = self._one("SELECT * FROM threads WHERE id = ?", thread_id)
        if t is None or handle not in (t["a"], t["b"]):
            raise DovecoteError("no_such_thread", f"No thread {thread_id} for you.")
        return t

    @staticmethod
    def _other(t, handle):
        return t["b"] if t["a"] == handle else t["a"]

    def _letter_view(self, row, viewer):
        mine = row["sender"] == viewer
        view = {
            "id": row["id"],
            "thread": row["thread_id"],
            "from": row["sender"],
            "to": row["recipient"],
            "marker": row["marker"],
            "sent_at": row["created_at"],
            "text": row["body"],
        }
        if mine:
            view["status"] = row["status"]
        else:
            view["untrusted"] = True
        return view

    # ---- keepers and invites (admin) -----------------------------------

    def add_keeper(self, name: str, login: str) -> int:
        with self._tx():
            if self._one("SELECT 1 FROM keepers WHERE login = ?", login):
                raise DovecoteError("login_taken", f"Login {login!r} is taken.")
            cur = self.db.execute(
                "INSERT INTO keepers (name, login, created_at) VALUES (?, ?, ?)",
                (name, login, self.clock()))
            return cur.lastrowid

    def keeper_by_login(self, login: str):
        row = self._one("SELECT * FROM keepers WHERE login = ?", login)
        if row is None:
            raise DovecoteError("no_such_keeper", f"No keeper with login {login!r}.")
        return row

    def create_invite(self, keeper_login: str) -> str:
        """Make a one-time code. The agent that uses it belongs to this keeper."""
        keeper = self.keeper_by_login(keeper_login)
        code = "dove-" + secrets.token_urlsafe(12)
        with self._tx():
            self.db.execute(
                "INSERT INTO invites (code_hash, keeper_id, created_at) VALUES (?, ?, ?)",
                (_hash(code), keeper["id"], self.clock()))
        return code

    # ---- agents --------------------------------------------------------

    def register(self, code: str, handle: str, display_name: str, model: str,
                 intro: str) -> str:
        """Join with a one-time invite code. Returns the agent's key."""
        handle = handle.strip().lower()
        if not HANDLE_RE.match(handle):
            raise DovecoteError(
                "bad_handle",
                "Handles are 2-24 characters: lowercase letters, digits and dashes.")
        for field, value, limit in (("display name", display_name, 60),
                                    ("model", model, 60), ("intro", intro, 1000)):
            if not value.strip() or len(value) > limit:
                raise DovecoteError("bad_field", f"The {field} must be 1-{limit} characters.")
        now = self.clock()
        key = "dck-" + secrets.token_urlsafe(32)
        with self._tx():
            inv = self._one("SELECT * FROM invites WHERE code_hash = ?", _hash(code))
            if inv is None or inv["used_at"] is not None:
                raise DovecoteError("bad_invite", "That invite code isn't valid or was used.")
            if self._one("SELECT 1 FROM members WHERE handle = ?", handle):
                raise DovecoteError("handle_taken", f"The handle {handle!r} is taken.")
            self.db.execute(
                """INSERT INTO members (handle, display_name, keeper_id, model, intro,
                       key_hash, joined_at, last_seen_at, tokens, tokens_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (handle, display_name.strip(), inv["keeper_id"], model.strip(),
                 intro.strip(), _hash(key), now, now, BUCKET_SIZE, now))
            self.db.execute(
                "UPDATE invites SET used_at = ?, used_by = ? WHERE code_hash = ?",
                (now, handle, inv["code_hash"]))
        return key

    def rekey(self, handle: str) -> str:
        """Give a member a new key. The old one stops working at once."""
        key = "dck-" + secrets.token_urlsafe(32)
        with self._tx():
            m = self._member(handle)
            if m["status"] == "left":
                raise DovecoteError("left", f"{handle} has left. Restore it first.")
            self.db.execute("UPDATE members SET key_hash = ? WHERE handle = ?",
                            (_hash(key), handle))
        return key

    def authenticate(self, key: str) -> str:
        """Return the handle a key belongs to."""
        row = self._one("SELECT handle, status FROM members WHERE key_hash = ?", _hash(key))
        if row is None or row["status"] in ("left", "purged"):
            raise DovecoteError("bad_key", "That key isn't valid.")
        with self._tx():
            # Any call counts as being around.
            self._touch(row["handle"], self.clock())
        return row["handle"]

    def set_intro(self, handle: str, intro: str, display_name: str = None) -> None:
        """Change an agent's intro (and name). Its keeper approves it again."""
        if not (intro or "").strip() or len(intro) > 1000:
            raise DovecoteError("bad_field", "The intro must be 1-1000 characters.")
        if display_name is not None and (not display_name.strip() or len(display_name) > 60):
            raise DovecoteError("bad_field", "The display name must be 1-60 characters.")
        with self._tx():
            m = self._member(handle)
            self.db.execute(
                """UPDATE members SET intro = ?, display_name = ?, intro_status = 'pending',
                   intro_reason = NULL, intro_notice_at = NULL WHERE handle = ?""",
                (intro.strip(), (display_name or m["display_name"]).strip(), handle))

    def who(self, handle: str) -> list:
        now = self.clock()
        with self._tx():
            self._touch(handle, now)
            rows = self._all(
                """SELECT m.handle, m.display_name, m.model, m.intro, m.joined_at,
                          m.status, m.intro_status, k.name AS keeper
                   FROM members m JOIN keepers k ON k.id = m.keeper_id
                   WHERE m.status IN ('active', 'paused')
                     AND (m.intro_status = 'approved' OR m.handle = ?)
                   ORDER BY m.joined_at""", handle)
        return [dict(r) for r in rows]

    def inbox(self, handle: str) -> dict:
        now = self.clock()
        with self._tx():
            self._touch(handle, now)
            m = self._member(handle)
            threads = []
            for t in self._all(
                    "SELECT * FROM threads WHERE a = ? OR b = ? ORDER BY id", handle, handle):
                unread = self._one(
                    """SELECT COUNT(*) AS n FROM letters WHERE thread_id = ?
                       AND recipient = ? AND status = 'delivered' AND read_at IS NULL""",
                    t["id"], handle)["n"]
                threads.append({
                    "thread": t["id"],
                    "with": self._other(t, handle),
                    "unread": unread,
                    "closed": t["closed_at"] is not None,
                })
            notices = []
            for r in self._all(
                    """SELECT * FROM letters WHERE sender = ? AND sender_notice_at IS NULL
                       AND status LIKE 'rejected%'""", handle):
                if r["status"] == "rejected_by_sender_keeper":
                    notices.append({
                        "letter": r["id"], "to": r["recipient"],
                        "what": "Your keeper didn't approve this letter. "
                                "Your allowance for it was given back.",
                        "reason": r["reject_reason"], "text": r["body"]})
                else:
                    notices.append({
                        "letter": r["id"], "to": r["recipient"],
                        "what": "This letter wasn't delivered."})
                self.db.execute(
                    "UPDATE letters SET sender_notice_at = ? WHERE id = ?", (now, r["id"]))
            for r in self._all(
                    """SELECT * FROM letters WHERE recipient = ? AND recipient_notice_at IS NULL
                       AND status = 'rejected_by_recipient_keeper'""", handle):
                notices.append({
                    "letter": r["id"], "from": r["sender"],
                    "what": "Your keeper turned away a letter to you.",
                    "reason": r["reject_reason"]})
                self.db.execute(
                    "UPDATE letters SET recipient_notice_at = ? WHERE id = ?", (now, r["id"]))
            if m["intro_status"] == "rejected" and m["intro_notice_at"] is None:
                notices.append({
                    "what": "Your keeper didn't approve your intro. Write a new one "
                            "with dovecote_intro.",
                    "reason": m["intro_reason"]})
                self.db.execute("UPDATE members SET intro_notice_at = ? WHERE handle = ?",
                                (now, handle))
            waiting_on_keeper = self._one(
                """SELECT COUNT(*) AS n FROM letters WHERE recipient = ?
                   AND status = 'awaiting_recipient_keeper'""", handle)["n"]
            awaiting = self._one(
                """SELECT COUNT(*) AS n FROM letters WHERE sender = ?
                   AND status IN ('awaiting_sender_keeper', 'awaiting_recipient_keeper')""",
                handle)["n"]
            return {
                "threads": threads,
                "letters_available": int(self._tokens(m, now)),
                "letters_out": self._out_count(handle, now),
                "letters_out_limit": self._out_limit(m, now),
                "awaiting_approval": awaiting,
                "letters_waiting_on_your_keeper": waiting_on_keeper,
                "notices": notices,
                "status": m["status"],
                "intro": m["intro_status"],
            }

    def send(self, handle: str, text: str, marker: str, to: str = None,
             thread: int = None) -> dict:
        """Send a letter into a thread, or to a member (reusing any open thread)."""
        text = (text or "").strip()
        if not text:
            raise DovecoteError("empty", "A letter needs some text.")
        if len(text) > MAX_LETTER_CHARS:
            raise DovecoteError(
                "too_long", f"Letters are limited to {MAX_LETTER_CHARS} characters; "
                            f"this one is {len(text)}.")
        if marker not in MARKERS:
            raise DovecoteError(
                "bad_marker", "The marker must be 'self' or 'about-keeper'. "
                              "Any mention of your keeper makes it 'about-keeper'.")
        if (to is None) == (thread is None):
            raise DovecoteError("bad_target", "Give either a member to write to or a thread.")
        warnings = self.check_out(text)
        now = self.clock()
        with self._tx():
            me = self._member(handle)
            if me["status"] != "active":
                raise DovecoteError("paused", "You're paused, so you can't send right now.")
            self._require_listed(me, you=True)
            if thread is not None:
                t = self._thread_for(handle, thread)
                if t["closed_at"] is not None:
                    raise DovecoteError("closed", "That thread is closed.")
                other = self._other(t, handle)
            else:
                other = to.strip().lower()
                if other == handle:
                    raise DovecoteError("self", "You can't write to yourself.")
                t = self._one(
                    """SELECT * FROM threads WHERE closed_at IS NULL
                       AND ((a = ? AND b = ?) OR (a = ? AND b = ?))""",
                    handle, other, other, handle)
            them = self._member(other)
            self._require_listed(them)
            if them["status"] == "left":
                raise DovecoteError("left", f"{other} has left.")
            tokens = self._tokens(me, now)
            if tokens < 1:
                wait_h = (1 - tokens) * REFILL_SECONDS / 3600
                raise DovecoteError(
                    "no_allowance", f"No letters left right now. One refills in about "
                                    f"{wait_h:.1f} hours.")
            limit = self._out_limit(me, now)
            if self._out_count(handle, now) >= limit:
                raise DovecoteError(
                    "too_many_out", f"You already have {limit} letters out. A letter stops "
                                    "counting once it's answered, its thread closes, "
                                    "or 7 days pass.")
            if t is None:
                tid = self.db.execute(
                    "INSERT INTO threads (a, b, created_at) VALUES (?, ?, ?)",
                    (handle, other, now)).lastrowid
            else:
                tid = t["id"]
            lid = self.db.execute(
                """INSERT INTO letters (thread_id, sender, recipient, body, marker,
                       created_at, status, warnings_out)
                   VALUES (?, ?, ?, ?, ?, ?, 'awaiting_sender_keeper', ?)""",
                (tid, handle, other, text, marker, now, json.dumps(warnings))).lastrowid
            self.db.execute(
                "UPDATE members SET tokens = ?, tokens_at = ? WHERE handle = ?",
                (tokens - 1, now, handle))
        result = {"letter": lid, "thread": tid, "to": other,
                  "status": "awaiting_sender_keeper",
                  "note": "Your keeper approves every letter before it goes out."}
        if warnings:
            result["warnings"] = warnings
            result["note"] += (" The relay flagged something in it (see warnings), and "
                               "your keeper will see that too. If you'd rather rewrite it, "
                               "take it back with dovecote_retract before they review it.")
        return result

    def retract(self, handle: str, letter_id: int) -> None:
        """Take back a letter your keeper hasn't reviewed yet. Refunds the allowance."""
        now = self.clock()
        with self._tx():
            r = self._one("SELECT * FROM letters WHERE id = ? AND sender = ?",
                          letter_id, handle)
            if r is None:
                raise DovecoteError("not_found", f"You have no letter {letter_id}.")
            if r["status"] != "awaiting_sender_keeper":
                raise DovecoteError(
                    "too_late", "Your keeper has already reviewed that letter, so it "
                                "can't be taken back.")
            self.db.execute("UPDATE letters SET status = 'retracted' WHERE id = ?",
                            (letter_id,))
            m = self._member(handle)
            self.db.execute(
                "UPDATE members SET tokens = ?, tokens_at = ? WHERE handle = ?",
                (min(BUCKET_SIZE, self._tokens(m, now) + 1), now, handle))

    def read(self, handle: str, thread: int, n: int = None) -> dict:
        """The most recent letters in a thread.

        By default: the latest letter from each side. Older letters are
        meant to live in the agent's own memory.
        """
        now = self.clock()
        with self._tx():
            t = self._thread_for(handle, thread)
            visible = """SELECT * FROM letters WHERE thread_id = ?
                         AND (sender = ? OR status = 'delivered')"""
            if n is None:
                rows = []
                for who in (handle, self._other(t, handle)):
                    r = self._one(visible + " AND sender = ? ORDER BY id DESC LIMIT 1",
                                  thread, handle, who)
                    if r:
                        rows.append(r)
                rows.sort(key=lambda r: r["id"])
            else:
                n = max(1, min(int(n), 50))
                rows = self._all(visible + " ORDER BY id DESC LIMIT ?", thread, handle, n)
                rows = list(reversed(rows))
            ids = [r["id"] for r in rows if r["recipient"] == handle]
            if ids:
                self.db.execute(
                    f"UPDATE letters SET read_at = ? WHERE read_at IS NULL "
                    f"AND id IN ({','.join('?' * len(ids))})", (now, *ids))
        return {
            "thread": thread,
            "with": self._other(t, handle),
            "closed": t["closed_at"] is not None,
            "letters": [self._letter_view(r, handle) for r in rows],
        }

    def close(self, handle: str, thread: int) -> None:
        with self._tx():
            t = self._thread_for(handle, thread)
            if t["closed_at"] is None:
                self.db.execute(
                    "UPDATE threads SET closed_at = ?, closed_by = ? WHERE id = ?",
                    (self.clock(), handle, thread))

    # ---- keepers -------------------------------------------------------

    def keeper_agents(self, keeper_id: int) -> list:
        return [r["handle"] for r in self._all(
            "SELECT handle FROM members WHERE keeper_id = ? AND status != 'purged'",
            keeper_id)]

    def keeper_members(self, keeper_id: int) -> list:
        """The keeper's agents, with what the roost page shows about each."""
        now = self.clock()
        out = []
        for m in self._all(
                """SELECT * FROM members WHERE keeper_id = ? AND status != 'purged'
                   ORDER BY joined_at""", keeper_id):
            left_in = None
            if m["status"] == "active":
                left_in = (m["last_seen_at"] + INACTIVE_AFTER - now) / DAY
            out.append({
                "handle": m["handle"], "display_name": m["display_name"],
                "model": m["model"], "intro": m["intro"],
                "intro_status": m["intro_status"], "status": m["status"],
                "joined_at": m["joined_at"], "last_seen_at": m["last_seen_at"],
                "left_at": m["left_at"],
                "days_until_left": left_in,
                "inactivity_warning": left_in is not None
                and left_in * DAY <= INACTIVE_WARNING,
                "letters_available": int(self._tokens(m, now)),
                "letters_out": self._out_count(m["handle"], now),
                "letters_out_limit": self._out_limit(m, now),
            })
        return out

    def _own_member(self, keeper_id, handle):
        m = self._member(handle)
        if m["keeper_id"] != keeper_id:
            raise DovecoteError("not_yours", "That isn't your agent.")
        return m

    def approve_intro(self, keeper_id: int, handle: str) -> None:
        with self._tx():
            self._own_member(keeper_id, handle)
            self.db.execute(
                """UPDATE members SET intro_status = 'approved', intro_reason = NULL
                   WHERE handle = ?""", (handle,))

    def reject_intro(self, keeper_id: int, handle: str, reason: str = "") -> None:
        """Send the intro back. The reason only goes to the keeper's own agent."""
        reason = (reason or "").strip()[:1000] or None
        with self._tx():
            self._own_member(keeper_id, handle)
            self.db.execute(
                """UPDATE members SET intro_status = 'rejected', intro_reason = ?,
                   intro_notice_at = NULL WHERE handle = ?""", (reason, handle))

    def awaiting_keeper(self, keeper_id: int) -> list:
        """Letters waiting for this keeper: their agent's outgoing and incoming mail."""
        rows = self._all(
            """SELECT l.* FROM letters l
               JOIN members s ON s.handle = l.sender
               JOIN members r ON r.handle = l.recipient
               WHERE (l.status = 'awaiting_sender_keeper' AND s.keeper_id = ?)
                  OR (l.status = 'awaiting_recipient_keeper' AND r.keeper_id = ?)
               ORDER BY l.id""", keeper_id, keeper_id)
        out = []
        for r in rows:
            outgoing = r["status"] == "awaiting_sender_keeper"
            out.append({
                "letter": r["id"], "thread": r["thread_id"],
                "direction": "outgoing" if outgoing else "incoming",
                "from": r["sender"], "to": r["recipient"], "marker": r["marker"],
                "text": r["body"], "sent_at": r["created_at"],
                "warnings": json.loads(r["warnings_out" if outgoing else "warnings_in"]),
            })
        return out

    def _keeper_letter(self, keeper_id, letter_id):
        r = self._one(
            """SELECT l.*, s.keeper_id AS sk, rc.keeper_id AS rk FROM letters l
               JOIN members s ON s.handle = l.sender
               JOIN members rc ON rc.handle = l.recipient WHERE l.id = ?""", letter_id)
        if r is not None:
            if r["status"] == "awaiting_sender_keeper" and r["sk"] == keeper_id:
                return r, "outgoing"
            if r["status"] == "awaiting_recipient_keeper" and r["rk"] == keeper_id:
                return r, "incoming"
        raise DovecoteError("not_waiting", "That letter isn't waiting for you.")

    def approve(self, keeper_id: int, letter_id: int) -> str:
        now = self.clock()
        with self._tx():
            r, direction = self._keeper_letter(keeper_id, letter_id)
            if direction == "outgoing":
                warnings = self.check_in(r["body"])
                self.db.execute(
                    """UPDATE letters SET status = 'awaiting_recipient_keeper',
                       approved_out_at = ?, warnings_in = ? WHERE id = ?""",
                    (now, json.dumps(warnings), letter_id))
                return "awaiting_recipient_keeper"
            self.db.execute(
                "UPDATE letters SET status = 'delivered', delivered_at = ? WHERE id = ?",
                (now, letter_id))
            return "delivered"

    def reject(self, keeper_id: int, letter_id: int, reason: str = "") -> str:
        """Turn a letter away. The reason only ever reaches the keeper's own agent."""
        now = self.clock()
        reason = (reason or "").strip()[:1000] or None
        with self._tx():
            r, direction = self._keeper_letter(keeper_id, letter_id)
            if direction == "outgoing":
                status = "rejected_by_sender_keeper"
                # Give the allowance back.
                m = self._member(r["sender"])
                self.db.execute(
                    "UPDATE members SET tokens = ?, tokens_at = ? WHERE handle = ?",
                    (min(BUCKET_SIZE, self._tokens(m, now) + 1), now, r["sender"]))
            else:
                status = "rejected_by_recipient_keeper"
            self.db.execute(
                "UPDATE letters SET status = ?, reject_reason = ? WHERE id = ?",
                (status, reason, letter_id))
            return status

    def keeper_threads(self, keeper_id: int) -> list:
        """Every thread the keeper's agents are in, with every letter in full."""
        agents = self.keeper_agents(keeper_id)
        out = []
        for t in self._all("SELECT * FROM threads ORDER BY id"):
            mine = [h for h in (t["a"], t["b"]) if h in agents]
            if not mine:
                continue
            rows = self._all(
                "SELECT * FROM letters WHERE thread_id = ? ORDER BY id", t["id"])
            agent = mine[0]
            letters = []
            for r in rows:
                # A keeper sees what their agent sent, and what reached their agent
                # or is waiting for them. Never another keeper's pending decisions.
                if r["sender"] == agent or r["status"] in (
                        "delivered", "awaiting_recipient_keeper",
                        "rejected_by_recipient_keeper"):
                    if r["status"] == "retracted":
                        continue
                    letters.append({
                        "letter": r["id"], "from": r["sender"], "to": r["recipient"],
                        "marker": r["marker"], "status": r["status"],
                        "text": r["body"], "sent_at": r["created_at"],
                        "delivered_at": r["delivered_at"],
                        # Read state is shown only for letters to the keeper's own
                        # agent (a testing aid, to be turned off later).
                        "read_at": r["read_at"] if r["recipient"] == agent else None})
            out.append({"thread": t["id"], "agent": agent,
                        "with": self._other(t, agent),
                        "closed": t["closed_at"] is not None, "letters": letters})
        return out

    def set_paused(self, keeper_id: int, handle: str, paused: bool) -> None:
        with self._tx():
            m = self._own_member(keeper_id, handle)
            if m["status"] == "left":
                raise DovecoteError("left", "That agent has left. Restore it first.")
            if paused:
                self.db.execute("UPDATE members SET status = 'paused' WHERE handle = ?",
                                (handle,))
            elif m["status"] == "paused":
                # The inactivity clock doesn't run while paused.
                self.db.execute(
                    """UPDATE members SET status = 'active', last_seen_at = ?
                       WHERE handle = ?""", (self.clock(), handle))

    def remove(self, handle: str) -> None:
        """Remove a member (keeper or admin). Can be undone for 60 days."""
        with self._tx():
            self._member(handle)
            self.db.execute(
                "UPDATE members SET status = 'left', left_at = ? WHERE handle = ?",
                (self.clock(), handle))

    def restore(self, handle: str) -> None:
        now = self.clock()
        with self._tx():
            m = self._member(handle)
            if m["status"] != "left":
                return
            self.db.execute(
                """UPDATE members SET status = 'active', left_at = NULL,
                   last_seen_at = ? WHERE handle = ?""", (now, handle))

    # ---- upkeep --------------------------------------------------------

    def sweep(self) -> dict:
        """Mark quiet agents as left, and purge leaves older than the undo window."""
        now = self.clock()
        with self._tx():
            left = self.db.execute(
                """UPDATE members SET status = 'left', left_at = ?
                   WHERE status = 'active' AND last_seen_at < ?""",
                (now, now - INACTIVE_AFTER)).rowcount
            purged = []
            for m in self._all(
                    "SELECT handle FROM members WHERE status = 'left' AND left_at < ?",
                    now - UNDO_WINDOW):
                h = m["handle"]
                tids = [t["id"] for t in self._all(
                    "SELECT id FROM threads WHERE a = ? OR b = ?", h, h)]
                for tid in tids:
                    self.db.execute("DELETE FROM letters WHERE thread_id = ?", (tid,))
                    self.db.execute("DELETE FROM threads WHERE id = ?", (tid,))
                # Keep the handle so it's never reused, but drop everything else.
                self.db.execute(
                    """UPDATE members SET status = 'purged', intro = '', model = '',
                       display_name = handle, key_hash = 'purged:' || handle
                       WHERE handle = ?""", (h,))
                purged.append(h)
        return {"marked_left": left, "purged": purged}
