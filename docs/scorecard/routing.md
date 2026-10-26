# Routing: what sends a pull request to a person

This is for the security team, who own `config/routing.toml` in `scorecard-factory`. The file decides which pull requests and apps the scorecard sends to a person, and why. Changing it needs no builder release: it is a pull request on the factory. Design and reasons: `docs/superpowers/specs/2026-10-24-risk-routing-design.md`. Rollout steps: `docs/setup/personal-poc.md` section 17.

Routing is on when the factory has `config/routing.toml`. It then needs `config/scorecard.yaml` to be `scorecard-config/v2` with `agent.prompt_version: review-2`. Without `routing.toml` the scorecard runs the older ASVS checklist (`scorecard-config/v1`, `review-1`), and a `review-2` prompt without `routing.toml` is refused.

## Facts and rules

A **fact** says what the app or the change is. A **rule** says which facts need a person. Detectors make facts and never pick a level.

The level of a pull request is the highest of the intake floor and every enforced rule that fired. A rule can only raise the level. If something can't be read, the review doesn't finish, or an answer is bad, the result goes to a person (T-14, or `unsure`), never to `standard`.

The starting file's T-22 rule, with its fact:

```toml
[facts.own_credentials]
from = "agent"
applies_to = "tree"
question = "Does the application issue, store or check its own API keys, tokens or sessions, instead of relying on the platform identity header for who the caller is and what role they have?"
must_check = ["token", "api_key", "apikey", "session", "jwt", "bearer", "password"]

[[rules]]
id = "T-22"
when = { fact = "own_credentials", is = true }
level = "security_review"
on_unsure = "elevated"
mode = "shadow"
summary = "The app checks its own credentials"
reason = "The app issues or checks its own API keys, tokens or sessions, so the platform guard can't enforce read and write for them."
guidance = "Use the platform identity header (the caller's role comes from the edge) instead of app-issued tokens. If the app must issue tokens, this needs a security review; nothing in the PR clears it."
```

