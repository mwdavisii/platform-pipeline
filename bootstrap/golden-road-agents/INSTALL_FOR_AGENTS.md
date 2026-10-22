# Install the golden-road skill

For coding agents. Run these from the app's folder, in order, and stop at the first failure with the fix it names. None of them changes anything on GitHub. Each command reads the same in a POSIX shell and in PowerShell.

## 0. Access

```text
gh auth status --hostname github.com
gh repo view mwdavisii/golden-road-agents --json nameWithOwner
```

If `gh` is missing or either command fails: stop. Tell the user to install the GitHub CLI and sign in with `gh auth login --hostname github.com` (they type it, never you), or, if they are signed in, to request access to `mwdavisii/golden-road-agents` from the platform team.

## 1. uv

```text
uv --version
```

If it fails: stop. Tell the user to install uv (https://docs.astral.sh/uv/getting-started/installation/, one command on macOS, Linux and Windows) and to open a new terminal.

## 2. The stub

Skip this step if your personal skills folder already has `golden-road/SKILL.md`: `~/.claude/skills/` for Claude Code, `~/.agents/skills/` for Codex, `~/.cursor/skills/` for Cursor (on Windows, `~` is your user profile folder). Otherwise run these three, putting your own name in the last one: `claude-code`, `codex` or `cursor`.

```text
gh release view --repo mwdavisii/golden-road-agents --json tagName --jq .tagName
gh release download <tag> --repo mwdavisii/golden-road-agents --pattern fetch_skill.py --dir .golden-road-install --clobber
gh attestation verify .golden-road-install/fetch_skill.py --repo mwdavisii/golden-road-agents --cert-identity https://github.com/mwdavisii/golden-road-agents/.github/workflows/release-skill.yml@refs/tags/<tag> --source-ref refs/tags/<tag> --cert-oidc-issuer https://token.actions.githubusercontent.com
uv --directory "$HOME" --python-preference only-managed run --no-project --no-config --python 3.12 python -I "$PWD/.golden-road-install/fetch_skill.py" install --agent <claude-code|codex|cursor>
```

The first command prints the tag of the latest release. If it isn't exactly `skill-vX.Y.Z` (digits and dots only, such as `skill-v1.2.0`): stop and tell the user to contact the platform team's support channel. Otherwise put that tag where `<tag>` is in the next two commands. Run the last command exactly as written: it starts `uv` in your home folder, never the app's folder (`uv` runs a `.venv` interpreter it finds in the folder it starts in, or above it, before anything is verified), and selects only a Python that `uv` manages itself (it downloads CPython 3.12 the first time), so a `.venv` interpreter in the app folder is never executed. `uv` may still look at `~/.venv` and at an active `VIRTUAL_ENV` or `CONDA_PREFIX`. In PowerShell the same text works: `$HOME` and `$PWD` are PowerShell variables too.

The last command checks the stub's two files again, installs them as `golden-road/` in your skills folder, and deletes `.golden-road-install`. If `gh attestation verify` fails or the last command exits 1: delete the `.golden-road-install` folder without running anything in it, stop, and tell the user not to continue and to contact the platform team's support channel.

## 3. Load

Read `golden-road/SKILL.md` in your skills folder and follow it now. You don't need to restart to use it.
