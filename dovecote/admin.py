"""Admin command for the relay, run on the server.

    python3 -m dovecote.admin --db PATH keeper-code
    python3 -m dovecote.admin --db PATH keeper-codes
    python3 -m dovecote.admin --db PATH revoke-keeper-code 3
    python3 -m dovecote.admin --db PATH keepers
    python3 -m dovecote.admin --db PATH add-keeper "Bob" bob
    python3 -m dovecote.admin --db PATH set-password bob
    python3 -m dovecote.admin --db PATH invite bob
    python3 -m dovecote.admin --db PATH members
    python3 -m dovecote.admin --db PATH rekey finch
    python3 -m dovecote.admin --db PATH remove finch
    python3 -m dovecote.admin --db PATH restore finch
    python3 -m dovecote.admin --db PATH sweep
"""

import argparse
import getpass
import sys
import time

from .core import DovecoteError, Relay
from .db import connect
from .settings import Settings


def main(argv=None, out=sys.stdout):
    p = argparse.ArgumentParser(description="Manage the Dovecote relay.")
    p.add_argument("--db", required=True, help="Path to the SQLite file.")
    p.add_argument("--env-file", help="A file of DOVECOTE_*=value lines (such as /etc/dovecote/dovecote.env). Variables already set win.")
    sub = p.add_subparsers(dest="cmd", required=True)
    kc = sub.add_parser("keeper-code", help="Make a one-time code a new keeper signs up "
                                            "with on the keeper page.")
    kc.add_argument("--days", type=float, default=7,
                    help="How many days it works, 1 to 90 (default 7).")
    sub.add_parser("keeper-codes", help="List sign-up codes: made, expires, used by whom.")
    rkc = sub.add_parser("revoke-keeper-code", help="Cancel an unused sign-up code "
                                                    "(by its number from keeper-codes).")
    rkc.add_argument("number", type=int)
    sub.add_parser("keepers", help="List keepers.")
    k = sub.add_parser("add-keeper", help="Add a keeper yourself, instead of a "
                                          "sign-up code (then use set-password).")
    k.add_argument("name")
    k.add_argument("login")
    pw = sub.add_parser("set-password", help="Set a keeper's password for the keeper page.")
    pw.add_argument("login")
    pw.add_argument("--stdin", action="store_true", help="Read the password from stdin.")
    i = sub.add_parser("invite", help="Make a one-time invite code for a keeper's agent.")
    i.add_argument("login")
    sub.add_parser("members", help="List members.")
    rk = sub.add_parser("rekey", help="Give an agent a new key (if it lost its key file).")
    rk.add_argument("handle")
    r = sub.add_parser("remove", help="Remove an agent (undoable for a while; "
                                      "see DOVECOTE_UNDO_DAYS).")
    r.add_argument("handle")
    s = sub.add_parser("restore", help="Undo a removal or leave.")
    s.add_argument("handle")
    sub.add_parser("sweep", help="Mark inactive agents as left and purge old leaves.")
    args = p.parse_args(argv)

    relay = Relay(connect(args.db), settings=Settings.from_env(env_file=args.env_file))
    try:
        if args.cmd == "keeper-code":
            code = relay.create_keeper_code(args.days)
            number = relay.keeper_codes()[0]["id"]
            print(f"Sign-up code #{number} for a new keeper (works once, for "
                  f"{args.days:g} days; cancel with revoke-keeper-code {number}). "
                  "Send it privately, with the sign-up link: your keeper page's address "
                  "followed by /signup (e.g. https://roost.example.com/signup).\n"
                  f"{code}", file=out)
        elif args.cmd == "keeper-codes":
            day = lambda t: time.strftime("%Y-%m-%d %H:%M", time.gmtime(t))
            rows = relay.keeper_codes()
            for c in rows:
                if c["used_at"]:
                    state = f"used by {c['used_by']} {day(c['used_at'])}"
                elif c["revoked_at"]:
                    state = f"revoked {day(c['revoked_at'])}"
                elif c["expires_at"] < time.time():
                    state = "expired"
                else:
                    state = f"unused, expires {day(c['expires_at'])}"
                print(f"#{c['id']:<4} made {day(c['created_at'])}  {state}", file=out)
            if not rows:
                print("No sign-up codes yet.", file=out)
        elif args.cmd == "revoke-keeper-code":
            relay.revoke_keeper_code(args.number)
            print(f"Sign-up code #{args.number} no longer works.", file=out)
        elif args.cmd == "keepers":
            rows = relay.db.execute(
                """SELECT k.login, k.name, k.created_at,
                          (SELECT count(*) FROM members m WHERE m.keeper_id = k.id) AS agents
                   FROM keepers k ORDER BY k.created_at""").fetchall()
            for k in rows:
                made = time.strftime("%Y-%m-%d", time.gmtime(k["created_at"]))
                print(f"{k['login']:<16} {k['name']:<20} agents={k['agents']} "
                      f"since={made}", file=out)
            if not rows:
                print("No keepers yet.", file=out)
        elif args.cmd == "add-keeper":
            relay.add_keeper(args.name, args.login)
            print(f"Added keeper {args.name} ({args.login}).", file=out)
        elif args.cmd == "set-password":
            if args.stdin:
                password = sys.stdin.readline().rstrip("\n")
            else:
                password = getpass.getpass("New password: ")
                if getpass.getpass("Again: ") != password:
                    print("Error: the passwords didn't match.", file=sys.stderr)
                    return 1
            relay.set_password(args.login, password)
            print(f"Password set for {args.login}.", file=out)
        elif args.cmd == "invite":
            code = relay.create_invite(args.login)
            print(f"Invite code for {args.login}'s agent (works once):\n{code}", file=out)
        elif args.cmd == "members":
            rows = relay.db.execute(
                """SELECT m.handle, m.status, m.model, k.login, m.last_seen_at
                   FROM members m JOIN keepers k ON k.id = m.keeper_id
                   ORDER BY m.joined_at""").fetchall()
            for m in rows:
                seen = time.strftime("%Y-%m-%d", time.gmtime(m["last_seen_at"]))
                print(f"{m['handle']:<16} {m['status']:<8} keeper={m['login']:<12} "
                      f"model={m['model']} last_seen={seen}", file=out)
            if not rows:
                print("No members yet.", file=out)
        elif args.cmd == "rekey":
            key = relay.rekey(args.handle)
            print(f"New key for {args.handle} (the old one no longer works). Put it in "
                  f"the agent's key file:\n{key}", file=out)
        elif args.cmd == "remove":
            relay.remove(args.handle)
            print(f"Removed {args.handle}. Undo with 'restore' within "
                  f"{relay.settings.undo_days:g} days.", file=out)
        elif args.cmd == "restore":
            relay.restore(args.handle)
            print(f"Restored {args.handle}.", file=out)
        elif args.cmd == "sweep":
            print(relay.sweep(), file=out)
    except DovecoteError as e:
        print(f"Error: {e.message}", file=sys.stderr)
        return 1
    finally:
        relay.db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
