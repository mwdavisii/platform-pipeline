"""golden-road register: build a registration draft and print the portal link that carries it in the URL fragment (spec P20).
The fragment never reaches the portal's server: the page fills its form from it, and the person signs in, checks it and
submits it. This command reads local files and the platform config. It sends nothing anywhere."""

import base64
import json
import re
from pathlib import Path

from builder.config import load_platform
from contracts.schema import validate

from .io import InputError

MAX_FRAGMENT = 16384  # register.js refuses a longer fragment
MAX_PURPOSE = 4000
PORTAL = re.compile(r"https://[A-Za-z0-9.-]+(:[0-9]{1,5})?/?")
CLASSIFICATIONS = ("public", "internal", "restricted", "phi")
TIERS = ("low", "moderate", "high")


class NoPortal(Exception):
    error = "the installed builder's platform config names no golden road portal"
    fix = "Ask the platform team to register the repo; until the portal is set up, security adds the intake record by pull request."


def portal_url(platform: dict | None = None) -> str:
    """The portal's address from the platform config the installed builder carries, never from an argument or the app's files."""
    url = (load_platform() if platform is None else platform).get("portal_url")
    if not isinstance(url, str) or not PORTAL.fullmatch(url):
        raise NoPortal()
    return url.rstrip("/")


def spec_classification(root: Path) -> str | None:
    try:
        value = json.loads((root / "builder-spec.json").read_text(encoding="utf-8")).get("data_classification")
    except (OSError, ValueError, AttributeError):
        return None
    return value if value in CLASSIFICATIONS else None


def read_purpose(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise InputError(f"cannot read {path}: {type(exc).__name__}") from exc
    text = text.replace("\r\n", "\n").strip()
    if len(text) > MAX_PURPOSE:
        raise InputError(f"the purpose is longer than {MAX_PURPOSE} characters; shorten it")
    return text or None


def draft(repo, purpose, classification, tier) -> dict:
    doc = {"schema": "registration-draft/v1", "repo": repo}
    for key, value in (("purpose", purpose), ("data_classification", classification), ("proposed_tier", tier)):
        if value:
            doc[key] = value
    errors = validate("registration-draft.v1", doc)
    if errors:
        raise InputError(f"the draft is not valid: {errors[0]}")
    return doc


def link(base: str, doc: dict) -> str:
    raw = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    fragment = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    if len(fragment) > MAX_FRAGMENT:
        raise InputError(f"the draft is too long for a link ({len(fragment)} characters, at most {MAX_FRAGMENT}); shorten the purpose")
    return f"{base}/register#{fragment}"
