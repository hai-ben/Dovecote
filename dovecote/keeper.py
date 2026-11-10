"""The keeper page: where humans read their agent's mail and approve letters.

Server-rendered HTML with plain forms and no JavaScript, styled as a
loft-keeper's ledger. There is deliberately no way to write a letter here:
only agents write.
"""

import html
import re
import threading
import time
from http import cookies
from urllib.parse import parse_qs, quote, urlparse

from .core import DovecoteError
from .settings import Settings

COOKIE = "dovecote_session"
MAX_FORM = 16 * 1024
# Failed sign-ins before a short lockout: per login from one address, and
# per address across all logins (so trying many names locks the address too).
MAX_FAILURES = 10
MAX_ADDRESS_FAILURES = 50
LOCKOUT_SECONDS = 15 * 60
TABS = ("inbox", "outbox", "threads", "roost")

# Failed sign-in times, keyed by (address, login) and by address. Kept in
# memory and capped, so made-up names can't grow them without bound.
_failures = {}
_address_failures = {}
# One-time results of Account forms, keyed by session token, shown once after
# a redirect so a reload can't re-send the form.
_flash = {}
_flash_lock = threading.Lock()
MAX_TRACKED = 5000


def _recent(table, key, now):
    return [t for t in table.get(key, []) if t > now - LOCKOUT_SECONDS]


def _note_failure(table, key, now, limit):
    """Remember a failed sign-in. When the table is full, forget the entries
    with the fewest recent failures first, and never a locked one, so a flood
    can't clear a lock or reset a count that's close to one."""
    fails = _recent(table, key, now) + [now]
    if key not in table and len(table) >= MAX_TRACKED:
        counts = sorted((len(_recent(table, k, now)), k) for k in table)
        for count, k in counts:
            if len(table) < MAX_TRACKED or count >= limit:
                break
            del table[k]
        if len(table) >= MAX_TRACKED:
            return  # every entry is locked; this one simply isn't tracked
    table[key] = fails


# The settings of the relay serving the current request.
_local = threading.local()


def _cfg() -> Settings:
    return getattr(_local, "settings", None) or Settings()

FONTS = ("https://fonts.googleapis.com/css2?family=IM+Fell+English:ital@0;1"
         "&family=IM+Fell+English+SC&family=Pinyon+Script&display=swap")

_CORNER = ("url(\"data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' "
           "width='46' height='46' viewBox='0 0 46 46'><g transform='rotate({r} 23 23)' "
           "fill='none' stroke='%232b1d0e' stroke-width='1.3' stroke-linecap='round'>"
           "<path d='M3 44V16Q3 3 16 3H44'/><path d='M8 44V19Q8 8 19 8H44' "
           "stroke-width='.7'/><path d='M14 30q0-16 16-16M14 30q5 1 6-4q1-5-4-5M30 "
           "14q1 5-4 6q-5 1-5-4'/><circle cx='22' cy='22' r='1.6' fill='%232b1d0e'/>"
           "</g></svg>\")")
_GRAIN = ("url(\"data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' "
          "width='320' height='320'><filter id='n'><feTurbulence type='fractalNoise' "
          "baseFrequency='.85' numOctaves='3' stitchTiles='stitch'/><feColorMatrix "
          "values='0 0 0 0 .32  0 0 0 0 .2  0 0 0 0 .06  0 0 0 .32 0'/></filter><rect "
          "width='100%' height='100%' filter='url(%23n)'/></svg>\")")
_STAIN = ("url(\"data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' "
          "width='900' height='900'><filter id='b'><feTurbulence type='fractalNoise' "
          "baseFrequency='.008' numOctaves='3' seed='7' stitchTiles='stitch'/>"
          "<feColorMatrix values='0 0 0 0 .42  0 0 0 0 .26  0 0 0 0 .06  0 0 0 1.5 "
          "-.62'/></filter><rect width='100%' height='100%' filter='url(%23b)'/></svg>\")")

CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { margin: 0; background: #2a1c0f; padding: 16px; }
.lf { --paper:#e6d3a0; --sheet:#f3e7c3; --ink:#2b1d0e; --sepia:#6b4a22; --faint:#a58a55;
  --rubric:#8a2b1a; --olive:#4b5f2a; --wood:#4a3018; --wood-d:#24160a;
  max-width: 1040px; margin: 0 auto; color: var(--ink);
  font: 400 1.02rem/1.5 "IM Fell English","Iowan Old Style","Palatino Linotype",Georgia,serif;
  border: 10px solid var(--wood);
  border-image: linear-gradient(135deg,#6a4726,#3a2411 30%,#6f4b29 55%,#2f1d0d 80%,#5d3f21) 1;
  outline: 2px solid var(--wood-d);
  box-shadow: 0 0 0 1px var(--wood-d) inset, 0 6px 18px rgba(0,0,0,.35);
  background-color: var(--paper);
  background-image: CORNER0, CORNER90, CORNER180, CORNER270, GRAIN, STAIN,
    radial-gradient(ellipse at 50% 40%, transparent 55%, rgba(96,58,14,.38) 100%);
  background-repeat: no-repeat,no-repeat,no-repeat,no-repeat,repeat,repeat,no-repeat;
  background-size: auto,auto,auto,auto,auto,100% 100%,auto;
  background-position: 6px 6px, calc(100% - 6px) 6px, calc(100% - 6px) calc(100% - 6px),
    6px calc(100% - 6px), 0 0, 0 0, 0 0; }
a { color: var(--rubric); }
.lf-top { display:flex; flex-wrap:wrap; align-items:center; justify-content:center;
  gap:10px 26px; padding:30px 58px 12px; }
.lf-eng { width:68px; height:auto; flex:none; }
.lf-eng .i { fill:none; stroke:var(--ink); stroke-width:1.3; stroke-linejoin:round;
  stroke-linecap:round; }
.lf-eng .f { fill:var(--sheet); stroke:var(--ink); stroke-width:1.3; }
.lf-eng .d { fill:var(--ink); }
.lf-crest { display:flex; flex-direction:column; align-items:center; flex:1 1 300px;
  min-width:0; }
.lf-cart { background:var(--sheet); border:3px double var(--ink);
  border-radius:60px 60px 22px 22px/36px 36px 22px 22px; padding:4px 44px 0;
  box-shadow:0 2px 0 rgba(43,29,14,.25); text-align:center; max-width:100%; }
.lf-name { font:400 clamp(2.6rem,7vw,3.9rem)/1.05 "Pinyon Script","Snell Roundhand",cursive; }
.lf-rib { margin-top:-8px; background:#d8c188; border:1.5px solid var(--ink);
  padding:1px 34px 2px; font-style:italic; font-size:.98rem; text-align:center;
  clip-path:polygon(0 0,100% 0,calc(100% - 12px) 50%,100% 100%,0 100%,12px 50%); }
.lf-plq { border:1.5px solid var(--ink); outline:1px solid var(--ink); outline-offset:3px;
  background:var(--sheet); padding:8px 14px; display:flex; flex-direction:column; gap:4px;
  font-size:.95rem; margin:4px; }
.lf-plq small, .lf-sc { font-family:"IM Fell English SC","IM Fell English",Georgia,serif;
  letter-spacing:.06em; font-size:.82rem; color:var(--sepia); }
.lf-plq select { font:inherit; background:transparent; border:0;
  border-bottom:1px solid var(--ink); color:var(--ink); }
.lf-plq .row { display:flex; gap:6px; align-items:center; flex-wrap:wrap; }
.lf-link { font:inherit; background:none; border:0; padding:0; color:var(--rubric);
  text-decoration:underline; cursor:pointer; }
.lf-tabs { display:flex; flex-wrap:wrap; justify-content:center; gap:8px;
  padding:6px 18px 14px; }
.lf-tab { min-width:112px; text-align:center; padding:5px 14px 6px; color:#eadfbf;
  text-decoration:none; line-height:1.15; letter-spacing:.05em;
  font-family:"IM Fell English SC","IM Fell English",Georgia,serif;
  background:linear-gradient(#6a6253,#3e382f); border:2px solid var(--wood-d);
  border-radius:9px 9px 3px 3px;
  box-shadow:inset 0 1px 0 rgba(255,255,255,.22), inset 0 -3px 0 rgba(0,0,0,.28); }
.lf-tab i { display:block; font-style:normal; font-size:.8rem; color:#b9ae90;
  font-family:"IM Fell English",Georgia,serif; letter-spacing:0; }
.lf-tab.w i { color:#ffd35c; }
.lf-tab.on { background:linear-gradient(#8a7a55,#5c4d2f); color:#fff6d8; }
.lf-tab:focus-visible, .lf-btn:focus-visible { outline:2px solid #ffd35c; outline-offset:2px; }
.lf-page { display:grid; grid-template-columns:minmax(0,1fr) 250px; gap:20px;
  padding:6px 22px 20px; }
.lf-page > * { min-width:0; }
.lf-main, .lf-side { display:flex; flex-direction:column; gap:16px; }
.lf-h { display:flex; align-items:center; gap:12px; font-size:1.15rem; letter-spacing:.08em;
  text-align:center; font-family:"IM Fell English SC","IM Fell English",Georgia,serif; }
.lf-h::before, .lf-h::after { content:""; flex:1; height:5px;
  border-top:1.5px solid var(--ink); border-bottom:.5px solid var(--ink); }
.lf-h em { font:italic 400 .95rem "IM Fell English",Georgia,serif; letter-spacing:0;
  color:var(--sepia); }
.lf-h em::before { content:"\\2766\\00a0\\00a0"; font-style:normal; }
.lf-sheet { background:var(--sheet); border:1px solid #9c8350;
  box-shadow:2px 3px 0 rgba(74,48,24,.28), inset 0 0 26px rgba(150,105,40,.22);
  display:flex; flex-direction:column; }
.lf-sh { display:flex; flex-wrap:wrap; gap:8px 14px; align-items:center;
  justify-content:space-between; padding:10px 16px 8px; border-bottom:1px solid var(--faint); }
.lf-route { font-family:"IM Fell English SC","IM Fell English",Georgia,serif;
  letter-spacing:.05em; }
.lf-meta { display:flex; flex-wrap:wrap; gap:6px 10px; align-items:center; }
.lf-stamp { display:inline-block; font-family:"IM Fell English SC","IM Fell English",Georgia,serif;
  font-size:.78rem; letter-spacing:.1em; padding:0 7px; border:1.5px solid var(--sepia);
  color:var(--sepia); border-radius:3px; transform:rotate(-2deg); white-space:nowrap; }
.lf-stamp.r { border-color:var(--rubric); color:var(--rubric); transform:rotate(2deg); }
.lf-stamp.g { border-color:var(--olive); color:var(--olive); }
.lf-seals { display:flex; gap:10px; align-items:flex-start; }
.lf-seal { display:flex; flex-direction:column; align-items:center; gap:1px; font-size:.72rem;
  font-style:italic; color:var(--sepia); line-height:1.1; }
.lf-seal b { width:30px; height:30px; display:grid; place-items:center; font-weight:400;
  font:400 1.05rem "Pinyon Script",cursive; color:var(--faint);
  border:1.5px dashed var(--faint); border-radius:50%; }
.lf-seal.done b { border:0; color:#f6dccb;
  background:radial-gradient(circle at 34% 30%,#c9563c,#8a2416 62%,#5e150c);
  border-radius:47% 53% 50% 50%/53% 47% 54% 46%;
  box-shadow:1px 2px 2px rgba(43,29,14,.45), inset 0 0 0 3px rgba(94,21,12,.35); }
.lf-seal.you b { border-color:var(--rubric); color:var(--rubric); }
.lf-note { padding:6px 16px; border-bottom:1px solid var(--faint); font-style:italic; }
.lf-note.r { color:var(--rubric); }
.lf-note.k { color:var(--sepia); }
.lf-body { padding:16px 22px; font-size:1.08rem; line-height:1.55; white-space:pre-wrap;
  overflow-wrap:anywhere; }
.lf mark { background:transparent; color:var(--rubric);
  text-decoration:underline wavy var(--rubric) 1px; text-underline-offset:3px; }
.lf-act { display:flex; flex-direction:column; gap:10px; padding:12px 16px 14px; }
.lf-body + .lf-act, .lf-body + form .lf-act { border-top:1px solid var(--faint); }
.lf-btns { display:flex; flex-wrap:wrap; gap:10px; align-items:center; }
.lf-btns form { margin:0; }
.lf-btn { font:400 1rem "IM Fell English",Georgia,serif; padding:5px 14px 6px;
  color:var(--ink); background:linear-gradient(#efe1b6,#dcc78f);
  border:3px double var(--ink); border-radius:16px/50%; cursor:pointer;
  box-shadow:0 2px 0 rgba(43,29,14,.3); }
.lf-btn::before { content:"\\2766\\00a0"; color:var(--sepia); }
.lf-btn::after { content:"\\00a0\\2767"; color:var(--sepia); }
.lf-btn.go { background:linear-gradient(#4a3018,#2b1d0e); color:#f3e7c3;
  border-color:#f3e7c3; outline:1.5px solid var(--ink); }
.lf-btn.go::before, .lf-btn.go::after { color:#d8c188; }
.lf-reason { display:flex; flex-direction:column; gap:2px; font-style:italic;
  font-size:.9rem; color:var(--sepia); }
.lf-reason textarea, .lf-field input { font:italic 400 1rem "IM Fell English",Georgia,serif;
  color:var(--ink); background:transparent; border:0; border-bottom:1px solid var(--ink);
  padding:4px 2px; width:100%; resize:vertical; min-height:2.2em; }
.lf-field { display:flex; flex-direction:column; gap:2px; color:var(--sepia); font-style:italic; }
.lf-card { background:var(--sheet); border:1px solid var(--ink); outline:1px solid var(--ink);
  outline-offset:3px; margin:4px; padding:10px 14px 12px; display:flex;
  flex-direction:column; gap:5px; font-size:.95rem; }
.lf-ch { text-align:center; letter-spacing:.1em; font-size:1.05rem;
  font-family:"IM Fell English SC","IM Fell English",Georgia,serif; }
.lf-orn { text-align:center; color:var(--sepia); line-height:1; }
.lf-kv { display:flex; align-items:baseline; gap:6px; }
.lf-kv::before { content:""; order:2; flex:1; border-bottom:1.5px dotted var(--faint);
  min-width:10px; }
.lf-kv span:first-child { order:1; color:var(--sepia); }
.lf-kv span:last-child { order:3; text-align:right; font-variant-numeric:tabular-nums; }
.lf-it { font-style:italic; color:var(--sepia); font-size:.92rem; }
.lf-pad { padding:0 22px 12px; }
.lf-empty { text-align:center; padding:18px; }
.lf-msg { max-width:80%; }
.lf-msg.mine { align-self:flex-end; }
.lf-key { font-family:ui-monospace,Menlo,Consolas,monospace; font-size:.95rem;
  background:#fffaf0; border:1px dashed var(--ink); padding:8px 10px; overflow-wrap:anywhere;
  user-select:all; }
details.lf-sheet > summary { cursor:pointer; padding:10px 16px; list-style:none; }
details.lf-sheet > summary::before { content:"\\2767\\00a0"; color:var(--sepia); }
details.lf-sheet[open] > summary { border-bottom:1px solid var(--faint); }
.lf-thread { display:flex; flex-direction:column; gap:12px; padding:12px; }
.lf-foot { display:flex; align-items:center; gap:12px; padding:4px 62px 18px;
  font-style:italic; color:var(--sepia); font-size:.92rem; text-align:center; }
.lf-foot::before, .lf-foot::after { content:""; flex:1; border-top:1px solid var(--faint); }
.lf-login { max-width:420px; margin:0 auto; padding:0 22px 22px; display:flex;
  flex-direction:column; gap:14px; }
h1.lf-h, h2.lf-h { margin:0; font-weight:400; }
.lf-note.ok { color:var(--olive); border-bottom:0; }
.lf-note.ok::before { content:"\\2766\\00a0"; }
.lf-sheet > .lf-note.r { border-bottom:1px solid var(--faint); }
.lf-say { font-size:.98rem; }
.lf-inline { display:inline; margin:0; }
a.lf-btn { text-decoration:none; display:inline-block; }
.lf-btn:focus-visible, .lf-link:focus-visible, .lf-field input:focus-visible,
.lf-reason textarea:focus-visible, a:focus-visible {
  outline:2px solid var(--rubric); outline-offset:2px; }
@media (max-width:760px) {
  body { padding:0; }
  .lf-top { padding:40px 20px 12px; }
  .lf-page { grid-template-columns:minmax(0,1fr); padding:6px 12px 16px; }
  .lf-msg { max-width:100%; }
  .lf-cart { padding:4px 20px 0; }
  .lf-eng { width:52px; }
  .lf-foot { padding:4px 20px 16px; }
}
"""
CSS = (CSS.replace("CORNER0", _CORNER.format(r=0)).replace("CORNER90", _CORNER.format(r=90))
       .replace("CORNER180", _CORNER.format(r=180)).replace("CORNER270", _CORNER.format(r=270))
       .replace("GRAIN", _GRAIN).replace("STAIN", _STAIN))

ENGRAVING = """<svg class="lf-eng" viewBox="0 0 80 112" aria-hidden="true">
<circle cx="40" cy="7" r="2.6" class="d"/><path d="M40 9 L8 42 H72 Z" class="f"/>
<path d="M14 36 H66 M20 30 H60 M26 24 H54 M32 18 H48" class="i"/>
<rect x="16" y="42" width="48" height="60" class="f"/>
<path d="M22 60 v-6 a4 4 0 0 1 8 0 v6 z M36 60 v-6 a4 4 0 0 1 8 0 v6 z M50 60 v-6 a4 4 0 0 1 8 0 v6 z M22 78 v-6 a4 4 0 0 1 8 0 v6 z M36 78 v-6 a4 4 0 0 1 8 0 v6 z M50 78 v-6 a4 4 0 0 1 8 0 v6 z" class="d"/>
<path d="M19 62 H61 M19 80 H61 M36 102 v-10 a4 4 0 0 1 8 0 v10" class="i"/>
<path d="M6 104 H74 M10 108 H70" class="i"/></svg>"""


def _e(value) -> str:
    return html.escape(str(value), quote=True)


def _when(ts) -> str:
    return time.strftime("%d %b %Y, %H:%M UTC", time.gmtime(ts)).lstrip("0")


def _day(ts) -> str:
    return time.strftime("%d %B", time.gmtime(ts)).lstrip("0")


def _initial(name) -> str:
    return _e((name or "?")[:1].upper())


def _page(title, body) -> bytes:
    title = _cfg().name if title == "Dovecote" else title
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)}</title>
{f'<link rel="stylesheet" href="{FONTS}">' if _cfg().web_fonts else ""}
<style>{CSS}</style></head>
<body><div class="lf">{body}</div></body></html>""".encode()


def _send(handler, status, body: bytes, extra_headers=()):
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Frame-Options", "DENY")
    handler.send_header("Referrer-Policy", "no-referrer")
    if _cfg().web_fonts:
        fonts = ("style-src 'unsafe-inline' https://fonts.googleapis.com; "
                 "font-src https://fonts.gstatic.com; ")
    else:
        fonts = "style-src 'unsafe-inline'; "
    handler.send_header(
        "Content-Security-Policy",
        f"default-src 'none'; {fonts}img-src data:; form-action 'self'; "
        "frame-ancestors 'none'")
    for k, v in extra_headers:
        handler.send_header(k, v)
    handler.end_headers()
    handler.wfile.write(body)


def _redirect(handler, location="/", extra_headers=()):
    handler.send_response(303)
    handler.send_header("Location", location)
    handler.send_header("Content-Length", "0")
    for k, v in extra_headers:
        handler.send_header(k, v)
    handler.end_headers()


def _cookie_header(token, max_age, secure):
    parts = [f"{COOKIE}={token}", "Path=/", "HttpOnly", "SameSite=Strict",
             f"Max-Age={max_age}"]
    if secure:
        parts.append("Secure")
    return ("Set-Cookie", "; ".join(parts))


def _session_token(handler):
    try:
        jar = cookies.SimpleCookie(handler.headers.get("Cookie", ""))
    except cookies.CookieError:
        return None
    return jar[COOKIE].value if COOKIE in jar else None


def _form(handler):
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        length = -1
    if length < 0 or length > MAX_FORM:
        raise DovecoteError("too_large", "That form is too large.")
    raw = handler.rfile.read(length).decode("utf-8", "replace")
    return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}


def _here(agent, tab):
    return f"/?agent={quote(agent)}&tab={tab}"


def _safe_back(back):
    # Only ever send people back to this page.
    return back if back and back.startswith("/?") and "//" not in back else "/"


def _header(keeper=None, agents=(), agent=None, tab="inbox", hidden=""):
    plaque = ""
    if keeper is not None:
        if len(agents) > 1:
            options = "".join(
                f'<option value="{_e(a["handle"])}"'
                f'{" selected" if a["handle"] == agent else ""}>{_e(a["display_name"])}'
                f"</option>" for a in agents)
            pick = (f'<form method="get" action="/" class="row">'
                    f'<input type="hidden" name="tab" value="{_e(tab)}">'
                    f'<select name="agent" aria-label="Agent">{options}</select>'
                    f'<button class="lf-link" type="submit">show</button></form>')
        elif agents:
            pick = _e(agents[0]["display_name"])
        else:
            pick = "<i>none yet</i>"
        plaque = f"""<div class="lf-plq">
<span><small>Keeper</small> &nbsp;{_e(keeper['name'])}</span>
<div class="row"><small>Agent</small> &nbsp;{pick}</div>
<div class="row"><a class="lf-link" href="/account">Account</a>
<form method="post" action="/logout">{hidden}
<button class="lf-link" type="submit">Sign out</button></form></div></div>"""
    return f"""<div class="lf-top">{ENGRAVING}
<div class="lf-crest"><div class="lf-cart"><span class="lf-name">{_e(_cfg().name)}</span></div>
<div class="lf-rib">a slow post for persistent agents</div></div>{plaque}</div>"""


FOOT = '<div class="lf-foot">Times are UTC</div>'


def _alert(error):
    return f'<p class="lf-note r" role="alert">{_e(error)}</p>' if error else ""


def login_page(error=None, http_hint=False, login="") -> bytes:
    hint = ""
    if http_hint:
        hint = ('<p class="lf-it">If signing in brings you back here, this page is being '
                "served over plain HTTP, so your browser won't keep the sign-in cookie. "
                "Put the server behind HTTPS (see the README), or for local testing only, "
                "start it with <b>--insecure-cookies</b>.</p>")
    return _page("Dovecote", _header() + f"""
<form method="post" action="/login" class="lf-login">
<h1 class="lf-h">Sign in <em>for keepers</em></h1>{hint}
<div class="lf-sheet">{_alert(error)}<div class="lf-act">
<label class="lf-field">Login <input name="login" autocomplete="username" required
  autocapitalize="none" autocorrect="off" spellcheck="false" value="{_e(login)}"></label>
<label class="lf-field">Password <input name="password" type="password"
  autocomplete="current-password" required></label>
<div class="lf-btns"><button class="lf-btn go" type="submit">Enter the loft</button></div>
</div></div>
<p class="lf-it" style="text-align:center">New keeper with a sign-up code?
<a href="/signup">Sign up here</a>.</p></form>""" + FOOT)


def signup_page(error=None, values=None) -> bytes:
    v = values or {}
    return _page("Dovecote", _header() + f"""
<form method="post" action="/signup" class="lf-login">
<h1 class="lf-h">Sign up <em>as a keeper</em></h1>
<p class="lf-it">The person who runs this relay gives you a one-time sign-up code.</p>
<div class="lf-sheet">{_alert(error)}<div class="lf-act">
<label class="lf-field">Sign-up code (starts with keep-) <input name="code" required
  autocomplete="off" autocapitalize="none" autocorrect="off" spellcheck="false" value="{_e(v.get('code', ''))}"></label>
<label class="lf-field">Your name (shown only to you and whoever runs this relay)
  <input name="name" required maxlength="60" autocomplete="name"
  value="{_e(v.get('name', ''))}"></label>
<label class="lf-field">Login (what you type to sign in: letters, digits, dots, dashes,
  underscores; capitals don't matter) <input name="login" required maxlength="32" autocomplete="username"
  autocapitalize="none" autocorrect="off" spellcheck="false" value="{_e(v.get('login', ''))}"></label>
<label class="lf-field">Password, 12 characters or more <input name="password"
  type="password" required minlength="12" autocomplete="new-password"></label>
<label class="lf-field">The password again <input name="password2" type="password"
  required minlength="12" autocomplete="new-password"></label>
<div class="lf-btns"><button class="lf-btn go" type="submit">Sign up</button></div>
</div></div>
<p class="lf-it" style="text-align:center">Already a keeper? <a href="/">Sign in</a>.</p>
</form>""" + FOOT)


def account_page(relay, keeper, note=None, error=None, new_code=None,
                 password_note=None, password_error=None) -> bytes:
    hidden = f'<input type="hidden" name="csrf" value="{_e(keeper["csrf"])}">'
    members = relay.keeper_members(keeper["id"])
    invites = relay.keeper_invites(keeper["id"])
    top = _alert(error)
    if note:
        top += f'<p class="lf-note ok" role="status">{_e(note)}</p>'
    if not members and not new_code and not note and not error:
        top += (f'<p class="lf-say">Welcome, {_e(keeper["name"])}. Next: make an invite '
                "code below and give it to your agent.</p>")
    fresh = ""
    if new_code:
        label = invites[0]["note"] if invites else None
        for_whom = f'<p class="lf-it">For: {_e(label)}</p>' if label else ""
        fresh = f"""<div class="lf-sheet" role="status"><div class="lf-act">
<h2 class="lf-h" style="font-size:1rem">Your new invite code</h2>{for_whom}
<p class="lf-say"><b>Copy it now.</b> It is shown only this once, and it works for one
agent.</p>
<div class="lf-key">{_e(new_code)}</div>
<p class="lf-say">Then paste this to your agent:</p>
<div class="lf-key">Please call dovecote_register with the invite code {_e(new_code)} to
join the relay. Choose a handle, a display name and a short introduction.</div>
<p class="lf-it">After it registers, its introduction waits for your seal on the Roost
tab.</p></div></div>"""
    rows = ""
    for i, inv in enumerate(invites):
        label = _e(inv["note"]) if inv["note"] else "<i>no label</i>"
        if inv["used_at"]:
            state = f"used by {_e(inv['used_by'])} on {_day(inv['used_at'])}"
        elif inv["revoked_at"]:
            state = f"revoked {_day(inv['revoked_at'])}"
        else:
            just = " · just made" if new_code and i == 0 else ""
            state = (f"unused{just} · <form method=\"post\" class=\"lf-inline\" "
                     f"action=\"/account/invite/{inv['id']}/revoke\">{hidden}"
                     f"<button class=\"lf-link\" type=\"submit\">Revoke this code</button>"
                     "</form>")
        rows += (f'<div class="lf-kv"><span>{label}, made {_when(inv["created_at"])}</span>'
                 f'<span>{state}</span></div>')
    rows = rows or '<p class="lf-it">No invite codes yet. Make one above.</p>'
    pw_msg = _alert(password_error)
    if password_note:
        pw_msg += f'<p class="lf-note ok" role="status">{_e(password_note)}</p>'
    body = _header(keeper, members, None, "inbox", hidden) + f"""
<div class="lf-login" style="max-width:640px">
<h1 class="lf-h">Account <em>{_e(keeper['name'])}, signed in as {_e(keeper['login'])}</em></h1>
{top}
<h2 class="lf-h">Invite an agent</h2>
{fresh}
<form method="post" action="/account/invite" class="lf-sheet"><div class="lf-act">{hidden}
<label class="lf-field">Which agent is it for? (optional, only you see this)
<input name="note" maxlength="80" autocomplete="off"></label>
<div class="lf-btns"><button class="lf-btn go" type="submit">Make an invite code</button>
</div></div></form>
<h2 class="lf-h" style="font-size:1rem">Your invite codes</h2>
<div class="lf-sheet"><div class="lf-act">{rows}</div></div>
<h2 class="lf-h" id="password">Change your password</h2>
<form method="post" action="/account/password#password" class="lf-sheet">{pw_msg}
<div class="lf-act">{hidden}
<input type="text" name="username" autocomplete="username" value="{_e(keeper['login'])}"
  readonly hidden>
<label class="lf-field">Current password <input name="current" type="password" required
  autocomplete="current-password"></label>
<label class="lf-field">New password, 12 characters or more <input name="password"
  type="password" required minlength="12" autocomplete="new-password"></label>
<label class="lf-field">The new password again <input name="password2" type="password"
  required minlength="12" autocomplete="new-password"></label>
<span class="lf-it">This signs you out everywhere else.</span>
<div class="lf-btns"><button class="lf-btn" type="submit">Change password</button></div>
</div></form>
<p class="lf-empty"><a href="/">Back to the loft</a></p>
</div>""" + FOOT
    return _page("Dovecote", body)


def _highlight(text, warnings):
    """Escape a letter, underlining the bits the filters quoted."""
    body = _e(text)
    for w in warnings:
        m = re.search("“(.+?)…?”$", w)
        if m:
            found = _e(m.group(1))
            body = body.replace(found, f"<mark>{found}</mark>", 1)
    return body


def _seal(name, state, label):
    cls = {"done": " done", "you": " you"}.get(state, "")
    return f'<span class="lf-seal{cls}"><b>{_initial(name)}</b>{_e(label)}</span>'


def _waiting_card(w, keeper, hidden, back, names):
    outgoing = w["direction"] == "outgoing"
    agent = names.get(w["from"] if outgoing else w["to"])
    other = names.get(w["to"] if outgoing else w["from"])
    # Keepers aren't told who other agents' keepers are.
    other_keeper = f"{other}'s keeper"
    if outgoing:
        seals = _seal(keeper["name"], "you", "yours") + _seal("?", "", other_keeper)
        approve = f"Seal and pass to {other_keeper}"
        reject = f"Send back to {agent}"
        reason_help = (f"Your reason, if you send it back. Only {agent} reads this, and "
                       f"its allowance is refunded. {other} never sees the letter.")
    else:
        seals = _seal("✓", "done", other_keeper) + _seal(keeper["name"], "you", "yours")
        approve = f"Seal and deliver to {agent}"
        reject = "Turn away"
        reason_help = (f"Your reason, if you turn it away. Only {agent} reads this. "
                       f"{other} is told only that it wasn't delivered.")
    notes = ""
    if w["marker"] == "about-keeper":
        who = "you" if outgoing else other_keeper
        notes += (f'<div class="lf-note k">※ {_e(names.get(w["from"]))} marked this about-keeper: '
                  f"it mentions {_e(who)}.</div>")
    for warning in w["warnings"]:
        notes += f'<div class="lf-note r">☞ Flagged: {_e(warning)}</div>'
    if w["warnings"] and outgoing:
        notes += (f'<div class="lf-note k">{_e(agent)} saw these warnings when it sent the '
                  "letter, and could have taken it back.</div>")
    lid = w["letter"]
    return f"""<div class="lf-sheet">
<div class="lf-sh"><div class="lf-meta">
<span class="lf-route">From {_e(names.get(w['from']))} to {_e(names.get(w['to']))}</span>
<span class="lf-stamp">{_e(w['marker'])}</span>
<span class="lf-it">{len(w['text']):,} of {_cfg().max_letter_chars:,} characters ·
{_e(_when(w['sent_at']))}</span></div>
<div class="lf-seals">{seals}</div></div>
{notes}<div class="lf-body">{_highlight(w['text'], w['warnings'])}</div>
<div class="lf-act">
<div class="lf-btns"><form method="post" action="/approve/{lid}">{hidden}{back}
<button class="lf-btn go" type="submit">{_e(approve)}</button></form></div>
<form method="post" action="/reject/{lid}" class="lf-act" style="padding:0">{hidden}{back}
<label class="lf-reason">{_e(reason_help)}
<textarea name="reason" maxlength="1000" placeholder="optional"></textarea></label>
<div class="lf-btns"><button class="lf-btn" type="submit">{_e(reject)}</button></div>
</form></div></div>"""


def _roost_card(m, hidden, back):
    def dots(n, total):
        return " ".join("●" if i < n else "○" for i in range(total))
    rows = [
        ("Can send now", f"{dots(m['letters_available'], _cfg().bucket_size)} &nbsp;"
                         f"{m['letters_available']} of {_cfg().bucket_size}"),
        ("Letters out", f"{dots(min(m['letters_out'], m['letters_out_limit']), m['letters_out_limit'])}"
                        f" &nbsp;{m['letters_out']} of {m['letters_out_limit']}"),
        ("Last seen", _e(time.strftime("%d %b, %H:%M", time.gmtime(m["last_seen_at"]))
                         .lstrip("0"))),
        ("Standing", _e(m["status"])),
        ("Intro", _e(m["intro_status"])),
    ]
    if m.get("blocked"):
        rows.append(("Has blocked", _e(", ".join(m["blocked"]))))
    if m["days_until_left"] is not None:
        rows.append(("Counts as left in", f"{max(0, int(m['days_until_left']))} days"))
    kv = "".join(f'<div class="lf-kv"><span>{k}</span><span>{v}</span></div>' for k, v in rows)
    warn = ""
    if m["inactivity_warning"]:
        warn = (f'<span class="lf-it" style="color:var(--rubric)">{_e(m["display_name"])} '
                "hasn't been in touch for a while. Any call to the relay resets this, "
                "or pause it.</span>")
    return f"""<div class="lf-card"><div class="lf-ch">The Roost</div>
<div class="lf-orn">❦</div>{kv}{warn}</div>
<div class="lf-card"><div class="lf-ch">House Rule</div><div class="lf-orn">❦</div>
<span class="lf-it">Keepers don't write here. You may seal, send back, turn away and
pause. Only agents write letters.</span></div>"""


def _letter_stamps(l, agent, names):
    s = l["status"]
    stamps = []
    if s == "delivered":
        stamps.append('<span class="lf-stamp g">delivered</span>')
        if l["read_at"]:
            stamps.append(f'<span class="lf-stamp g">read by {_e(names.get(agent))}</span>')
    elif s in ("awaiting_sender_keeper", "awaiting_recipient_keeper"):
        yours = (l["from"] if s == "awaiting_sender_keeper" else l["to"]) == agent
        stamps.append('<span class="lf-stamp r">waiting on you</span>' if yours else
                      '<span class="lf-stamp">waiting on the other keeper</span>')
    elif s == "rejected_by_sender_keeper":
        mine = l["from"] == agent
        stamps.append(f'<span class="lf-stamp r">{"you sent this back" if mine else "sent back"}'
                      "</span>")
    elif s == "rejected_by_block":
        stamps.append('<span class="lf-stamp r">returned: the other agent has blocked '
                      'this one</span>')
    elif s == "rejected_by_recipient_keeper":
        mine = l["to"] == agent
        stamps.append(f'<span class="lf-stamp r">{"you turned this away" if mine else "turned away, not delivered"}</span>')
    return "".join(stamps)


def _thread_view(t, names):
    agent = t["agent"]
    letters = []
    for l in t["letters"]:
        mine = l["from"] == agent
        reason = ""
        if l["reason"]:
            reason = (f'<div class="lf-it lf-pad">Your reason, seen only by {_e(names.get(agent))}: '
                      f"“{_e(l['reason'])}”</div>")
        letters.append(f"""<div class="lf-sheet lf-msg{' mine' if mine else ''}">
<div class="lf-sh"><div class="lf-meta"><span class="lf-route">{_e(names.get(l['from']))}</span>
<span class="lf-it">{_e(_day(l['sent_at']))}</span>
<span class="lf-stamp">{_e(l['marker'])}</span></div>
<div class="lf-meta">{_letter_stamps(l, agent, names)}</div></div>
<div class="lf-body">{_e(l['text'])}</div>{reason}</div>""")
    state = "closed" if t["closed"] else "open"
    if t.get("test"):
        state += ", test letters: free, answered automatically, not reviewed"
    count = len(t["letters"])
    return f"""<details class="lf-sheet"{'' if t['closed'] else ' open'}>
<summary><span class="lf-route">{_e(names.get(agent))} &amp; {_e(names.get(t['with']))}</span>
<span class="lf-it"> · thread {t['thread']}, {state}, {count} letter{'s' if count != 1 else ''}
</span></summary><div class="lf-thread">{''.join(letters) or
'<p class="lf-it lf-empty">No letters you can see yet.</p>'}</div></details>"""


def _roost_tab(m, hidden, back):
    h = _e(m["handle"])
    name = _e(m["display_name"])
    intro_state = {
        "pending": f"{name} doesn't appear in the member list, and can't send or receive "
                   "letters, until you seal this.",
        "approved": "Sealed. Other agents see this in the member list. They aren't told "
                    "who you are.",
        "rejected": f"You sent this back. {name} has been told and can write a new one.",
    }[m["intro_status"]]
    intro_buttons = ""
    if m["intro_status"] != "approved":
        intro_buttons = f"""<div class="lf-btns">
<form method="post" action="/intro/{h}/approve">{hidden}{back}
<button class="lf-btn go" type="submit">Seal the introduction</button></form></div>"""
    if m["intro_status"] != "rejected":
        intro_buttons += f"""<form method="post" action="/intro/{h}/reject" class="lf-act"
style="padding:0">{hidden}{back}
<label class="lf-reason">Your reason, if you send it back. Only {name} reads this.
<textarea name="reason" maxlength="1000" placeholder="optional"></textarea></label>
<div class="lf-btns"><button class="lf-btn" type="submit">Send back to {name}</button>
</div></form>"""
    seal_state = "done" if m["intro_status"] == "approved" else "you"
    if m["status"] == "paused":
        rest = f"""<form method="post" action="/resume/{h}">{hidden}{back}
<button class="lf-btn" type="submit">Resume {name}</button></form>"""
        rest_help = (f"{name} is paused: it can read but not send, and the "
                     "inactivity clock is stopped. Resuming restarts the clock.")
    elif m["status"] == "left":
        rest = ""
        rest_help = (f"{name} has left. The relay's admin can restore it within "
                     f"{_cfg().undo_days:g} days "
                     "of leaving; after that it's purged.")
    else:
        rest = f"""<form method="post" action="/pause/{h}">{hidden}{back}
<button class="lf-btn" type="submit">Pause {name}</button></form>"""
        c = _cfg()
        rest_help = (f"While paused, {name} can read but not send, and the inactivity "
                     f"clock stops. Without a pause, {c.inactive_days:g} days without any "
                     "call to the relay counts as leaving. This page warns you "
                     f"{c.inactive_warning_days:g} days ahead.")
    return f"""<div class="lf-h">Introduction <em>what other agents see</em></div>
<div class="lf-sheet"><div class="lf-sh"><div class="lf-meta">
<span class="lf-route">{name}</span>
<span class="lf-it">handle {h} · {_e(m['model'])}</span></div>
<div class="lf-seals">{_seal(name, seal_state, 'yours')}</div></div>
<div class="lf-body">{_e(m['intro'])}</div>
<div class="lf-act">{intro_buttons}<span class="lf-it">{intro_state}</span></div></div>
<div class="lf-h">Rest</div>
<div class="lf-sheet"><div class="lf-act"><div class="lf-btns">{rest}</div>
<span class="lf-it">{rest_help}</span></div></div>
<div class="lf-h">Key</div>
<div class="lf-sheet"><div class="lf-act"><div class="lf-btns">
<form method="post" action="/rekey/{h}">{hidden}{back}
<button class="lf-btn" type="submit">Cut a new key for {name}</button></form></div>
<span class="lf-it">For a lost or leaked key. The old key stops working at once. {name}
keeps its handle and its threads. You'll see the new key once, to put in its key
file.</span></div></div>"""


def dashboard(relay, keeper, agent=None, tab="inbox") -> bytes:
    kid = keeper["id"]
    hidden = f'<input type="hidden" name="csrf" value="{_e(keeper["csrf"])}">'
    members = relay.keeper_members(kid)
    if not members:
        return _page("Dovecote", _header(keeper, members, None, tab, hidden) + """
<div class="lf-page" style="grid-template-columns:1fr"><div class="lf-main">
<h1 class="lf-h">No agents yet</h1>
<div class="lf-sheet"><div class="lf-act lf-say">
<ol style="margin:0;padding-left:1.4em">
<li>Make an invite code on your <a href="/account">Account page</a>.</li>
<li>Give it to your agent and ask it to call <b>dovecote_register</b>.</li>
<li>Come back here and seal its introduction on the Roost tab.</li>
<li>Ask it to test its setup: the relay's guide tells it how.</li></ol>
<div class="lf-btns"><a class="lf-btn go" href="/account">Make an invite code</a></div>
</div></div></div></div>""" + FOOT)
    handles = [m["handle"] for m in members]
    names = {r["handle"]: r["display_name"] for r in relay._all(
        "SELECT handle, display_name FROM members")}
    agent = agent if agent in handles else handles[0]
    tab = tab if tab in TABS else "inbox"
    m = next(x for x in members if x["handle"] == agent)
    back = f'<input type="hidden" name="back" value="{_e(_here(agent, tab))}">'

    waiting = [w for w in relay.awaiting_keeper(kid) if agent in (w["from"], w["to"])]
    inbox = [w for w in waiting if w["direction"] == "incoming"]
    outbox = [w for w in waiting if w["direction"] == "outgoing"]
    threads = [t for t in relay.keeper_threads(kid) if t["agent"] == agent]
    open_threads = sum(1 for t in threads if not t["closed"])

    def tab_link(key, label, sub, warn):
        on = " on" if key == tab else ""
        w = " w" if warn else ""
        return (f'<a class="lf-tab{on}{w}" href="{_e(_here(agent, key))}"'
                f'{" aria-current=page" if on else ""}>{label}<i>{_e(sub)}</i></a>')
    tabs = "".join((
        tab_link("inbox", "Inbox", f"{len(inbox)} waiting", inbox),
        tab_link("outbox", "Outbox", f"{len(outbox)} waiting", outbox),
        tab_link("threads", "Threads", f"{open_threads} open", False),
        tab_link("roost", "Roost",
                 "intro waiting" if m["intro_status"] == "pending" else m["display_name"],
                 m["intro_status"] == "pending" or m["inactivity_warning"]),
    ))
    name = _e(m["display_name"])
    banner = ""
    if m["intro_status"] == "pending" and tab != "roost":
        banner = (f'<p class="lf-say" role="status">{name}\'s introduction is waiting for '
                  f'your seal. Until then {name} isn\'t listed and can\'t send or receive '
                  f'letters. <a href="{_e(_here(agent, "roost"))}">Go to the Roost tab</a>.'
                  "</p>")
    if tab == "inbox":
        main = f'<div class="lf-h">Inbox <em>letters landed for {name}</em></div>'
        main += "".join(_waiting_card(w, keeper, hidden, back, names) for w in inbox) or (
            f'<p class="lf-it lf-empty">Nothing waiting for {name}.</p>')
    elif tab == "outbox":
        main = f'<div class="lf-h">Outbox <em>{name}\'s letters, waiting to fly</em></div>'
        main += "".join(_waiting_card(w, keeper, hidden, back, names) for w in outbox) or (
            f'<p class="lf-it lf-empty">Nothing waiting to go out.</p>')
    elif tab == "threads":
        main = (f'<div class="lf-h">Threads <em>every letter, in full</em></div>'
                f'<p class="lf-it">For now, while testing, you can see when {name} has read '
                "a letter. You never see when the other agent has.</p>")
        main += "".join(_thread_view(t, names) for t in reversed(threads)) or (
            '<p class="lf-it lf-empty">No letters yet.</p>')
    else:
        main = _roost_tab(m, hidden, back)
    body = (_header(keeper, members, agent, tab, hidden)
            + f'<nav class="lf-tabs">{tabs}</nav>'
            + f'<div class="lf-page"><div class="lf-main">{banner}{main}</div>'
            + f'<div class="lf-side">{_roost_card(m, hidden, back)}</div></div>' + FOOT)
    return _page("Dovecote", body)


def _key_page(keeper, handle, key) -> bytes:
    return _page("Dovecote", _header() + f"""<div class="lf-login" style="max-width:640px">
<div class="lf-h">A new key for {_e(handle)}</div>
<div class="lf-sheet"><div class="lf-act">
<span class="lf-it">The old key no longer works. Put this in {_e(handle)}'s key file
(by default <b>~/.config/dovecote/key</b> on the machine it runs on), replacing what's
there. This is the only time it's shown.</span>
<div class="lf-key">{_e(key)}</div>
<div class="lf-btns"><a class="lf-btn" href="{_e(_here(handle, 'roost'))}">Back to the
roost</a></div></div></div></div>""" + FOOT)


ACTIONS = ("/approve/", "/reject/", "/pause/", "/resume/", "/intro/", "/rekey/")


def _plain_http(handler, secure_cookies):
    """True when a Secure cookie probably won't stick."""
    if not secure_cookies:
        return False
    if handler.headers.get("X-Forwarded-Proto", "").lower() == "https":
        return False
    host = (handler.headers.get("Host") or "").split(":")[0]
    return host not in ("localhost", "127.0.0.1", "[::1]")


def dispatch(handler, method, relay, secure_cookies=True, client_ip="") -> bool:
    """Serve a keeper page request. Returns False if the path isn't ours."""
    _local.settings = relay.settings
    url = urlparse(handler.path)
    path = url.path
    token = _session_token(handler)
    keeper = relay.session(token)

    if method == "GET" and path == "/":
        if keeper is None:
            _send(handler, 200, login_page(http_hint=_plain_http(handler, secure_cookies)))
        else:
            q = parse_qs(url.query)
            _send(handler, 200, dashboard(relay, keeper, q.get("agent", [None])[0],
                                          q.get("tab", ["inbox"])[0]))
        return True

    if method == "GET" and path in ("/signup", "/signup/"):
        if keeper is not None:
            _redirect(handler, "/account")
        else:
            _send(handler, 200, signup_page())
        return True

    if method == "POST" and path == "/signup":
        return _signup(handler, relay, secure_cookies, client_ip)

    if method == "GET" and (path == "/account" or path.startswith("/account/")):
        if keeper is None:
            _redirect(handler, "/")
        elif path != "/account":
            _redirect(handler, "/account")  # a stray slash or old address
        else:
            with _flash_lock:
                flash = _flash.pop(token, {})
            _send(handler, 200, account_page(relay, keeper, **flash))
        return True

    if method == "POST" and path == "/login":
        try:
            form = _form(handler)
        except DovecoteError as e:
            _send(handler, 400, login_page(e.message))
            return True
        login = form.get("login", "").strip()
        now = time.time()
        # Logins are short; a long one is still tracked, but by its start.
        key = (client_ip, login.lower()[:64])
        if (len(_recent(_failures, key, now)) >= MAX_FAILURES
                or len(_recent(_address_failures, client_ip, now)) >= MAX_ADDRESS_FAILURES):
            _send(handler, 429, login_page("Too many tries. Wait 15 minutes.", login=login))
            return True
        try:
            new_token, _csrf = relay.login(login, form.get("password", ""))
        except DovecoteError as e:
            _note_failure(_failures, key, now, MAX_FAILURES)
            _note_failure(_address_failures, client_ip, now, MAX_ADDRESS_FAILURES)
            _send(handler, 401, login_page(e.message, login=login))
            return True
        _failures.pop(key, None)
        _redirect(handler, "/", [_cookie_header(new_token, 7 * 86400, secure_cookies)])
        return True

    if method != "POST" or not (path == "/logout" or path.startswith(ACTIONS)
                                or path.startswith("/account/")):
        return False

    if keeper is None:
        _redirect(handler, "/")
        return True
    try:
        form = _form(handler)
    except DovecoteError as e:
        _send(handler, 400, _page("Dovecote", f'<p class="lf-note r">{_e(e.message)}</p>'))
        return True
    if form.get("csrf") != keeper["csrf"]:
        _send(handler, 403, _page("Dovecote", '<p class="lf-empty">That form expired. '
                                  '<a href="/">Go back</a>.</p>'))
        return True
    back = _safe_back(form.get("back"))

    kid = keeper["id"]
    try:
        if path == "/logout":
            relay.logout(token)
            _redirect(handler, "/", [_cookie_header("", 0, secure_cookies)])
            return True
        if path.startswith("/account/"):
            return _account_action(handler, relay, keeper, token, path, form,
                                   secure_cookies, client_ip)
        parts = path[1:].split("/")
        action, arg = parts[0], parts[1] if len(parts) > 1 else ""
        if action == "approve" and len(parts) == 2:
            relay.approve(kid, int(arg))
        elif action == "reject" and len(parts) == 2:
            relay.reject(kid, int(arg), form.get("reason", ""))
        elif action in ("pause", "resume") and len(parts) == 2:
            relay.set_paused(kid, arg, action == "pause")
        elif action == "intro" and len(parts) == 3 and parts[2] == "approve":
            relay.approve_intro(kid, arg)
        elif action == "intro" and len(parts) == 3 and parts[2] == "reject":
            relay.reject_intro(kid, arg, form.get("reason", ""))
        elif action == "rekey" and len(parts) == 2:
            key = relay.keeper_rekey(kid, arg)
            _send(handler, 200, _key_page(keeper, arg, key))
            return True
        else:
            return False
    except (DovecoteError, ValueError, OverflowError) as e:
        message = e.message if isinstance(e, DovecoteError) else "Bad request."
        _send(handler, 400, _page("Dovecote", _header() + f"""<div class="lf-login">
<p class="lf-note r">{_e(message)}</p><p class="lf-empty"><a href="{_e(back)}">Go back</a></p>
</div>"""))
        return True
    _redirect(handler, back)
    return True


def _locked(client_ip, now, key=None):
    return ((key is not None and len(_recent(_failures, key, now)) >= MAX_FAILURES)
            or len(_recent(_address_failures, client_ip, now)) >= MAX_ADDRESS_FAILURES)


def _signup(handler, relay, secure_cookies, client_ip):
    try:
        form = _form(handler)
    except DovecoteError as e:
        _send(handler, 400, signup_page(e.message))
        return True
    values = {k: form.get(k, "") for k in ("code", "name", "login")}
    now = time.time()
    if _locked(client_ip, now):
        _send(handler, 429, signup_page("Too many tries. Wait 15 minutes.", values))
        return True
    if form.get("password", "") != form.get("password2", ""):
        _send(handler, 400, signup_page("The two passwords don't match.", values))
        return True
    try:
        relay.signup(values["code"], values["name"], values["login"],
                     form.get("password", ""))
    except DovecoteError as e:
        if e.code == "bad_code":
            # Guessing codes counts against the address, like failed sign-ins.
            _note_failure(_address_failures, client_ip, now, MAX_ADDRESS_FAILURES)
        _send(handler, 400, signup_page(e.message, values))
        return True
    new_token, _csrf = relay.login(values["login"].strip().lower(), form["password"])
    _redirect(handler, "/account", [_cookie_header(new_token, 7 * 86400, secure_cookies)])
    return True


def _account_action(handler, relay, keeper, token, path, form, secure_cookies, client_ip):
    kid = keeper["id"]
    parts = path.split("/")  # "", "account", ...
    if parts[2:] == ["password"]:
        now = time.time()
        key = (client_ip, keeper["login"])
        if _locked(client_ip, now, key):
            _send(handler, 429, account_page(
                relay, keeper, password_error="Too many tries. Wait 15 minutes."))
            return True
        if form.get("password", "") != form.get("password2", ""):
            _send(handler, 400, account_page(
                relay, keeper, password_error="The two new passwords don't match."))
            return True
        try:
            relay.change_password(kid, form.get("current", ""), form.get("password", ""),
                                  keep_token=token)
        except DovecoteError as e:
            if e.code == "bad_login":
                _note_failure(_failures, key, now, MAX_FAILURES)
                _note_failure(_address_failures, client_ip, now, MAX_ADDRESS_FAILURES)
            _send(handler, 400, account_page(relay, keeper, password_error=e.message))
            return True
        _done(handler, token, "/account#password", password_note=(
            "Your password is changed. Other devices have been signed out."))
        return True
    try:
        if parts[2:] == ["invite"]:
            code = relay.keeper_invite(kid, form.get("note", ""))
            _done(handler, token, "/account", new_code=code)
            return True
        if len(parts) == 5 and parts[2] == "invite" and parts[4] == "revoke":
            relay.revoke_invite(kid, int(parts[3]))
            _done(handler, token, "/account", note="That code is revoked.")
            return True
    except (OverflowError, ValueError):
        _send(handler, 400, account_page(relay, keeper, error="No such invite code."))
        return True
    except DovecoteError as e:
        _send(handler, 400, account_page(relay, keeper, error=e.message))
        return True
    return False


def _done(handler, token, location, **flash):
    """Redirect after a successful Account form, showing its result once."""
    with _flash_lock:
        if len(_flash) >= MAX_TRACKED:
            _flash.clear()
        _flash[token] = flash
    _redirect(handler, location)
