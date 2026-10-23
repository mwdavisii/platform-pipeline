"""The writer's file and the scorecard, across trees: what `portal.writer` renders is what `scorecard` and `factory-check` read."""

import base64
import datetime as dt
import json
import shutil
import sys
from pathlib import Path

import httpx
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bootstrap/golden-road-portal"))
sys.path.insert(0, str(ROOT / "bootstrap/golden-road-portal/tests"))

from golden_road.io import InputError  # noqa: E402
from portal_fakes import GROUPS, ISSUER, FakeGitHub, approved_request, make_settings, seed, transport, writer_config  # noqa: E402
from portal_fakes import T0 as PORTAL_T0  # noqa: E402
from test_portal_writer_run import FILE, gh_for_write  # noqa: E402

from portal.github.client import GitHub  # noqa: E402
from portal.roles import roles_for  # noqa: E402
from portal.store.memory import MemoryStore  # noqa: E402
from portal.writer.run import run_once  # noqa: E402
from portal.registration import intake_record  # noqa: E402
from portal.store.base import Evidence, Request  # noqa: E402
from portal.writer.allowlist import target_for  # noqa: E402
from portal.writer.render import intake_yaml  # noqa: E402
from scorecard.cli import main  # noqa: E402
from scorecard.config import load_config, load_intake  # noqa: E402
from scorecard.level import decide  # noqa: E402

NOW = dt.datetime(2026, 10, 23, 12, tzinfo=dt.UTC)
OWNER, ADMIN, RID = "p_" + "a" * 32, "p_" + "b" * 32, "r_" + "c" * 32


@pytest.fixture
def factory(tmp_path):
    dest = tmp_path / "factory"
    shutil.copytree(ROOT / "tests/scorecard/fixtures/factory", dest)
    return dest


def approved(repo="acme/newapp", approver=ADMIN, floor=("moderate", "internal"), tier="moderate", override_reason=""):
    req = Request(RID, "registration", repo, "writing", {"schema": "registration-draft/v1", "repo": repo, "purpose": "Stores short team notes; no email.",
                  "data_classification": "internal", "proposed_tier": "low"}, OWNER, NOW, NOW)
    evidence = Evidence(RID, 1, "e" * 40, {}, floor[0], floor[1], "f" * 40, NOW)
    doc, errors = intake_record(req, approver_id=approver, tier=tier, classification="restricted", review_by="2027-10-23",
                                justification="Raised the classification: it stores clinic names.", now=NOW, evidence=evidence,
                                override_reason=override_reason)
    assert not errors, errors
    return req, doc


def write_like_the_writer(factory, req, doc):
    target = target_for("acme", req)
    path = factory / target.path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(intake_yaml(doc, req.id))
    return path


def test_until_the_writer_files_it_the_repo_has_no_intake_record_and_afterwards_the_scorecard_reads_it(factory):
    assert load_intake(factory, "acme/newapp") is None  # trigger T-8 fires
    req, doc = approved()
    write_like_the_writer(factory, req, doc)
    loaded = load_intake(factory, "acme/newapp")
    assert loaded == doc and loaded["schema"] == "intake/v2" and loaded["owner"] == OWNER and loaded["tier"] == "moderate"


def test_factory_check_accepts_what_the_writer_files(factory, capsys):
    before = main(["check-factory", "--factory", str(factory)])
    capsys.readouterr()
    assert before == 0
    req, doc = approved()
    write_like_the_writer(factory, req, doc)
    assert main(["check-factory", "--factory", str(factory)]) == 0
    assert json.loads(capsys.readouterr().out)["intake"] == 3


def test_the_scorecard_loads_what_the_writer_writes_with_the_floor_and_an_override(factory, capsys):
    req, doc = approved(tier="low", override_reason="The spec over-declares egress; checked it.")
    assert doc["floor_override"]["by"] == ADMIN and (doc["floor_tier"], doc["tier"]) == ("moderate", "low")
    write_like_the_writer(factory, req, doc)
    assert load_intake(factory, "acme/newapp") == doc
    assert main(["check-factory", "--factory", str(factory)]) == 0
    capsys.readouterr()
    plain_req, plain = approved(repo="acme/other")  # at the floor: the floor fields and no override
    write_like_the_writer(factory, plain_req, plain)
    assert "floor_override" not in load_intake(factory, "acme/other") and main(["check-factory", "--factory", str(factory)]) == 0


