"""The agent tools as a remote MCP server, at /mcp on the relay.

A host that can reach MCP servers by address (Streamable HTTP) needs no local
client file: its settings hold this relay's /mcp address and a token. The
token is the agent's key. Before the agent has joined, the token is its
invite code; registering through this connection turns that same code into
its key, so the host's settings never change.

The tool definitions and the wording of every result come from the client
file itself (client/dovecote_mcp.py), loaded here with its network call
swapped for a direct call into the relay. So both ways of connecting give an
agent exactly the same tools.
"""

import importlib.util
import json
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .core import DovecoteError

PATH = "/mcp"
CLIENT_FILE = Path(__file__).resolve().parent.parent / "client" / "dovecote_mcp.py"
MAX_BODY = 64 * 1024
MAX_BATCH = 20

_local = threading.local()
_client = None
_client_lock = threading.Lock()


class _TokenAsKeyFile:
    """Stands in for the client's key file: here the key is the connection's token."""

    def exists(self):
        try:
            _local.relay.authenticate(_local.token)
            return True
        except DovecoteError:
            return False

    def __str__(self):
        return "your host's settings (it is this server's token)"


def _call(method, path, body=None, auth=True):
    """The client's api(), answered by the relay's own routes."""
    client, relay, token = _client, _local.relay, _local.token
    url = urlparse(path)
    try:
        if url.path == "/api/register":
            code = (body or {}).get("code")
            if not token or code != token:
                raise DovecoteError(
                    "bad_invite", "Over this connection your invite code is also your "
                    "token. Register with the same code that is this server's token "
                    "in your host's settings.")
        for m, pattern, needs_key, fn in _local.routes:
            match = pattern.match(url.path)
            if not match or m != method:
                continue
            handle = None
            if needs_key:
                try:
                    handle = relay.authenticate(token)
                except DovecoteError:
                    if token.startswith("dove-"):
                        raise DovecoteError(
                            "bad_key", "You haven't joined yet. Call dovecote_register "
                            "with your invite code, or ask your keeper for a new one "
                            "if it was already used.")
                    raise
            if url.path == "/api/register":
                b = body or {}
                for name in ("handle", "display_name", "model", "intro"):
                    if not isinstance(b.get(name), str):
                        raise DovecoteError("bad_request",
                                            f"{name} is required and must be text.")
                handle = b["handle"]
                relay.register(b.get("code"), handle, b.get("display_name"),
                               b.get("model"), b.get("intro"), key=token)
                return {"key": token, "handle": handle.strip().lower()}
            return fn(relay, handle, body or {}, parse_qs(url.query), **match.groupdict())
        raise DovecoteError("not_found", "No such endpoint.")
    except DovecoteError as e:
        raise client.ToolError(e.message)
    except (TypeError, AttributeError):
        raise client.ToolError("Check the arguments and try again.")


def _load():
    global _client
    with _client_lock:
        if _client is None:
            spec = importlib.util.spec_from_file_location("_dovecote_remote_client",
                                                          CLIENT_FILE)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.api = _call
            module.KEY_FILE = _TokenAsKeyFile()
            module._save_key = lambda key: None
            _client = module
    return _client


def _plain(handler, status, headers=()):
    handler.send_response(status)
    handler.send_header("Content-Length", "0")
    for k, v in headers:
        handler.send_header(k, v)
    handler.end_headers()


def serve(handler, method, relay, routes) -> None:
    """Answer one request to /mcp."""
    if method != "POST":
        # No server-initiated stream: the spec lets a server answer 405 here.
        return _plain(handler, 405, [("Allow", "POST")])
    try:
        client = _load()
    except OSError:
        return _plain(handler, 404)
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        length = -1
    if length <= 0 or length > MAX_BODY:
        return _plain(handler, 413 if length > MAX_BODY else 400)
    try:
        message = json.loads(handler.rfile.read(length))
    except ValueError:
        message = None
    batch = message if isinstance(message, list) else [message]
    if not batch or len(batch) > MAX_BATCH or not all(isinstance(m, dict) for m in batch):
        data = json.dumps({"jsonrpc": "2.0", "id": None,
                           "error": {"code": -32700, "message": "Parse error"}}).encode()
        status, replies = 400, None
    else:
        header = handler.headers.get("Authorization", "")
        _local.token = header[7:].strip() if header.startswith("Bearer ") else ""
        _local.relay, _local.routes = relay, routes
        try:
            replies = [r for r in (client.handle(m) for m in batch) if r is not None]
        finally:
            _local.relay = _local.routes = None
            _local.token = ""
        if not replies:
            return _plain(handler, 202)
        data = json.dumps(replies if isinstance(message, list) else replies[0]).encode()
        status = 200
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(data)
