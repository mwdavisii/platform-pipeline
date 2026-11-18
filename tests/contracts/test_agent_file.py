"""agent/v1 (spec 2026-11-17 "agent.yaml"): what agent_file.load accepts and refuses, and that dump reads back as it was written."""

import json
from importlib import resources

import pytest

from contracts import agent_file
from contracts.agent_file import AgentFileError, dump, load
from contracts.schema import validate

GOOD = b"""schema: agent/v1
display_name: Calendar helper
model: chat-default
instructions: |
  You help the team find free time across calendars.
  Say which calendars you read.
starters:
  - When is everyone free on Thursday?
"""


def test_a_good_file_loads():
    doc = load(GOOD, models=["chat-default"])
    assert doc == {"schema": "agent/v1", "display_name": "Calendar helper", "model": "chat-default",
                   "instructions": "You help the team find free time across calendars.\nSay which calendars you read.\n",
                   "starters": ["When is everyone free on Thursday?"]}


def test_the_contracts_schema_is_the_module_s():
    path = resources.files("contracts") / "schemas" / "agent.v1.json"
    assert json.loads(path.read_text(encoding="utf-8")) == agent_file.SCHEMA
    assert validate("agent.v1", load(GOOD)) == []


@pytest.mark.parametrize("raw, words", [
    (GOOD.replace(b"model: chat-default", b"model: chat-default\ncolour: blue"), "a key agent/v1 doesn't know"),
    (GOOD.replace(b"schema: agent/v1", b"schema: agent/v2"), "schema must be agent/v1"),
    (GOOD.replace(b"display_name: Calendar helper\n", b""), "is missing a required key"),
    (GOOD.replace(b"Calendar helper", b"x" * 65), "display_name is too long (at most 64 characters)"),
    (GOOD.replace(b"Calendar helper", b"'   '"), "display_name is blank"),
    (GOOD.replace(b"Calendar helper", b'"Cal\\aendar"'), "display_name has a control character"),  # YAML's \a is BEL (0x07)
    (GOOD.replace(b"chat-default", b"Chat Default"), "model has a character it can't hold"),
    (b"schema: agent/v1\ndisplay_name: A\nmodel: m\ninstructions: ''\n", "instructions is empty"),
    (b"schema: agent/v1\ndisplay_name: A\nmodel: m\ninstructions: " + b"x" * 8001 + b"\n", "instructions is too long (at most 8000 characters)"),
    (GOOD.replace(b"  - When", b"  - a\n  - b\n  - c\n  - d\n  - When"), "starters has more than 4 items"),
    (GOOD.replace(b"When is everyone free on Thursday?", b"x" * 201), "starters.0 is too long (at most 200 characters)"),
    (GOOD.replace(b"display_name: Calendar helper", b"display_name: &n Calendar helper"), "anchors or aliases"),
    (GOOD + b"model: other\n", "names a key twice"),
    (b"? [a]\n: x\n", "has a key that isn't text"),  # a complex key is unhashable in a Python dict
    (b"? {a: 1}\n: x\n", "has a key that isn't text"),
    (GOOD + b"1: one\n", "has a key that isn't text"),
    (GOOD.replace(b"Calendar helper", b'"Cal\\x85endar"'), "display_name has a control character"),  # NEL, a C1 control
    (GOOD.replace(b"Calendar helper", b'"Cal\\u2028endar"'), "display_name has a control character"),
    (b"schema: agent/v1\ndisplay_name: A\nmodel: m\ninstructions: \"x\\n\\x85y\"\n", "instructions has a control character"),
    (b"schema: agent/v1\ndisplay_name: A\nmodel: m\ninstructions: \"x\\x9fy\"\n", "instructions has a control character"),
    (GOOD.replace(b"Calendar helper", b'"Cal\\ud800endar"'), "display_name has an unpaired surrogate"),
    (b"schema: agent/v1\ndisplay_name: A\nmodel: m\ninstructions: \"x\\udfffy\"\n", "instructions has an unpaired surrogate"),
    (GOOD.replace(b"When is everyone", b'"\\ud800 When is everyone').replace(b"Thursday?", b'Thursday?"'), "starters.0 has an unpaired surrogate"),
    (GOOD.replace(b"model: chat-default", b'model: "chat-default\\n"'), "model has a control character"),
    (GOOD.replace(b"chat-default", b"m" * 65), "model is too long (at most 64 characters)"),
    (b"---\n---\n" + GOOD, "one YAML document"),
    (b"null\n---\n" + GOOD, "one YAML document"),
    (GOOD + b"---\n", "one YAML document"),
    (b"- a list\n", "exactly one mapping"),
    (b"a: [\n", "not valid YAML"),
    (b"\xff\xfe", "not UTF-8"),
    (GOOD + b"#" * (16 * 1024), "larger than 16 KiB"),
])
def test_what_is_refused_names_the_field_never_the_text(raw, words):
    with pytest.raises(AgentFileError) as caught:
        load(raw)
    assert words in str(caught.value)
    assert "Calendar" not in str(caught.value) and "Thursday" not in str(caught.value) and "model'" not in str(caught.value)


