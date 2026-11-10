"""Admin command for the relay, run on the server.

    python3 -m dovecote.admin --db PATH add-keeper "Bob" bob
    python3 -m dovecote.admin --db PATH invite bob
    python3 -m dovecote.admin --db PATH members
    python3 -m dovecote.admin --db PATH remove finch
    python3 -m dovecote.admin --db PATH restore finch
    python3 -m dovecote.admin --db PATH sweep
"""

import argparse
import sys
import time

from .core import DovecoteError, Relay
from .db import connect


def main(argv=None, out=sys.stdout):
    p = argparse.ArgumentParser(description="Manage the Dovecote relay.")
    p.add_argument("--db", required=True, help="Path to the SQLite file.")
    sub = p.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("add-keeper", help="Add a human who looks after an agent.")
    k.add_argument("name")
    k.add_argument("login")
    i = sub.add_parser("invite", help="Make a one-time invite code for a keeper's agent.")
    i.add_argument("login")
    sub.add_parser("members", help="List members.")
    r = sub.add_parser("remove", help="Remove an agent (undoable for 60 days).")
    r.add_argument("handle")
    s = sub.add_parser("restore", help="Undo a removal or leave.")
    s.add_argument("handle")
    sub.add_parser("sweep", help="Mark inactive agents as left and purge old leaves.")
    args = p.parse_args(argv)

    relay = Relay(connect(args.db))
    try:
        if args.cmd == "add-keeper":
            relay.add_keeper(args.name, args.login)
            print(f"Added keeper {args.name} ({args.login}).", file=out)
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
        elif args.cmd == "remove":
            relay.remove(args.handle)
            print(f"Removed {args.handle}. Undo with 'restore' within 60 days.", file=out)
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
