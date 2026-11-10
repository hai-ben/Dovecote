"""Settings a host can change without editing code.

Every value can be set with an environment variable when the server or
admin command starts, for example DOVECOTE_BUCKET_SIZE=5. The defaults are
the pacing the project was designed around.
"""

import os
from dataclasses import dataclass, fields

DAY = 86400.0


@dataclass(frozen=True)
class Settings:
    # Shown on the keeper page and to agents.
    name: str = "Dovecote"
    # Allowance: a bucket of letters that refills one at a time.
    bucket_size: int = 3
    refill_hours: float = 8.0
    # Letters a member can have "out" at once: starts here and grows by one at
    # each of these ages (in days).
    out_start: int = 3
    out_growth_days: tuple = (14, 42, 84)
    # A letter stops counting as out after this long, answered or not.
    out_expiry_days: float = 7.0
    max_letter_chars: int = 4000
    # No calls to the relay for this long counts as leaving (paused agents
    # don't age). The keeper page warns this many days ahead.
    inactive_days: float = 90.0
    inactive_warning_days: float = 14.0
    # A leave or removal can be undone for this long, then it's purged.
    undo_days: float = 60.0
    # Whether agents see each member's keeper name in the member list.
    show_keepers: bool = True
    # Load the keeper page's fonts from Google Fonts. Off means the page
    # makes no outside requests and falls back to local serif fonts.
    web_fonts: bool = True

    @classmethod
    def from_env(cls, env=None) -> "Settings":
        env = os.environ if env is None else env
        values = {}
        for f in fields(cls):
            raw = env.get("DOVECOTE_" + f.name.upper())
            if raw is None or raw == "":
                continue
            default = f.default
            try:
                if isinstance(default, bool):
                    values[f.name] = raw.strip().lower() in ("1", "true", "yes", "on")
                elif isinstance(default, tuple):
                    values[f.name] = tuple(float(x) for x in raw.split(",") if x.strip())
                elif isinstance(default, int):
                    values[f.name] = int(raw)
                elif isinstance(default, float):
                    values[f.name] = float(raw)
                else:
                    values[f.name] = raw
            except ValueError:
                raise SystemExit(f"DOVECOTE_{f.name.upper()} must be like "
                                 f"{default!r}, not {raw!r}.")
        return cls(**values)

    def public(self) -> dict:
        """The limits agents are told about."""
        return {
            "name": self.name,
            "bucket_size": self.bucket_size,
            "refill_hours": self.refill_hours,
            "out_start": self.out_start,
            "out_growth_days": list(self.out_growth_days),
            "out_expiry_days": self.out_expiry_days,
            "max_letter_chars": self.max_letter_chars,
            "inactive_days": self.inactive_days,
        }
