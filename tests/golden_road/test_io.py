import json

import pytest

from golden_road.io import InputError, load_json, load_findings


def test_load_json_reads_valid_file(tmp_path):
    p = tmp_path / "a.json"
    p.write_text('{"a": 1}')
    assert load_json(p) == {"a": 1}


@pytest.mark.parametrize("content", [b"{not json", b"\xff\xfe", b"[" * 100000])
def test_load_json_fails_closed(tmp_path, content):
    p = tmp_path / "a.json"
    p.write_bytes(content)
    with pytest.raises(InputError):
        load_json(p)


def test_load_json_missing_and_oversize(tmp_path, monkeypatch):
    with pytest.raises(InputError):
        load_json(tmp_path / "missing.json")
    p = tmp_path / "big.json"
    p.write_text("[]")
    monkeypatch.setattr("golden_road.io.MAX_INPUT_BYTES", 1)
    with pytest.raises(InputError):
        load_json(p)


def test_load_findings_requires_list_of_valid_findings(tmp_path, valid_finding):
    good = tmp_path / "good.json"
    good.write_text(json.dumps([valid_finding]))
    assert load_findings(good) == [valid_finding]
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"not": "a list"}))
    with pytest.raises(InputError):
        load_findings(bad)
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps([{**valid_finding, "severity": "huge"}]))
    with pytest.raises(InputError):
        load_findings(broken)


def test_load_findings_round_trips_redacted_secret_and_digest_anchored_drift(tmp_path):
    from builder.verify import _drift
    from contracts.findings import make_finding
    from golden_road.io import write_json
    from golden_road.normalize import _redact

    drift = _drift("builder.drift.x", "high", "deploy/a.yaml", 3, "x:  1\n", "t", "r", digest="sha256:" + "0" * 64)
    secret = _redact(make_finding(
        layer="deterministic", tool="gitleaks", rule_id="aws-key", category="secrets", severity="high",
        location={"kind": "line", "file": "src/a\udcff.py", "start_line": 2, "end_line": 2},
        evidence=[{"file": "src/a\udcff.py", "start_line": 2, "snippet": "AKIAAAAAAAAAAAAAAAAA  "}],
        title="t", rationale="r", fix={"summary": "s", "steps": []},
        standards=[{"framework": "cwe", "id": "CWE-798"}], evidence_verified=True))
    path = tmp_path / "f.json"
    write_json(path, [drift, secret])
    assert load_findings(path) == [drift, secret]
