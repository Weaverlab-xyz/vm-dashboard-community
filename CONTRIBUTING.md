# Contributing

Thanks for your interest in improving the Infrastructure Management Dashboard.

## Repo model

This repo (`vm-dashboard-community`) is the **upstream** for the dashboard's
shared code — the core app, AWS, Azure, GCP, auth, jobs, and feature-flag
plumbing. A separate **private** repo tracks internal/enterprise-only code:
custom integrations, hosted-deployment tooling, and any work that depends
on commercial APIs we can't ship publicly. The private repo periodically
syncs from this one.

Two practical implications:

1. **Shared code changes land here first.** Bug fixes or features in
   `web_dashboard/` (anything that would also exist in the private repo)
   should be opened as PRs against this repo. They flow downstream
   automatically on the next sync.
2. **Enterprise-only code doesn't belong here.** If a change requires a
   BeyondTrust Password Safe or PRA API call, an Ansible playbook
   invocation against shared infra, or an Entitle approval check, it
   lives in the private repo. *(Exceptions: BeyondTrust EPM for Linux —
   `api/epml.py` and `services/epml_service.py` — and the Portainer CE
   client — `services/portainer_service.py`, `portainer_node_service.py`
   and `portainer_runner.py`, which became a dashboard-managed offering —
   are community-edition code gated by `BEYONDTRUST_ENABLED` and
   `PORTAINER_ENABLED` respectively.)* Keep
   integrations behind the existing feature flags
   (`BEYONDTRUST_ENABLED`, `PORTAINER_ENABLED`, `ANSIBLE_ENABLED`,
   `ENTITLE_ENABLED`) — community code paths must function with all of
   them off.

### Where SaaS fits

The hosted SaaS edition is **not** this repo. It builds on the same core
but adds capabilities the community edition deliberately doesn't ship:

- **Multi-tenancy** — tenant isolation, per-tenant Key Vaults, no
  shared filesystem.
- **Security uplift** — workload-identity-bootstrapped JWT root key (no
  static credentials anywhere), audit logs for root-key access, managed
  rotation. See [docs/editions/comparison.md](docs/editions/comparison.md) for
  the detailed walkthrough.
- **AI helper services** — e.g. an AI-assisted Ansible playbook
  generator, hosted as a tenant-scoped service rather than embedded in
  the dashboard image.

If your change is one of those SaaS-only capabilities, it lands in the
hosted codebase, not here. PRs against this repo that fold in
multi-tenant assumptions, hosted-only auth flows, or AI-helper
plumbing will be redirected.

## Making a change

1. Fork + branch from `main`.
2. Bring up a local stack so you can test against it. Either onboarder
   works:
   ```bash
   ./scripts/onboard.sh                 # bash — WSL / Linux / macOS
   ```
   ```powershell
   .\scripts\Onboard-Dashboard.ps1      # PowerShell — Windows
   ```
3. Make your change. Keep the diff tight — no drive-by refactors.
4. Smoke-test locally:
   - `docker compose up -d --build`
   - `curl http://localhost:8001/api/health` returns `200`
     *(The community Compose file binds host port 8001 to coexist with
     other local dashboards on 8000; see `docker-compose.yml`.)*
   - Your feature works end-to-end against your own AWS / Azure / GCP
     account.
