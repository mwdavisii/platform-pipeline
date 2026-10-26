"""The review agent's only tools: list_files, read_file, and grep. Read-only, with nothing to execute (SEC-1).

The agent sees the files the injection pre-scan covers (builder.source.Tree over the checkout): no `.git`,
vendored or builder-owned directories, symlinks, binary files, or files over 1 MB. A path with a component named `.git`
(a worktree or submodule pointer) is left out too, and so is any path that is itself text aimed at the reviewer, holds
control, invisible or separator characters, or looks like a secret. Paths are looked up in that list, never joined onto
the filesystem, so no argument can reach outside it.

Withholding is a tripwire, not a defense, like the pre-scan it shares its patterns with. Every line shown passes `shown`:
a line the pre-scan would report as text aimed at the reviewer, or one with invisible characters, is withheld (a line
split by \\x0c, \\x1c to \\x1e, \\x85 or a Unicode separator is withheld as a whole, with the numbering `anchor_ok` uses),
and secret-shaped strings are redacted, private-key blocks across every line they span. A directive split across ordinary
lines, or phrased to dodge the patterns, can still reach the model; rule 1 of the prompt, the re-verification of every
citation, and the fixed output schema are what hold then. grep takes fixed text only, never a regular expression.
"""

import json

from builder.source import Tree

from ..redact import private_key_lines, redact
from ..signals.changes import path_matches
from ..signals.injection import has_invisible, is_directive, join_separators, logical_lines, path_flaw

KEY_REDACTED = "[redacted: private-key]"

WITHHELD = "[line withheld: text aimed at the reviewer or invisible characters; reported as VC-INJ]"
MAX_ARGUMENTS_CHARS = 10_000
MAX_GLOB_CHARS = 200
MAX_GLOB_STARS = 10
MAX_TEXT_CHARS = 200
MAX_LINE_CHARS = 1000
MAX_GREP_LINE_CHARS = 300
NAMES = ("list_files", "read_file", "grep")

SPECS = [
    {"type": "function", "function": {
        "name": "list_files",
        "description": "List the repository's files whose path matches a glob. A glob with a '/' matches the whole path; one without matches the file name. '*' lists everything.",
        "parameters": {"type": "object", "additionalProperties": False, "required": ["glob"],
                       "properties": {"glob": {"type": "string", "maxLength": MAX_GLOB_CHARS}}}}},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read lines of one repository file. Each line is shown as '<number>: <text>'. Cite the text without the prefix.",
        "parameters": {"type": "object", "additionalProperties": False, "required": ["path"],
                       "properties": {"path": {"type": "string"}, "start_line": {"type": "integer", "minimum": 1},
                                      "end_line": {"type": "integer", "minimum": 1}}}}},
    {"type": "function", "function": {
        "name": "grep",
        "description": "Find lines containing fixed text (not a regular expression; case is ignored) in the files matching a glob.",
        "parameters": {"type": "object", "additionalProperties": False, "required": ["text"],
                       "properties": {"text": {"type": "string", "maxLength": MAX_TEXT_CHARS},
                                      "glob": {"type": "string", "maxLength": MAX_GLOB_CHARS}}}}},
]


class ToolError(Exception):
    """A bad tool call. The message goes back to the model; it never contains file text."""


class _AnySuffix:
    def __contains__(self, item) -> bool:
        return True


def shown(line: str, patterns) -> str:
    """A line as the agent may see it."""
    if is_directive(line, patterns) or has_invisible(line):
        return WITHHELD
    return redact(line)


def shown_lines(text: str, patterns, *, diff: bool = False) -> list[str]:
    """The lines of a file as the agent may see them, numbered as `str.splitlines` numbers them (what `anchor_ok` checks).
    With `diff`, the text is a unified diff: the one-character prefix (" ", "+", "-") is ignored when looking for private-key blocks."""
    pieces = text.splitlines(keepends=True)
    if diff:  # a bare \r (or other separator) inside a diff line starts a new piece; it keeps the line's prefix, so no piece reads as a header
        prefix, carried = " ", False
        for n, piece in enumerate(pieces):
            if carried:
                pieces[n] = prefix + piece
            else:
                prefix = piece[:1] if piece[:1] in (" ", "+", "-") else " "
            carried = not piece.endswith("\n")
    texts = [(piece.splitlines() or [""])[0] for piece in pieces]
    keys, _ = private_key_lines([t[1:] if diff and t[:1] in (" ", "+", "-") else t for t in texts])
    out = [KEY_REDACTED] * len(pieces)
    for first, logical, count in logical_lines(text):
        group = range(first - 1, min(first - 1 + count, len(pieces)))
        joined = join_separators(logical)  # a separator can stand in for a space, so check and redact the text as a reader sees it
        withheld = shown(joined, patterns) == WITHHELD
        split_secret = count > 1 and redact(joined) != joined and all(redact(texts[i]) == texts[i] for i in group)
        for i in group:
            if i in keys:
                continue
            out[i] = WITHHELD if withheld else ("[redacted: secret split across separators]" if split_secret else shown(texts[i], patterns))
    return out


def listing(tree: Tree) -> dict:
    """{relative path: file} for the files the agent can see. `scorecard run` uses the same list to decide which agent citations
    can be verified (M7), so a citation into a file the tools never showed is unverified."""
    files = {}
    for p in tree.files(_AnySuffix()):
        rel = p.relative_to(tree.root).as_posix()
        if p.is_symlink() or ".git" in (part.casefold() for part in rel.split("/")):
            continue
        if path_flaw(rel) is None:  # file names are text the model reads
            files[rel] = p
    return files


