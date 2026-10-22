"""The vendored OWASP ASVS 5.0.0 requirement list.

`standards/asvs-5.0.0.csv` is the official English CSV export, byte for byte, from
github.com/OWASP/ASVS at tag v5.0.0_release (commit 5cf9b032440be53ce345ab3c130fda46ba1ce7a2),
path 5.0/docs_en/OWASP_Application_Security_Verification_Standard_5.0.0_en.csv, under the OWASP
Foundation's CC BY-SA 4.0 license. Requirement text is only ever read from it, never written
in code. The CSV uses native IDs such as `V8.2.2`; everything the scorecard emits uses the
versioned form the ASVS recommends, `v5.0.0-8.2.2`.
"""

import csv
import hashlib
import io
import re
from importlib import resources

from golden_road.io import InputError

ASVS_FILE = "asvs-5.0.0.csv"
ASVS_SHA256 = "98c8fe911b9edb403af8ee05d3ce8201ecac2659e313b053890a62847cdcf680"
COLUMNS = ["chapter_id", "chapter_name", "section_id", "section_name", "req_id", "req_description", "L"]
PREFIX = "v5.0.0-"
_NATIVE = re.compile(r"^V([1-9][0-9]*)(\.[1-9][0-9]*)?(\.[1-9][0-9]*)?$")


def versioned(native: str) -> str:
    """`V8.2.2` -> `v5.0.0-8.2.2` (also chapters `V8` and sections `V8.2`)."""
    if not isinstance(native, str) or not _NATIVE.match(native):
        raise InputError(f"not an ASVS 5.0 ID: {native!r}")
    return PREFIX + native[1:]


def load_asvs() -> list[dict]:
    data = resources.files("scorecard").joinpath("standards", ASVS_FILE).read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    if actual != ASVS_SHA256:
        raise InputError(f"{ASVS_FILE} has SHA-256 {actual}, expected {ASVS_SHA256}; re-vendor the official export")
    return parse_asvs(data.decode("utf-8"))


def parse_asvs(text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if reader.fieldnames != COLUMNS:
        raise InputError(f"ASVS CSV columns are {reader.fieldnames}, expected {COLUMNS}")
    requirements, seen = [], set()
    for row in reader:
        chapter, section, req = row["chapter_id"], row["section_id"], row["req_id"]
        consistent = (_NATIVE.match(chapter) and _NATIVE.match(section) and _NATIVE.match(req)
                      and section.startswith(chapter + ".") and req.startswith(section + ".") and req.count(".") == 2)
        if not consistent or row["L"] not in ("1", "2", "3") or not row["req_description"].strip() or req in seen:
            raise InputError(f"ASVS CSV row for {req!r} is malformed")
        seen.add(req)
        requirements.append({
            "id": versioned(req), "chapter": versioned(chapter), "chapter_name": row["chapter_name"],
            "section": versioned(section), "section_name": row["section_name"], "level": int(row["L"]),
            "text": row["req_description"],
        })
    if not requirements:
        raise InputError("ASVS CSV has no requirements")
    return requirements


def for_level(requirements: list[dict], level: int) -> list[dict]:
    """L1 is a subset of L2, which is a subset of L3: a requirement applies at its level and above."""
    return [r for r in requirements if r["level"] <= level]


def matches(asvs_id: str, selector: str) -> bool:
    """A selector names a chapter (`v5.0.0-8`), a section (`v5.0.0-8.2`), or one requirement."""
    return asvs_id == selector or asvs_id.startswith(selector + ".")


def in_any(asvs_id: str, selectors) -> bool:
    return any(matches(asvs_id, s) for s in selectors)


def check_ids(requirements: list[dict], config: dict, confirmed_na: list[dict]) -> None:
    """Every ASVS ID in the factory config and na/ file must exist, so a typo can't quietly drop a blocking chapter."""
    ids = [r["id"] for r in requirements]
    named = [("blocking", s) for s in config["blocking"]]
    named += [("platform_provided", e["id"]) for e in config["platform_provided"]]
    named += [("tool_covered", e["id"]) for e in config["tool_covered"]]
    named += [("na_rules", e["selector"]) for e in config["na_rules"]]
    named += [("na", e["asvs_id"]) for e in confirmed_na]
    for where, selector in named:
        if not any(matches(i, selector) for i in ids):
            raise InputError(f"{where} names {selector}, which is not in ASVS 5.0.0")


def chapter_names(requirements: list[dict]) -> dict[str, str]:
    return {r["chapter"]: r["chapter_name"] for r in requirements}
