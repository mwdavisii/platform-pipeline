# Suppressions

`suppressions/<owner>/<repo>/*.json` are accepted scorecard findings for that app, one `override/v2` document per file.
A suppression binds to both the finding's ID and its rule ID, applies only between `created` and `expires` (at most 90
days), and is ignored once it has expired (the report lists it as expired). A secrets finding can never be suppressed:
rotate the secret. Every change is a reviewed pull request (CODEOWNERS: the security team).

For example, `suppressions/example/hello-app/trivyignore-base-image.json`:

```json
{
  "schema": "override/v2",
  "id": "hello-app-2026-10-21-1",
  "repo": "example/hello-app",
  "finding_id": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "rule_id": "T-15/suppression-file",
  "approver": "security-reviewer",
  "justification": "Why the finding is accepted, and what will fix it.",
  "created": "2026-10-21",
  "expires": "2026-12-31"
}
```

The finding ID and rule ID are in the `scorecard` artifact of the run (`scorecard.json`), next to the finding.