5. Update the docs (see [Documentation](#documentation) below).
6. Open a PR. Describe what changed and why; note any `.env.example`
   keys added.

### Documentation

`docs/` is the product's in-app help as well as the GitHub tree, so a feature
without a page is a feature nobody can find. Two CI checks hold that line:

- **`tests/test_docs_coverage.py`** (in the `tests` job) fails when a page
  route, nav link, `*_enabled` feature flag, or request model that accepts a
  change-window booking (`ScheduleRequestMixin`) is not named in any doc. The
  failure says what is missing and which page it most likely belongs on.
- **The `docs-gate` job** fails a PR that has a `feat:` commit, or that changes
  `web_dashboard/templates/`, `api/`, `models/`, `main.py` or
  `services/feature_flags.py`, without also changing `docs/`, a README or this
  file. If
  the change really has nothing for a reader (a refactor, or a fix that
  restores documented behaviour), add a trailer to any commit on the branch:

  ```
  Docs: none — <one sentence on why>
  ```

  Run it locally before pushing with `scripts/ci/docs_gate.sh origin/main`.
  It is bash, so on Windows use Git Bash or WSL.

A new page needs the one-line header block under its H1 and a row in
[docs/README.md](docs/README.md); `tests/test_docs_conventions.py` enforces
both.

Moving or renaming a page? Repoint the links in the repo, and add the old path to
`_MOVED` in [web_dashboard/api/docs_pages.py](web_dashboard/api/docs_pages.py) so
bookmarks and links outside the repo get a 301 instead of a 404. A folder entry covers
every page under it. `tests/test_docs_moved_redirects.py` checks every target exists.

### Faster lab infra for testing

If your change touches AWS / Azure / GCP integration code, the
[sandbox bootstrappers](docs/cloud/sandbox.md) can stand up isolated
lab infra in any cloud with one command and tear it down with another.
Saves the manual VPC/IAM/SP setup you'd otherwise repeat per-PR. Both
bash (`scripts/sandbox/Linux/`) and PowerShell
(`scripts/sandbox/Windows/`) variants exist — match whichever shell
you're working in.

### Where to land new target-cloud support for image promotion

Cross-cloud image promotion is driven by transient containers under
[`runners/promote/`](runners/promote/) — one image, multiple targets.
Adding a fourth target cloud (Oracle Cloud, on-prem KVM, …) means:

1. Extending [`runners/promote/entrypoint.py`](runners/promote/entrypoint.py)
   with a new `upload_<target>(...)` path + a `--target <name>` arg.
2. Adding a `services/<cloud>_service.py` helper that launches the
   container as a transient task in that cloud (mirror
   `aws_service.run_promote_runner_ecs` /
   `azure_service.run_aci_promote_runner_task` /
   `gcp_service.run_cloud_run_promote_runner_task`).
3. Adding a `services/<cloud>_service.create_image_from_<storage>(...)`
   that calls the cloud's image-import API against the staged blob.
4. Wiring `services/promote_runner_service.run_for_<target>_target(...)`
   + `services/image_registry_service.promote_to_<target>_automated(...)`
   + the branch in `api/images.py`.
5. Adding the `promote_runner_<cloud>_*` config keys to `config.py`
   and `api/storage.py`.

[`runners/promote/README.md`](runners/promote/README.md) is the
runner-internals reference; [`docs/cloud/image-management.md`](docs/cloud/image-management.md)
is the operator-facing flow. Update both when adding a target.

## What to avoid

- Adding dependencies to [web_dashboard/requirements.txt](web_dashboard/requirements.txt)
  without a clear reason — every added package is one more thing users
  install. Keep dependency floors loose (`>=`) unless there's a known
  incompatibility.
- Introducing platform-specific assumptions in the base Compose file or
  in shipping scripts. If you add an automation script, ship both a bash
  and a PowerShell variant when possible (see `scripts/sandbox/` for
  the pattern). At minimum, document the platform a script targets.
- Hard-coding account IDs, subnet IDs, AMI IDs, or cloud region
  specifics in [terraform/](terraform/). Everything flows through
  variables.
- Landing a change that only works with an enterprise integration
  enabled. If the flag is off, the feature must be cleanly absent
  (route 404s where appropriate, nav entry hidden, no warmer errors).
- Adding multi-tenant assumptions or hosted-only auth flows. Those
  belong in the SaaS codebase.

## Testing

### Running the suite

CI runs every file in `tests/` as a script on **Python 3.12**, the Dockerfile's base
image, with `scripts/ci/run_tests.sh` after `pip install -r web_dashboard/requirements.txt`.
Pass file names to run just those. Use 3.12 locally too. On a newer
interpreter, SQLAlchemy 2.0.27 fails at import and `psycopg2-binary` has no wheel, so
every test that imports the app fails for reasons CI never sees.

Claude Code cloud sessions do this for you: `.claude/hooks/session-start.sh` builds a 3.12
virtualenv in `.venv/` from the requirements and puts it first on the session's `PATH`.
It installs no pytest, because CI has none: a test file whose imports fail prints `SKIP`
and exits 0 without pytest, but exits 1 with it, so an extra package would make a session
disagree with CI.

A test that reads the clock should pass at any time of day and on any date. Steer by a
fixed `now=` where the code takes one; where it reads the real clock, build every
timestamp the assertion depends on from `utcnow()`, never a literal date, and don't step
a fixed number of hours into a window that closes. `scripts/ci/fake_clock.py --sweep
tests/test_x.py` runs a file at the times that break such tests (midnight, the hour
before it, month and year ends, a leap day, daylight-saving changes, dates years ahead)
and names any time whose result differs from a real run.

A test that reads the Settings page as text reads it through
`tests/_settings_src.py`'s `settings_source()`, not `settings.html` itself. Each
integration's slide-over form is its own file, `templates/settings/panels/<key>.html`,
included by `settings.html`, and `settings_source()` puts them back in place. The page's
tabs, the `integrations[]` list and the `settingsPage()` script stay in `settings.html`.
A new panel is a new file there plus one `{%- include %}` line, and its state goes in the
script.

### Other checks CI runs

Each has a script, or a single command, that runs the same way locally:

| Check | What it catches | Run it locally |
|---|---|---|
| Skip guard (in `run_tests.sh`) | A test file that passed only because its tests printed `SKIP`, from a missing stub or dependency | `scripts/ci/run_tests.sh` |
| `browser-smoke` | A page that throws a JavaScript error in a real browser: a binding that reads a still-null object, a name the component never defines. Also a failure the page catches and hides: a `console.error`, or an `/api/` call answering a 5xx other than 503 (503 is the deliberate "provider unavailable" answer). Every page is loaded, every tab clicked, every Settings panel opened. Then: the built Tailwind CSS is fresh, and every class a page carried has a rule | `scripts/ci/browser_smoke.sh` (needs `npm install playwright@1.56.1` and `npx playwright install chromium`) |
| `ruff` | Bug rules only: undefined names, a dict key written twice, a closure over a loop variable, a mutable default (`ruff.toml`) | `ruff check` |
| `ansible-syntax` | A playbook Ansible cannot load: an unknown module, a malformed task | `scripts/ci/ansible_syntax.sh` (needs `pip install ansible`) |
| `terraform` | Unformatted HCL, or a module that fails `terraform validate` | `scripts/ci/terraform_validate.sh` |
| `Images` workflow | A Dockerfile that no longer builds, or a dashboard image that does not boot to `GET /api/health` and render a page | `docker build .` |
| Trivy (in `Images`) | A fixable HIGH/CRITICAL vulnerability in an image's OS or Python packages. Vendored Go binaries (Terraform, providers, Packer, OPA) are reported, not blocking | `scripts/ci/trivy_image.sh IMAGE` |
| `pip-audit` (`Security`) | A Python package the app installs has a published vulnerability | `scripts/ci/pip_audit.sh` (needs `pip install pip-audit`) |
| `gitleaks` (`Security`) | A commit adds something shaped like a credential | `scripts/ci/gitleaks.sh origin/main..HEAD` |

`Security` and the image scans also run nightly, because an advisory published today
applies to code nobody changed. Dependabot (`.github/dependabot.yml`) proposes the
upgrades that clear them. A finding that does not apply goes in that tool's exceptions
file with the reason, never silently: `.pip-audit-ignore`, `.trivyignore.yaml`
(scoped by path) or `.gitleaksignore` (reviewed fingerprints, never a real secret). A
binary a Dockerfile downloads is pinned to a version and to its vendor's SHA256, and a
version bump updates both. Front-end libraries are pinned too: Alpine.js is vendored
under the static `js/vendor` folder (the npm package's `dist/cdn.min.js`, named for its
version), and every CDN script or stylesheet names an exact version, which
`tests/test_static_assets.py` enforces. Tailwind is not loaded from a CDN at all:
`web_dashboard/static/css/tailwind.css` is built by `scripts/build_css.sh` (a pinned
tailwindcss scanning every template, script and Python file that `tailwind.config.js`
lists) and committed. After adding a class name that appears nowhere else, run
`scripts/build_css.sh` and commit the result; CI fails a stale file. Write class names out
in full -- `'bg-' + colour` gets no rule, and `scripts/ci/css_coverage.sh` fails the smoke
test when a page carries one. `scripts/ci/frontend_pins.py` (the weekly **Front-end pins**
workflow) reports every one of these pins that is behind a release on its own major; to
bump Tailwind, change `VERSION` in `scripts/build_css.sh` and rebuild, and to bump Alpine,
replace the vendored file and the `static_url` reference on the four pages that own their
`<head>`. A page error, `console.error` or 5xx the smoke test
should tolerate goes in `tests/browser_smoke_allow.json`, scoped to its page, with the
reason.

When a skip is expected in the `tests` job, because another job runs the file with what
`tests` lacks (a real OPA binary, a PostgreSQL server), add the file to
`scripts/ci/skip_allowlist.txt` with the reason. Fix any other skip.

### Where the community can help most

The maintainer's home lab covers **VMware Workstation** and
**Hyper-V** — those two integrations get exercised on every PR that
touches `web_dashboard/`. The other on-premises hypervisor integrations
ship without regular hands-on coverage:

- **Proxmox VE** (`/proxmox`, `services/proxmox_service.py`)
- **VMware vSphere / ESXi** (`/vsphere`, `services/vsphere_service.py`)
- **Nutanix AHV** (`/nutanix`, `services/nutanix_service.py`)
- **XCP-ng / XenServer** (`/xcpng`, `services/xcpng_service.py`)

If you run any of those in your own lab, you are exactly the person we
need help from. Useful contributions include:

- **Bug reports** — file an issue when an action fails. Include the
  hypervisor version, what you clicked, the job's `extra_data` from
  `/api/jobs/{id}`, and the relevant lines from `docker compose logs app`.
- **PRs that fix what you find.** SDK behaviour drifts across hypervisor
  versions — a fix that worked for Nutanix Prism 6.x may need rework
  for 7.x. Patches that only widen the supported version matrix are
  welcome.
- **PRs that round out a feature** — add list/deploy/destroy parity
  with the other hypervisors. The pattern is consistent across services
  so a vSphere addition usually maps cleanly to a Proxmox one.
- **Smoke-test reports on a PR.** Even a "ran the new vSphere change
  against ESXi 8.0 U2 in my lab, deploy worked, destroy worked, list
  shows correct power state" comment closes the gap that the maintainer
  can't.

To enable an integration, flip its flag in `Settings → Integrations`
(or in the setup wizard on first run) and fill in the connection
details. The matching page in [docs/integrations/](docs/integrations/README.md)
has the field-by-field walkthrough for each hypervisor.

When you report a finding, please mention:

- Hypervisor product + version (e.g. `Proxmox VE 8.2.4`,
  `Nutanix AOS 6.10`, `vCenter 8.0 U2`).
- Whether you reached the failure via the UI or via `/api/...` directly.
- Whether the same action works against another hypervisor in your lab,
  if you have one — helps us tell "dashboard bug" from "this hypervisor
  rejects the call we make".

### Database: the suite is SQLite, production is PostgreSQL

Almost every file in `tests/` runs on SQLite. That is deliberate — it needs no service
and the application code is dialect-neutral by design — but it leaves one gap you need
to know about if you touch the schema.

#### Making a schema change

A new table needs nothing: `init_db()` runs `create_all` first, which builds it from the
model. A change to an **existing** table (a new column, an index, a retype) is a numbered
migration in `web_dashboard/schema_migrations.py`:

```python
MIGRATIONS = (
    Migration("0001_jobs_owner_note", (
        add_column("jobs", "owner_note", "TEXT"),
        create_index("ix_jobs_owner_note", "jobs", "owner_note"),
    )),
)
```

Add the column to the model as well. Each migration runs once per database, in name order,
inside `init_db`'s locked boot transaction, and is recorded in `schema_markers` under
`ddl:<name>`. Its steps must be safe on a database `create_all` has just built, because on
a new install the column is already there: `add_column` and `create_index` look before
they act, and a raw `sql(...)` step has to be written that way. A failing step rolls its
migration back, stops the ones after it, and is retried next boot with a warning naming
it. Never edit, rename or reorder a migration that has shipped.

**Do not add to `init_db`'s `_migrations` list.** It is frozen
(`tests/test_schema_migrations.py` holds its length). Each database runs it until one boot
leaves everything it adds in place, with no statement skipped for lock contention, then
records `ddl:legacy_statements_v1` and never reads it again. So a statement added there
would run on new installs and never on upgraded ones. Until a database has recorded it,
every boot logs what is still missing: *the legacy migration list has not fully applied
... Missing: ...*. That names a real gap to fix, not noise.

This exists because the old way, every statement on every boot, was not free on
PostgreSQL. Even a no-op `ALTER TABLE` takes ACCESS EXCLUSIVE on its table until the boot
transaction commits, which is how `init_db` came to deadlock with the jobs worker.

#### SQLite in tests, PostgreSQL in production

The trap the legacy list taught: a statement that is **malformed for PostgreSQL** fails
like one that was redundant, so on the old swallow-everything path it never applied, and
SQLite (more permissive about literals and types) kept the suite green.
`ALTER TABLE users ADD COLUMN is_admin BOOLEAN DEFAULT 0` was a live instance: PostgreSQL
will not coerce `0` to a boolean default, so the column was never added on any install
that predated it. Write a boolean default as `false`.

Three things catch schema mistakes, and a schema change should keep them green:

- `tests/test_schema_migrations.py`: the ledger on SQLite. A migration applies once,
  skips what is already there, and a failing one stops the rest.
- `tests/test_rbac_migration.py`: static, runs everywhere, no database needed.
- `tests/test_postgres_migrations.py`: the real statements against a real PostgreSQL,
  including an upgrade from before the ledger and a boot under lock contention. It
  **skips unless `DATABASE_URL` points at one**, so it costs nothing locally; CI runs it
  in a separate `postgres` job with a service container.

To run it yourself:

```bash
docker run -d --name pgtest -e POSTGRES_USER=test -e POSTGRES_PASSWORD=test \
  -e POSTGRES_DB=testdb -p 5432:5432 postgres:16-alpine
DATABASE_URL=postgresql://test:test@localhost:5432/testdb \
  python tests/test_postgres_migrations.py
```

A **fresh** database proves nothing about an `ALTER`: `create_all` has already built every
column, so the statement has nothing to do. Test a migration the way it meets an existing
install: drop its column, run `init_db()`, and require it back.

### Local Filesystem / UNC storage backend

The Local backend on `/storage` is a contributor-testing focus area for
the same reason: the maintainer's lab uses it for VMware Workstation and
Hyper-V, but any combination of (on-prem hypervisor) × (corporate file
share) is worth testing. Specifically useful:

- **UNC paths** against domain-joined Windows file servers (`\\server\share`
  with AD username + password / domain).
- **Bind-mounted host directories** for Linux dashboard hosts pointing at
  e.g. `/mnt/playbooks` exposed in `docker-compose.yml`.
- The constraint that this backend **only activates when the Ansible
  runner is `local`** — if you switch the runner to ECS/ACI/Cloud Run,
  the radio button on `/storage` should disable with a useful tooltip,
  and `PATCH /api/storage/config` should return 400. Confirm both.

See [docs/operations/storage-management.md → Local Filesystem / UNC](docs/operations/storage-management.md#local-filesystem--unc)
for the field-by-field walkthrough.

### Sanity check: integrations off

Independent of the above, the community edition must still **boot
cleanly** with every optional flag off — no contributor PR should
break that. Before merging, verify in a fresh `.env` with only `AWS_*`,
`AZURE_*`, `GCP_*` set:

- `/api/features` returns all optional flags `false`.
- Feature-gated pages 404:
  - `/vms` (vmware_enabled)
  - `/proxmox`, `/vsphere`, `/hyperv`, `/nutanix`, `/xcpng`
  - `/config-mgmt` (ansible_enabled)
- Always-on pages render: `/`, `/aws`, `/azure`, `/gcp`, `/jobs`,
  `/users`, `/groups`, `/settings`, `/secrets`, `/containers`.
  *(`/containers` shows On-Premises / Cloud tabs; the on-prem
  Portainer tab self-gates on `portainer_enabled` per-call.)*
- No `ImportError` or warmer errors in `docker compose logs app`
  except cloud-auth failures (expected with placeholder creds).

## Reporting issues

Open a GitHub issue with:

- The onboarder output (`./scripts/onboard.sh` or
  `.\scripts\Onboard-Dashboard.ps1`).
- Last 100 lines of `docker compose logs app`.
- OS / Docker / shell version (`docker version`, `bash --version` or
  `$PSVersionTable`).
- Expected vs. actual behaviour.

**Do not paste `.env` contents** — they contain cloud credentials.
