# UI robot

Drives the two things the APIs cannot do. Everything else (PS setup, PRA Jump Groups,
users, vault) stays on the APIs.

| Command | What it does |
|---|---|
| `installer` | Logs into Password Safe, downloads the Resource Broker installer, captures the installer key, uploads the installer through the dashboard's **active storage backend** |
| `integration` | Logs into PRA and creates the Password Safe integration |

Run `installer` first, API setup in between, `integration` last (it needs the PS API key).

## Storage

The robot has no cloud credentials. It uses the dashboard's chunked upload API
(`/api/storage/upload/*`, see `docs/operations/storage-management/uploading.md`), so the
file goes wherever the dashboard's active backend points (S3 on pov.weaverlab.app).

## Run

```bash
docker build -t ui-robot runners/ui-robot
docker run --rm -v "$PWD/work:/work" \
  -e DASHBOARD_URL -e DASHBOARD_SPIFFE_JWT_FILE \
  -e PS_URL -e PS_USER -e PS_PASSWORD -e PS_TOTP_SECRET \
  -e INSTALLER_KEY_FILE=/work/rb_installer_key \
  ui-robot installer
```

`integration` additionally needs `PRA_URL`, `PRA_USER`, `PRA_PASSWORD`,
`PRA_TOTP_SECRET` (optional), `PS_API_KEY`, `PS_RUNAS_USER`.
Dashboard auth is a **service account**, never a PAT (the dashboard refuses one at use).
Preferred: set `DASHBOARD_SPIFFE_JWT_FILE` to a JWT-SVID file whose audience is
`<dashboard>/api/oauth/token`, and the robot stores no secret (see
[Authenticating with a SPIFFE assertion](../../docs/profiles/pov/design/ui-robot.md#authenticating-with-a-spiffe-assertion)).
Otherwise set `DASHBOARD_CLIENT_ID` and `DASHBOARD_CLIENT_SECRET`. `DASHBOARD_SCOPE`
optionally narrows the token.
Set `DASHBOARD_CA_BUNDLE` for a corporate CA and `ROBOT_HEADED=1` to watch locally.

## Recording the selectors

`robot/selectors.py` is all `TODO`. For each flow, run once by hand:

```bash
pip install playwright && playwright install chromium
playwright codegen https://<password-safe-or-pra-url>
```

Do the steps, then copy the generated selectors into `selectors.py`. A flow stops with the
name of any selector still `TODO`.

## Not yet verified

Nothing here has been run against a live Password Safe or PRA, and the download and
integration steps are skeletons until the selectors are recorded.
