from golden_road.summary import annotations


def test_annotation_format_and_escaping(valid_finding):
    hostile = {**valid_finding, "title": "bad\nline%::x", "rule_id": "r,1:2"}
    [line] = annotations([hostile])
    assert line.startswith("::error file=requirements.txt,line=3,title=r%2C1%3A2::")
    assert "\n" not in line and "%0A" in line and "%25" in line


def test_unknown_file_has_no_file_property(valid_finding):
    f = {**valid_finding, "location": {"kind": "file", "file": "<unknown>"}}
    [line] = annotations([f])
    assert "file=" not in line


def test_levels_and_cap(valid_finding):
    many = [{**valid_finding, "severity": "low", "id": f"{i:064x}"} for i in range(80)]
    lines = annotations(many)
    assert len(lines) == 50 and all(l.startswith("::notice ") for l in lines)
    assert annotations([{**valid_finding, "severity": "medium"}])[0].startswith("::warning ")


def _file_finding(valid_finding, evidence_file):
    return {**valid_finding, "location": {"kind": "file", "file": "Dockerfile"},
            "evidence": [{"file": evidence_file, "start_line": 7, "snippet": "x"}]}


def test_file_finding_uses_evidence_line_only_for_the_same_file(valid_finding):
    [same] = annotations([_file_finding(valid_finding, "Dockerfile")])
    assert "file=Dockerfile,line=7," in same
    [other] = annotations([_file_finding(valid_finding, "requirements.txt")])
    assert "file=Dockerfile," in other and "line=" not in other
