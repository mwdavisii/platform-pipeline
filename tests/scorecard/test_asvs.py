import hashlib
from importlib import resources

import pytest

from golden_road.io import InputError
from scorecard import asvs
from scorecard.asvs import ASVS_SHA256, check_ids, for_level, in_any, load_asvs, matches, parse_asvs, versioned

HEADER = "chapter_id,chapter_name,section_id,section_name,req_id,req_description,L\n"


def test_vendored_file_matches_the_recorded_checksum():
    data = resources.files("scorecard").joinpath("standards", "asvs-5.0.0.csv").read_bytes()
    assert hashlib.sha256(data).hexdigest() == ASVS_SHA256


def test_loads_the_real_export():
    requirements = load_asvs()
    assert len(requirements) == 345
    by_id = {r["id"]: r for r in requirements}
    idor = by_id["v5.0.0-8.2.2"]
    assert idor["chapter"] == "v5.0.0-8" and idor["chapter_name"] == "Authorization"
    assert idor["section"] == "v5.0.0-8.2" and idor["section_name"] == "General Authorization Design"
    assert idor["level"] == 1 and "insecure direct object reference" in idor["text"]
    assert by_id["v5.0.0-13.3.1"]["section_name"] == "Secret Management"


def test_blocking_chapter_names_are_the_real_ones():
    names = asvs.chapter_names(load_asvs())
    assert names["v5.0.0-6"] == "Authentication"
    assert names["v5.0.0-7"] == "Session Management"
    assert names["v5.0.0-8"] == "Authorization"
    assert names["v5.0.0-9"] == "Self-contained Tokens"
    assert names["v5.0.0-13"] == "Configuration"


def test_level_filter_is_cumulative():
    requirements = load_asvs()
    assert [len(for_level(requirements, n)) for n in (1, 2, 3)] == [70, 253, 345]


def test_a_tampered_file_is_refused(monkeypatch):
    monkeypatch.setattr(asvs, "ASVS_SHA256", "0" * 64)
    with pytest.raises(InputError, match="SHA-256"):
        load_asvs()


@pytest.mark.parametrize(
    "text",
    [
        "chapter,name\nV1,x\n",
        HEADER,
        HEADER + "V1,Enc,V1.1,Arch,V2.1.1,Verify something.,1\n",
        HEADER + "V1,Enc,V1.1,Arch,V1.1.1,Verify something.,4\n",
        HEADER + "V1,Enc,V1.1,Arch,V1.1.1,,1\n",
        HEADER + "V1,Enc,V1.1,Arch,V1.1.1,Verify a.,1\nV1,Enc,V1.1,Arch,V1.1.1,Verify b.,1\n",
    ],
)
def test_malformed_csv_is_refused(text):
    with pytest.raises(InputError):
        parse_asvs(text)


def test_ids_and_selectors():
    assert versioned("V13.3.1") == "v5.0.0-13.3.1"
    assert versioned("V13") == "v5.0.0-13"
    assert matches("v5.0.0-13.3.1", "v5.0.0-13.3")
    assert not matches("v5.0.0-13.3.1", "v5.0.0-1")  # a chapter prefix is not a string prefix
    assert in_any("v5.0.0-8.2.2", ["v5.0.0-6", "v5.0.0-8"])


def test_config_ids_must_exist(config):
    requirements = load_asvs()
    check_ids(requirements, config, [])
    config["blocking"] = config["blocking"] + ["v5.0.0-13.9"]
    with pytest.raises(InputError, match="v5.0.0-13.9"):
        check_ids(requirements, config, [])


def test_na_entries_must_exist(config):
    with pytest.raises(InputError, match="v5.0.0-6.99.1"):
        check_ids(load_asvs(), config, [{"asvs_id": "v5.0.0-6.99.1"}])
