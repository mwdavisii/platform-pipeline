"""check-factory validates bindings/<owner>/<repo>.yaml (binding/v1): its schema, its repo and the app's intake record."""

import json

import pytest
import yaml

from scorecard.cli import main

BINDING = {"schema": "binding/v1", "repo": None, "linked_by": "p_" + "a" * 32, "linked_at": "2026-10-29T15:00:00Z",
           "request": "r_" + "b" * 32,
           "azure_identity": {"directory_id": "sp-notes", "tenant_id": "00000000-0000-4000-8000-00000000a2e0",
                              "client_id": "11111111-1111-4111-8111-111111111111", "verified_by": "mock-directory"}}


def an_intake_repo(root):
    owner = next(p for p in sorted((root / "intake").iterdir()) if p.is_dir())
    record = next(iter(sorted(owner.glob("*.yaml"))))
    return f"{owner.name}/{record.stem}"


def write_binding(root, repo, doc):
    owner, name = repo.split("/")
    (root / "bindings" / owner).mkdir(parents=True, exist_ok=True)
    (root / "bindings" / owner / f"{name}.yaml").write_text(yaml.safe_dump(doc))


def check(root, capsys):
    code = main(["check-factory", "--factory", str(root)])
    return code, capsys.readouterr()


def test_a_valid_binding_is_counted(factory, capsys):
    repo = an_intake_repo(factory)
    write_binding(factory, repo, {**BINDING, "repo": repo})
    code, out = check(factory, capsys)
    assert code == 0 and json.loads(out.out.strip().splitlines()[-1])["bindings"] == 1


def test_no_bindings_directory_counts_zero(factory, capsys):
    code, out = check(factory, capsys)
    assert code == 0 and json.loads(out.out.strip().splitlines()[-1])["bindings"] == 0


def test_a_readme_directly_under_bindings_is_allowed(factory, capsys):
    (factory / "bindings").mkdir()
    (factory / "bindings/README.md").write_text("# Bindings\n")
    code, out = check(factory, capsys)
    assert code == 0 and json.loads(out.out.strip().splitlines()[-1])["bindings"] == 0


@pytest.mark.parametrize("change, message", [
    (lambda d, repo: {**d, "repo": "someone/else"}, "is for someone/else"),
    (lambda d, repo: {**d, "repo": repo, "azure_identity": {**d["azure_identity"], "client_id": "x"}}, "not a valid binding.v1"),
])
def test_a_bad_binding_fails(factory, capsys, change, message):
    repo = an_intake_repo(factory)
    write_binding(factory, repo, change(BINDING, repo))
    code, out = check(factory, capsys)
    assert code != 0 and message in (out.out + out.err)


def test_a_binding_needs_an_intake_record(factory, capsys):
    write_binding(factory, "acme/unregistered", {**BINDING, "repo": "acme/unregistered"})
    code, out = check(factory, capsys)
    assert code != 0 and "no intake record" in (out.out + out.err)


def test_a_symlinked_binding_is_refused(factory, capsys, tmp_path):
    repo = an_intake_repo(factory)
    owner, name = repo.split("/")
    target = tmp_path / "elsewhere.yaml"
    target.write_text(yaml.safe_dump({**BINDING, "repo": repo}))
    (factory / "bindings" / owner).mkdir(parents=True)
    (factory / "bindings" / owner / f"{name}.yaml").symlink_to(target)
    code, out = check(factory, capsys)
    assert code != 0 and "symlink" in out.err


def test_a_broken_intake_record_is_reported_once(factory, capsys):
    repo = an_intake_repo(factory)
    owner, name = repo.split("/")
    intake = factory / "intake" / owner / f"{name}.yaml"
    intake.write_text(intake.read_text().replace("tier: ", "tier: huge\nold_tier: ", 1))
    write_binding(factory, repo, {**BINDING, "repo": repo})
    code, out = check(factory, capsys)
    problems = json.loads(out.err)["problems"]
    binding = [p for p in problems if p.startswith(f"bindings/{repo}:")]
    assert code != 0 and len(binding) == 1 and "has no valid intake record" in binding[0] and "intake.v" not in binding[0]
    assert sum(1 for p in problems if p.startswith(f"intake/{repo}:")) == 1
