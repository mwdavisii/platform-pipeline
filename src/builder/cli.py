"""builder CLI: JSON out on stdout, exit code from builder.errors."""

import argparse
import json
import sys
from pathlib import Path

from . import VERSION
from .detect import detect
from .errors import EXIT_DRIFT, EXIT_INVALID, EXIT_OK, BuilderError
from .generate import generate
from .guide import guide
from .lock import SPEC_PATH
from .verify import Reader, verify


def _emit(result: dict, code: int) -> int:
    print(json.dumps(result, indent=2, sort_keys=True))
    return code


COMMANDS = ("detect", "generate", "verify", "guide")
READ_PROBLEMS = {
    "missing": "not found",
    "symlink": "is a link; use the real file",
    "not a regular file": "is not a regular file",
    "outside": "is outside its directory",
    "oversize": "is larger than 1 MB",
    "unreadable": "can't be read",
}


class UsageError(Exception):
    def __init__(self, command: str, message: str):
        self.command, self.message = command, message
        super().__init__(message)


class _Parser(argparse.ArgumentParser):
    """Usage errors become a builder-result instead of usage text on stderr."""

    def error(self, message):
        last = self.prog.split()[-1]
        raise UsageError(last if last in COMMANDS else "detect", message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="builder", description="Package an app into a platform-compliant container and deployment config.")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    sub.add_parser("detect", help="Inspect the app and draft a spec. Writes nothing.").add_argument("path", type=Path)
    gen = sub.add_parser("generate", help="Write the Dockerfile, deploy/base, _platform, and .builder.lock.")
    gen.add_argument("path", type=Path)
    gen.add_argument("--input", type=Path, help=f"Spec file to use instead of <path>/{SPEC_PATH}")
    sub.add_parser("verify", help="Report drift from the generated files as findings.").add_argument("path", type=Path)
    sub.add_parser("guide", help="Print the spec fields and rules of this builder version as JSON. Reads nothing in the app.")
    return parser


def _load_spec(spec_path: Path):
    """The spec as a parsed value, or (error message) as a str wrapped in a tuple. Reads are contained and size-capped."""
    parent = spec_path.resolve().parent if spec_path.parent.exists() else spec_path.parent
    data, reason = Reader(parent).read_bytes(spec_path.name)
    if data is None:
        return (f"{spec_path} {READ_PROBLEMS.get(reason, 'can not be read')}. Save the confirmed spec as {SPEC_PATH} (run builder detect first).",)
    try:
        return json.loads(data.decode("utf-8"))
    except UnicodeDecodeError:
        return (f"{spec_path} is not valid UTF-8 text.",)
    except RecursionError:
        return (f"{spec_path} is not valid JSON: nested too deeply.",)
    except ValueError as exc:
        return (f"{spec_path} is not valid JSON: {str(exc)[:200]}",)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    base = {"command": "detect", "builder_version": VERSION, "warnings": [], "questions": [], "errors": []}
    try:
        args = _parser().parse_args(argv)
    except UsageError as exc:
        # argparse reports an unrecognized argument from the top-level parser, which doesn't know the subcommand.
        command = argv[0] if argv and argv[0] in COMMANDS else exc.command
        return _emit({**base, "command": command, "status": "invalid", "errors": [exc.message]}, EXIT_INVALID)
    base["command"] = args.command
    try:
        if args.command == "guide":
            print(json.dumps(guide(), indent=2, sort_keys=True))
            return EXIT_OK
        root = args.path.resolve()
        if not root.is_dir():
            return _emit({**base, "status": "invalid", "errors": [f"{args.path} is not a directory"]}, EXIT_INVALID)
        if args.command == "detect":
            return _emit({**base, **detect(root)}, EXIT_OK)
        if args.command == "generate":
            spec = _load_spec(args.input or root / SPEC_PATH)
            if isinstance(spec, tuple):
                return _emit({**base, "status": "invalid", "errors": [spec[0]]}, EXIT_INVALID)
            return _emit({**base, "status": "ok", **generate(root, spec)}, EXIT_OK)
        findings = verify(root)
        status, code = ("drift", EXIT_DRIFT) if findings else ("ok", EXIT_OK)
        return _emit({**base, "status": status, "findings": findings}, code)
    except BuilderError as exc:
        status = "invalid" if exc.exit_code == EXIT_INVALID else "needs_input"
        return _emit({**base, "status": status, "errors": exc.errors, "questions": exc.questions, "warnings": exc.warnings}, exc.exit_code)
    except (OSError, ValueError, RecursionError) as exc:
        return _emit({**base, "status": "invalid", "errors": [f"{args.command} failed: {type(exc).__name__}: {str(exc)[:200]}"]}, EXIT_INVALID)
    except Exception as exc:  # last resort: the pipeline needs JSON and an exit code, never a traceback
        return _emit({**base, "status": "invalid", "errors": [f"internal error: {type(exc).__name__}"]}, EXIT_INVALID)


if __name__ == "__main__":
    sys.exit(main())
