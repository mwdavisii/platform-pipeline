"""The golden-road blocks in AGENTS.md and CLAUDE.md: the platform rules every coding agent reads in an app repo.

Only the text from the begin marker line to the end marker line is the builder's. Text outside the markers belongs to
the team: generate keeps it and verify never reads it.

A marker is a line that holds only the marker (surrounding whitespace ignored), wherever it sits: a marker alone on a
line counts even inside a code fence, so a team that documents the markers must put them in a line with other text.
Lines end at "\n" only (a "\r\n" file works), and one leading byte order mark is ignored and kept at the front.
"""

import re
from typing import NamedTuple

from ..lock import NEVER_EDIT

BEGIN = "<!-- golden-road:begin -->"
END = "<!-- golden-road:end -->"
MANAGED_FILES = ("AGENTS.md", "CLAUDE.md")
# Without the platform config's links (configure.py's --landing-page-url and --approved-models-url), these placeholders stay.
# Changing a link changes the block, so every app regenerates.
SKILL_SOURCE = "the golden-road landing page (the platform team has the link)"
APPROVED_MODELS = "The list of models approved for `{data}` data is not published yet: ask the platform team before you add a model."
LINK = re.compile(r"https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~%/?&=+#-]*)?")


BOM = "\ufeff"


def _lines(text: str) -> list[str]:
    """The lines of text with their "\n" endings. Unlike str.splitlines, only "\n" ends a line."""
    parts = text.split("\n")
    return [part + "\n" for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def _marker_lines(text: str) -> list[str]:
    lines = [line.strip() for line in _lines(text)]
    if lines:
        lines[0] = lines[0].lstrip(BOM).strip()
    return lines


class Block(NamedTuple):
    """Where the markers are. state is ok, missing, duplicated or unmatched. For ok, first and last are the 0-based
    indexes of the begin and end marker lines; otherwise both are the line to report."""

    state: str
    first: int
    last: int


def _link(platform: dict | None, key: str) -> str | None:
    value = (platform or {}).get(key)
    return value if isinstance(value, str) and LINK.fullmatch(value) else None


def render_blocks(spec: dict, platform: dict | None = None) -> dict[str, str]:
    data = spec["data_classification"]
    landing, models = _link(platform, "landing_page_url"), _link(platform, "approved_models_url")
    approved = f"Use only the models approved for `{data}` data: <{models}>." if models else APPROVED_MODELS.format(data=data)
    source = f"the golden-road landing page: <{landing}>" if landing else SKILL_SOURCE
    never_edit = ", ".join(f"`{rel}`" for rel in NEVER_EDIT)
    agents = [
        BEGIN,
        "## Golden road",
        "",
        "`builder generate` writes this block. Don't edit it: put your own notes outside the golden-road markers.",
        "",
        f"- Don't edit the generated files ({never_edit}). Change `builder-spec.json` and run `builder generate .` instead.",
        "- Call models only through the platform gateway, never a model provider directly. `builder guide` names the gateway host.",
        "- Never make this repository public.",
        "- Ask the user about data classification, roles and egress. Never decide them yourself.",
        f"- This app's data classification is `{data}`. " + approved,
        f"- To package or update this app, use the golden-road skill. Install it from {source}.",
        END,
    ]
    return {"AGENTS.md": "\n".join(agents) + "\n", "CLAUDE.md": f"{BEGIN}\n@AGENTS.md\n{END}\n"}


def find_block(text: str) -> Block:
    """A marker is a whole line (surrounding whitespace ignored), so a marker quoted in the team's prose is not one."""
    lines = _marker_lines(text)
    begins = [i for i, line in enumerate(lines) if line == BEGIN]
    ends = [i for i, line in enumerate(lines) if line == END]
    if not begins and not ends:
        return Block("missing", 0, 0)
    if len(begins) > 1 or len(ends) > 1:
        extra = min(begins[1:] + ends[1:])
        return Block("duplicated", extra, extra)
    if len(begins) != 1 or len(ends) != 1 or ends[0] < begins[0]:
        first = min(begins + ends)
        return Block("unmatched", first, first)
    return Block("ok", begins[0], ends[0])


def marker_at(text: str, index: int) -> str:
    """The stripped text of line index, split the way find_block splits it (so a BOM or a stray \\x0c never leaks in)."""
    return _marker_lines(text)[index]


def block_text(text: str, block: Block) -> str:
    body = text[1:] if text.startswith(BOM) else text
    return "".join(_lines(body)[block.first:block.last + 1])


def merge(existing: str | None, block: str) -> str:
    """The file's new text: the block alone for a new or blank file, the block on top of a file without markers, or the
    file with its block replaced. Raises ValueError for duplicated or unmatched markers: which text is the team's is unknown."""
    if existing is None or not existing.strip(BOM + " \t\r\n"):
        return block
    bom = BOM if existing.startswith(BOM) else ""
    body = existing[len(bom):]
    found = find_block(body)
    if found.state == "missing":
        return bom + block + "\n" + body
    if found.state != "ok":
        raise ValueError(found.state)
    lines = _lines(body)
    return bom + "".join(lines[:found.first]) + block + "".join(lines[found.last + 1:])
