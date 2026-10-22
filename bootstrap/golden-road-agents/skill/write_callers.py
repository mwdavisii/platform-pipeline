#!/usr/bin/env python3
"""Write the platform's four caller workflows into an app folder, from this bundle's templates.

    uv run --no-project --no-config --python 3.12 python -I <bundle>/write_callers.py [app folder]

The app name comes from the folder's builder-spec.json. The callers are platform-owned: a caller the platform wrote, even
edited, is overwritten, and golden-road doctor checks them. A workflow with one of these names that doesn't call the platform's
workflow is the user's own: the script refuses and writes nothing until they rename it. Writes only
.github/workflows/<the four names>, never through a link. Output: one JSON
object on stdout. Exit 0 done, 2 usage, 3 can't continue (the message and fix say what to do).
"""

import json
import re
import sys
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parent / "templates" / "workflows"
NAMES = ("ci.yml", "promote.yml", "scorecard.yml", "security-scans.yml")
# What marks a caller as the platform's: a job whose `uses:` calls a workflow of this repository (the same names doctor checks).
# Read line by line (this script is standard library only): a comment, a name or a step that only mentions the repository doesn't count.
USES = re.compile(r"^\s*uses:\s*[\"']?([^\s\"'#]+)")
PLATFORM_REPO = {"ci.yml": "/platform-pipeline/", "promote.yml": "/platform-pipeline/", "scorecard.yml": "/scorecard-factory/",
                 "security-scans.yml": "/security-scans/"}
APP_TOKEN = "__APP_NAME__"
APP_NAME = re.compile(r"[a-z]([a-z0-9-]{0,40}[a-z0-9])?")  # builder-spec/v1's app_name pattern (a test keeps them equal)
UNCONFIGURED = "ORG_" + "PLACEHOLDER"  # split, so configure.py's rewrite of the placeholder leaves this check alone


def emit(doc, code):
    print(json.dumps(doc, indent=2))
    return code


def error(code, message, fix):
    return emit({"status": "error", "message": message, "fix": fix}, code)


def calls_platform(file, content):
    """True if a job in this workflow file calls a workflow of the platform repository that owns the name."""
    for line in content.decode("utf-8", "replace").splitlines():
        match = USES.match(line)
        if match and PLATFORM_REPO[file] in match.group(1):
            return True
    return False


def app_name(root):
    spec = root / "builder-spec.json"
    if spec.is_symlink() or not spec.is_file():
        return None
    try:
        name = json.loads(spec.read_text(encoding="utf-8")).get("app_name")
    except (OSError, ValueError, AttributeError):
        return None
    return name if isinstance(name, str) and APP_NAME.fullmatch(name) else None


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) > 1 or any(arg.startswith("-") for arg in argv):
        return error(2, "usage: write_callers.py [app folder]", "Run it from the app folder, or pass the folder.")
    root = Path(argv[0]) if argv else Path(".")
    name = app_name(root)
    if name is None:
        return error(3, "builder-spec.json is missing, a link, or has no valid app_name", "Write builder-spec.json and run builder generate . first.")
    texts = {}
    for file in NAMES:
        text = (TEMPLATES / file).read_text(encoding="utf-8")
        if UNCONFIGURED in text:
            return error(3, f"the {file} template names no org", "This bundle was built from a tree scripts/configure.py never ran on. Tell the platform team.")
        texts[file] = text.replace(APP_TOKEN, name).encode("utf-8")
    workflows = root / ".github" / "workflows"
    for folder in (root / ".github", workflows):
        if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
            return error(3, f"{folder.relative_to(root).as_posix()} is a link or not a folder", "Make it a real folder, then run this again.")
    for file in NAMES:
        target = workflows / file
        if target.is_symlink() or (target.exists() and not target.is_file()):
            return error(3, f".github/workflows/{file} is a link or not a regular file", "Remove it, then run this again.")
    try:
        current = {file: (workflows / file).read_bytes() if (workflows / file).exists() else None for file in NAMES}
        for file in NAMES:  # all four are checked before any is written
            old = current[file]
            if old is not None and old != texts[file] and not calls_platform(file, old):
                return error(3, f"rename your workflow {file}; the platform owns this name",
                             f"Rename .github/workflows/{file} (and any references to it), then run this again. Nothing was written.")
        workflows.mkdir(parents=True, exist_ok=True)
        written, unchanged = [], []
        for file in NAMES:
            if current[file] == texts[file]:
                unchanged.append(file)
            else:
                (workflows / file).write_bytes(texts[file])
                written.append(file)
    except OSError as exc:
        return error(3, f"can't read or write the caller workflows: {exc.strerror or 'file error'}", "Fix the folder's permissions, then run this again.")
    return emit({"status": "ok", "app_name": name, "written": written, "unchanged": unchanged}, 0)


if __name__ == "__main__":
    sys.exit(main())
