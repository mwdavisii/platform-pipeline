"""The code-computed facts: built-in detectors, builder-spec.json measures and diff patterns."""

import copy
import json

from scorecard.routing.facts import added_lines, builtin_facts, compute_facts, diff_fact, spec_facts

GATEWAY = ("litellm.internal",)


def _finding(trigger=None, vc=None, file="app.py", line=3):
    standards = []
    if vc:
        standards.append({"framework": "golden-road-vc", "id": vc})
    if trigger:
        standards.append({"framework": "golden-road-trigger", "id": trigger})
    return {"id": "a" * 64, "source": {"layer": "deterministic"}, "standards": standards,
            "location": {"kind": "line", "file": file, "start_line": line}}


def _builtin(**kw):
    args = dict(findings=[], pr_hits=[], problems=[], lookup_failures=[], change_size={"lines": 10, "files": 1},
                large_change={"lines": 1000, "files": 40}, intake={"tier": "low", "data_classification": "internal"},
                registration=None, assessment_complete=True)
    return builtin_facts(**{**args, **kw})


def test_a_quiet_change_has_no_builtin_facts():
    facts = _builtin()
    assert all(f["value"] is False and f["refs"] == [] for f in facts.values())
    assert facts["large_change"]["params"] == {"lines": 10, "files": 1}


def test_signals_map_to_their_builtin_facts():
    facts = _builtin(findings=[_finding("T-15"), _finding(vc="VC-1"), _finding(vc="VC-INJ")], pr_hits=[{"source": "pr-body"}])
    assert facts["suppression_added"]["value"] and facts["tests_weakened"]["value"]
    assert facts["reviewer_text"]["refs"] == [{"finding_id": "a" * 64, "file": "app.py", "line": 3}, {"source": "pr-body"}]


def test_no_intake_problems_and_an_incomplete_review_are_facts():
    facts = _builtin(intake=None, problems=[{"source": "lockfiles", "detail": "x"}], assessment_complete=False)
    assert facts["no_intake"]["value"] and facts["input_problem"]["refs"] == [{"source": "lockfiles"}]
    assert facts["review_incomplete"]["value"]


def test_large_change_and_registration_floor():
    facts = _builtin(change_size={"lines": 1001, "files": 2},
                     registration={"tier": "high", "data_classification": "phi", "rules": ["phi field names"]})
    assert facts["large_change"]["value"] and facts["large_change"]["params"] == {"lines": 1001, "files": 2}
    reg = facts["below_registration"]
    assert reg["value"] and reg["refs"] == [{"source": "phi field names"}]
    assert reg["params"] == {"registered": "low/internal", "needed": "high/phi", "signals": "phi field names"}


def test_an_unmeasured_change_size_is_not_large():
    facts = _builtin(change_size=None)
    assert facts["large_change"]["value"] is False and facts["large_change"]["params"] == {"lines": "unknown", "files": "unknown"}


def test_spec_measures(notes_spec):
    head = copy.deepcopy(notes_spec)
    head["authz"]["rules"].append({"methods": ["POST", "GET"], "path": "/api/search", "roles": ["reader"], "justification": "search uses POST bodies"})
    head["egress"].append({"host": "api.example.com", "port": 443, "reason": "weather data"})
    head["data_classification"] = "restricted"
    facts = spec_facts(json.dumps(notes_spec), json.dumps(head), GATEWAY)
    assert facts["role_count"]["value"] == 2 and facts["write_role_count"]["value"] == 1
    assert facts["secret_env"]["value"] == ["NOTES_DB_URL"]
    assert facts["egress_hosts"]["base"] == [] and facts["egress_hosts"]["added"] == ["api.example.com"]
    grants = facts["read_role_write_grants"]
    assert grants["base"] == [] and grants["added"] == [{"path": "/api/search", "methods": ["POST"], "roles": ["reader"],
                                                         "justification": "search uses POST bodies"}]
    c = facts["data_classification"]
    assert (c["base"], c["head"], c["order"]) == ("internal", "restricted", ["public", "internal", "restricted", "phi"])


def test_a_missing_or_invalid_spec_has_no_values():
    facts = spec_facts(None, "{not json", GATEWAY)
    assert all(f["value"] is None and f["base"] is None for f in facts.values())


DIFF = """diff --git a/db/migrations/001.sql b/db/migrations/001.sql
new file mode 100644
--- /dev/null
+++ b/db/migrations/001.sql
@@ -0,0 +1,2 @@
+CREATE TABLE notes (id int);
+DROP TABLE users;
diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -3,2 +3,3 @@ app = FastAPI()
 x = 1
-y = 2
+y = 3
+z = 4
"""


def test_added_lines_are_numbered_from_the_head():
    assert added_lines(DIFF) == {"db/migrations/001.sql": [(1, "CREATE TABLE notes (id int);"), (2, "DROP TABLE users;")],
                                 "app.py": [(4, "y = 3"), (5, "z = 4")]}


def test_an_added_line_that_looks_like_a_header_is_still_a_line():
    diff = "diff --git a/a.c b/a.c\n--- a/a.c\n+++ b/a.c\n@@ -1,1 +1,3 @@\n int i;\n+++ i;\n+int k = 1;\n"
    assert added_lines(diff) == {"a.c": [(2, "++ i;"), (3, "int k = 1;")]}


def test_diff_facts_match_paths_and_added_lines():
    changed = [{"status": "A", "path": "db/migrations/001.sql"}, {"status": "M", "path": "app.py"}]
    added = added_lines(DIFF)
    assert diff_fact({"from": "diff", "paths": ["*/migrations/*"]}, changed, added)["refs"] == [{"file": "db/migrations/001.sql"}]
    drop = diff_fact({"from": "diff", "paths": ["*.sql"], "added_lines": [r"\bdrop\s+table\b"]}, changed, added)
    assert drop["value"] and drop["refs"] == [{"file": "db/migrations/001.sql", "line": 2}]
    assert diff_fact({"from": "diff", "paths": ["*.js"]}, changed, added)["value"] is False


def test_compute_facts_names_them_from_the_config():
    routing = {"facts": {"wf": {"from": "builtin", "measure": "workflow_change"}, "roles": {"from": "spec", "measure": "role_count"},
                         "mig": {"from": "diff", "paths": ["*.sql"]}, "creds": {"from": "agent", "applies_to": "tree", "question": "q" * 10}}}
    builtin, spec = _builtin(), spec_facts(None, None, GATEWAY)
    agent = {"creds": {"kind": "agent", "type": "answer", "value": None, "refs": []}}
    facts = compute_facts(routing, builtin=builtin, spec=spec, changed=[{"status": "A", "path": "x.sql"}], added={}, agent=agent)
    assert facts["wf"] is builtin["workflow_change"] and facts["roles"] is spec["role_count"]
    assert facts["mig"]["refs"] == [{"file": "x.sql"}] and facts["creds"] is agent["creds"]
