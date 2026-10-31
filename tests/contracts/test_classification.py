"""The three data classification levels and the retired ones (spec 2026-10-30, C1 to C4)."""

import json

import pytest

from contracts.classification import (LEVELS, MEANS, POLICY_PLACEHOLDER, RETIRED, policy_link_ok, retired_message, retired_problems,
                                       retired_values)


def test_the_levels_least_to_most_sensitive():
    assert LEVELS == ("public", "confidential", "protected")
    assert list(MEANS) == list(LEVELS) and all(MEANS[level] for level in LEVELS)
    assert MEANS["public"] == "anyone may see it"
    assert MEANS["confidential"] == "staff and the groups that need it"
    assert MEANS["protected"] == "regulated or highly sensitive data, named people only"
    assert POLICY_PLACEHOLDER == "https://policies.example.com/data-classification"


def test_the_mapping_mike_chose():
    assert RETIRED == {"internal": "confidential", "restricted": "confidential", "phi": "protected"}
    assert retired_message("internal") == "`internal` is retired: use `confidential`"


@pytest.mark.parametrize("doc, found", [
    ({"data_classification": "internal"}, ["internal"]),
    ({"floor_classification": "phi", "data_classification": "public"}, ["phi"]),
    ({"classification_tier": {"public": "low", "internal": "low", "restricted": "high"}}, ["internal", "restricted"]),
    ({"no_spec": {"tier": "high", "data_classification": "restricted"}}, ["restricted"]),
    ({"sensitive_classifications": ["restricted", "phi"]}, ["restricted", "phi"]),
    ({"rules": [{"escalate": {"classifications": ["phi"], "level": "security_review"}}]}, ["phi"]),
    ({"app": {"data_classification": "internal"}, "other": {"data_classification": "internal"}}, ["internal"]),
])
def test_retired_values_are_found_where_a_classification_goes(doc, found):
    assert retired_values(doc) == found


@pytest.mark.parametrize("doc", [
    {"purpose": "internal tools for the phi team"},
    {"egress": [{"host": "litellm.internal", "port": 443}]},
    {"registry": {"internal_packages": {"npm": ["internal"]}}},
    {"data_classification": "confidential"},
    "internal", ["phi"], None, 7,
])
def test_the_word_elsewhere_is_not_a_classification(doc):
    assert retired_values(doc) == []


def test_problems_name_the_replacement():
    assert retired_problems({"data_classification": "phi"}) == ["`phi` is retired: use `protected`"]
    assert retired_problems({"data_classification": "protected"}) == []


def test_a_deeply_nested_document_is_walked_without_recursion():
    """A 6 KB spec can nest 3000 deep and still parse: the walk must not use the call stack (review C1)."""
    deep = json.loads('{"data_classification": ' + "[" * 3000 + '"phi"' + "]" * 3000 + "}")
    assert retired_values(deep) == ["phi"] and retired_values(json.loads("[" * 3000 + "]" * 3000)) == []


GOOD_LINKS = [POLICY_PLACEHOLDER, "https://wiki.example.org/policies/data?id=7", "https://wiki.example.org:8443/p#levels",
              "https://Wiki.Example.org", "https://wiki.example.org/" + "a" * 2000]
BAD_LINKS = ["http://wiki.example.org/p", "javascript:alert(1)", "https://user@wiki.example.org/p", "https://user:pw@wiki.example.org/p",
             "https://wiki.example.org/a b", "https://wiki.example.org/a\nb", "https://wiki.example.org/p\n", " https://wiki.example.org/p",
             "https://wiki.example.org/p ", 'https://wiki.example.org/"x', "https://wiki.example.org/'x", "https://wiki.example.org/<x>",
             "https://wiki.example.org/\x01", "https://wiki.example.org/" + "a" * 2030, "https://localhost/p", "https://wiki..example.org/",
             "https://wiki.example.org:99999/p", "https://-wiki.example.org/", "", None, 7]


@pytest.mark.parametrize("url", GOOD_LINKS)
def test_a_policy_link_is_https_with_a_host(url):
    assert policy_link_ok(url) and len(url) <= 2048


@pytest.mark.parametrize("url", BAD_LINKS)
def test_anything_else_is_not_a_policy_link(url):
    assert not policy_link_ok(url)


@pytest.mark.parametrize("url", GOOD_LINKS + BAD_LINKS[:-2])
def test_the_guide_schema_s_pattern_agrees_with_the_rule(url):
    from contracts.schema import validate
    import builder.guide
    doc = builder.guide.guide()
    doc["data_classification"]["policy_url"] = url
    assert (validate("builder-guide.v1", doc) == []) is policy_link_ok(url), url
