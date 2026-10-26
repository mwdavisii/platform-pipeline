# Risk routing: send risky PRs and apps to a human, defensibly

Status: approved design (brainstormed 2026-10-24 to 2026-10-26). Built on branch `risk-routing`; §10 records where the build differs from this text.

## 1. Problem

The scorecard's AI review answers the OWASP ASVS checklist on every push. One `hello-app` push costs about 7.35M tokens,
51 batches (244 requirements, 12 per batch, each batch a fresh conversation with no prompt caching) and 1h50m. 74% of the
answers are `na`, and every PR comes out `elevated` only because of T-11 (coverage), T-12 (80 to 100 needs-human items)
and T-14. The checklist answers "is this app compliant?"; the question a PR gate has to answer is "does this PR or app
need a human?".

**Goal:** route risky PRs and apps to a human, plain and simple, in a way that can be defended. Low-risk apps go through.
High risk (sensitive data, an app becoming a system of record, ways around the platform's access control) gets a
reviewer. The audience for every result is the app team's coding agent (vibe-coded apps behind Cloudflare), so each
route says why and what would clear it. The triggers will keep changing after deployment, so everything security will
tune is configuration, not a builder release.

**Out of scope:** removing the ASVS code (a later cleanup), moving `registration.yaml` into the new file, carrying facts
forward between pushes, a release probe that calls reader routes to prove they don't write (later work, see §9).

## 2. Shape: facts and rules

Two layers. **Facts** say what the app or the change is. **Rules** say which facts need a human. Detectors produce facts
and never decide a level, so one fact can feed several rules and the rules stay readable.

The level is the highest of the intake floor and every enforced rule that fired. **A rule can only raise.** Failure
stays closed: anything that can't be read, a review that doesn't finish, or a bad submission routes to a person.

### 2.1 Fact kinds

| `from` | Computed by | Values | Can be `unsure` |
|---|---|---|---|
| `builtin` | Code: the deterministic detectors that exist today (spec diff, changes, injection pre-scan, dependencies, size, registration floor, intake, input problems) | true/false, with refs | no |
| `spec` | Code: a fixed menu of measures over `builder-spec.json` at base and head | number, enum, or list, at base and head | no |
| `diff` | Code: `paths` globs, optionally `added_lines` regexes, over the base...head diff | true/false, with refs | no |
| `agent` | The model, in one conversation; code checks every answer | yes/no/unsure, with evidence | yes |

**`spec` measures (v1):** `data_classification` (ordered enum), `resources_tier` (ordered enum), `app_type`,
`role_count`, `write_role_count`, `read_role_write_grants` (rules that give a role with `access = "read"` a write method;
each item carries the rule's path, methods and `justification`), `secret_env` (names only), `egress_hosts` (hosts other
than the model gateway).

**`agent` facts** have `question`, `applies_to` and `must_check`:
- `applies_to = "tree"`: a fact about the app as a whole. The first reviewed PR routes a risky app even when the change
  is small.
- `applies_to = "change"`: a fact about this diff only. A `yes` must cite at least one file this PR changed.
- `must_check`: fixed search terms (the `grep` tool matches fixed text, case ignored). A `no` is accepted only if the
  tool log shows a `grep` over the whole tree (glob `*`) for every term.

### 2.2 How code checks an agent answer

The model's answer is never final on its own:
- **`yes`** needs citations, verified against the file exactly as today (`anchor_ok`, and the file must be in the
  agent's citable list). Unverified citations turn the answer into `unsure` ("the evidence could not be confirmed").
  A `change` fact's `yes` that cites no changed file is `unsure` too.
- **`no`** can't cite an absence, so it has to show what was searched. Code reads the tool log (recorded by the
  harness, not reported by the model). A `no` with a `must_check` term that was never searched becomes `unsure`
  ("`jwt` was not searched"). The searches run are listed in the output, so "we checked for X and found nothing" is
  shown, not asserted.
- **`unsure`** is allowed and expected when the code doesn't settle it.
- A fact the model didn't answer is `unsure` ("not answered").
- Each question's text is hashed (SHA-256 over the canonical JSON of every agent fact's `question`, `applies_to` and
  `must_check`). The hash is part of the assessment, and `scorecard run` refuses an assessment made with other questions,
  the way it refuses another prompt or model version today.

### 2.3 Rules

```toml
[[rules]]
id = "T-22"                      # every rule id is T-<number>: built-ins keep theirs, new rules take new numbers
when = { fact = "own_credentials", is = true }
level = "security_review"
on_unsure = "elevated"           # agent facts only: elevated | rule_level | standard
mode = "enforce"                 # or "shadow": recorded in the scorecard, doesn't change the level or the comment
summary = "The app checks its own credentials"
reason = "The app issues or checks its own API keys, tokens or sessions, so the platform guard can't enforce read and write for them."
guidance = "Use the platform identity header instead of app-issued tokens. If the app must issue tokens, this needs a security review; nothing in the PR clears it."
```

