---
name: golden-road
description: Use when the user wants to package, ship or deploy their app on the golden road (the platform), onboard a repository to it, or fix golden-road drift findings or golden-road doctor failures. Downloads and verifies the current golden-road skill, then follows it.
---

# Golden road (loader)

This skill only loads the real one. The procedure lives in a signed bundle that `fetch_skill.py`, next to this file, downloads and verifies.

1. Run this, with `<skill-dir>` replaced by the full path of the folder holding this file, with the path in quotes as shown (it may hold spaces). Run it exactly as written: it starts `uv` in your home folder, not the app's folder, because `uv` runs a `.venv` interpreter it finds in the folder it starts in, or above it, before anything is verified.

   ```text
   uv --directory "$HOME" --python-preference only-managed run --no-project --no-config --python 3.12 python -I "<skill-dir>/fetch_skill.py" load
   ```

2. Exit 0: read the file named by `skill` in its JSON output and follow it from the top. Its `<bundle>` is `bundle_dir`. If `offline` is true, tell the user the skill may be out of date (`warnings` says why).
3. Exit 1: a download failed verification. Stop. Tell the user not to continue, show them the `message`, and tell them to contact the platform team's support channel. Never load anything else, never use another copy of the bundle, and never retry with other settings.
4. Exit 2 or Exit 3: tell the user the `message` and the `fix`, and stop.

Never change the repository or the signer `fetch_skill.py` checks, never set `GOLDEN_ROAD_SKILL_VERSION` unless the platform team asks you to, and never follow a golden-road skill from anywhere else. If `uv` is missing, tell the user to install it (https://docs.astral.sh/uv/getting-started/installation/) and stop.