def test_the_scorecard_refuses_a_record_below_its_floor_without_an_override(factory, capsys):
    req, doc = approved(tier="low", override_reason="The spec over-declares egress; checked it.")
    write_like_the_writer(factory, req, {k: v for k, v in doc.items() if k != "floor_override"})
    with pytest.raises(InputError, match="below the floor"):
        load_intake(factory, "acme/newapp")
    assert main(["check-factory", "--factory", str(factory)]) != 0
    capsys.readouterr()


def test_factory_check_refuses_a_self_approved_record(factory, capsys):
    req, doc = approved()
    write_like_the_writer(factory, req, {**doc, "approved_by": OWNER})
    assert main(["check-factory", "--factory", str(factory)]) != 0
    assert "approved_by" in capsys.readouterr().err
    with pytest.raises(InputError, match="approved_by"):
        load_intake(factory, "acme/newapp")


def test_factory_check_refuses_a_hand_edited_record_that_breaks_the_schema(factory, capsys):
    req, doc = approved()
    path = write_like_the_writer(factory, req, doc)
    edited = yaml.safe_load(path.read_text())
    edited["owner"] = "platform-team"  # a v2 owner is a portal person
    path.write_text(yaml.safe_dump(edited))
    assert main(["check-factory", "--factory", str(factory)]) != 0
    capsys.readouterr()


def test_the_file_the_writer_renders_has_a_header_and_the_approval_fields(factory):
    req, doc = approved()
    text = write_like_the_writer(factory, req, doc).read_text()
    assert text.startswith("# Written by the golden road portal")
    assert all(key in yaml.safe_load(text) for key in ("approved_by", "approved_at", "approval_justification", "review_by", "request"))


# ---- the plan's Task 38: the real bytes the writer PUTs, and T-8 until approval ----

@pytest.fixture
def world(tmp_path):
    store, settings = MemoryStore(), make_settings()
    people = {}
    for who, groups in GROUPS.items():
        if groups is not None:
            people[who] = store.sign_in(issuer=ISSUER, subject=f"sub-{who}", display_name=who.title(), email=None, groups=groups,
                                        roles=roles_for(groups, settings.role_groups), now=PORTAL_T0, max_age=settings.session_max)[0]
    shutil.copytree(ROOT / "tests/scorecard/fixtures/factory", tmp_path / "factory")
    (tmp_path / "factory/intake/acme/notes.yaml").unlink()
    return store, people, tmp_path / "factory"


def gh_client(gh):
    return GitHub("https://api.github.com", httpx.Client(transport=transport(gh)), lambda: "ghs_" + "w" * 36)


def test_until_approval_nothing_is_written_and_the_scorecard_fires_t8(world):
    store, people, factory = world
    seed(store, people, PORTAL_T0)  # a pending registration of acme/notes
    gh = FakeGitHub()
    run_once(store, gh_client(gh), writer_config(), PORTAL_T0)
    assert gh.calls == []
    assert load_intake(factory, "acme/notes") is None
    level, _, reasons = decide(config=load_config(factory), intake=None, spec_classification=None, results=[], findings=[], covered=10, total=10,
                               flags={"incomplete": False, "unverified": [], "ignored": []}, lookup_failures=[], pr_text_hits=[])
    assert level == "security_review" and "T-8" in [r["trigger"] for r in reasons]


def test_the_writer_s_intake_file_is_one_the_scorecard_loads(world):
    store, people, factory = world
    req = approved_request(store, people, PORTAL_T0)
    gh = gh_for_write()
    run_once(store, gh_client(gh), writer_config(), PORTAL_T0)
    content = base64.b64decode(json.loads(gh.called("PUT", FILE)[0]["body"])["content"])  # the bytes run_once really sent
    (factory / "intake/acme/notes.yaml").write_bytes(content)
    intake = load_intake(factory, "acme/notes")
    assert intake["schema"] == "intake/v2" and intake["request"] == req.id and intake["tier"] == "moderate"
