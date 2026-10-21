import json

import pytest

from golden_road.checks import GitHubAPI, load_required, snapshot, wait
from golden_road.io import InputError

SHA = "a" * 40
REPO = "org/hello-app"


class FakeAPI:
    """Returns a scripted sequence of (check_runs, statuses) per poll."""

    def __init__(self, polls):
        self.polls = list(polls)
        self.calls = 0

    def paged(self, path, key=None):
        runs, statuses = self.polls[min(self.calls // 2, len(self.polls) - 1)]
        self.calls += 1
        return runs if key == "check_runs" else statuses


def run(name, status="completed", conclusion="success", id=1, app=1):
    return {"name": name, "status": status, "conclusion": conclusion, "id": id, "app": {"id": app}}


def status(context, state, id=1, creator="bot"):
    return {"context": context, "state": state, "id": id, "creator": {"login": creator}}


def R(name, app_id=1):
    return {"name": name, "app_id": app_id}


def S(context, creator="bot"):
    return {"context": context, "creator": creator}


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def do_wait(api, required, timeout=60):
    clock = Clock()
    return wait(api, REPO, SHA, required, timeout=timeout, interval=10, sleep=clock.sleep, clock=clock)


def test_all_green_passes():
    api = FakeAPI([([run("scanner-a"), run("golden-road / drift")], [])])
    ok, results = do_wait(api, [R("scanner-a"), R("golden-road / drift")])
    assert ok and [r["state"] for r in results] == ["success", "success"]


def test_commit_status_counts():
    api = FakeAPI([([], [status("security/scanner-a (org)", "success")])])
    assert do_wait(api, [S("security/scanner-a (org)")])[0]


def test_failure_fails_immediately():
    api = FakeAPI([([run("scanner-a", conclusion="failure")], [])])
    ok, results = do_wait(api, [R("scanner-a")])
    assert not ok and results[0]["state"] == "failure"


@pytest.mark.parametrize("conclusion", ["neutral", "skipped", "cancelled", "timed_out", "action_required", None])
def test_anything_but_success_fails(conclusion):
    api = FakeAPI([([run("scanner-a", conclusion=conclusion)], [])])
    assert not do_wait(api, [R("scanner-a")])[0]


def test_pending_then_success():
    api = FakeAPI([([run("scanner-a", status="in_progress", conclusion=None)], []), ([run("scanner-a")], [])])
    assert do_wait(api, [R("scanner-a")])[0]


def test_missing_times_out_and_fails():
    api = FakeAPI([([], [])])
    ok, results = do_wait(api, [R("scanner-a")], timeout=30)
    assert not ok and results[0]["state"] == "missing"


def test_latest_rerun_wins():
    api = FakeAPI([([run("scanner-a", conclusion="failure", id=1), run("scanner-a", id=2)], [])])
    assert do_wait(api, [R("scanner-a")])[0]


def test_latest_status_wins_newest_first():
    api = FakeAPI([([], [status("ctx", "success", id=2), status("ctx", "failure", id=1)])])
    assert do_wait(api, [S("ctx")])[0]


def test_error_status_fails():
    api = FakeAPI([([], [status("ctx", "error")])])
    assert not do_wait(api, [S("ctx")])[0]


def test_empty_required_passes():
    assert do_wait(FakeAPI([([], [])]), [])[0]


@pytest.mark.parametrize("repo, sha", [("bad repo", SHA), (REPO, "abc"), (REPO, "A" * 40)])
def test_bad_inputs(repo, sha):
    with pytest.raises(InputError):
        wait(FakeAPI([([], [])]), repo, sha, [], timeout=1, interval=1)


def test_load_required(tmp_path):
    p = tmp_path / "r.json"
    good = [{"name": "Scanner-a Code", "app_id": 12345}, {"context": "security/scanner-a (org)", "creator": "scanner-bot"},
            {"context": "x", "creator": "dependabot[bot]"}]
    p.write_text(json.dumps({"required": good}))
    assert load_required(p) == good
    n = {"name": "a", "app_id": 1}
    bad_docs = [
        {"required": "a"}, {"required": ["a"]}, {"required": [n, n]}, {"other": []}, [],
        {"required": [{"name": "", "app_id": 1}]}, {"required": [{"name": " a", "app_id": 1}]},
        {"required": [{"name": "a ", "app_id": 1}]}, {"required": [{"name": "a\nb", "app_id": 1}]},
        {"required": [{"name": "a\x00", "app_id": 1}]}, {"required": [{"name": "a\x7f", "app_id": 1}]},
        {"required": [{"name": "a", "app_id": 0}]}, {"required": [{"name": "a", "app_id": -1}]},
        {"required": [{"name": "a", "app_id": True}]}, {"required": [{"name": "a", "app_id": "1"}]},
        {"required": [{"name": "a", "app_id": 1.0}]}, {"required": [{"name": "a"}]},
        {"required": [{"name": "a", "app_id": 1, "creator": "x"}]},
        {"required": [{"name": "a", "app_id": 1, "extra": 1}]},
        {"required": [{"context": "c"}]}, {"required": [{"context": "c", "creator": ""}]},
        {"required": [{"context": "c", "creator": "-x"}]}, {"required": [{"context": "c", "creator": "a b"}]},
        {"required": [{"context": "c", "creator": "a[bot]x"}]}, {"required": [{"context": "c", "creator": 5}]},
        {"required": [{"context": "c", "creator": "x\n"}]},
        {"required": [{"context": "c ", "creator": "x"}]}, {"required": [{"context": "", "creator": "x"}]},
        {"required": [{"name": "a", "app_id": 1}, {"name": "a", "app_id": 2}, {"name": "a", "app_id": 1}]},
        {"required": [{"name": "a", "app_id": 1, "context": "c"}]},
        {"required": [{"name": 5, "app_id": 1}]},
        {"required": [n] * 2}, {"required": [{"name": f"n{i}", "app_id": 1} for i in range(51)]},
    ]
    for bad in bad_docs:
        p.write_text(json.dumps(bad))
        with pytest.raises(InputError):
            load_required(p)
    # the same text may legitimately appear as both a check name and a status context
    both = [{"name": "x", "app_id": 1}, {"context": "x", "creator": "bot"}]
    p.write_text(json.dumps({"required": both}))
    assert load_required(p) == both


class FakeResponse:
    def __init__(self, body, link=""):
        self.body, self.headers = json.dumps(body).encode(), {"Link": link}

    def read(self, n):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_paged_follows_next_on_same_host_only():
    pages = {
        "https://api.github.com/x?per_page=100": FakeResponse([1], '<https://api.github.com/x?page=2>; rel="next"'),
        "https://api.github.com/x?page=2": FakeResponse([2]),
    }
    api = GitHubAPI("https://api.github.com", "t", opener=lambda req, timeout: pages[req.full_url])
    assert api.paged("/x") == [1, 2]
    evil = {"https://api.github.com/x?per_page=100": FakeResponse([1], '<https://evil.example/x>; rel="next"')}
    api = GitHubAPI("https://api.github.com", "t", opener=lambda req, timeout: evil[req.full_url])
    with pytest.raises(InputError):
        api.paged("/x")


def test_missing_token():
    with pytest.raises(InputError):
        GitHubAPI("https://api.github.com", "")


# ---- hardening beyond the plan's tests ----

@pytest.mark.parametrize("url", [
    "https://api.github.com.evil.com/x",
    "https://api.github.com@evil.example/x",
    "https://api.github.com:444/x",
    "https://api.github.com\\@evil.example/x",
    "https://api.github.com\n.evil.example/x",
    "https://api.github.com /x",
    "http://api.github.com/x",
    "https://user:pw@api.github.com/x",
    "https://evil.example/https://api.github.com/x",
    "//evil.example/x",
    "/x",
    "https://api.github.com",
])
def test_get_refuses_urls_outside_the_api_base(url):
    calls = []
    api = GitHubAPI("https://api.github.com", "secret", opener=lambda req, timeout: calls.append(req))
    with pytest.raises(InputError):
        api._get(url)
    assert calls == []  # the token was never sent anywhere


def test_get_respects_a_path_prefix_base():
    api = GitHubAPI("https://ghe.example/api/v3", "t", opener=lambda req, timeout: FakeResponse([]))
    assert api._get("https://ghe.example/api/v3/repos/x")[0] == []
    for bad in ("https://ghe.example/other", "https://ghe.example/api/v3evil", "https://ghe.example/api/v33/x"):
        with pytest.raises(InputError):
            api._get(bad)


@pytest.mark.parametrize("base", ["http://api.github.com", "https://user@api.github.com", "api.github.com", "https://", "https://a/b?x=1", "https://a/b#f", ""])
def test_constructor_rejects_bad_api_urls(base):
    with pytest.raises(InputError):
        GitHubAPI(base, "t")


def test_default_opener_does_not_follow_redirects(monkeypatch):
    import email.message
    import io
    import urllib.request
    import urllib.response

    seen = []

    class Fake302(urllib.request.BaseHandler):
        handler_order = 100

        def https_open(self, req):
            seen.append((req.full_url, req.get_header("Authorization")))
            headers = email.message.Message()
            headers["Location"] = "https://evil.example/x"
            response = urllib.response.addinfourl(io.BytesIO(b""), headers, req.full_url, 302)
            response.msg = "Found"
            return response

    real = urllib.request.build_opener
    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: real(*handlers, Fake302))
    api = GitHubAPI("https://api.github.com", "t")  # the real _open, with its no-redirect handler
    with pytest.raises(InputError):
        api._get("https://api.github.com/x")
    assert seen == [("https://api.github.com/x", "Bearer t")]


def test_http_and_network_errors_become_input_errors():
    import urllib.error

    def boom(req, timeout):
        raise urllib.error.URLError("nope")

    clock = Clock()
    api = GitHubAPI("https://api.github.com", "t", opener=boom, sleep=clock.sleep, clock=clock)
    with pytest.raises(InputError):
        api._get("https://api.github.com/x")
    assert clock.t >= 100  # retried until the default budget ran out

    class Bad(FakeResponse):
        def read(self, n):
            return b"[" * 100_000

    with pytest.raises(InputError):
        GitHubAPI("https://api.github.com", "t", opener=lambda r, timeout: Bad([]))._get("https://api.github.com/x")


def test_too_many_pages_fails():
    loop = FakeResponse([1], '<https://api.github.com/x?page=2>; rel="next"')
    api = GitHubAPI("https://api.github.com", "t", opener=lambda req, timeout: loop)
    with pytest.raises(InputError):
        api.paged("/x")


def test_pending_at_timeout_fails():
    api = FakeAPI([([run("scanner-a", status="in_progress", conclusion=None)], [])])
    ok, results = do_wait(api, [R("scanner-a")], timeout=30)
    assert not ok and results[0]["state"] == "pending"


def test_forged_newer_run_from_another_app_is_ignored():
    api = FakeAPI([([run("scanner-a", conclusion="failure", id=100, app=1), run("scanner-a", id=101, app=999)], [])])
    ok, results = do_wait(api, [R("scanner-a", 1)])
    assert not ok and results[0]["state"] == "failure"


def test_run_from_the_wrong_app_only_is_missing():
    api = FakeAPI([([run("scanner-a", id=101, app=999)], [])])
    ok, results = do_wait(api, [R("scanner-a", 1)], timeout=20)
    assert not ok and results[0]["state"] == "missing"


def test_status_from_the_wrong_creator_is_ignored():
    api = FakeAPI([([], [status("ctx", "success", creator="mallory")])])
    ok, results = do_wait(api, [S("ctx", "scanner-bot")], timeout=20)
    assert not ok and results[0]["state"] == "missing"
    api = FakeAPI([([], [status("ctx", "failure", id=1, creator="scanner-bot"), status("ctx", "success", id=2, creator="mallory")])])
    assert not do_wait(api, [S("ctx", "scanner-bot")])[0]
    api = FakeAPI([([], [status("ctx", "success", creator="scanner-bot")])])
    assert do_wait(api, [S("ctx", "scanner-bot")])[0]


def test_right_app_passes_and_entries_only_look_at_their_own_source():
    api = FakeAPI([([run("scanner-a", app=7)], [status("scanner-a", "failure")])])
    assert do_wait(api, [R("scanner-a", 7)])[0]  # a same-named status is another source
    api = FakeAPI([([run("ctx", conclusion="failure")], [status("ctx", "success")])])
    assert do_wait(api, [S("ctx")])[0]  # a same-named check run is another source
    ok, results = do_wait(api, [R("ctx"), S("ctx")])
    assert not ok and [r["state"] for r in results] == ["failure", "success"]


@pytest.mark.parametrize("app", [None, "1", {"id": "1"}, [], 1])
def test_runs_with_a_malformed_app_are_ignored(app):
    item = run("scanner-a", id=5)
    item["app"] = app
    api = FakeAPI([([item], [])])
    ok, results = do_wait(api, [R("scanner-a", 1)], timeout=10)
    assert not ok and results[0]["state"] == "missing"


def test_a_forged_success_cannot_hide_a_missing_other_required_check():
    api = FakeAPI([([run("a")], [])])
    ok, results = do_wait(api, [R("a"), R("b")], timeout=20)
    assert not ok and [r["state"] for r in results] == ["success", "missing"]


def test_latest_by_highest_id_regardless_of_listing_order():
    api = FakeAPI([([], [status("c", "failure", id=1), status("c", "success", id=2)])])
    assert do_wait(api, [S("c")])[0]
    api = FakeAPI([([run("n", id=5), run("n", conclusion="failure", id=9)], [])])
    assert not do_wait(api, [R("n")])[0]
    api = FakeAPI([([run("n", id=None), run("n", conclusion="failure", id=3)], [])])
    assert not do_wait(api, [R("n")])[0]


def test_malformed_items_never_count_as_success():
    api = FakeAPI([(["junk", {"name": 5}, {"status": "completed", "conclusion": "success"}], [None, {"context": 3, "state": "success"}])])
    assert not do_wait(api, [R("x")], timeout=10)[0]


@pytest.mark.parametrize("kwargs", [{"timeout": float("nan")}, {"timeout": -1}, {"interval": 0}, {"interval": -5}, {"interval": float("inf")}])
def test_bad_timing_is_rejected(kwargs):
    args = {"timeout": 10, "interval": 1, **kwargs}
    with pytest.raises(InputError):
        wait(FakeAPI([([], [])]), REPO, SHA, [], **args)


def test_non_string_required_names_rejected():
    with pytest.raises(InputError):
        wait(FakeAPI([([], [])]), REPO, SHA, [1], timeout=1, interval=1)


def test_snapshot_validates_repo_and_sha_too():
    with pytest.raises(InputError):
        snapshot(FakeAPI([([], [])]), "a/b/../../c", SHA, [])


@pytest.mark.parametrize("bad_id", ["9", 1.5, True, None, -3, [1]])
def test_malformed_id_on_a_required_run_fails(bad_id):
    hostile = {"name": "x", "status": "completed", "conclusion": "failure", "id": bad_id, "app": {"id": 1}}
    api = FakeAPI([([run("x", id=1), hostile], [])])
    assert not do_wait(api, [R("x")])[0]
    api = FakeAPI([([hostile, run("x", id=1)], [])])
    assert not do_wait(api, [R("x")])[0]


def test_missing_id_on_a_required_run_fails():
    api = FakeAPI([([run("x", id=1), {"name": "x", "status": "completed", "conclusion": "success", "app": {"id": 1}}], [])])
    assert not do_wait(api, [R("x")])[0]


@pytest.mark.parametrize("bad_id", ["9", 1.5, True, None, -3])
def test_malformed_id_on_a_required_status_fails(bad_id):
    api = FakeAPI([([], [status("c", "success", id=2), {"context": "c", "state": "success", "id": bad_id, "creator": {"login": "bot"}}])])
    assert not do_wait(api, [S("c")])[0]
    api = FakeAPI([([], [{"context": "c", "state": "success", "creator": {"login": "bot"}}, status("c", "success", id=2)])])
    assert not do_wait(api, [S("c")])[0]


def test_malformed_id_on_an_unrelated_name_is_ignored():
    api = FakeAPI([([run("x", id=1), {"name": "other", "status": "completed", "conclusion": "failure", "id": "z", "app": {"id": 1}}], [])])
    assert do_wait(api, [R("x")])[0]


def test_tied_highest_id_takes_the_worst_state():
    api = FakeAPI([([run("x", id=4), run("x", conclusion="failure", id=4)], [])])
    assert not do_wait(api, [R("x")])[0]
    api = FakeAPI([([run("x", conclusion="failure", id=4), run("x", id=4)], [])])
    assert not do_wait(api, [R("x")])[0]
    api = FakeAPI([([], [status("c", "success", id=4), status("c", "failure", id=4)])])
    assert not do_wait(api, [S("c")])[0]
    api = FakeAPI([([run("x", status="in_progress", conclusion=None, id=4), run("x", id=4)], [])])
    ok, results = do_wait(api, [R("x")], timeout=10)
    assert not ok and results[0]["state"] == "pending"


@pytest.mark.parametrize("repo", ["org/.", "./x", "./.", "org/..", "../x", "../.."])
def test_dot_segments_in_repo_rejected(repo):
    with pytest.raises(InputError):
        wait(FakeAPI([([], [])]), repo, SHA, [], timeout=1, interval=1)
    with pytest.raises(InputError):
        snapshot(FakeAPI([([], [])]), repo, SHA, [])


def test_non_string_name_or_context_is_ignored_not_an_error():
    api = FakeAPI([([{"name": ["x"], "id": 1, "app": {"id": 1}}, run("x")], [{"context": {"a": 1}, "id": 1}, status("c", "success")])])
    ok, results = do_wait(api, [R("x"), S("c")])
    assert ok


def test_head_sha_mismatch_is_skipped():
    other = {**run("x", id=9, conclusion="failure"), "head_sha": "b" * 40}
    api = FakeAPI([([run("x", id=1), other], [])])
    assert do_wait(api, [R("x")])[0]
    mine = {**run("x", id=1), "head_sha": SHA}
    assert do_wait(FakeAPI([([mine], [])]), [R("x")])[0]
    only_other = {**run("x"), "head_sha": "b" * 40}
    assert not do_wait(FakeAPI([([only_other], [])]), [R("x")], timeout=10)[0]


def test_keyed_response_must_be_a_dict():
    class Bare(FakeResponse):
        pass

    api = GitHubAPI("https://api.github.com", "t", opener=lambda req, timeout: Bare([{"name": "x"}]))
    with pytest.raises(InputError):
        api.paged("/x", key="check_runs")


@pytest.mark.parametrize("base", ["https://api.github.com/x?", "https://api.github.com/x#", "https://api.github.com?", "https://api.github.com/#"])
def test_literal_query_or_fragment_marker_in_api_url_rejected(base):
    with pytest.raises(InputError):
        GitHubAPI(base, "t")


def test_wait_does_not_sleep_past_the_deadline():
    clock = Clock()
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock.sleep(seconds)

    api = FakeAPI([([], [])])
    ok, _ = wait(api, REPO, SHA, [R("a")], timeout=25, interval=10, sleep=sleep, clock=clock)
    assert not ok and sleeps == [10, 10, 5] and clock.t == 25


def test_write_text_atomic_mode_is_0644(tmp_path):
    import os
    import stat

    from golden_road.io import write_text_atomic

    old = os.umask(0o077)
    try:
        write_text_atomic(tmp_path / "o.json", "x")
    finally:
        os.umask(old)
    assert stat.S_IMODE((tmp_path / "o.json").stat().st_mode) == 0o644


# ---- transient errors ----

def _http_error(code):
    import urllib.error

    return urllib.error.HTTPError("https://api.github.com/x", code, "msg", {}, None)


class Scripted:
    """An opener that raises or returns each scripted item in turn."""

    def __init__(self, *items):
        self.items, self.calls = list(items), 0

    def __call__(self, req, timeout):
        item = self.items[min(self.calls, len(self.items) - 1)]
        self.calls += 1
        if isinstance(item, BaseException):
            raise item
        return item


def _api(opener, clock=None):
    clock = clock or Clock()
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock.sleep(seconds)

    return GitHubAPI("https://api.github.com", "t", opener=opener, sleep=sleep, clock=clock), sleeps


@pytest.mark.parametrize("failure", [_http_error(502), _http_error(500), _http_error(503), _http_error(429),
                                     __import__("urllib.error").error.URLError("reset"), TimeoutError("t"), ConnectionResetError()])
def test_transient_errors_are_retried(failure):
    opener = Scripted(failure, FakeResponse([1]))
    api, sleeps = _api(opener)
    assert api._get("https://api.github.com/x")[0] == [1]
    assert opener.calls == 2 and sleeps == [1]


@pytest.mark.parametrize("code", [400, 401, 403, 404, 422, 302])
def test_other_http_errors_are_not_retried(code):
    opener = Scripted(_http_error(code), FakeResponse([1]))
    api, sleeps = _api(opener)
    with pytest.raises(InputError):
        api._get("https://api.github.com/x")
    assert opener.calls == 1 and sleeps == []


def test_backoff_doubles_is_capped_and_ends_at_the_deadline():
    opener = Scripted(_http_error(502))
    api, sleeps = _api(opener)
    api.set_deadline(200)
    with pytest.raises(InputError):
        api._get("https://api.github.com/x")
    assert sleeps[:6] == [1, 2, 4, 8, 16, 30] and max(sleeps) <= 30
    assert sum(sleeps) <= 200 and sum(sleeps) >= 190


def test_invalid_json_is_not_retried():
    class Junk(FakeResponse):
        def read(self, n):
            return b"{"

    opener = Scripted(Junk([]))
    api, sleeps = _api(opener)
    with pytest.raises(InputError):
        api._get("https://api.github.com/x")
    assert opener.calls == 1


def test_wait_bounds_retries_by_its_own_timeout():
    clock = Clock()
    opener = Scripted(_http_error(503))
    api, sleeps = _api(opener, clock)
    with pytest.raises(InputError):
        wait(api, REPO, SHA, [R("x")], timeout=20, interval=5, sleep=clock.sleep, clock=clock)
    assert clock.t <= 20 + 1
