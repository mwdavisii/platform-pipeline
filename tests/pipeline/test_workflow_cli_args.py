"""Every golden-road command a workflow runs parses with golden_road.cli's own parser, so a flag the CLI dropped or renamed (as
render and record lost --binding in builder 0.4.0) fails here, not in a release. Values are not checked (they are shell
variables here): only the subcommand, the flag names, the required flags and the number of arguments."""

import argparse
import re
import shlex
from pathlib import Path

import pytest
import yaml

from golden_road.cli import _parser

ROOT = Path(__file__).resolve().parents[2]
GLOBS = (".github/workflows/*.yml", "bootstrap/*/.github/workflows/*.yml", "bootstrap/*/.github/actions/*/action.yml",
         "bootstrap/golden-road-agents/skill/templates/workflows/*.yml")
PUNCTUATION = set("();<>|&")
ARRAY_SET = re.compile(r"(?<![\w$])([A-Za-z_][A-Za-z0-9_]*)\+?=\(([^)]*)\)")
ARRAY_USE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\[@\]\}$")


def _arrays(script: str) -> dict:
    """Every bash array the script builds, with all the words any assignment adds: "${name[@]}" is checked at its fullest."""
    arrays = {}
    for name, words in ARRAY_SET.findall(script):
        arrays.setdefault(name, []).extend(shlex.split(words))
    return arrays


def _runs(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "run" and isinstance(value, str):
                yield value
            else:
                yield from _runs(value)
    elif isinstance(node, list):
        for item in node:
            yield from _runs(item)


def _logical_lines(script: str):
    """The script's lines with backslash continuations joined and comment lines dropped."""
    lines = [line for line in script.splitlines() if not line.lstrip().startswith("#")]
    return "\n".join(lines).replace("\\\n", " ").splitlines()


def _tokens(line: str) -> list[str]:
    lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def invocations():
    """(file, argv) for every `golden-road <subcommand> ...` run as a command (a quoted message naming it is one token, so skipped)."""
    found = []
    for pattern in GLOBS:
        for path in sorted(ROOT.glob(pattern)):
            for script in _runs(yaml.safe_load(path.read_text())):
                arrays = _arrays(script)
                for line in _logical_lines(script):
                    if "golden-road" not in line:
                        continue
                    try:
                        tokens = _tokens(line)
                    except ValueError:
                        pytest.fail(f"{path.relative_to(ROOT)}: can't split {line.strip()[:120]!r}")
                    for i, token in enumerate(tokens):
                        if token != "golden-road":
                            continue
                        argv = []
                        for arg in tokens[i + 1:]:
                            if arg and set(arg) <= PUNCTUATION:
                                break
                            use = ARRAY_USE.match(arg)
                            if use and use.group(1) not in arrays:
                                pytest.fail(f"{path.relative_to(ROOT)}: {arg} is not built in the same script")
                            argv.extend(arrays[use.group(1)] if use else [arg])
                        found.append((path.relative_to(ROOT).as_posix(), argv))
    return found


def _lenient(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """The parser with value types and choices off (the workflow passes "$VARS"); flag names, required flags and arity stay."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for sub in action.choices.values():
                _lenient(sub)
        else:
            action.type = None
            action.choices = None
    return parser


INVOCATIONS = invocations()


def test_the_workflows_run_the_commands_this_test_must_cover():
    commands = {argv[0] for _, argv in INVOCATIONS if argv}
    assert {"render", "record", "promotion-record", "authz-probe", "gate", "checks", "app-token", "deploy-verify", "deploy-pins", "deploy-pin", "compose"} <= commands, commands


@pytest.mark.parametrize("where,argv", INVOCATIONS, ids=[f"{w}:{a[0] if a else '?'}:{n}" for n, (w, a) in enumerate(INVOCATIONS)])
def test_every_workflow_invocation_parses_with_the_cli(where, argv, capsys):
    try:
        _lenient(_parser()).parse_args(argv)
    except SystemExit as exc:
        pytest.fail(f"{where}: golden-road {' '.join(argv)[:300]} does not parse: {capsys.readouterr().err.strip()} (exit {exc.code})")


def test_a_dropped_flag_would_fail(capsys):
    with pytest.raises(SystemExit):
        _lenient(_parser()).parse_args(["render", "--repo-root", "/src", "--image", "$REF", "--binding", "/out/binding.yaml", "--output", "/o"])
