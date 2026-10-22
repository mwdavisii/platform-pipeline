import pytest

from builder.lock import NEVER_EDIT
from builder.render.agents import BEGIN, END, MANAGED_FILES, Block, block_text, find_block, merge, render_blocks


def test_blocks_are_marked_and_name_the_classification(notes_spec):
    blocks = render_blocks(notes_spec)
    assert set(blocks) == set(MANAGED_FILES) == {"AGENTS.md", "CLAUDE.md"}
    agents = blocks["AGENTS.md"]
    assert agents.startswith(BEGIN + "\n") and agents.endswith(END + "\n")
    assert "`internal`" in agents and "Never make this repository public." in agents
    for rel in NEVER_EDIT:
        assert f"`{rel}`" in agents
    assert blocks["CLAUDE.md"] == f"{BEGIN}\n@AGENTS.md\n{END}\n"


def test_block_text_depends_only_on_the_classification(notes_spec):
    other = {**notes_spec, "app_name": "other", "egress": [], "env": []}
    assert render_blocks(other) == render_blocks(notes_spec)
    assert render_blocks({**notes_spec, "data_classification": "phi"})["AGENTS.md"] != render_blocks(notes_spec)["AGENTS.md"]


def test_never_edit_lists_the_generated_files_and_dirs():
    assert NEVER_EDIT == ("Dockerfile", ".dockerignore", "deploy/base/", "_platform/", ".builder.lock")


@pytest.mark.parametrize("text, expected", [
    ("# notes\n", Block("missing", 0, 0)),
    (f"a\n{BEGIN}\nx\n{END}\nb\n", Block("ok", 1, 3)),
    (f"  {BEGIN}  \n{END}\n", Block("ok", 0, 1)),
    (f"{BEGIN}\n{END}\n{BEGIN}\n{END}\n", Block("duplicated", 2, 2)),
    (f"{BEGIN}\n{BEGIN}\n{END}\n", Block("duplicated", 1, 1)),
    (f"{END}\n{BEGIN}\n", Block("unmatched", 0, 0)),
    (f"x\n{BEGIN}\nno end\n", Block("unmatched", 1, 1)),
    (f"see the `{BEGIN}` marker\n", Block("missing", 0, 0)),
])
def test_find_block(text, expected):
    assert find_block(text) == expected


def test_block_text_includes_both_marker_lines():
    text = f"intro\n{BEGIN}\nbody\n{END}\noutro\n"
    assert block_text(text, find_block(text)) == f"{BEGIN}\nbody\n{END}\n"


def test_merge_creates_inserts_and_replaces():
    block = f"{BEGIN}\nnew\n{END}\n"
    assert merge(None, block) == block
    assert merge("  \n", block) == block
    assert merge("# Ours\n", block) == block + "\n# Ours\n"
    assert merge(f"top\n{BEGIN}\nold\n{END}\nbottom\n", block) == f"top\n{block}bottom\n"
    assert merge(f"{BEGIN}\nold\n{END}", block) == block


def test_merge_is_idempotent():
    block = f"{BEGIN}\nnew\n{END}\n"
    once = merge("# Ours\n", block)
    assert merge(once, block) == once


@pytest.mark.parametrize("text", [f"{BEGIN}\n{END}\n{BEGIN}\n{END}\n", f"{END}\n{BEGIN}\n", f"{BEGIN}\n"])
def test_merge_refuses_duplicated_or_unmatched_markers(text):
    with pytest.raises(ValueError):
        merge(text, f"{BEGIN}\nnew\n{END}\n")


def test_a_marker_quoted_in_a_prose_line_next_to_a_block_does_not_count():
    text = f"{BEGIN}\nbody\n{END}\nthe end marker is `{END}` and the begin is {BEGIN} too\n"
    assert find_block(text) == Block("ok", 0, 2)
    assert block_text(text, find_block(text)) == f"{BEGIN}\nbody\n{END}\n"


BOM = "﻿"