- **Conditions are a fixed set,** no expression language: `is` (bool, or enum equality), `over` and `at_least`
  (numbers, list lengths), `any` (enum in a list, or a list fact that shares an item with it), and `changed`
  (`"any"`, `"up"` for ordered enums and numbers, `"added"` for list items and false-to-true).
  `when` is one condition or a list of them; a list fires when any one does.
- **Placeholders** in `summary`, `reason` and `guidance` come from the fact (`{value}`, `{base}`, `{head}`, `{count}`,
  and for `read_role_write_grants` `{justification}`). An unknown placeholder fails validation.
- **`escalate`** raises a rule's level for listed data classifications (today's T-10 rule:
  `escalate = { classifications = ["restricted", "phi"], level = "security_review" }`).
- **`on_unsure`** decides what an `unsure` answer does: `elevated` (route at elevated, reason "the reviewer couldn't
  determine whether ..."), `rule_level` (route at the rule's level), or `standard` (record it, don't route). It is
  required on a rule over an `agent` fact, and refused on any other.
- **Rule ids are `T-<number>`.** That keeps the comment format, `remediate`'s parser, the re-review event and the
  portal unchanged. The built-in triggers keep their numbers so history and docs still line up.

## 3. Configuration: `config/routing.toml`

A new file in `scorecard-factory`, owned by security (a CODEOWNERS line for `/config/routing.toml`). It is TOML, read with
the standard library's `tomllib` (no new dependency), and validated against a JSON Schema in `src/contracts/schemas/`
(`routing.v1.json`) and then by code.

**Everything security tunes lives here:**

| Section | Holds | Today |
|---|---|---|
| `[floors]` | intake tier → level | `tier_floor` |
| `[facts.*]`, `[[rules]]` | every trigger, built-in and new, with its level, mode, `on_unsure`, texts | `triggers:` plus detectors fixed in code |
| `[detectors]` | the built-in detectors' inputs: `paths`, `patterns`, `large_change`, `max_comment_block_lines`, `sensitive_classifications` | the same keys in `scorecard.yaml` |
| `[packages]` | `new_package_days`, `internal_npm_scopes`, `internal_packages`, `internal_registries` | `registry.*` |
| `[reviewers]` | teams per level | `reviewers` |
| `[evidence]` | `excluded_paths` | `evidence` |
| `[suppressions]` | `secret_rule_prefixes` | `suppressions` |

**`scorecard.yaml` keeps the mechanics** (platform-owned): `gate1_checks`, `drift_check`, `central`, the `agent` block
(gateway, model and prompt versions, budgets, tool limits) and the registry's `timeout_seconds` and `max_total_seconds`.
Getting those wrong breaks the review rather than changing what it routes. A factory with `routing.toml` uses
`scorecard-config/v2`, which has only those keys; the loader merges the two files into the one config dict the detectors
already read, so the detectors' code doesn't change.

**Deleted with ASVS** (absent from `scorecard-config/v2`): `tier_asvs_level`, `classification_asvs_level`,
`missing_intake_asvs_level`, `blocking`, `coverage_threshold`, `platform_provided`, `tool_covered`, `na_rules`. T-1, T-11
and T-12 have no rule; the `na/` records are no longer read.

**What validation refuses** (the same loader runs in `scorecard check-factory`, so a bad change fails the factory PR):
- an unknown key, a wrong type, or a duplicate rule id or fact name;
- a rule naming a missing fact; a fact no rule uses is a warning;
- a `measure` outside the menu, a condition that doesn't fit the fact's type, an unknown placeholder;
- `on_unsure` missing on a rule over an agent fact, or present on any other;
- an invalid regex in `[detectors] patterns` or a `diff` fact's `added_lines`;
- an empty `question`, `must_check` term or `reason`;
- `scorecard.yaml` naming a prompt that doesn't match the file: `review-2` needs `routing.toml`, and `review-1` refuses it.

`routing.toml` is one of the factory inputs in `factory-digest`, so the assess run and the score run are proven to have
read the same rules.

**The line between config and code:** a measure's logic (how a test is judged weakened, how a lockfile is compared, how
citations are checked) is code, with its inputs exposed as parameters. Security can change what each check looks at, how
much it matters and what it says, add `agent` questions and `diff` facts freely, and run anything in shadow mode first.
A new kind of measure, the base prompt and the evidence checks are a builder release.

## 4. The review: one conversation (`review-2`)

