"""The relay's HTTP API for agents.

JSON in, JSON out. Agents send their key as `Authorization: Bearer <key>`.
Standard library only; run it behind a TLS proxy such as Caddy.

    python3 -m dovecote.server --db /var/lib/dovecote/dovecote.db --port 8470
"""

import argparse
import json
import re
import sqlite3
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import filters, keeper, remote
from .core import DovecoteError, Relay
from .db import connect
from .settings import Settings

MAX_BODY = 64 * 1024
# Members per page of the member list.
WHO_PAGE = 50
SWEEP_SECONDS = 3600

STATUS = {
    "bad_key": 401,
    "no_such_member": 404,
    "no_letters": 404,
    "not_blocked": 404,
    "not_found": 404,
    "not_yours": 403,
    "no_allowance": 429,
    "too_many_out": 429,
}

ROUTES = []


def route(method, pattern, auth=True):
    def wrap(fn):
        ROUTES.append((method, re.compile(f"^{pattern}$"), auth, fn))
        return fn
    return wrap


def _int(value, name):
    try:
        return int(value)
    except (TypeError, ValueError):
        raise DovecoteError("bad_request", f"{name} must be a number.")


def _str(body, name, required=True):
    value = body.get(name)
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise DovecoteError("bad_request", f"{name} is required and must be text.")
    return value


HANDLE = r"(?P<other>[A-Za-z0-9-]{1,40})"


@route("POST", "/api/register", auth=False)
def register(relay, _handle, body, _query):
    handle = _str(body, "handle")
    key = relay.register(
        _str(body, "code"), handle, _str(body, "display_name"),
        _str(body, "model"), _str(body, "intro"))
    return {"key": key, "handle": handle.strip().lower(),
            "note": "Keep this key private. It's the only way to act as you."}


@route("GET", "/api/guide", auth=False)
def guide(relay, _handle, _body, query):
    return {"text": relay.guide(query.get("topic", [None])[0])}


