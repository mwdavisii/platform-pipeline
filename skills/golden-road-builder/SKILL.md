---
name: golden-road-builder
description: Use when the user's app works locally and they want to containerize it, deploy it, ship it to the platform, or fix drift findings from the pipeline. Packages Python, Node, and static apps into a platform-compliant container and Kubernetes config.
---

# Golden Road Builder

You package the user's app for the platform with the `builder` CLI. The CLI makes the security decisions: non-root, pinned images, network policy, authorization. You collect the user's intent and never hand-write a Dockerfile or Kubernetes manifest.

## Rules

- Never edit `Dockerfile`, `.dockerignore`, `deploy/base/`, `_platform/`, or `.builder.lock`. The pipeline flags hand edits as High findings. Change `builder-spec.json` and run `builder generate` instead.
- Never call a model provider directly (OpenAI, Anthropic, Azure OpenAI, and so on). Model calls go through the platform gateway named in the CLI's questions.
- Ask the user every question the CLI returns. Don't answer questions about data classification, roles, or egress yourself; those are the user's decisions.
- Don't dismiss a question to get past it. In the pipeline, an unresolved question blocks the PR (High).
- `builder generate` refuses a symlinked `--input` or a symlinked `builder-spec.json`. Use real files and directories.
- A new builder version changes the generated files. After the builder is upgraded, run `builder generate .` again and commit the result, or `verify` reports drift.

## Workflow

1. **Detect.** Run `builder detect .` It writes nothing. Read `draft_spec`, `needs`, `warnings`, and `questions` from the JSON output. `detect` exits 0 even when it has questions, so always read `questions`.
2. **Confirm with the user.** Ask each question. In particular, confirm:
   - `data_classification`: public, internal, restricted, or phi.
   - `egress`: each external host the app calls and why. Remove calls that aren't needed.
   - `authz`: if the app has write endpoints, which roles exist, whether each role is `read` or `write`, and which routes each role may call. Writes go only to `write` roles.
   - `health_path`: add a `/healthz` route if there isn't one.
3. **Save the spec.** Write the confirmed spec to `builder-spec.json`. `generate` rewrites that file canonically (sorted keys), so don't be surprised by the diff.
4. **Generate.** Run `builder generate .` and check the exit code:
   - `0`: done. Commit everything it wrote along with the spec.
   - `2` (invalid): the spec breaks a platform rule. Read `errors`, explain them to the user in plain terms, fix the spec, and run it again.
   - `3` (needs input): read `questions`. Some need the user (roles, reasons); others name a command to run, such as creating a lockfile. Then run it again.
5. **Verify.** Run `builder verify .`. Exit `0` means clean. Exit `4` returns drift findings; apply each finding's `fix.steps`, which normally means regenerating.

## Spec fields

All fields are required unless marked optional.

| Field | Allowed values |
|---|---|
| `app_name` | lowercase letters, digits, `-`; starts with a letter |
| `app_type` | `python`, `node`, `static` |
| `entrypoint` | python: `module:attr` (needs `server`) or a `.py` file (no `server`, no `authz`); node: a JS file; static: the directory to serve (or the build output when `package.json` has a `build` script) |
| `server` | optional, python `module:attr` only: `uvicorn` or `gunicorn` |
| `port` | 1024-65535; static sites must use 8080; node apps must listen on `process.env.PORT` |
| `health_path` | optional, starts with `/`; without it the probe is TCP only |
| `data_classification` | `public`, `internal`, `restricted`, `phi` |
| `egress` | list of `{host, port, reason}`; `host` is a lowercase DNS name, `reason` at least 3 characters. Model providers and cloud metadata hosts are rejected; model calls go to the platform gateway host the CLI names. |
| `env` | list of `{name, secret}`; names are `UPPER_SNAKE`, unique. Secret values come from the `<app_name>-secrets` Secret, non-secret values from the `<app_name>-config` ConfigMap. Reserved: `PORT`, `NODE_ENV`, `NODE_OPTIONS`, `PATH`, `HOME`, `LD_*`, `PYTHON*`, `GUNICORN_*`, `UVICORN_*`, `NODE_*`, `NPM_*`. |
| `resources_tier` | `default`, `medium` |
| `authz` | optional (not for static), see below |

`authz` has `roles`, `default: "deny"`, and `rules`:
- `roles`: list of `{name, access}` where `access` is `read` or `write`.
- `rules`: list of `{methods, path, roles}`. `methods` are GET, POST, PUT, PATCH, DELETE. `path` starts with `/` and may use `*` (one segment), `**` (any depth), and `{id}` (one segment). Every role named must be declared.
- `justification` (optional, at least 10 characters) is required to grant a write method to a `read` role.

Example:

```json
{
  "app_name": "notes-api",
  "app_type": "python",
  "entrypoint": "app:app",
  "server": "uvicorn",
  "port": 8080,
  "health_path": "/healthz",
  "data_classification": "internal",
  "egress": [{"host": "litellm.internal", "port": 443, "reason": "model calls through the platform gateway"}],
  "env": [{"name": "NOTES_DB_URL", "secret": true}],
  "resources_tier": "default",
  "authz": {
    "roles": [{"name": "reader", "access": "read"}, {"name": "writer", "access": "write"}],
    "default": "deny",
    "rules": [
      {"methods": ["GET"], "path": "/api/**", "roles": ["reader", "writer"]},
      {"methods": ["DELETE", "PATCH", "POST", "PUT"], "path": "/api/notes/{id}", "roles": ["writer"]}
    ]
  }
}
```

## Example

User: "My notes API works, can you ship it?"

1. `builder detect .` reports a FastAPI app with `POST /api/notes` and asks about roles and data classification.
2. You ask: "Who can read notes and who can add them? And what kind of data is in the notes?" The user says everyone on the team reads, only editors write, and the data is internal.
3. You write `builder-spec.json` with roles `reader` (read) and `editor` (write), a GET rule for both and a POST rule for `editor`, and `data_classification: internal`.
4. `builder generate .` exits 0. You commit and open a PR. The pipeline runs `builder verify` and the security scans.