`scorecard assess` with prompt `review-2` holds **one conversation per review**:
- The system prompt (`prompts/review-2.md`, frozen by checksum like `review-1`) says what the reviewer is for and the
  rules of evidence. It carries no questions.
- The user message carries the trusted intake facts, every `agent` fact from `routing.toml` (name, `applies_to`,
  question, `must_check`), and the change summary (file list plus diff, through the same withholding and redaction as
  today's `repo_context`).
- Tools: the existing read-only `list_files`, `read_file`, `grep`, plus `submit_facts` (schema `agent-facts.v1`). The
  harness records every tool call (name, `text`, `glob`, `path`) in the tool log. The model never sees the log as
  something to fill in.
- The system prompt and the facts message don't change during a review, so a gateway with prompt caching re-reads them
  cheaply; that is the prefix every turn shares.
- Budget, turn cap, the nudge before the last turn and the one correction for an invalid submission work as today
  (`run_batch`'s loop, generalised over the submit tool).

It writes `facts-assessment/v1`: tree SHA, prompt and model versions, `questions_sha256`, `complete`, the answers
(`fact`, `answer`, `justification`, `evidence`), and the tool log. Raw model text is never logged; the run's stats carry
counts and code-written reasons only.

**The VC checks become agent facts** with their trigger numbers: tests weakened (T-3), a control removed (T-4), a debug or
bypass path (T-6), text aimed at the reviewer (T-7), behaviour beyond the purpose (T-9), sensitive data to logs (T-10).
The deterministic parts of those triggers (VC-1 test diff, VC-INJ pre-scan, VC-3 dependencies, PURPOSE.md) stay built-in
facts feeding the same rule ids. A finding from the harness-risk rule (an app that calls a model) becomes an agent fact
too (`llm_harness_risk`).

**Target:** about 100 to 300k tokens and a few minutes per review, against 7.35M and 1h50m.

## 5. The first rules (the starting `routing.toml`)

The built-in triggers carry over at their current levels, in `enforce` mode: T-2, T-3, T-4, T-5, T-6, T-7, T-8, T-9,
T-10 (escalated for restricted and phi), T-14, T-15, T-16, T-17, T-18, T-19, T-21. Their texts move into the file.

The new rules start in **shadow** mode. They target the failure seen in two of three vibe-coded apps: a "read only" API
token that can write. The generated guard (`authz.py.tmpl`) already enforces method-by-role in front of the app, the
builder refuses a read role with a write method unless it carries a `justification` (`src/builder/spec.py`), and the
release probe proves readers can't write. So the rules aim at the three ways around the guard:

| Rule | Fact | Kind | Level | `on_unsure` |
|---|---|---|---|---|
| T-22 | `own_credentials`: the app issues, stores or checks its own API keys, tokens or sessions instead of the platform identity header (tree; `must_check`: token, api_key, apikey, session, jwt, bearer, password) | agent | security_review | elevated |
| T-23 | `mutates_on_read_route`: this change adds or modifies a handler, reachable by a read role under the spec's rules, that changes stored data (a `GET` that deletes, a GraphQL or RPC endpoint with mutations) (change) | agent | security_review | elevated |
| T-24 | `read_role_write_grants` added | spec | elevated | n/a |
| T-25 | `personal_data`: the app stores, returns or logs data about identifiable people (names, contact details, dates of birth, record numbers, diagnoses) (tree) | agent | security_review | elevated |
| T-26 | `llm_harness_risk`: untrusted text in the app's own model prompts, model output used as code, queries or commands, or secrets sent in prompts (change) | agent | elevated | standard |

Role count stays available as a measure, but no rule uses it at the start: `hello-app`'s plain reader and writer setup
should pass.

## 6. Output

`scorecard run` writes **`scorecard/v2`** to the same `scorecard.json`, so the workflows, artifacts and the App's
report job don't change. Compared with v1 it drops `asvs`, slims `assessment` (present, prompt and model versions,
`questions_sha256`, complete), and adds `routing`:
- `facts`: each fact's kind, value (and base value for `spec`), and for agent facts the checked answer, evidence, the
  searches run, and why an answer became `unsure`;
- `fired`: the enforced rules that fired, the same entries as `reasons`;
- `shadow`: the shadow rules that would have fired, with their levels and refs.

`reasons` keep their shape (`trigger`, `level`, `summary`, `refs`, `total`) and gain `guidance` and `unsure` (true when
the rule fired on an `unsure` answer). The re-review event reads only `reasons`, `level`, `floor` and
`assessment.complete`, so it is unchanged.

**The comment** keeps its heading and `### Reasons` lines (`- **T-n** · level · summary: refs`), so
`pr_comments.parse_scorecard` still reads it. Under each reason it adds the reason text and a `Fix:` line with the
guidance; the parser learns to read the `Fix:` line, and `golden-road remediate` ("Fix my PR") uses it instead of its
built-in `TRIGGER_NOTES` when present. Shadow rules never appear in the comment. The ASVS table and coverage line go;
the header reads `## Scorecard: ELEVATED — 2 reasons · 9 facts checked`.

## 7. Compatibility and rollout

Both engines live side by side, chosen by the factory: `agent.prompt_version: review-1` (no `routing.toml`) keeps the
ASVS engine exactly as it is; `review-2` (with `routing.toml`) uses routing. Switching back is one factory PR.

1. **Finish the central migration on the current engine.** It doesn't depend on the review logic. Record the cheap proof
   items now.
2. **Release the routing engine** as the next builder version, with `review-2`, the starting `routing.toml` (§5), and
   every new agent rule in shadow mode. The factory PR that pins the release also adds `routing.toml`, switches
   `scorecard.yaml` to v2 and to `review-2`, and adds the CODEOWNERS line.
3. **Watch the shadow results** on real PRs (the `routing.shadow` part of `scorecard.json`), then flip rules to
   `enforce` one factory PR at a time.

## 8. Testing and defensibility

- **Unit tests** for every built-in measure and condition, every refusal in §3, and every way an agent answer is
  downgraded in §2.2 (unverified citation, `change` fact citing no changed file, missing search, missing answer,
  question hash mismatch).
- **Golden fixtures per fact.** Each agent fact gets fixture PRs where the answer is `yes` and where it's `no`, run with
  the fake model in CI and with the live model on demand. The first fixtures copy the three vibe-coded failures: an
  app-issued read token that can write, a `GET` that deletes, and a GraphQL mutation behind a read route. Results are
  recorded against the question hash; `check-factory` warns when an enforced agent rule has no golden record for its
  current question.
- **A false-alarm regression set:** the existing `hello-app` PRs. Each should come out `standard` unless a real rule fires.
  That is the check today's engine fails.
- **Cost:** the run's stats report tokens and turns, and the golden run records them per case, so the target in §4 is
  measured, not assumed.

## 9. Follow-ups (not in this build)

- Remove the ASVS engine (`asvs.py`, `scoping.py`, `review-1`, `agent-batch`/`agent-scope`, the ASVS data) once
  `review-2` has been the factory's prompt for a while.
- Carry `tree` facts forward between pushes when the cost numbers show it's needed.
- A release-probe check that calls each reader route and proves it doesn't change state.
- Fold `registration.yaml` into `routing.toml` (the portal's collector reads it too, so that needs a portal change).
- Move the builder's base images to Debian 13 and remove the CVE-2026-84782 exceptions in `security-scans` before they
  expire on 2026-11-09.
- A platform-built nightly base image, so upstream CVE fixes don't wait on the upstream image.
- The Cloudflare `security-audit-skill` as a periodic whole-app baseline audit, separate from the PR gate.

## 10. Refinements made while building

Where the build differs from, or fixes the detail of, the text above:

- **§3, `[detectors]`:** `sensitive_classifications` is not a detector input. The T-10 rule's `escalate` replaces it.
- **§6, `routing`:** it holds `facts`, `shadow` and `undecided`. The enforced rules that fired are `reasons`, so there is no separate `fired` list. `undecided` records the rules that did not route because an agent answer was `unsure` under `on_unsure = "standard"`. Shadow rules are shown in the job summary and in `routing.shadow`, never in the PR comment. The re-review event's trigger ids now reference `scorecard.v2.json`'s wider pattern (`T-<1..999>`).
- **§2.1, `must_check`:** terms are fixed text (the `grep` tool matches fixed text), so a term has no commas and no leading or trailing space, and a term can't be listed twice (ignoring case).
- **§8, golden harness (`src/scorecard/routing/golden.py`, `tests/scorecard/golden_routing/cases.yaml`):** each case's `expect` can name `answers`, `level`, `fired`, `not_fired` and `shadow`. `fired` and `shadow` are exact: the sorted list of rule ids that fired (the scorecard's `reasons`) or would have fired in shadow (`routing.shadow`) must equal the expected list, so an extra rule fails a case as surely as a missing one. The false-alarm set is the cases named `false-alarm-*` (a README change, a dependency bump, an added test, a read-only GET, a style change); each must expect `level: standard`, `fired: []` and `shadow: []`, and `test_routing_golden.py` asserts that and that there are at least five. The cases run in fake mode in CI (`test_routing_golden.py`) and live on demand (`test_routing_golden_live.py`, opt-in with `GOLDEN_ROAD_SCORECARD_LIVE=1`; it reports and asserts nothing about answers).
- **`check-factory` warnings:** `scorecard check-factory` prints routing warnings (a fact no rule uses) in its output without failing. The golden-record warning in §8 is not built.
