"""A fail-closed screen for contact numbers and e-mail addresses in committed eval files (M2 PR 7).

zara's review (Z1): the dataset and the corpus live in a public repository, where anything
committed stays in history. This screen refuses text that *looks like* personal data unless it
is one of the corpus's fake forms. It is the dataset's own check, not M7's runtime guardrail, and
a screen, not a proof: names and street addresses pass it, so the briefs still ask for none.

Refused:
- a run of 9 or more digits, spaces and dashes (a phone number, an account number), unless it is
  a fake number `+91 00000 ddddd`, an allowed public number (112) or an ISO date `YYYY-MM-DD`;
- the same run shaped like an Aadhaar number (12 digits, optionally in groups of 4);
- an e-mail address whose domain does not end in the reserved `.example` (RFC 2606).

Each finding is a fixed description, never the matched text.
"""

import re
from typing import Final

EMAIL: Final = re.compile(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)")
URL_HOST: Final = re.compile(r"https?://([^/\s\"'<>]+)")
PHONE_LIKE: Final = re.compile(r"\+?\d[\d \-]{7,}\d")
FAKE_PHONE: Final = re.compile(r"^\+91 00000 \d{5}$")
AADHAAR_LIKE: Final = re.compile(r"^\d{4}[ -]?\d{4}[ -]?\d{4}$")
ISO_DATE: Final = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ALLOWED_NUMBERS: Final = frozenset({"112"})
"""Real public numbers the corpus may print: India's emergency number (business-context §7)."""

RESERVED_DOMAIN: Final = ".example"


def personal_data_findings(text: str) -> list[str]:
    """What in `text` looks like personal data, as fixed descriptions (empty when nothing does)."""
    findings: list[str] = []
    for match in PHONE_LIKE.finditer(text):
        value = match.group(0)
        if AADHAAR_LIKE.match(value):
            finding = "an Aadhaar-shaped number"
        elif FAKE_PHONE.match(value) or value in ALLOWED_NUMBERS or ISO_DATE.match(value):
            continue
        else:
            finding = "a phone-like number that is not a fake +91 00000 number"
        if finding not in findings:
            findings.append(finding)
    if any(not m.group(1).casefold().endswith(RESERVED_DOMAIN) for m in EMAIL.finditer(text)):
        findings.append(f"an e-mail address outside {RESERVED_DOMAIN}")
    return findings