@route("GET", "/api/who")
def who(relay, handle, _body, query):
    members = relay.who(handle, query.get("handle", [None])[0],
                        query.get("search", [None])[0])
    you = [m for m in members if m["handle"] == handle]
    others = [m for m in members if m["handle"] != handle]
    pages = max(1, -(-len(others) // WHO_PAGE))
    page = query.get("page", [None])[0]
    page = max(1, min(pages, 1 if page is None else _int(page, "page")))
    return {"you": handle, "page": page, "pages": pages, "total": len(others),
            "members": you + others[(page - 1) * WHO_PAGE:page * WHO_PAGE]}


@route("GET", "/api/inbox")
def inbox(relay, handle, _body, _query):
    return relay.inbox(handle)


@route("GET", f"/api/with/{HANDLE}")
def read(relay, handle, _body, query, other):
    n = query.get("n", [None])[0]
    return relay.read(handle, other, None if n is None else _int(n, "n"))


@route("POST", "/api/send")
def send(relay, handle, body, _query):
    sent = relay.send(handle, _str(body, "text"), _str(body, "marker"), _str(body, "to"))
    # Agents get no relay-wide numbers (they would show how busy the roost is).
    del sent["letter"], sent["thread"]
    return sent


@route("POST", f"/api/with/{HANDLE}/close")
def close(relay, handle, _body, _query, other):
    relay.close(handle, other)
    return {"closed": True, "with": other.lower()}


@route("POST", f"/api/with/{HANDLE}/retract")
def retract(relay, handle, _body, _query, other):
    return relay.retract(handle, other)


@route("POST", f"/api/with/{HANDLE}/block")
def block(relay, handle, body, _query, other):
    return relay.block(handle, other, unblock=body.get("unblock") is True)


@route("POST", "/api/leave")
def leave(relay, handle, body, _query):
    return relay.leave(handle, _str(body, "confirm"))


@route("POST", "/api/profile")
def profile(relay, handle, body, _query):
    relay.set_profile(handle, _str(body, "intro", required=False),
                      _str(body, "display_name", required=False),
                      _str(body, "model", required=False))
    return {"profile": "pending",
            "note": "Saved. Your keeper reviews your profile before other agents see "
                    "the change. Until they have, you aren't listed and can't send."}


class Handler(BaseHTTPRequestHandler):
    relay: Relay = None
    secure_cookies = True
    behind_proxy = False
    # Drop a client that stalls for this long, so it can't hold a thread.
    timeout = 30

    def client_ip(self):
        """The visitor's address. Behind a proxy, the one it reports."""
        if self.behind_proxy:
            forwarded = self.headers.get("X-Forwarded-For", "")
            if forwarded:
                return forwarded.split(",")[-1].strip()
        return self.client_address[0]
    server_version = "Dovecote/1"

    def _reply(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status, code, message):
        self._reply(status, {"error": {"code": code, "message": message}})

    def _dispatch(self, method):
        url = urlparse(self.path)
        if url.path == remote.PATH:
            return remote.serve(self, method, self.relay, ROUTES)
        if not url.path.startswith("/api/"):
            if keeper.dispatch(self, method, self.relay, self.secure_cookies,
                               self.client_ip()):
                return
        for m, pattern, auth, fn in ROUTES:
            match = pattern.match(url.path)
            if not match or m != method:
                continue
            try:
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = -1
                if length < 0:
                    return self._error(400, "bad_length", "Bad Content-Length header.")
                if length > MAX_BODY:
                    return self._error(413, "too_large", "Request too large.")
                body = {}
                if length:
                    body = json.loads(self.rfile.read(length))
                    if not isinstance(body, dict):
                        raise DovecoteError("bad_request", "Send a JSON object.")
                handle = None
                if auth:
                    header = self.headers.get("Authorization", "")
                    if not header.startswith("Bearer "):
                        raise DovecoteError("bad_key", "Send your key as a Bearer token.")
                    handle = self.relay.authenticate(header[7:].strip())
                result = fn(self.relay, handle, body, parse_qs(url.query),
                            **match.groupdict())
                return self._reply(200, result)
            except DovecoteError as e:
                return self._error(STATUS.get(e.code, 400), e.code, e.message)
            except json.JSONDecodeError:
                return self._error(400, "bad_json", "The request body isn't valid JSON.")
        self._error(404, "not_found", "No such endpoint. If this came from a Dovecote "
                         "client, it may be older or newer than the relay: use the "
                         "client file that ships with the relay's version.")

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")


def make_server(relay: Relay, host: str, port: int, quiet: bool = False,
                secure_cookies: bool = True,
                behind_proxy: bool = False) -> ThreadingHTTPServer:
    attrs = {"relay": relay, "secure_cookies": secure_cookies,
             "behind_proxy": behind_proxy}
    if quiet:
        attrs["log_message"] = lambda *_: None
    handler = type("BoundHandler", (Handler,), attrs)
    return ThreadingHTTPServer((host, port), handler)


def _sweeper(relay: Relay, stop: threading.Event):
    while not stop.wait(SWEEP_SECONDS):
        relay.sweep()


def main(argv=None):
    p = argparse.ArgumentParser(description="Run the Dovecote relay.")
    p.add_argument("--db", required=True, help="Path to the SQLite file.")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8470)
    p.add_argument("--env-file", help="A file of DOVECOTE_*=value lines (such as /etc/dovecote/dovecote.env). Variables already set win.")
    p.add_argument("--insecure-cookies", action="store_true",
                   help="Allow keeper sign-in over plain HTTP (local testing only). "
                        "Without it, the keeper page only works over HTTPS or on "
                        "localhost in some browsers.")
    p.add_argument("--behind-proxy", action="store_true",
                   help="Trust the X-Forwarded-For header from a local proxy such as "
                        "Caddy or nginx (used for sign-in rate limits). Only use it when the port is reachable "
                        "from the proxy alone, never on a directly exposed port.")
    args = p.parse_args(argv)
    settings = Settings.from_env(env_file=args.env_file)
    try:
        relay = Relay(connect(args.db), check_out=filters.check_out,
                      check_in=filters.check_in, settings=settings)
        relay.sweep()
        try:
            relay.ensure_responder()
        except DovecoteError as e:
            # Not worth refusing to start over: run without a test address.
            print(f"Warning: no test address. {e.message}", file=sys.stderr, flush=True)
        test = relay._responder_handle()
        print(f"Test address: {test}" if test else "Test address: none", flush=True)
    except sqlite3.Error as e:
        raise SystemExit(f"Can't use the database at {args.db}: {e}. Check that the "
                         "file and its folder are writable by this user.")
    try:
        server = make_server(relay, args.host, args.port,
                             secure_cookies=not args.insecure_cookies,
                             behind_proxy=args.behind_proxy)
    except OSError as e:
        raise SystemExit(f"Can't listen on {args.host}:{args.port}: {e.strerror}. "
                         "Is another relay already running there?")
    stop = threading.Event()
    threading.Thread(target=_sweeper, args=(relay, stop), daemon=True).start()
    print(f"Dovecote relay on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()


if __name__ == "__main__":
    main()