@pytest.mark.parametrize("raw", [b"? [a]\n: x\n", b"? {a: 1}\n: x\n", b"schema: agent/v1\n? [a, [b]]\n: x\n", b"a: {? [b]\n: c}\n",
                                 b"!!python/object:os.system x\n", b"a: !!binary =\n", b"a: !!timestamp 2026-13-45\n",
                                 b"a: !!int x\n", b"a: !!float x\n", b"? !!set {a}\n: x\n", b"[" * 5000])
def test_load_raises_only_agent_file_error(raw):
    with pytest.raises(AgentFileError):
        load(raw)


def test_only_bytes_are_read_and_the_size_is_its_own_refusal():
    with pytest.raises(AgentFileError, match="must be read as bytes"):
        load(GOOD.decode("utf-8"))
    with pytest.raises(AgentFileError, match="larger than 16 KiB"):
        load(b"#" * (16 * 1024 + 1))


def test_a_model_outside_the_platform_s_list_is_refused_only_when_the_list_is_given():
    assert load(GOOD)["model"] == "chat-default"
    with pytest.raises(AgentFileError) as caught:
        load(GOOD, models=["other"])
    assert str(caught.value) == "agent.yaml: model is not one of the platform's models (other)"  # never the file's value


@pytest.mark.parametrize("instructions", ["one line", "two\nlines\n", "trailing spaces  \nnext", "\ttabbed\n", "ends without newline\nx",
                                          "unicode: café ✓", "a: colon and # hash", "line\u2028separator",
                                          "paragraph\u2029separator", "zero\u200bwidth"])
def test_dump_reads_back_as_written(instructions):
    doc = {"schema": "agent/v1", "display_name": "A", "model": "m", "instructions": instructions, "starters": ["Hi?"]}
    text = dump(doc, "# header\n")
    assert text.startswith("# header\nschema: agent/v1\ndisplay_name: A\n")
    assert load(text.encode("utf-8")) == doc


def test_dump_keeps_the_schema_s_key_order_and_writes_a_literal_block():
    text = dump({"starters": [], "instructions": "a\nb\n", "model": "m", "display_name": "A", "schema": "agent/v1"})
    assert text == "schema: agent/v1\ndisplay_name: A\nmodel: m\ninstructions: |\n  a\n  b\nstarters: []\n"


def test_every_character_agent_v1_accepts_dumps_and_reads_back():
    """A C1 NEL (\\x85) once read back from a literal block as a line break; every accepted text must survive dump then load."""
    chars = [chr(c) for c in [*range(0x00, 0x250), *range(0x2000, 0x2070), 0x3000, 0xfeff, 0xfffd, 0x1f600]]
    for c in chars:
        for instructions in (f"x\n{c}y", f"{c}x", f"x{c}", f"x{c}\n"):
            doc = {"schema": "agent/v1", "display_name": "A", "model": "m", "instructions": instructions}
            if agent_file._VALIDATOR.is_valid(doc):
                assert load(dump(doc).encode("utf-8")) == doc, hex(ord(c))
            else:  # dump writes only what load accepts: a NEL would be written and read back as a different, valid text
                with pytest.raises(AgentFileError, match="isn't valid agent/v1"):
                    dump(doc)


# ---- problems: a document checked as it is, before it is written (the portal's form) ----

DOC = {"schema": "agent/v1", "display_name": "Calendar helper", "model": "chat-default", "instructions": "Help.", "starters": []}


def test_problems_is_none_for_a_valid_document_and_names_the_field_otherwise():
    assert agent_file.problems(DOC) is None and agent_file.problems(DOC, models=["chat-default"]) is None
    assert agent_file.problems({**DOC, "instructions": "Help \U0001F600.\x07"}) == "agent.yaml: instructions has a control character"
    assert agent_file.problems({**DOC, "instructions": "\u00e9" * 3000 + "\x00"}) == "agent.yaml: instructions has a control character"
    assert agent_file.problems({**DOC, "starters": ["Hi\x1b"]}) == "agent.yaml: starters.0 has a control character"
    assert agent_file.problems({**DOC, "colour": "blue"}).startswith("agent.yaml: the file has a key agent/v1 doesn't know")
    assert agent_file.problems({**DOC, "display_name": "a\ud800"}) == "agent.yaml: display_name has an unpaired surrogate (text that isn't valid Unicode)"
    assert agent_file.problems(DOC, models=["other"]) == "agent.yaml: model is not one of the platform's models (other)"
    assert agent_file.problems(["not", "a", "mapping"]) == "agent.yaml must hold exactly one mapping"


def test_load_and_problems_agree():
    with pytest.raises(AgentFileError) as caught:
        load(GOOD.replace(b"Calendar helper", b"\"Calendar\\x07\""))
    assert str(caught.value) == agent_file.problems({**load(GOOD), "display_name": "Calendar\x07"})