@pytest.mark.parametrize("text", [f"{BOM}{BEGIN}\nold\n{END}\n", f"{BOM}{BEGIN}\nold\n{END}\nours\n"])
def test_a_leading_bom_does_not_hide_the_block(text):
    block = f"{BEGIN}\nnew\n{END}\n"
    assert find_block(text) == Block("ok", 0, 2)
    assert block_text(text, find_block(text)) == f"{BEGIN}\nold\n{END}\n"
    merged = merge(text, block)
    assert merged.startswith(BOM + BEGIN) and merged.count(BOM) == 1 and f"\nnew\n" in merged


@pytest.mark.parametrize("text", [f"{BOM}# Ours\n", f"{BOM}\n# Ours\n"])
def test_a_bom_file_without_markers_keeps_the_bom_at_the_front(text):
    block = f"{BEGIN}\nnew\n{END}\n"
    merged = merge(text, block)
    assert merged.startswith(BOM + BEGIN + "\n") and merged.count(BOM) == 1
    assert merged.endswith("# Ours\n")
    assert merge(merged, block) == merged


@pytest.mark.parametrize("sep", ["\x0c", "\x1c", "\x1d", "\x1e", "\x85", " ", " "])
def test_only_newline_splits_lines(sep):
    team = f"before{sep}{BEGIN}{sep}after\n"
    assert find_block(team) == Block("missing", 0, 0)
    text = f"top{sep}line\n{BEGIN}\nold\n{END}\nbottom{sep}line\n"
    assert find_block(text) == Block("ok", 1, 3)
    assert merge(text, f"{BEGIN}\nnew\n{END}\n") == f"top{sep}line\n{BEGIN}\nnew\n{END}\nbottom{sep}line\n"


def test_crlf_files_keep_working():
    text = f"top\r\n{BEGIN}\r\nold\r\n{END}\r\nbottom\r\n"
    assert find_block(text) == Block("ok", 1, 3)


LANDING = "https://acme.example.org/sites/platform/golden-road"
MODELS = "https://acme.example.org/sites/ai/approved-models?view=all"


def test_without_links_the_block_keeps_the_placeholder_sentences(notes_spec):
    assert render_blocks(notes_spec, {}) == render_blocks(notes_spec) == render_blocks(notes_spec, None)
    agents = render_blocks(notes_spec)["AGENTS.md"]
    assert "the golden-road landing page (the platform team has the link)" in agents and "is not published yet" in agents


def test_the_platform_config_s_links_go_into_the_block(notes_spec):
    agents = render_blocks(notes_spec, {"landing_page_url": LANDING, "approved_models_url": MODELS})["AGENTS.md"]
    assert f"Install it from the golden-road landing page: <{LANDING}>." in agents
    assert f"Use only the models approved for `internal` data: <{MODELS}>." in agents
    assert "not published yet" not in agents and "(the platform team has the link)" not in agents


@pytest.mark.parametrize("bad", ["http://acme.example.org/x", "https://acme.example.org/a b", "javascript:alert(1)", "https://x.org/`y`", 7,
                                 "https://x.org/a-->b", "https://x.org/a>b", "https://x.org/[a](b)", "https://x.org/a\nb", 'https://x.org/"a"', "https://x.org/<!--"])
def test_a_link_that_is_not_a_plain_https_url_is_ignored(notes_spec, bad):
    assert render_blocks(notes_spec, {"landing_page_url": bad, "approved_models_url": bad}) == render_blocks(notes_spec)


def test_generate_and_verify_use_the_platform_config_s_links(tmp_path, monkeypatch, notes_spec, python_app):
    import json
    from builder.config import load_platform
    from builder.generate import generate
    from builder.verify import verify

    config = tmp_path / "platform.json"
    config.write_text(json.dumps({**load_platform(), "landing_page_url": LANDING}))
    monkeypatch.setenv("BUILDER_PLATFORM_CONFIG", str(config))
    generate(python_app, notes_spec)
    assert f"<{LANDING}>" in (python_app / "AGENTS.md").read_text()
    assert [f["rule_id"] for f in verify(python_app) if f["rule_id"].startswith("builder.managed-block")] == []
