import json

import pytest

from golden_road.cli import main
from golden_road.preview import annotations, render

PASS = {"status": "pass", "cases": 120, "denied": 80, "failures": [], "failures_total": 0}
FAIL = {"status": "fail", "cases": 120, "denied": 80, "failures_total": 2, "failures": [
    {"method": "POST", "path": "/items", "roles": "reader", "status": 200},
    {"method": "GET", "path": "/admin", "roles": None, "status": None}]}
RUN = "https://github.com/o/r/actions/runs/123"


def verdict(md):
    return md.splitlines()[1]


def test_comment_starts_with_the_marker_the_poster_looks_for():
    assert render(PASS, "success", RUN).splitlines()[0] == "<!-- golden-road:preview -->"


def test_passes_only_when_the_job_succeeded_and_the_probe_passed():
    assert "PASS" in verdict(render(PASS, "success", RUN))
    assert "PASS" in verdict(render(PASS, None, None))  # the in-job report has no job result yet
    assert "PASS" in verdict(render({"status": "not-declared"}, "success", None))


@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped", "", "weird", "SUCCESS", "unknown"])
def test_a_job_that_did_not_succeed_is_never_reported_as_passed(result):
    md = render(PASS, result, RUN)
    assert "PASS" not in md and "passed" not in md.lower()


@pytest.mark.parametrize("authz", [None, [], "pass", {}, {"status": "weird"}, {"status": ["pass"]}, {"status": "fail"}, FAIL])
def test_a_missing_or_malformed_probe_result_is_a_failure(authz):
    md = render(authz, "success", RUN)
    assert "FAIL" in verdict(md) and "PASS" not in md


def test_failures_are_listed_and_capped():
    md = render(FAIL, "failure", RUN)
    assert "POST" in md and "/items" in md and "no response" in md
    many = {**FAIL, "failures": [{"method": "GET", "path": f"/p{i}", "roles": None, "status": 200} for i in range(200)]}
    assert "more" in render(many, "failure", RUN) and len(render(many, "failure", RUN)) < 20_000


def test_hostile_text_cannot_mention_people_link_or_inject_html():
    hostile = {"status": "fail", "failures": [{"method": "GET", "path": "/<img src=x>@octocat [x](http://e.vil)\n::error::forged", "roles": "a|b", "status": 200}],
               "reason": "@everyone <script>"}
    md = render(hostile, "failure", "https://evil.example/not-a-run")
    assert "<img" not in md and "<script" not in md and "@octocat" not in md.replace("@‍", "") and "@everyone" not in md.replace("@‍", "")
    assert "](http" not in md and "evil.example" not in md
    assert "\n::error::" not in md


def test_run_link_only_for_a_real_actions_run_url():
    assert RUN in render(PASS, "success", RUN)


def test_annotations_are_single_line_commands_with_escaped_data():
    hostile = {"status": "fail", "failures": [{"method": "GET", "path": "/x\n::error::forged%", "roles": "r", "status": 200}]}
    lines = annotations(hostile)
    assert lines and all(l.startswith(("::error ", "::notice ")) and "\n" not in l for l in lines)
    assert not any(l.startswith("::error::forged") for l in lines) and "%0A" in "".join(lines)


def test_annotations_cap_and_missing_result():
    many = {"status": "fail", "failures": [{"method": "GET", "path": f"/p{i}", "roles": None, "status": 200} for i in range(100)]}
    assert len(annotations(many)) <= 11
    [line] = annotations(None)
    assert line.startswith("::error ") and "did not" in line
    assert annotations(PASS)[0].startswith("::notice ")


def test_cli_writes_markdown_and_prints_annotations(tmp_path, capsys):
    authz = tmp_path / "authz.json"
    authz.write_text(json.dumps(FAIL))
    out = tmp_path / "p.md"
    assert main(["preview-report", "--authz", str(authz), "--job-result", "failure", "--run-url", RUN, "--annotate", "--output", str(out)]) == 0
    assert "FAIL" in verdict(out.read_text()) and capsys.readouterr().out.startswith("::error ")


def test_cli_without_a_probe_file_reports_failure(tmp_path):
    out = tmp_path / "p.md"
    assert main(["preview-report", "--authz", str(tmp_path / "nope.json"), "--job-result", "success", "--output", str(out)]) == 0
    assert "FAIL" in verdict(out.read_text())


def test_cli_bad_json_is_a_failure_report_not_a_crash(tmp_path):
    bad = tmp_path / "authz.json"
    bad.write_text("{nope")
    out = tmp_path / "p.md"
    assert main(["preview-report", "--authz", str(bad), "--job-result", "success", "--output", str(out)]) == 0
    assert "FAIL" in verdict(out.read_text())


def test_a_job_that_failed_after_a_passing_probe_says_so():
    md = render(PASS, "failure", RUN)
    assert "FAIL" in verdict(md) and "failed after the probe" in md and "check log" in md
    assert "failed after the probe" in annotations(PASS, "failure")[0]


def test_the_exit_code_of_a_crashed_app_is_shown():
    crashed = {"status": "fail", "reason": "app exited before the probe could run", "exit_code": 1}
    md = render(crashed, "failure", RUN)
    assert "app exited before the probe could run" in md and "exit code 1" in md
    assert any("exit code 1" in line for line in annotations(crashed, "failure"))
    assert "exit code" not in render({**crashed, "exit_code": "1; @x"}, "failure", RUN)


def test_the_tested_commit_is_shown_only_when_it_is_a_full_sha():
    sha = "a" * 40
    assert f"`{sha}`" in render(PASS, "success", RUN, sha)
    for bad in ("main", "a" * 39, "@octocat", None):
        assert "Tested commit" not in render(PASS, "success", RUN, bad)


def test_cli_passes_the_commit_through(tmp_path):
    out = tmp_path / "p.md"
    assert main(["preview-report", "--commit", "b" * 40, "--job-result", "skipped", "--output", str(out)]) == 0
    assert "b" * 40 in out.read_text() and "DID NOT RUN" in out.read_text()
