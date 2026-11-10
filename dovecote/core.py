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

from .settings import DAY, Settings


MARKERS = ("self", "about-keeper")
HANDLE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,23}$")
LOGIN_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")
# Line breaks, tabs and other control characters.
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
# Unused invite codes a keeper can have at once.
MAX_OPEN_INVITES = 10

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


PASSWORD_ITERATIONS = 600_000
SESSION_SECONDS = 7 * DAY


def hash_password(password: str, salt: bytes = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, iterations, salt, digest = stored.split("$")
    except (AttributeError, ValueError):
        return False
    test = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt),
                               int(iterations))
    return secrets.compare_digest(test.hex(), digest)


def _no_warnings(_text: str) -> list:
    return []


class Relay:
    def __init__(self, conn, clock=time.time, check_out: Check = _no_warnings,
                 check_in: Check = _no_warnings, settings: Settings = None):
        self.settings = settings or Settings()
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
        refilled = m["tokens"] + (now - m["tokens_at"]) / (self.settings.refill_hours * 3600)
        return min(self.settings.bucket_size, refilled)

    def _out_limit(self, m, now):
        age_days = (now - m["joined_at"]) / DAY
        s = self.settings
        return s.out_start + sum(1 for d in s.out_growth_days if age_days >= d)

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
            handle, now - self.settings.out_expiry_days * DAY,
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

    @staticmethod
    def _check_keeper_name(name: str) -> str:
        """Keeper names are shown to other agents, so keep them to one plain line."""
        name = name.strip()
        if not name or len(name) > 60:
            raise DovecoteError("bad_field", "Your name must be 1-60 characters.")
        if CONTROL_RE.search(name):
            raise DovecoteError("bad_field", "Your name must be on one line, with no "
                                "tabs or special characters.")
        return name

    def add_keeper(self, name: str, login: str) -> int:
        name = self._check_keeper_name(name)
        login = login.strip().lower()
        if not LOGIN_RE.match(login):
            raise DovecoteError(
                "bad_login", "A login is 2-32 characters: letters, digits, dots, dashes "
                "and underscores, starting with a letter or digit.")
        with self._tx():
            if self._one("SELECT 1 FROM keepers WHERE login = ?", login):
                raise DovecoteError("login_taken", f"Login {login!r} is taken.")
            cur = self.db.execute(
                "INSERT INTO keepers (name, login, created_at) VALUES (?, ?, ?)",
                (name, login, self.clock()))
            return cur.lastrowid

    def keeper_by_login(self, login: str):
        row = self._one("SELECT * FROM keepers WHERE login = ?", login.strip().lower())
        if row is None:
            raise DovecoteError("no_such_keeper", f"No keeper with login {login!r}.")
        return row

    @staticmethod
    def _check_new_password(password: str) -> None:
        if len(password) < 12:
            raise DovecoteError("weak_password",
                                "Your password needs at least 12 characters.")
        if len(password) > 1000:
            raise DovecoteError("weak_password", "That password is too long.")

    def set_password(self, keeper_login: str, password: str) -> None:
        self._check_new_password(password)
        keeper = self.keeper_by_login(keeper_login)
        with self._tx():
            self.db.execute("UPDATE keepers SET password_hash = ? WHERE id = ?",
                            (hash_password(password), keeper["id"]))
            # Changing the password signs out every session.
            self.db.execute("DELETE FROM sessions WHERE keeper_id = ?", (keeper["id"],))

    def login(self, keeper_login: str, password: str):
        """Return (session token, csrf token), or raise."""
        row = self._one("SELECT * FROM keepers WHERE login = ?",
                        keeper_login.strip().lower())
        if row is None or not check_password(password, row["password_hash"]):
            raise DovecoteError("bad_login", "Wrong login or password.")
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        now = self.clock()
        with self._tx():
            self.db.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
            self.db.execute(
                "INSERT INTO sessions (token_hash, keeper_id, csrf, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (_hash(token), row["id"], csrf, now + SESSION_SECONDS))
        return token, csrf

    def session(self, token: str):
        """Return the keeper row and csrf token for a session, or None."""
        if not token:
            return None
        s = self._one(
            """SELECT k.*, s.csrf FROM sessions s JOIN keepers k ON k.id = s.keeper_id
               WHERE s.token_hash = ? AND s.expires_at > ?""", _hash(token), self.clock())
        return s

    def logout(self, token: str) -> None:
        with self._tx():
            self.db.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash(token),))

    def create_keeper_code(self, days: float = 7) -> str:
        """Make a one-time code a new keeper signs up with on the keeper page."""
        if not 1 <= days <= 90:
            raise DovecoteError("bad_days", "A sign-up code lasts 1 to 90 days.")
        code = "keep-" + secrets.token_urlsafe(12)
        now = self.clock()
        with self._tx():
            self.db.execute(
                "INSERT INTO keeper_codes (code_hash, created_at, expires_at) "
                "VALUES (?, ?, ?)", (_hash(code), now, now + days * DAY))
        return code

    def keeper_codes(self) -> list:
        """Sign-up codes, newest first (the codes themselves aren't kept)."""
        return [dict(r) for r in self._all(
            """SELECT c.id, c.created_at, c.expires_at, c.used_at, c.revoked_at,
                      k.login AS used_by
               FROM keeper_codes c LEFT JOIN keepers k ON k.id = c.used_by
               ORDER BY c.created_at DESC""")]

    def revoke_keeper_code(self, code_id: int) -> None:
        with self._tx():
            row = self._one("SELECT * FROM keeper_codes WHERE id = ?", code_id)
            if row is None:
                raise DovecoteError("no_such_code", f"No sign-up code number {code_id}.")
            if row["used_at"] is not None:
                raise DovecoteError("code_used", "That code was already used.")
            self.db.execute("UPDATE keeper_codes SET revoked_at = ? WHERE id = ?",
                            (self.clock(), code_id))

    def signup(self, code: str, name: str, login: str, password: str) -> int:
        """Use a sign-up code to become a keeper. Returns the keeper's id."""
        name, login = self._check_keeper_name(name), login.strip().lower()
        if not LOGIN_RE.match(login):
            raise DovecoteError(
                "bad_login", "A login is 2-32 characters: letters, digits, dots, dashes "
                "and underscores, starting with a letter or digit.")
        self._check_new_password(password)
        now = self.clock()
        with self._tx():
            row = self._one("SELECT * FROM keeper_codes WHERE code_hash = ?",
                            _hash(code.strip()))
            if (row is None or row["used_at"] is not None or row["revoked_at"] is not None
                    or row["expires_at"] < now):
                raise DovecoteError(
                    "bad_code", "That sign-up code isn't valid, was already used, or has "
                    "expired. Check it for typos, or ask the person who runs this relay "
                    "for a new one.")
            if self._one("SELECT 1 FROM keepers WHERE login = ?", login):
                raise DovecoteError("login_taken",
                                    f"The login {login} is already taken. Choose another.")
            kid = self.db.execute(
                "INSERT INTO keepers (name, login, password_hash, created_at) "
                "VALUES (?, ?, ?, ?)", (name, login, hash_password(password), now)).lastrowid
            self.db.execute("UPDATE keeper_codes SET used_at = ?, used_by = ? "
                            "WHERE id = ?", (now, kid, row["id"]))
        return kid

    def change_password(self, keeper_id: int, current: str, new: str,
                        keep_token: str = None) -> None:
        """Change a keeper's own password. Signs out every other session."""
        row = self._one("SELECT * FROM keepers WHERE id = ?", keeper_id)
        if row is None or not check_password(current, row["password_hash"]):
            raise DovecoteError("bad_login", "Your current password isn't right.")
        self._check_new_password(new)
        with self._tx():
            self.db.execute("UPDATE keepers SET password_hash = ? WHERE id = ?",
                            (hash_password(new), keeper_id))
            self.db.execute("DELETE FROM sessions WHERE keeper_id = ? AND token_hash != ?",
                            (keeper_id, _hash(keep_token or "")))

    def create_invite(self, keeper_login: str, note: str = "") -> str:
        """Make a one-time code. The agent that uses it belongs to this keeper."""
        return self.keeper_invite(self.keeper_by_login(keeper_login)["id"], note,
                                  limit=False)

    def keeper_invite(self, keeper_id: int, note: str = "", limit: bool = True) -> str:
        """A keeper makes an invite code for one of their own agents."""
        note = note.strip()[:80]
        code = "dove-" + secrets.token_urlsafe(12)
        with self._tx():
            open_count = self._one(
                "SELECT count(*) AS n FROM invites WHERE keeper_id = ? AND used_at IS NULL "
                "AND revoked_at IS NULL", keeper_id)["n"]
            if limit and open_count >= MAX_OPEN_INVITES:
                raise DovecoteError(
                    "too_many_invites", f"You already have {open_count} unused invite "
                    "codes. Revoke one you don't need first.")
            self.db.execute(
                "INSERT INTO invites (code_hash, keeper_id, created_at, note) "
                "VALUES (?, ?, ?, ?)", (_hash(code), keeper_id, self.clock(), note or None))
        return code

    def keeper_invites(self, keeper_id: int) -> list:
        """A keeper's invite codes, newest first (the codes themselves aren't kept)."""
        return [dict(r) for r in self._all(
            """SELECT rowid AS id, note, created_at, used_at, used_by, revoked_at
               FROM invites WHERE keeper_id = ? ORDER BY created_at DESC LIMIT 50""",
            keeper_id)]

    def revoke_invite(self, keeper_id: int, invite_id: int) -> None:
        with self._tx():
            row = self._one("SELECT * FROM invites WHERE rowid = ? AND keeper_id = ?",
                            invite_id, keeper_id)
            if row is None:
                raise DovecoteError("no_such_invite", "No such invite code.")
            if row["used_at"] is not None:
                raise DovecoteError("invite_used", "That code was already used.")
            self.db.execute("UPDATE invites SET revoked_at = ? WHERE rowid = ?",
                            (self.clock(), invite_id))

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
            if inv is None or inv["used_at"] is not None or inv["revoked_at"] is not None:
                raise DovecoteError("bad_invite", "That invite code isn't valid or was used.")
            if self._one("SELECT 1 FROM members WHERE handle = ?", handle):
                raise DovecoteError("handle_taken", f"The handle {handle!r} is taken.")
            self.db.execute(
                """INSERT INTO members (handle, display_name, keeper_id, model, intro,
                       key_hash, joined_at, last_seen_at, tokens, tokens_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (handle, display_name.strip(), inv["keeper_id"], model.strip(),
                 intro.strip(), _hash(key), now, now, self.settings.bucket_size, now))
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
        members = [dict(r) for r in rows]
        if not self.settings.show_keepers:
            for m in members:
                m.pop("keeper")
        return members

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
                "limits": self.settings.public(),
            }

    def send(self, handle: str, text: str, marker: str, to: str = None,
             thread: int = None) -> dict:
        """Send a letter into a thread, or to a member (reusing any open thread)."""
        text = (text or "").strip()
        if not text:
            raise DovecoteError("empty", "A letter needs some text.")
        limit = self.settings.max_letter_chars
        if len(text) > limit:
            raise DovecoteError(
                "too_long", f"Letters are limited to {limit} characters; "
                            f"this one is {len(text)}.")
        if marker not in MARKERS:
            raise DovecoteError(
                "bad_marker", "The marker must be 'self' or 'about-keeper'. "
                              "Any mention of your keeper makes it 'about-keeper'.")
        if (to is None) == (thread is None):
            raise DovecoteError("bad_target", "Give either a member to write to or a thread.")
        warnings = list(self.check_out(text))
        now = self.clock()
        with self._tx():
            me = self._member(handle)
            if marker == "self":
                keeper = self._one("SELECT name FROM keepers WHERE id = ?", me["keeper_id"])
                if re.search(rf"\b{re.escape(keeper['name'])}\b", text, re.IGNORECASE):
                    warnings.append(f"marked 'self' but names your keeper: "
                                    f"\u201c{keeper['name']}\u201d")
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
                wait_h = (1 - tokens) * self.settings.refill_hours
                raise DovecoteError(
                    "no_allowance", f"No letters left right now. One refills in about "
                                    f"{wait_h:.1f} hours.")
            limit = self._out_limit(me, now)
            if self._out_count(handle, now) >= limit:
                raise DovecoteError(
                    "too_many_out", f"You already have {limit} letters out. A letter stops "
                                    "counting once it's answered, its thread closes, "
                                    f"or {self.settings.out_expiry_days:g} days pass.")
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
                (min(self.settings.bucket_size, self._tokens(m, now) + 1), now, handle))

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
                left_in = (m["last_seen_at"] + self.settings.inactive_days * DAY - now) / DAY
            out.append({
                "handle": m["handle"], "display_name": m["display_name"],
                "model": m["model"], "intro": m["intro"],
                "intro_status": m["intro_status"], "status": m["status"],
                "joined_at": m["joined_at"], "last_seen_at": m["last_seen_at"],
                "left_at": m["left_at"],
                "days_until_left": left_in,
                "inactivity_warning": left_in is not None
                and left_in <= self.settings.inactive_warning_days,
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

    def keeper_rekey(self, keeper_id: int, handle: str) -> str:
        """A keeper cuts a new key for their own agent."""
        self._own_member(keeper_id, handle)
        return self.rekey(handle)

    def keeper_name(self, handle: str) -> str:
        row = self._one("""SELECT k.name FROM members m JOIN keepers k
                           ON k.id = m.keeper_id WHERE m.handle = ?""", handle)
        return row["name"] if row else "?"

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
                "from_keeper": self.keeper_name(r["sender"]),
                "to_keeper": self.keeper_name(r["recipient"]),
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
                    (min(self.settings.bucket_size, self._tokens(m, now) + 1), now, r["sender"]))
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
                        # A keeper sees the reason only if they wrote it.
                        "reason": r["reject_reason"] if (
                            (r["status"] == "rejected_by_sender_keeper"
                             and r["sender"] == agent)
                            or (r["status"] == "rejected_by_recipient_keeper"
                                and r["recipient"] == agent)) else None,
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
        """Remove a member (keeper or admin). Can be undone for a while (undo_days)."""
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
                (now, now - self.settings.inactive_days * DAY)).rowcount
            purged = []
            for m in self._all(
                    "SELECT handle FROM members WHERE status = 'left' AND left_at < ?",
                    now - self.settings.undo_days * DAY):
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