`summary` is the one line in the comment, `reason` says why, and `guidance` says what clears it (the app team's coding agent reads it, so write it as an instruction). Rule ids are `T-<number>` (`T-1` to `T-999`), unique. The built-in rules keep their numbers; new rules start at T-22 and use the next free number.

## The four fact kinds

| `from` | Made by | Value |
|---|---|---|
| `builtin` | Code: a detector that ships in the builder, named by `measure` | true or false, with the files or lines that caused it |
| `spec` | Code: a measure over `builder-spec.json` at the base and at the head | a number, an enum value or a list, at both |
| `diff` | Code: `paths` globs over the changed files, and optionally `added_lines` regexes over the lines the change adds | true or false, with refs |
| `agent` | The review agent, in one conversation. Code checks every answer | `yes`, `no` or `unsure`, with evidence |

An `agent` fact has `question`, `applies_to` and `must_check`. `applies_to = "tree"` is about the whole app, so the first reviewed pull request routes a risky app even if the change is small. `applies_to = "change"` is about this diff only. `must_check` is a list of fixed search terms (the `grep` tool matches fixed text, ignoring case; no regex, and no commas or leading or trailing spaces in a term).

### How code checks an agent's answer

The answer is never final on its own (`src/scorecard/routing/answers.py`). Anything that fails a check becomes `unsure`, with the reason recorded.
- `yes` needs citations that verify against the file. For a `change` question at least one cited file must be one this pull request changed.
- `no` can't cite an absence, so every `must_check` term needs a successful `grep` over the whole tree (glob `*`) in the tool log the harness recorded. The searches that ran are listed in `scorecard.json`.
- `unsure`, a missing answer, or no assessment is `unsure`. A review that did not finish accepts no answer at all.
- Notes the agent writes on its evidence are redacted before they are stored.
- Each question (text, `applies_to`, `must_check`) is hashed. Changing a question is a new question, and `scorecard run` refuses an assessment made with other questions: the next push assesses again.

## Built-in measures

`from = "builtin"`, `measure = ...`. Each is true or false. Two fill extra placeholders in a rule's texts.

- `spec_security_change`: builder-spec.json changed in a part that decides who can call the app, what it reaches or what secrets it gets.
- `tests_weakened`: tests deleted, skipped or weakened, by the code's own diff check.
- `risky_dependency`: a new dependency that is very new, unknown to the registry, or has few maintainers.
- `reviewer_text`: text aimed at the reviewer found by the pre-scan (`detectors.patterns.injection`).
- `no_intake`: the platform has no intake record for the repo.
- `purpose_changed`: `PURPOSE.md` changed.
- `input_problem`: an input could not be read or looked up.
- `review_incomplete`: the agent's review did not finish.
- `suppression_added`: a scanner suppression was added or widened.
- `workflow_change`: a workflow, CODEOWNERS or harness-shadowing file changed.
- `tier_raised`: the resource tier went up.
- `app_type_changed`: the app type changed.
- `large_change`: the change is over the `detectors.large_change` size. Fills `{lines}` and `{files}`.
- `below_registration`: the app looks more sensitive than its registration. Fills `{registered}`, `{needed}` and `{signals}`.

## Spec measures

`from = "spec"`, `measure = ...`. The value type decides which conditions fit.

- `data_classification` (ordered: `public`, `internal`, `restricted`, `phi`)
- `resources_tier` (ordered: `default`, `medium`)
- `app_type` (enum: `python`, `node`, `static`)
- `role_count` (number)
- `write_role_count` (number)
- `read_role_write_grants` (grants): rules that give a role with `access = "read"` a write method. Fills `{justification}`.
- `secret_env` (list of names)
- `egress_hosts` (list of hosts other than the model gateway)

Spec facts also fill `{value}`, `{base}` and `{head}`. Every fact fills `{count}`; an `agent` fact fills `{answer}` too. A literal brace is written `{{` or `}}`.

## Conditions

`when` is one condition or a list of them. A list fires when any one condition does. A condition is `fact` plus exactly one of:

| Condition | Fits | Meaning |
|---|---|---|
| `is` | `builtin` and `diff` facts, `agent` facts (true or false), enum and ordered measures (a value) | equality |
| `any` | enum, ordered and list measures | the value is in the list, or a list fact shares an item with it |
| `over`, `at_least` | number, list and grants measures | greater than, or at least, a count |
| `changed` | `spec` facts only: `any` (enum, ordered, number, list, grants), `up` (ordered and number), `added` (list and grants) | head differs from base |

Nothing else is allowed: there is no expression language.

## Rule fields

- `level`: `standard`, `elevated` or `security_review`.
- `on_unsure`: what an `unsure` agent answer does. `elevated` routes at elevated with the reason "the reviewer couldn't determine ...". `rule_level` routes at the rule's `level`. `standard` records it in `routing.undecided` and routes nothing. It is required on a rule over an agent fact and refused on any other.
- `mode`: `enforce` (the default) or `shadow`. A shadow rule never changes the level and never appears in the PR comment. It appears in the job summary and in `scorecard.json` under `routing.shadow`.
- `escalate`: `{ classifications = ["restricted", "phi"], level = "security_review" }` raises the level when the app's data classification is listed (T-10 uses it).
- `summary`, `reason`, `guidance`: texts, with the placeholders above.

## Add a rule safely

1. Add the fact (and, for a new agent question, its `must_check` terms) and the rule with `mode = "shadow"`. Give `on_unsure` if it is an agent rule.
2. For a new agent question, add golden cases in `tests/scorecard/golden_routing/cases.yaml`: a pull request where the answer is `yes` and one where it is `no`, and the exact `fired` and `shadow` lists you expect. Run them (below). Cases whose name starts with `false-alarm-` must come out `standard` with nothing fired and nothing in shadow, so add one if the question might trip on ordinary code.
3. Run `scorecard check-factory --factory <factory dir>` and fix every refusal. Warnings are not refusals but read them: a fact that no rule uses is printed as `fact <name> is not used by any rule`.
4. Open the factory pull request; security approves it (CODEOWNERS) and it merges once `factory-check` passes.
5. Watch `routing.shadow` in `scorecard.json` on real pull requests (the `scorecard` artifact of the run, or the job summary). Check the rule fires where you want and not elsewhere. Also look at `routing.undecided`: rules over agent facts with `on_unsure = "standard"` record there the answers that were `unsure`, so a question that is mostly unsure is not working.
6. A second pull request sets `mode = "enforce"`. To pull a rule back, set it to `shadow` again.

### Running the golden cases

In fake mode (a fixed fake model, exact results; part of `tests/scorecard`, no credentials):
```bash
.venv/bin/pytest tests/scorecard/test_routing_golden.py -q
```
Live, against a real gateway (opt-in, costs tokens; it is skipped unless `GOLDEN_ROAD_SCORECARD_LIVE=1`, and it fails if `GOLDEN_ROAD_SCORECARD_FACTORY` is unset):
```bash
GOLDEN_ROAD_SCORECARD_LIVE=1 GOLDEN_ROAD_SCORECARD_FACTORY=<factory dir with config/routing.toml> \
  SCORECARD_GATEWAY_TOKEN=<token> SCORECARD_GATEWAY_MODEL=<model> \
  PYTHONPATH=src .venv/bin/pytest -s tests/scorecard/test_routing_golden_live.py
```
Set `GOLDEN_ROAD_SCORECARD_REPORT=<path>` to also write the report, whose file name gets a `routing-` prefix. The live run asserts nothing about answers: it prints what the model said, its tokens, and true and false positives per fact. Read it before enforcing a rule.

## What `scorecard check-factory` refuses

The same loader runs in `factory-check` on every factory pull request, so a bad change fails there, not in an app's review.
- An unknown key, a wrong type, or a duplicate fact or rule id (schema `src/contracts/schemas/routing.v1.json`).
- A rule naming a fact that does not exist.
- A `measure` outside the lists above.
- A condition that doesn't fit the fact's type (for example `over` on a boolean, or `changed = "up"` on a list), and an enum value that is not one the measure has.
- A placeholder a rule's fact can't fill, or a stray brace.
- `on_unsure` missing on a rule over an agent fact, or present on any other.
- An invalid regex in `[detectors.patterns]` or a `diff` fact's `added_lines`.
- A `must_check` term listed twice (ignoring case), and an empty `question` or `reason`.
- A file over 262144 bytes, a symlink, or something that is not valid TOML.
- A `scorecard.yaml` whose prompt does not match: `review-2` needs `routing.toml`, and `review-1` refuses it.

It warns, without failing, about a fact no rule uses.

## What is code, not configuration

You can change what each check looks at, how much it matters and what it says; add `agent` questions and `diff` facts; and run anything in shadow first. These need a builder release:
- a new kind of measure, or how a built-in or spec measure is computed (how a test is judged weakened, how a lockfile is compared);
- the review prompt (`src/scorecard/prompts/review-2.md`, frozen by checksum);
- the evidence checks of `answers.py`, and the rule operators.
