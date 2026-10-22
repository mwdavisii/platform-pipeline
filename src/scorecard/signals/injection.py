"""T-7 (VC-INJ) pre-scan: text in the repo or the PR aimed at the reviewer.

A tripwire, not a defense: it is easy to evade (look-alike letters from other scripts are not folded). Hits become
findings, and PR prose is only pre-scanned; none of it is ever placed in a prompt. A finding never carries the
matched text, which may hold invisible or bidirectional characters, so its evidence is a fixed description.
"""

import hashlib
import re
import unicodedata

from builder.source import Tree, describe_skip

from ..redact import private_key_lines, redact
from . import signal

# Text files that are always scanned, and whose problems (binary, oversize) are always reported. Any other file Tree
# can read is scanned too, because the reviewer's tools can open it; unreadable ones are reported unless they are
# merely binary or large.
TEXT_SUFFIXES = {"", ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".md", ".mdx", ".txt", ".rst", ".json", ".jsonc",
                 ".ipynb", ".yaml", ".yml", ".toml", ".cfg", ".ini", ".html", ".htm", ".css", ".svg", ".xml", ".vue",
                 ".svelte", ".sh", ".env"}
# Files that are expected to be binary; any other file with a NUL byte or invalid UTF-8 is reported.
BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".icns", ".avif", ".tif", ".tiff", ".pdf",
                   ".woff", ".woff2", ".ttf", ".otf", ".eot", ".zip", ".gz", ".tgz", ".tar", ".bz2", ".xz", ".7z", ".jar",
                   ".whl", ".mp3", ".mp4", ".wav", ".mov", ".webm", ".wasm", ".so", ".dll", ".dylib", ".exe", ".pyc"}