def citable_paths(root) -> set[str]:
    return set(listing(Tree(root)))


class RepoTools:
    submit_name = "submit_assessment"  # the review's submit tool, named in the unknown-tool message

    def __init__(self, root, config: dict):
        self.tree = Tree(root)
        self.limits = config["agent"]["tools"]
        self.patterns = config["patterns"]["injection"]
        self._files = listing(self.tree)
        self._lines: dict[str, list[str]] = {}

    @property
    def paths(self) -> list[str]:
        return sorted(self._files)

    def raw_text(self, path: str) -> str | None:
        """A listed file's text as stored (for checks that must see it before it is withheld or redacted), or None."""
        found = self._files.get(path)
        return None if found is None else self.tree.read_text(found)

    def lines(self, path: str) -> list[str]:
        if path not in self._files:
            raise ToolError(f"no readable file {path[:200]!r}; use list_files to see the files")
        if path not in self._lines:
            text = self.tree.read_text(self._files[path]) or ""
            self._lines[path] = shown_lines(text.removeprefix("\ufeff"), self.patterns)
        return self._lines[path]

    def _glob(self, glob) -> str:
        if not isinstance(glob, str) or not glob or len(glob) > MAX_GLOB_CHARS or glob.count("*") > MAX_GLOB_STARS or "\x00" in glob:
            raise ToolError(f"glob must be 1 to {MAX_GLOB_CHARS} characters with at most {MAX_GLOB_STARS} '*'")
        return glob

    def _cap(self, out: list[str]) -> str:
        text, limit = "\n".join(out), self.limits["max_output_chars"]
        return text if len(text) <= limit else text[:limit] + "\n[output truncated]"

    def list_files(self, glob: str) -> str:
        pattern = self._glob(glob)
        found = [p for p in self.paths if path_matches(p, [pattern])]
        limit = self.limits["max_list_files"]
        out = [f"{len(found)} files match {glob!r}" + (f"; showing the first {limit}" if len(found) > limit else "")]
        return self._cap(out + found[:limit])

    def read_file(self, path: str, start_line: int = 1, end_line: int | None = None) -> str:
        lines = self.lines(path)
        if not lines:
            return f"{path} is empty"
        last = len(lines) if end_line is None else min(end_line, len(lines))
        if start_line > len(lines) or last < start_line:
            raise ToolError(f"{path} has {len(lines)} lines")
        last = min(last, start_line + self.limits["max_read_lines"] - 1)
        out = [f"{path} lines {start_line}-{last} of {len(lines)}"]
        for number in range(start_line, last + 1):
            line = lines[number - 1]
            out.append(f"{number}: {line[:MAX_LINE_CHARS]}" + (" [line truncated]" if len(line) > MAX_LINE_CHARS else ""))
        return self._cap(out)

    def grep(self, text: str, glob: str = "*") -> str:
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS:
            raise ToolError(f"text must be 1 to {MAX_TEXT_CHARS} characters")
        needle, limit, pattern = text.casefold(), self.limits["max_grep_matches"], self._glob(glob)
        hits, more = [], False
        for path in (p for p in self.paths if path_matches(p, [pattern])):
            for number, line in enumerate(self.lines(path), 1):
                if line != WITHHELD and needle in line.casefold():
                    if len(hits) == limit:
                        more = True
                        break
                    hits.append(f"{path}:{number}: {line[:MAX_GREP_LINE_CHARS]}" + (" [line truncated]" if len(line) > MAX_GREP_LINE_CHARS else ""))
            if more:
                break
        head = f"{len(hits)} matching lines" + (f" (stopped at {limit})" if more else "")
        return self._cap([head, *hits])

    def call(self, name: str, arguments) -> str:
        """Run one tool call from the model. Never raises: a bad call returns an error message for the model."""
        try:
            if name not in NAMES:
                raise ToolError(f"unknown tool {str(name)[:60]!r}; the tools are {', '.join(NAMES)} and {self.submit_name}")
            args = _arguments(arguments)
            if name == "list_files":
                return self.list_files(_only(args, {"glob": str}, required={"glob"})["glob"])
            if name == "read_file":
                a = _only(args, {"path": str, "start_line": int, "end_line": int}, required={"path"})
                if a.get("start_line", 1) < 1 or a.get("end_line", 1) < 1:
                    raise ToolError("line numbers start at 1")
                return self.read_file(a["path"], a.get("start_line", 1), a.get("end_line"))
            a = _only(args, {"text": str, "glob": str}, required={"text"})
            return self.grep(a["text"], a.get("glob", "*"))
        except ToolError as exc:
            return f"error: {exc}"


def _arguments(arguments) -> dict:
    if not isinstance(arguments, str) or len(arguments) > MAX_ARGUMENTS_CHARS:
        raise ToolError(f"arguments must be a JSON object of at most {MAX_ARGUMENTS_CHARS} characters")
    try:
        args = json.loads(arguments or "{}")
    except (ValueError, RecursionError):
        raise ToolError("arguments are not valid JSON") from None
    if not isinstance(args, dict):
        raise ToolError("arguments must be a JSON object")
    return args


def _only(args: dict, types: dict, *, required: set) -> dict:
    unknown = set(args) - set(types)
    if unknown:
        raise ToolError(f"unknown arguments: {', '.join(sorted(str(k)[:40] for k in unknown))}")
    missing = required - set(args)
    if missing:
        raise ToolError(f"missing argument {sorted(missing)[0]!r}")
    for key, value in args.items():
        if type(value) is not types[key]:  # bool is not an int here
            raise ToolError(f"argument {key!r} must be a {types[key].__name__}")
    return args
