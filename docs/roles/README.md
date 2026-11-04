# Golden road roles: who does what

This folder has one standard operating procedure (SOP) per role. Each SOP says what the role owns, the access it needs, the
procedures it runs (each ends with a **Check**), what it must never do, how to fix common failures, and the gaps that are still
open. For the design behind them, read [the architecture](../architecture.md) and [what is enforced](../enforcement.md).

**Mock today.** Several integrations exist only as mocks in this proof of concept. The portal reads a mock directory
(`bootstrap/golden-road-portal/portal/mock_directory.json`) instead of Azure and Databricks, and a mock Secret Server
(`bootstrap/golden-road-portal/portal/mock_secret_server.json`) instead of a real Delinea instance. Nothing is applied to a cluster:
the Flux adapters are suspended, the Argo CD adapters sync by hand only, the image policy is in warn mode, and the External Secrets
Operator files are documentation. ServiceNow is a mock too (`bootstrap/golden-road-portal/portal/mock_servicenow.json`): the CMDB
records and the lifecycle tasks a portal admin applies ([the lifecycle rules](../lifecycle.md)). Every SOP has a "Today (mock) and in
production" section that says which steps are real.

## Roles

| Role | Owns | Repos, files and screens | GitHub team or CODEOWNERS entry | SOP |
|---|---|---|---|---|
| App developer (contributor) | The app code, the answers to the builder's questions, fixing findings, asking for connections | The app repo (`builder-spec.json`, app code); portal Register, My requests and the app page | Admin of their own app repo. No platform team; never a code owner of `.github/` in their app | [app-developer.md](app-developer.md) |
| Security team | The scanners and their thresholds, AI review routing, the registration floor, intake records, suppressions and N/A records, security review of risky changes, `bindings.yaml` review | `security-scans`; `scorecard-factory` `intake/`, `suppressions/`, `na/`, `config/registration.yaml`, `config/routing.toml` (its CODEOWNERS line is added by hand; see the security SOP's Gaps); `golden-road-deploy` `apps/**/bindings.yaml`; CodeQL settings on app repos | `security` (factory CODEOWNERS for `/intake/`, `/suppressions/`, `/na/`, `/config/registration.yaml`; deploy repo CODEOWNERS for `/apps/**/bindings.yaml`); the `security_review` reviewer team | [security-team.md](security-team.md) |
| Portal admin | Registration approvals, the floor override, sending back, people and GitHub links, the audit log, suspending, resuming and decommissioning apps | Portal `/admin/approvals`, `/admin/people`, `/admin/directory`, `/admin/audit`, each app page's Lifecycle panel | The identity-provider group mapped to `admin` in `PORTAL_ROLE_GROUPS` (not a GitHub team) | [portal-admin.md](portal-admin.md) |
| Secret Server admin | The platform reader account `golden-road-eso`, sharing secrets with it, folders and templates | Secret Server admin UI; `golden-road-deploy` `external-secrets/` (with the cluster operators) | None in GitHub. Security creates the account (per `bootstrap/golden-road-deploy/external-secrets/README.md`) | [secret-server-admin.md](secret-server-admin.md) |
| Identity admin (Azure and Entra) | Service principals and workload identities per app and environment, their federated credentials, tagging them for a repo | Entra admin tools; today `bootstrap/golden-road-portal/portal/mock_directory.json` (`principals`) | None in GitHub. The app page calls it "security's process" | [identity-admin.md](identity-admin.md) |
| Data platform admin (Databricks) | SQL warehouses and grants to an app's principal | Databricks admin tools; today `mock_directory.json` (`databricks`) | `analytics` (deploy repo CODEOWNERS for `/apps/**/bindings.yaml`, shared with security) | [data-platform-admin.md](data-platform-admin.md) |
| Platform team | The builder, the pipeline, the scorecard code and factory config, releases, pins, the export, the Apps, rulesets | `platform-pipeline`, `scorecard-factory` (`*`), `golden-road-agents`, `golden-road-deploy` (`*` and `/.github/`), the portal deploy kit | `platform-reviewers` (CODEOWNERS `*` in the factory, the deploy repo and `golden-road-agents`; `/.github/` and the scanner ignore files in every app) | [platform-team.md](platform-team.md) |
| ServiceNow admin | The integration user's roles, the CI's `u_golden_road_*` fields, lifecycle tasks | ServiceNow (tables `sc_task`, `cmdb_ci_business_app`) | None in GitHub | [servicenow-admin.md](servicenow-admin.md) |
| Deploy reviewer | Reviewing and merging what changes in `golden-road-deploy`: release pins, prod bindings, decommission PRs | `golden-road-deploy` pull requests and their `deploy-check` runs | Code owners of `golden-road-deploy`: `platform-reviewers` for pins, `security` or `analytics` for `bindings.yaml` | [deploy-reviewer.md](deploy-reviewer.md) |
| Gate 2 reviewer | Approving a prod promotion of someone else's release | The app repo's `prod` environment approval | A required reviewer of the app's `prod` environment (set from `environment-prod.json` by the platform); never the person who started the promotion, except in the PoC's single-approver mode, where it is recorded as `self_approved` | [app-developer.md](app-developer.md), procedure 6 |
| Cluster operators | Installing controllers, applying the hand-applied files, switching reconciling on | The clusters; `golden-road-deploy` `clusters/`, `policy/`, `external-secrets/` (applied by hand) | None today | No SOP yet: see the handoff in [deploy-reviewer.md](deploy-reviewer.md) |

The CODEOWNERS files in `bootstrap/*/.github/CODEOWNERS` name `platform-reviewers`, `security` and `analytics` as
`<org>/<team>`. On a personal account there are no teams, so the setup document puts people there instead.

## Who to ask

| I need | Ask | They do | Where it shows up |
|---|---|---|---|
| A new app on the golden road | Nobody first: run the agent kit, then register in the portal | The agent packages it; a portal admin approves the registration | `intake/<owner>/<repo>.yaml` in `scorecard-factory`; `golden-road doctor`'s `intake` check passes |
| An Azure identity for my app (per environment) | The identity admin (the link "how to ask for one" on the app page, if `PORTAL_IDENTITY_PROCESS_URL` is set) | Creates the service principal and tags it for the repo | The principal is offered under "Associate Azure Identity" on the app page |
| A Databricks SQL warehouse | The data platform admin (the link "how to ask" on the app page, if `PORTAL_DATA_PROCESS_URL` is set) | Grants the app's principal access to a warehouse | The warehouse is offered under "Enable Databricks Connection" once the identity is linked |
| A secret (database password, API key) | The Secret Server admin (the "security's process" link on the app page, if `PORTAL_SECRET_PROCESS_URL` is set) | Stores the secret in your folder and shares it with `golden-road-eso` | The secret is selectable (not greyed "Not available") under Add a secret |
| A scanner finding I think is a false positive | The security team | Changes the rule in `security-scans` (there is no per-app waiver) | The scan check passes on your next push |
| A scorecard finding I think is wrong | The security team (and your PR reviewers) | Reviews it; may add a time-boxed suppression in the factory | The comment marks it "(suppressed in the factory)" |
| My PR merged past a high scorecard finding | The finding's override holder: the repo admin role in the PoC, the `--findings-bypass-team` team on an org | Reviews it and merges with "Merge without waiting for requirements" | A `bypass` entry in the repo's rule suites |
| A prod promotion | A Gate 2 reviewer (not yourself) | Approves the `prod` environment run | A prod release, then a `Pin <owner>/<repo> prod to <tag>` PR in `golden-road-deploy` |
| My prod binding merged | A deploy reviewer who is a `bindings.yaml` code owner | Reviews and merges the writer's PR | The app page shows the identity, connection or secret as linked on `prod` |
| A higher registration (after T-21) | A portal admin (another person than the requester) | Approves the change you ask for with **Change or renew the registration** | The writer's `Change the registration of ...` PR merges and T-21 stops on the next PR |
| My app's review date has passed | A portal admin | Approves the renewal you submit from the app page | The banner on the app page is gone |
| My app must be suspended (critical vulnerability) | A ServiceNow task for the platform team (the ServiceNow admin makes the task type) | A portal admin applies it | The app page's banner, the task's notes |
| My app was suspended and is fixed | The task's assignee in ServiceNow | A portal admin resumes it | The banner is gone |
| A builder or platform change, a broken pipeline | The platform team | Releases and pins | A new `builder-v*` release and pins in every workflow |

## The SOPs

- [App developer](app-developer.md)
- [Security team](security-team.md)
- [Portal admin](portal-admin.md)
- [Secret Server admin](secret-server-admin.md)
- [Identity admin (Azure and Entra)](identity-admin.md)
- [Data platform admin (Databricks)](data-platform-admin.md)
- [Platform team](platform-team.md)
- [Deploy reviewer](deploy-reviewer.md)
- [ServiceNow admin](servicenow-admin.md)

Related references: [routing rules for the scorecard](../scorecard/routing.md), [the personal PoC setup and proof run](../setup/personal-poc.md),
the [scorecard factory README](../../bootstrap/scorecard-factory/README.md), the [deploy repo README](../../bootstrap/golden-road-deploy/README.md),
and the [portal README](../../bootstrap/golden-road-portal/README.md).
