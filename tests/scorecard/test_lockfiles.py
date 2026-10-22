import json

import pytest

from scorecard import git
from scorecard.lockfiles import normalize, parse_package_lock, parse_requirements, read_packages

REQUIREMENTS = (
    "# locked\n"
    "fastapi==0.115.0 \\\n    --hash=sha256:" + "a" * 64 + "\n"
    "Typing_Extensions[extra]==4.12.2\n"
    "-r other.txt\n"
)

LOCK = {
    "name": "notes", "lockfileVersion": 3,
    "packages": {
        "": {"name": "notes", "dependencies": {"express": "^4"}},
        "node_modules/express": {"version": "4.21.2"},
        "node_modules/@acme/ui": {"version": "1.0.0"},
        "node_modules/express/node_modules/esbuild": {"version": "0.24.0", "hasInstallScript": True},
        "node_modules/local": {"link": True, "resolved": "../local"},
    },
}


def test_requirements_pins_with_lines():
    assert parse_requirements(REQUIREMENTS) == [
        {"ecosystem": "pypi", "name": "fastapi", "version": "0.115.0", "file": "requirements.txt", "line": 2, "install_script": None},
        {"ecosystem": "pypi", "name": "typing-extensions", "version": "4.12.2", "file": "requirements.txt", "line": 4, "install_script": None},
    ]


def test_package_lock_v3():
    text = json.dumps(LOCK, indent=2)
    packages = {p["name"]: p for p in parse_package_lock(text)}
    assert set(packages) == {"express", "@acme/ui", "esbuild"}
    assert packages["esbuild"]["install_script"] is True
    assert packages["express"]["install_script"] is False
    assert text.splitlines()[packages["express"]["line"] - 1].strip().startswith('"node_modules/express"')


def test_package_lock_keeps_resolved_and_integrity():
    lock = {"lockfileVersion": 3, "packages": {"node_modules/a": {"version": "1.0.0", "resolved": "https://registry.npmjs.org/a/-/a-1.0.0.tgz",
                                                                    "integrity": "sha512-x"}, "node_modules/b": {"version": "2.0.0"}}}
    packages = {p["name"]: p for p in parse_package_lock(json.dumps(lock))}
    assert packages["a"]["resolved"].endswith("a-1.0.0.tgz") and packages["a"]["integrity"] == "sha512-x"
    assert packages["b"]["resolved"] is None and packages["b"]["integrity"] is None


def test_unsupported_lockfile_is_a_problem_not_an_empty_list():
    files = {"package-lock.json": json.dumps({"lockfileVersion": 1, "dependencies": {}})}
    packages, problems = read_packages(files.get)
    assert packages == [] and problems[0]["source"] == "package-lock.json"


def test_missing_lockfiles_are_fine():
    assert read_packages({}.get) == ([], [])


def test_normalize():
    assert normalize("Requests_OAuthlib") == "requests-oauthlib"


def _problems(text):
    return read_packages({"requirements.txt": text}.get)[1]


def test_requirements_with_markers_hashes_and_comments_are_clean():
    text = ("# c\n\nfoo==1.0 ; python_version < '3.12'  # note\nbar==2.0 \\\n    --hash=sha256:aa \\\n    --hash=sha256:bb\n"
            "baz[x]==3.0 --hash=sha256:cc\n")
    packages, problems = read_packages({"requirements.txt": text}.get)
    assert problems == [] and [p["name"] for p in packages] == ["foo", "bar", "baz"]


@pytest.mark.parametrize("line", ["foo===1.0", "foo @ https://evil.example/x.whl", "foo>=1.0", "-e .", "-r other.txt",
                                  "--index-url https://evil.example/simple", "--extra-index-url https://evil.example/simple",
                                  "foo", "git+https://evil.example/x.git#egg=foo", "--hash=sha256:aa"])
def test_unsupported_requirement_lines_are_problems(line):
    problems = _problems("ok==1.0\n" + line + "\n")
    assert len(problems) == 1 and problems[0]["source"] == "requirements.txt" and "line 2" in problems[0]["detail"]


def test_package_lock_alias_uses_the_real_name():
    lock = {"lockfileVersion": 3, "packages": {"": {}, "node_modules/innocent": {"name": "next-auth", "version": "4.0.0"}}}
    assert [p["name"] for p in parse_package_lock(json.dumps(lock))] == ["next-auth"]


def test_unreadable_lockfile_is_a_problem_not_absent():
    def read(path):
        if path == "requirements.txt":
            raise git.Unreadable("too large")
        return None

    packages, problems = read_packages(read)
    assert packages == [] and problems[0]["source"] == "requirements.txt"
