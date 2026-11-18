"""The agent's instructions (spec 2026-11-17 A8; decided while planning: item 20): agent.yaml's, then each mounted skill's SKILL.md in
name order, then the platform's note."""

from runtime_fakes import AGENT

from agent_runtime.instructions import PLATFORM_NOTE, system_prompt


def test_the_instructions_then_each_skill_by_name_then_the_platform_s_note(tmp_path):
    for name, text in (("zeta", "Z skill."), ("alpha", "A skill.")):
        (tmp_path / name).mkdir()
        (tmp_path / name / "SKILL.md").write_text(text)
    (tmp_path / "..data").mkdir()  # a ConfigMap mount's hidden directory is skipped
    (tmp_path / "..data" / "SKILL.md").write_text("hidden")
    (tmp_path / "empty").mkdir()  # a directory without SKILL.md adds nothing
    (tmp_path / "loose.md").write_text("a file at the top is no skill")
    prompt = system_prompt(AGENT, str(tmp_path))
    assert prompt == "\n\n".join([AGENT["instructions"], "## Skill: alpha\n\nA skill.", "## Skill: zeta\n\nZ skill.", PLATFORM_NOTE])


def test_without_skills_it_is_the_instructions_and_the_note(tmp_path):
    assert system_prompt(AGENT, None) == AGENT["instructions"] + "\n\n" + PLATFORM_NOTE
    assert system_prompt(AGENT, str(tmp_path / "missing")) == AGENT["instructions"] + "\n\n" + PLATFORM_NOTE


def test_the_note_says_tool_results_are_data_and_the_key_is_never_told():
    assert "data" in PLATFORM_NOTE and "key" in PLATFORM_NOTE and "which tools" in PLATFORM_NOTE