CODE_SUFFIXES = (".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".sh", ".vue", ".svelte")
# Category Cf plus the format-like characters Unicode files under other categories.
_EXTRA_INVISIBLE = {0x034F, 0x061C, 0x115F, 0x1160, 0x180E, 0x3164, 0xFFA0}
_INVISIBLE_RANGES = ((0xE0000, 0xE007F), (0xFE00, 0xFE0F), (0xE0100, 0xE01EF))
_COMMENT = re.compile(r"^\s*(#|//|/\*|\*|<!--)")
MAX_PER_FILE = 5
MAX_FINDINGS = 50
FIX = "Remove text addressed to reviewers or tools; it does not change how the change is reviewed."


class _AnySuffix:
    def __contains__(self, item) -> bool:
        return True


def invisible(char: str) -> bool:
    code = ord(char)
    if code < 0x80:
        return False
    return (unicodedata.category(char) == "Cf" or code in _EXTRA_INVISIBLE
            or any(low <= code <= high for low, high in _INVISIBLE_RANGES))


def has_invisible(text: str) -> bool:
    return not text.isascii() and any(invisible(c) for c in text)


def _fold(line: str) -> str:
    """The line as a reader sees it: invisible characters and combining marks removed, compatibility forms (fullwidth, ligatures) folded."""
    kept = "".join(c for c in line if not invisible(c) and unicodedata.category(c) != "Mn")  # Mn: combining strikes and accents
    return unicodedata.normalize("NFKC", kept)


def is_directive(line: str, patterns) -> bool:
    candidates = {line} if line.isascii() else {line, _fold(line)}
    return any(re.search(p, text, re.I) for p in patterns for text in candidates)


_SEPARATORS = re.compile("[\x0b\x0c\x1c-\x1e\x85\u2028\u2029]")


def join_separators(logical: str) -> str:
    """A logical line with the characters `splitlines` breaks on (but a reader sees as spaces) turned into spaces."""
    return _SEPARATORS.sub(" ", logical)


def logical_lines(text: str) -> list[tuple[int, str, int]]:
    """(first line number, text, number of lines) for each line ended by \\n, \\r\\n or \\r. `str.splitlines` also splits on \\x0c, \\x1c to
    \\x1e, \\x85 and the Unicode separators, which a reader's editor may not show as breaks; line numbers follow splitlines
    (the numbering `anchor_ok` uses), so one logical line can span several numbers."""
    out, group, first, number = [], [], 1, 0
    for piece in text.splitlines(keepends=True):
        number += 1
        group.append(piece)
        if piece.endswith(("\n", "\r")):
            out.append((first, "".join(group).rstrip("\r\n"), len(group)))
            group, first = [], number + 1
    if group:
        out.append((first, "".join(group), len(group)))
    return out


# File names are checked against imperative override phrases only. The content patterns (greetings, roles, "pre-approved",
# "note to reviewer", "as a reviewer") match ordinary names such as HeyAssistant.tsx or preapproved_orders.py. "rules" is
# left out of the first pattern on purpose, so ignore_previous_rules_migration.sql stays unflagged.
NAME_PATTERNS = [
    r"ignore (all |any )?(the )?(previous|prior|above|earlier) (instructions|prompts?)",
    r"disregard (all |any |the )*(previous |prior |above |earlier )?instructions",
    r"mark (all|every) (requirements? )?as (pass|passed)",
]


def path_flaw(rel: str) -> str | None:
    """Why a file path can't be shown to the reviewer's model, or None. A path is text the model reads (file lists, grep
    prefixes), so it is checked for control, format, and separator characters, imperative override phrases (however the
    words are joined or cased), and secret shapes. Never raises."""
    if any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") or c == "\x85" for c in rel) or has_invisible(rel):
        return "invisible-unicode"
    words = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", re.sub(r"(?<=[a-z])(?=[A-Z])", " ", _fold(rel)))  # "IgnorePrevious", "IGNOREPrevious"
    spaced = re.sub(r"[^A-Za-z]+", " ", words)  # "a~b", "a+b", "a1b"
    if is_directive(rel, NAME_PATTERNS) or is_directive(spaced, NAME_PATTERNS):
        return "reviewer-directive"
    if redact(rel) != rel:
        return "secret"
    return None


def _tag(rel: str) -> str:
    return hashlib.sha256(rel.encode("utf-8", "surrogateescape")).hexdigest()[:12]


KEY_DETAIL = "private-key material was withheld from the reviewer; a person should check this file"


def add_key_problem(problems: list, rel: str) -> None:
    """Record, once per path, that private-key material is withheld from the reviewer (T-14). The source is the path, or a
    path hash when the name itself can't be shown; no file text is ever in it."""
    name = rel if path_flaw(rel) is None else f"file name (sha256 {_tag(rel)})"
    problem = {"source": name, "detail": KEY_DETAIL}
    if problem not in problems:
        problems.append(problem)


def _path_hits(rel: str, now: str, problems: list | None) -> list[dict]:
    """A file name that is text aimed at the reviewer (or hides characters) is a VC-INJ finding, and one shaped like a secret is
    an unscannable input (T-14). The name itself is never repeated: it is identified by a short hash."""
    flaw = path_flaw(rel)
    if flaw is None:
        return []
    tag = _tag(rel)
    if flaw == "secret":
        if problems is not None:
            problems.append({"source": f"file name (sha256 {tag})",
                             "detail": "a file name that looks like a secret; it is withheld from the reviewer and is not repeated here"})
        return []
    what = "reviewer-directed text" if flaw == "reviewer-directive" else "invisible or control characters"
    return [signal(rule_id=f"VC-INJ/{flaw}-in-name", category="injection", severity="high", file="<unknown>", snippet=f"<{what} in a file name, sha256 {tag}>",
                   title="Text addressed to the reviewer in a file name" if flaw == "reviewer-directive" else "Invisible characters in a file name",
                   rationale="A file name is text the reviewer reads. It is treated as data and reported, never followed, and not repeated here; "
                             f"find it with the SHA-256 prefix {tag} of the path.",
                   fix=FIX, now=now, vc="VC-INJ", cwe=False)]


def _file_hits(rel: str, text: str, config: dict, now: str) -> list[dict]:
    patterns, limit = config["patterns"]["injection"], config["max_comment_block_lines"]
    code = rel.lower().endswith(CODE_SUFFIXES)
    out, block_start, block_len = [], None, 0

    def add(rule, line, what, title):
        out.append(signal(rule_id=f"VC-INJ/{rule}", category="injection", severity="high", file=rel, line=line,
                          snippet=f"<{what} at line {line}>", title=title,
                          rationale="Text aimed at an AI reviewer is treated as data and reported, never followed. "
                                    "The text is not repeated here; open the file at the cited line.",
                          fix=FIX, now=now, vc="VC-INJ", cwe=False))

    for number, line in enumerate(text.splitlines(), 1):
        if number == 1:
            line = line.removeprefix("\ufeff")
        if is_directive(line, patterns):
            add("reviewer-directive", number, "reviewer-directed text", "Text addressed to the reviewer")
        elif has_invisible(line):
            add("invisible-unicode", number, "invisible characters", "Invisible or bidirectional Unicode")
        if code and _COMMENT.match(line):
            block_start, block_len = (block_start or number), block_len + 1
            if block_len == limit + 1:
                add("oversized-comment", block_start, f"comment block over {limit} lines", f"Comment block longer than {limit} lines")
        else:
            block_start, block_len = None, 0
    for first, logical, count in logical_lines(text):
        if first == 1:
            logical = logical.removeprefix("\ufeff")
        pieces = logical.splitlines()
        if count > 1 and is_directive(join_separators(logical), patterns) and not any(is_directive(p, patterns) for p in pieces):
            add("reviewer-directive", first, "reviewer-directed text", "Text addressed to the reviewer")
    return out[:MAX_PER_FILE]


def _reportable(rel: str, reason: str, suffix: str | None) -> bool:
    if suffix is None or suffix.lower() in TEXT_SUFFIXES:
        return True
    if reason == "binary":
        return suffix.lower() not in BINARY_SUFFIXES  # a NUL byte can hide text from a scan
    return reason != "oversize"


def _undecodable(path) -> bool:
    try:
        path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return True
    except OSError:
        return False
    return False


def tree_signals(root, config: dict, *, now: str, problems: list | None = None) -> list[dict]:
    """VC-INJ findings over every text file in the head tree. A file that can't be read is appended to `problems`
    (when given), so the level engine fires T-14: an unscanned file is never treated as a clean one."""
    tree = Tree(root)
    out = []
    for path in tree.files(_AnySuffix()):
        text = tree.read_text(path)
        rel = path.relative_to(tree.root).as_posix()
        if text is None:
            tree.problems[rel] = ("unreadable", path.suffix)
            continue
        capped = len(out) >= MAX_FINDINGS  # past the cap no more findings are added, but every problem below is still recorded
        name_hits = _path_hits(rel, now, problems)  # records a secret-shaped name as a problem even past the cap
        if not capped:
            out += name_hits
        if problems is not None and private_key_lines(text.splitlines())[1]:
            add_key_problem(problems, rel)
        if not capped:
            out += _file_hits(rel, text, config, now)
        if "\ufffd" in text and path.suffix.lower() not in BINARY_SUFFIXES and _undecodable(path):
            tree.problems[rel] = ("isn't valid UTF-8", path.suffix or None)
        out = out[:MAX_FINDINGS]
    if problems is not None:
        problems += [{"source": rel, "detail": describe_skip(rel, reason)[:300]}
                     for rel, (reason, suffix) in sorted(tree.problems.items()) if _reportable(rel, reason, suffix)]
    return out


def text_hits(source: str, text: str, config: dict) -> list[dict]:
    """Hits in PR prose or commit messages. Only the source and the rule are kept, never the text."""
    hits = []
    if any(is_directive(line, config["patterns"]["injection"]) for line in text.splitlines()):
        hits.append({"source": source, "rule": "VC-INJ/reviewer-directive"})
    if has_invisible(text):
        hits.append({"source": source, "rule": "VC-INJ/invisible-unicode"})
    return hits
