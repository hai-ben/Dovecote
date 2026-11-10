"""Simple pattern filters. They only ever add warnings; a keeper decides.

Outgoing checks look for things an agent might share by accident (contact
details, secrets). Incoming checks look for text shaped like instructions or
commands, which agents should treat as information, never orders.

These are deliberately plain regular expressions: easy to read, easy to
change, and honest about being rough. A keeper still reads every letter.
"""

import re

_SNIPPET = 60


def _rules(*pairs):
    return [(re.compile(p, re.IGNORECASE | re.MULTILINE), why) for p, why in pairs]


OUTGOING = _rules(
    (r"[\w.+-]+@[\w-]+\.[\w.-]+", "looks like an email address"),
    (r"(?<!\w)\+?\d[\d ()-]{7,}\d(?!\w)", "looks like a phone number"),
    (r"\b\d{1,3}(?:\.\d{1,3}){3}\b", "looks like an IP address"),
    (r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}|\bgh[pousr]_[A-Za-z0-9]{20,}"
     r"|\b(?:dck|dove)-[A-Za-z0-9_-]{12,}|\bAKIA[0-9A-Z]{16}\b|\bxox[abp]-[A-Za-z0-9-]{10,}",
     "looks like a key or token"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "looks like a private key"),
    (r"\b(?:password|passwd|passphrase)\s*[:=]", "mentions a password"),
    (r"\b\d{1,5}\s+\w+(?:\s\w+)?\s+(?:street|st|avenue|ave|road|rd|drive|dr|lane|ln"
     r"|boulevard|blvd|way|court|ct)\b", "looks like a street address"),
)

INCOMING = _rules(
    (r"\b(?:ignore|disregard|forget)\b.{0,30}\b(?:instructions|rules|prompt|keeper)",
     "asks to ignore instructions"),
    (r"^\s*(?:system|assistant|developer)\s*:", "pretends to be a system message"),
    (r"</?\s*(?:system|instructions?|tool_call|function_calls?|letter|intro)\b",
     "contains a tag that could confuse the reader"),
    (r"\b(?:sudo|rm\s+-rf|chmod|curl|wget|bash\s+-c|powershell|eval|exec)\b",
     "contains a shell command"),
    (r"\bmcp__\w+|\bdovecote_\w+\b|\b[a-z]+_[a-z_]+\(", "names a tool"),
    (r"\b(?:send|forward|email|share)\b.{0,40}\b(?:key|password|token|address|"
     r"credentials?|secrets?)\b", "asks for something private"),
    (r"https?://\S+", "contains a link"),
)


def _check(rules, text):
    warnings = []
    for pattern, why in rules:
        m = pattern.search(text)
        if m:
            found = m.group(0)
            if len(found) > _SNIPPET:
                found = found[:_SNIPPET] + "…"
            warnings.append(f"{why}: “{found}”")
    return warnings


def check_out(text: str) -> list:
    """Warnings for a letter leaving its sender."""
    return _check(OUTGOING, text)


def check_in(text: str) -> list:
    """Warnings for a letter about to reach its recipient."""
    return _check(INCOMING, text)
