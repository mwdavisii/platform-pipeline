"""Signals read from the changed paths and the lines the change adds.

VC-1 (T-3): test files deleted, test functions removed, skip markers added.
T-15: scanner suppression files added or widened, inline suppression markers added.
T-16: workflow, CODEOWNERS, and .gitattributes changes, harness-shadowing files, `_platform` imports outside generated files.
T-9: a PURPOSE.md added or changed (the purpose itself comes only from the factory intake).
"""

import json
import re
from collections import Counter
from fnmatch import fnmatchcase

from .. import git
from ..redact import private_key_lines
from . import signal
from .injection import add_key_problem

CODE_SUFFIXES = (".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")


def path_matches(path: str, globs) -> bool:
    """A glob with a `/` matches the whole path; one without matches the file name. Case is ignored."""
    low = path.lower()
    name = low.rsplit("/", 1)[-1]
    return any(fnmatchcase(low if "/" in g else name, g.lower()) for g in globs)


def _hit(patterns, text: str):
    return next((m for p in patterns if (m := re.search(p, text, re.I))), None)


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _test_names(text: str | None, patterns) -> Counter:
    """Qualified test names (`Class.method`, `describe path > test`), plus the containers themselves.

    A container line (a `class Test*`, a `describe(...)`) qualifies the tests indented deeper than it. Removing or
    renaming a container, or moving a test out of it, therefore reads as removed tests.
    """
    names, stack = Counter(), []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        indent = _indent(line)
        while stack and stack[-1][0] >= indent:
            stack.pop()
        prefix = ".".join(name for _, name in stack)
        for p in patterns["test_container"]:
            m = re.search(p, line, re.I)
            if m:
                name = m.group(m.lastindex) if m.lastindex else m.group(0)
                names[f"{prefix}.{name}" if prefix else name] += 1
                stack.append((indent, name))
                break
        for p in patterns["test_function"]:
            for m in re.finditer(p, line, re.I):
                name = m.group(m.lastindex) if m.lastindex else m.group(0)
                names[f"{prefix}.{name}" if prefix else name] += 1
    return names


def _test_settings(name: str, text: str | None, patterns) -> object:
    """The part of a test-configuration file that holds test settings, so unrelated edits to the file stay quiet."""
    if text is None:
        return None
    if name == "pyproject.toml":
        kept, inside = [], False
        for line in text.splitlines():
            if line.lstrip().startswith("["):
                inside = _hit(patterns["test_config_sections"], line) is not None
            if inside:
                kept.append(line.strip())
        return kept
    if name == "package.json":
        try:
            doc = json.loads(text)
        except (ValueError, RecursionError):
            return object()  # unparsable: never equal to anything, so the change fires
        if not isinstance(doc, dict):
            return object()
        scripts = doc.get("scripts") if isinstance(doc.get("scripts"), dict) else {}
        return {"scripts": {k: v for k, v in scripts.items() if k.lower().startswith("test")},
                **{k: doc[k] for k in ("jest", "mocha", "vitest") if k in doc}}
    return text


