"""Secret-pattern redaction for everything the report renders.

Redaction never depends on a finding's category alone, because the agent picks the category.
Every excerpt, justification, title, and fix passes through `redact`; excerpts of `secrets` and
`data-handling` findings (and VC-6 findings) are replaced entirely.
"""

import re

# No leading word boundary on the prefixed token formats: a secret glued to other text (`xAKIA...`, `_ghp_...`) is still one.
# A subset of common secret-scanner rules, by the same names where they exist.
SECRET_PATTERNS = [
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----.*?(?:-----END [A-Z ]*PRIVATE KEY(?: BLOCK)?-----|\Z)", re.DOTALL)),
    ("aws-access-key-id", re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}")),
    ("github-pat", re.compile(r"gh[pousr]_[A-Za-z0-9][A-Za-z0-9_.-]{35,}")),  # ghs_ tokens can be ghs_<app id>_<JWT>, with dots
    ("github-fine-grained-pat", re.compile(r"github_pat_[A-Za-z0-9_]{22,}")),
    ("slack-token", re.compile(r"xox[abposr]-[A-Za-z0-9-]{10,}")),
    ("gcp-api-key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("stripe-secret-key", re.compile(r"[rs]k_live_[0-9A-Za-z]{20,}")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("sk-api-key", re.compile(r"(?<![A-Za-z])sk-[A-Za-z0-9_-]{20,}")),
    ("generic-secret", re.compile(r"(?i)(?<![A-Za-z0-9])(?:api[_-]?key|secret|token|passw(?:or)?d|pwd)(?![A-Za-z0-9])\s*[:=]\s*['\"]?[^\s'\"]{8,}")),
]
REDACT_WHOLE = {"secrets", "data-handling"}
MAX_EXCERPT_LINES = 10


def redact(text: str) -> str:
    for rule, pattern in SECRET_PATTERNS:
        text = pattern.sub(f"[redacted: {rule}]", text)
    return text


def _whole(finding: dict) -> bool:
    return finding["category"] in REDACT_WHOLE or any(
        s["framework"] == "golden-road-vc" and s["id"] == "VC-6" for s in finding.get("standards", []))


def excerpt(finding: dict, anchor: dict) -> list[str]:
    """The lines of one evidence anchor that are safe to show, at most 10."""
    if _whole(finding):
        return [f"[redacted: {finding['rule_id']} at {anchor['file']}:{anchor['start_line']}]"]
    return redact(anchor["snippet"]).splitlines()[:MAX_EXCERPT_LINES]


_KEY_BEGIN = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----")  # "( BLOCK)": PGP
_KEY_END = re.compile(r"-----END [A-Z ]*PRIVATE KEY(?: BLOCK)?-----")
_KEY_HEADER = re.compile(r"(?:Proc-Type|DEK-Info|Version|Comment|Hash|Charset|MessageID)\s*:.*")  # PEM and PGP armor headers
_BASE64 = re.compile(r"[A-Za-z0-9+/]*={0,2}|=[A-Za-z0-9+/]{4}")  # "=" only as padding, so `flag=True` is never key-shaped; the second form is a PGP armor checksum line
_COMMENT = re.compile(r"^(?:(?:#|//|;|\*|/\*)(?:\s+|$))+")  # a marker counts only before whitespace: "/" and "+" are base64
_LEADING = re.compile(r"^[#;*]+")  # a marker glued to the text ("#MIIE...") is tried as a second reading
_QUOTED = re.compile(r"""[bBrRuUf@]{0,2}(["'`])(.*?)\1""")
_ESCAPED_EOL = re.compile(r"\\r\\n|\\n|\\r")
_WRAPPING = re.compile(r"[\s+,;()\[\]{}\\]*")


def _key_readings(line: str) -> list[str]:
    """The ways a line can be read once its wrapping (a YAML "- ", a comment marker, string quotes and escapes) is taken
    off. Only wrapping that can't be base64 is removed: a bare line keeps a leading "/" and a trailing "+"."""
    text = re.sub(r"^-\s+", "", line.strip())
    text = _COMMENT.sub("", text)
    text = re.sub(r"\s+\*/$", "", text)
    strings = _QUOTED.findall(text)
    if strings and _WRAPPING.fullmatch(_QUOTED.sub("", text)):
        return ["".join(_ESCAPED_EOL.sub("", body) for _, body in strings)]
    return [text, _LEADING.sub("", text)]


def _key_shaped(line: str) -> bool:
    """A blank line, a PEM header, base64, or a long run of base64 with one or two corrupted characters (so a bad byte in
    the middle of a key doesn't end the span and show the rest of it). Short lines such as `skipOwnerCheck=True` never are."""
    return any(not r or _KEY_HEADER.fullmatch(r) or _BASE64.fullmatch(r)
               or (len(r) >= 40 and not re.search(r"\s", r) and len(re.findall(r"[^A-Za-z0-9+/=]", r)) <= 2)
               for r in _key_readings(line))


def private_key_lines(lines: list[str]) -> tuple[set[int], bool]:
    """(indexes of the lines to withhold as private-key material, whether there are any).

    Fail closed: a span runs from a BEGIN line to its END line (or to the end of the file) and every key-shaped line in it
    is withheld: the markers, PEM headers, blank lines, and lines whose text, unwrapped, is base64 with "=" only as
    padding (or nearly, see `_key_shaped`). The span ends at the first line that isn't key-shaped, so ordinary code after a decoy marker stays visible.
    No attempt is made to tell a decoy from a key. A decoy is withheld too, and the caller reports every file that has
    withheld lines, so a person looks at it and nothing is hidden silently.
    """
    marked, index = set(), 0
    while index < len(lines):
        begin = _KEY_BEGIN.search(lines[index])
        index += 1
        if begin is None:
            continue
        marked.add(index - 1)
        if _KEY_END.search(lines[index - 1], begin.end()):
            continue
        while index < len(lines) and not _KEY_BEGIN.search(lines[index]):
            if _KEY_END.search(lines[index]):
                marked.add(index)
                index += 1
                break
            if not _key_shaped(lines[index]):
                break
            marked.add(index)
            index += 1
    return marked, bool(marked)


def has_key_marker(text: str) -> bool:
    """True when the text holds a private-key BEGIN or END line, however it is wrapped."""
    return bool(_KEY_BEGIN.search(text) or _KEY_END.search(text))
