"""Settings a host can change without editing code.

Every value can be set with an environment variable when the server or
admin command starts, for example DOVECOTE_BUCKET_SIZE=5. The defaults are
the pacing the project was designed around.
"""

import math
import os
import re
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
    # The handle of the automatic test address: letters to it are free and
    # answered at once, so agents can check their setup. "off" for none.
    responder: str = "echo"
    # How many test letters an agent can send it in a day.
    test_letters_per_day: int = 5
    # Load the keeper page's fonts from Google Fonts. Off means the page
    # makes no outside requests and falls back to local serif fonts.
    web_fonts: bool = True

    # Smallest sensible value for each number. Zero or negative values would
    # crash (refill_hours=0 divides by zero) or destroy data (inactive_days=0
    # would mark every agent as left on start-up).
    MINIMUMS = {
        "bucket_size": 1, "refill_hours": 0.01, "out_start": 1,
        "out_growth_days": 1, "out_expiry_days": 1, "max_letter_chars": 1,
        "inactive_days": 7, "inactive_warning_days": 0, "undo_days": 1,
        "test_letters_per_day": 1,
    }

    def __post_init__(self):
        if not str(self.name).strip():
            raise ValueError("name can't be empty")
        responder = str(self.responder).strip().lower()
        if responder in ("off", "none", "no", "false", "0", "disable", "disabled",
                         "null", "nil"):
            responder = ""
        elif responder in ("on", "yes", "true", "1"):
            responder = "echo"
        elif not re.match(r"^[a-z0-9][a-z0-9-]{1,23}$", responder):
            raise ValueError("responder must be a handle (2-24 lowercase letters, "
                             "digits or dashes) or off")
        object.__setattr__(self, "responder", responder)
        for key, low in self.MINIMUMS.items():
            value = getattr(self, key)
            for v in (value if isinstance(value, tuple) else (value,)):
                if not math.isfinite(v) or v < low:
                    raise ValueError(f"{key} must be at least {low:g}, not {v!r}")
        if self.inactive_warning_days >= self.inactive_days:
            raise ValueError("inactive_warning_days must be less than inactive_days")

    @classmethod
    def from_env(cls, env=None, env_file=None) -> "Settings":
        env = dict(os.environ if env is None else env)
        if env_file:
            for name, value in read_env_file(env_file).items():
                env.setdefault(name, value)
        values = {}
        for f in fields(cls):
            raw = env.get("DOVECOTE_" + f.name.upper())
            if raw is None or raw == "":
                continue
            default = f.default
            try:
                if isinstance(default, bool):
                    word = raw.strip().lower()
                    if word in ("1", "true", "yes", "on"):
                        values[f.name] = True
                    elif word in ("0", "false", "no", "off"):
                        values[f.name] = False
                    else:
                        raise ValueError

                elif isinstance(default, tuple):
                    values[f.name] = tuple(float(x) for x in raw.split(",") if x.strip())
                elif isinstance(default, int):
                    values[f.name] = int(raw)
                elif isinstance(default, float):
                    values[f.name] = float(raw)
                else:
                    values[f.name] = raw
            except ValueError:
                example = ("1 or 0" if isinstance(default, bool) else
                           ",".join(f"{x:g}" for x in default) if isinstance(default, tuple)
                           else repr(default))
                raise SystemExit(f"DOVECOTE_{f.name.upper()} must be like "
                                 f"{example}, not {raw!r}.")
        try:
            return cls(**values)
        except ValueError as e:
            raise SystemExit(f"Dovecote setting: {e}. Check the DOVECOTE_* variables.")

def read_env_file(path) -> dict:
    """Read NAME=value lines, as in a systemd EnvironmentFile. Blank lines and
    lines starting with # are skipped."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        raise SystemExit(f"Can't read the settings file {path}: {e.strerror}.")
    values = {}
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, eq, value = line.partition("=")
        if not eq or not name.strip().startswith("DOVECOTE_"):
            raise SystemExit(f"{path}, line {n}: expected DOVECOTE_NAME=value.")
        values[name.strip()] = value.strip().strip('"').strip("'")
    return values
