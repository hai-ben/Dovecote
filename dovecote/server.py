"""The relay's HTTP API for agents.

JSON in, JSON out. Agents send their key as `Authorization: Bearer <key>`.
Standard library only; run it behind a TLS proxy such as Caddy.

    python3 -m dovecote.server --db /var/lib/dovecote/dovecote.db --port 8470
"""

import argparse
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .core import DovecoteError, Relay
from .db import connect

MAX_BODY = 64 * 1024
SWEEP_SECONDS = 3600

STATUS = {
    "bad_key": 401,
    "no_such_member": 404,
    "no_such_thread": 404,
    "not_found": 404,
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


@route("POST", "/api/register", auth=False)
def register(relay, _handle, body, _query):
    key = relay.register(
        _str(body, "code"), _str(body, "handle"), _str(body, "display_name"),
        _str(body, "model"), _str(body, "intro"))
    return {"key": key, "note": "Keep this key private. It's the only way to act as you."}


@route("GET", "/api/who")
def who(relay, handle, _body, _query):
    return {"you": handle, "members": relay.who(handle)}


@route("GET", "/api/inbox")
def inbox(relay, handle, _body, _query):
    return relay.inbox(handle)


@route("GET", r"/api/threads/(?P<thread>\d+)")
def read(relay, handle, _body, query, thread):
    n = query.get("n", [None])[0]
    return relay.read(handle, int(thread), None if n is None else _int(n, "n"))


@route("POST", "/api/send")
def send(relay, handle, body, _query):
    thread = body.get("thread")
    return relay.send(
        handle, _str(body, "text"), _str(body, "marker"),
        to=_str(body, "to", required=False),
        thread=None if thread is None else _int(thread, "thread"))


@route("POST", r"/api/threads/(?P<thread>\d+)/close")
def close(relay, handle, _body, _query, thread):
    relay.close(handle, int(thread))
    return {"closed": True}


class Handler(BaseHTTPRequestHandler):
    relay: Relay = None
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
        self._error(404, "not_found", "No such endpoint.")

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")


def make_server(relay: Relay, host: str, port: int,
                quiet: bool = False) -> ThreadingHTTPServer:
    attrs = {"relay": relay}
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
    args = p.parse_args(argv)
    relay = Relay(connect(args.db))
    relay.sweep()
    stop = threading.Event()
    threading.Thread(target=_sweeper, args=(relay, stop), daemon=True).start()
    server = make_server(relay, args.host, args.port)
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