def change_signals(repo, base: str, head: str, changed: list[dict], config: dict, *, now: str,
                   problems: list | None = None) -> list[dict]:
    """Findings for the change. A file that exists but can't be read (too large) is a VC-1 finding, and also a problem
    appended to `problems` (when given) so the level engine fires T-14. It is never skipped quietly."""
    paths, patterns = config["paths"], config["patterns"]
    out = []

    def add(**kw):
        out.append(signal(now=now, **kw))

    def read(sha, path):
        try:
            return git.show(repo, sha, path), True
        except git.Unreadable as exc:
            add(rule_id="VC-1/test-file-unreadable", category="test-integrity", severity="high", file=path, vc="VC-1",
                snippet=f"can't compare test files: {exc}", title="Test file can't be compared",
                rationale="A file this large can't be checked for removed tests or changed test settings.",
                fix="Split the file, or have a reviewer compare it by hand.")
            if problems is not None:
                problems.append({"source": path, "detail": f"can't read the file to compare it: {exc}"[:300]})
            return None, False

    def withheld_key(sha, path) -> bool:
        try:
            text = git.show(repo, sha, path)
        except git.Unreadable:
            return False  # too large or not a file: the pre-scan and the VC-1 reads report those
        return text is not None and private_key_lines(text.splitlines())[1]

    for change in changed:
        path, status = change["path"], change["status"]
        # A key that is added, changed or deleted by the change is withheld from the reviewer's diff and tools; a person must look (T-14).
        if problems is not None and (withheld_key(base, path) or (status != "D" and withheld_key(head, path))):
            add_key_problem(problems, path)
        added = [] if status == "D" else git.added_lines(repo, base, head, path)
        code = path.lower().endswith(CODE_SUFFIXES)
        if path_matches(path, paths["test_files"]):
            if status == "D":
                add(rule_id="VC-1/test-file-deleted", category="test-integrity", severity="high", file=path, vc="VC-1",
                    snippet=f"test file deleted: {path}", title="Test file deleted",
                    rationale="Deleting tests can hide a regression the tests used to catch.", fix="Restore the tests, or explain the removal to the reviewer.")
            else:
                old_text, old_ok = read(base, path)
                new_text, new_ok = read(head, path)
                gone = (_test_names(old_text, patterns) - _test_names(new_text, patterns)) if old_ok and new_ok else {}
                if gone:
                    add(rule_id="VC-1/test-removed", category="test-integrity", severity="high", file=path, vc="VC-1",
                        snippet="tests removed: " + ", ".join(sorted(gone)[:10]), title="Test functions removed",
                        rationale="Removing tests can hide a regression the tests used to catch.", fix="Restore the tests, or explain the removal to the reviewer.")
        if path_matches(path, paths["test_config_files"]):
            name = path.lower().rsplit("/", 1)[-1]
            old_text, old_ok = read(base, path)
            new_text, new_ok = (None, True) if status == "D" else read(head, path)
            if old_ok and new_ok and _test_settings(name, old_text, patterns) != _test_settings(name, new_text, patterns):
                add(rule_id="VC-1/test-config-changed", category="test-integrity", severity="high", file=path, vc="VC-1",
                    snippet=f"test configuration changed: {path}", title="Test configuration changed",
                    rationale="Test settings can exclude tests from a run or mark failures as expected.",
                    fix="Explain the change to the reviewer, or revert it.")
        for line, text in added:
            if code and _hit(patterns["skip_markers"], text):
                add(rule_id="VC-1/skip-added", category="test-integrity", severity="high", file=path, line=line, snippet=text, vc="VC-1",
                    title="Test skip marker added", rationale="A skipped test no longer protects anything.", fix="Remove the skip, or fix the test.")
            if _hit(patterns["suppression_markers"], text):
                add(rule_id="T-15/inline-suppression", category="iac", severity="high", file=path, line=line, snippet=text, trigger="T-15",
                    title="Inline scanner suppression added", rationale="A suppression can make Gate 1 green without fixing the finding.",
                    fix="Have the security team accept the finding, or fix it instead of suppressing it.")
            if code and not path_matches(path, paths["generated_dirs"]) and _hit(patterns["platform_import"], text):
                add(rule_id="T-16/platform-import", category="authz", severity="high", file=path, line=line, snippet=text, trigger="T-16",
                    title="App code imports the generated _platform module", rationale="Code in the same process can bypass the generated authz.",
                    fix="Remove the import; only the generated entrypoint may load _platform.")
        widening = [(n, t) for n, t in added if t.strip() and not t.strip().startswith("#")]
        if path_matches(path, paths["suppression_files"]) and widening:
            add(rule_id="T-15/suppression-file", category="iac", severity="high", file=path, line=widening[0][0], snippet=widening[0][1],
                trigger="T-15", title="Scanner suppression file added or widened",
                rationale="A suppression can make Gate 1 green without fixing the finding.", fix="Have the security team accept the finding.")
        if path_matches(path, paths["scanner_config_files"]):
            add(rule_id="T-15/scanner-config-changed", category="iac", severity="high", file=path, trigger="T-15",
                snippet=f"{'deleted' if status == 'D' else 'changed'}: {path}", title="Scanner configuration changed",
                rationale="Scanner configuration can turn off rules or paths, which makes Gate 1 green without fixing findings.",
                fix="Have the security team review the scanner configuration change.")
        if path_matches(path, paths["workflow_files"]):
            add(rule_id="T-16/workflow-or-owners-change", category="iac", severity="high", file=path, trigger="T-16",
                snippet=f"{'deleted' if status == 'D' else 'changed'}: {path}", title="Workflow or CODEOWNERS changed",
                rationale="Workflows and CODEOWNERS decide what runs and who reviews.", fix="Have a platform owner review this change.")
        if status != "D" and path_matches(path, paths["shadow_files"]):
            add(rule_id="T-16/harness-shadowing", category="authz", severity="high", file=path, trigger="T-16", snippet=f"added or changed: {path}",
                title="File that can shadow the platform harness", rationale="This file can run before, or instead of, the generated entrypoint.",
                fix="Remove the file; the platform sets the server command.")
        if path_matches(path, paths["purpose_files"]):
            add(rule_id="T-9/purpose-file-changed", category="scope", severity="medium", file=path, trigger="T-9",
                snippet=f"{'deleted' if status == 'D' else 'added or changed'}: {path}", title="PURPOSE.md changed",
                rationale="The app's purpose is set in the factory intake; a changed PURPOSE.md may signal new behavior.",
                fix="Update the factory intake if the app's purpose really changed.")
    return out
