# Confirmed N/A

`na/<owner>/<repo>.yaml` lists the ASVS requirements a person has confirmed do not apply to that app. The scorecard
counts them as `na` with `status_source: human_na`. When the agent proposes `na`, the requirement stays `needs_human`
until someone adds it here, with a justification, through a reviewed pull request (CODEOWNERS: the security team).

For example, `na/example/hello-app.yaml`:

```yaml
schema: na/v1
repo: example/hello-app
entries:
  - asvs_id: v5.0.0-6.6.1
    justification: "The app has no out-of-band authentication; the edge handles sign-in."
    confirmed_by: "security-reviewer"
    confirmed: 2026-10-21
```

Each `asvs_id` must exist in ASVS 5.0.0 (the scorecard refuses an unknown one), and `repo` must match the file's path.
