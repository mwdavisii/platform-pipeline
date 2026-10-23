import json

import pytest

from scorecard.signals.lock_diff import builder_bumped


def _lock(version):
    return json.dumps({"builder_version": version, "files": {}})


def test_a_new_builder_version_is_recorded_with_both_versions():
    assert builder_bumped(_lock("0.3.7"), _lock("0.3.8")) == [
        {"id": "builder-bumped", "kind": "recorded", "summary": "builder 0.3.7 -> 0.3.8"}]


@pytest.mark.parametrize("base, head", [
    (None, _lock("0.3.8")),
    (_lock("0.3.7"), None),
    (None, None),
    (_lock("0.3.8"), _lock("0.3.8")),
    ("{not json", _lock("0.3.8")),
    (_lock("0.3.7"), json.dumps(["0.3.8"])),
    (_lock("0.3.7"), _lock("0.3.8; echo hi")),
    (_lock("0.3.7"), json.dumps({"builder_version": 38})),
])
def test_nothing_is_recorded_unless_both_sides_name_different_versions(base, head):
    assert builder_bumped(base, head) == []
