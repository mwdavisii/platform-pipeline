# golden-road-agents

The golden road takes an app that works on your machine to a reviewed, running deployment on the platform. Your coding agent packages the app with the platform's builder and opens the pull request; the pipeline checks every step, and you make every decision.

## Before you start

- Get access to `mwdavisii/golden-road-agents` on this GitHub host (ask the platform team).
- Install `uv`: https://docs.astral.sh/uv/getting-started/installation/
- Install the GitHub CLI and sign in yourself: `gh auth login --hostname github.com`.
- Use a certified agent: Claude Code, OpenAI Codex or Cursor. Other agents that can run commands should work but are not certified.
- On Windows, run `git config --global core.autocrlf false` before you clone an app repository (or clone with `git clone -c core.autocrlf=false`). With `true`, git rewrites line endings on checkout, so every generated file looks edited and the drift check fails. If the app's `.gitattributes` has `text` or `text=auto`, also run `git config core.eol lf` in the repository (the default, native, means CRLF on Windows). `* -text` in `.gitattributes` is fine; `eol=` is not (the builder flags it).

## Paste block

Open your agent in the app's folder and paste this:

```text
Package this app for the golden road. Read INSTALL_FOR_AGENTS.md from mwdavisii/golden-road-agents with
gh api --hostname github.com -H "Accept: application/vnd.github.raw" repos/mwdavisii/golden-road-agents/contents/INSTALL_FOR_AGENTS.md
and follow every step of it. If you can't read that repo, stop and tell me to request access from the platform team.
Ask me every question the builder returns; never answer data classification, roles, or egress yourself. Show me each
command that changes anything outside this folder and wait for my yes. Never make a repo public. You are done only
when `golden-road doctor` exits 0.
```

This README holds the canonical block. The landing page copies this block word for word.

## What is here

| Path | What it is |
|---|---|
| `INSTALL_FOR_AGENTS.md` | Commands only: the access check, the `uv` check, installing the stub into the agent's personal skills folder |
| `stub/golden-road/` | The stub skill: `SKILL.md` and `fetch_skill.py`, which downloads, verifies, caches and loads the skill bundle. It names this repository and `release-skill.yml` as the only signer |
| `skill/` | The skill bundle: the procedure (`SKILL.md`), `install_builder.py` (the platform's pinned builder, verified, in a venv), `write_callers.py` and the caller workflow templates, the intake request template |
| `scripts/build_release.py` | Builds a skill release's assets; the same tree always gives the same bytes |
| `.github/workflows/release-skill.yml` | Builds, attests and publishes a `skill-vX.Y.Z` release |
| `.github/rulesets/` | A protected `main`, and `skill-v*` tags that only admins create, move or delete |

## Releases

Two series of releases live here. Agents choose by version number among published releases, never by GitHub's latest flag.

- **`skill-vX.Y.Z`**: built by `release-skill.yml` from a tag on `main`: `golden-road-skill-X.Y.Z.tar.gz`, `fetch_skill.py`, `golden-road-stub.md` and the attestation bundle. Marked latest, so the install command in `INSTALL_FOR_AGENTS.md` finds `fetch_skill.py`. To release: bump `skill/VERSION` by pull request, then an admin tags the merge commit `skill-vX.Y.Z` and pushes the tag.
- **`builder-vX.Y.Z`**: published by the platform's builder release workflow as a draft: the wheel, its hash-pinned requirements, `platform-pin.json` and the attestation bundle, all attested by that workflow. The newest published one is the platform's current builder. The platform team publishes the draft when it pins that builder in the pipeline: `gh release edit builder-vX.Y.Z --repo mwdavisii/golden-road-agents --draft=false --latest=false`.
