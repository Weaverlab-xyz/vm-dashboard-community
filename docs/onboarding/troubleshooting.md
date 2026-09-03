# Onboarding troubleshooting

> **Audience:** operator · **Profile:** `both` · **Read this when:** the dashboard will not start, will not pull, or will not let you in.

Part of the [Onboarding Guide](../ONBOARDING.md).


### Onboarding script exits at preflight

- **"PowerShell 7+ is required"** — you're running Windows PowerShell 5.
  Install PS7 (<https://aka.ms/powershell>) and rerun with `pwsh`.
- **"docker not found"** — Docker isn't installed or isn't on `PATH`.
  Windows/Mac: reinstall Docker Desktop. Linux/WSL: install Docker Engine
  (`sudo apt install docker.io`) and restart your terminal.
- **"Docker daemon is not responding"** — Windows/Mac: Docker Desktop is
  installed but not running — launch it and wait for the whale icon to
  settle. Linux/WSL: run `sudo service docker start` (or
  `sudo systemctl start docker`) then rerun the script.

### WSL: `docker pull` fails with a certificate error

**Symptom:** `docker pull postgres:16-alpine` (or any image) fails with:
```
x509: certificate signed by unknown authority
```

**Cause:** Your network uses an SSL-inspection proxy (Zscaler, Palo Alto, etc.)
that re-signs outbound TLS traffic with a corporate root CA. WSL does not
inherit Windows' trusted root store, so Docker inside WSL rejects the
intercepted certificate.

**Fix — run once per WSL distro install:**

**Step 1 — identify and export the proxy root CA (PowerShell on Windows):**

```powershell
# List trusted roots — look for your security vendor (Zscaler, etc.)
Get-ChildItem Cert:\LocalMachine\Root | Select-Object Subject, Thumbprint | Sort-Object Subject

# Export the relevant cert (replace <Thumbprint> with the value above)
$cert = Get-ChildItem Cert:\LocalMachine\Root\<Thumbprint>
Export-Certificate -Cert $cert -FilePath "$env:TEMP\corp-root.cer" -Type CERT
```

If you are unsure which cert to export, export them all and let WSL sort it out:

```powershell
New-Item -ItemType Directory -Force "$env:TEMP\roots" | Out-Null
Get-ChildItem Cert:\LocalMachine\Root | ForEach-Object {
    Export-Certificate -Cert $_ `
        -FilePath "$env:TEMP\roots\$($_.Thumbprint).cer" -Type CERT
}
```

**Step 2 — import into WSL and update the system trust store:**

```bash
# Single cert
openssl x509 -inform DER \
    -in /mnt/c/Users/$(cmd.exe /c echo %USERNAME% 2>/dev/null | tr -d '\r')/AppData/Local/Temp/corp-root.cer \
    -out /tmp/corp-root.pem
sudo cp /tmp/corp-root.pem /usr/local/share/ca-certificates/corp-root.crt
sudo update-ca-certificates
```

If you exported all certs, convert and import them in a loop:

```bash
WINTEMP="/mnt/c/Users/$(cmd.exe /c echo %USERNAME% 2>/dev/null | tr -d '\r')/AppData/Local/Temp/roots"
sudo mkdir -p /usr/local/share/ca-certificates/windows-roots
for f in "$WINTEMP"/*.cer; do
    name=$(basename "$f" .cer)
    openssl x509 -inform DER -in "$f" \
        -out "/usr/local/share/ca-certificates/windows-roots/$name.crt" 2>/dev/null || true
done
sudo update-ca-certificates
```

**Step 3 — add the cert to Docker's registry trust store:**

```bash
sudo mkdir -p /etc/docker/certs.d/registry-1.docker.io
sudo cp /tmp/corp-root.pem /etc/docker/certs.d/registry-1.docker.io/ca.crt
sudo service docker restart
```

**Verify host-side pulls work:**

```bash
docker pull hello-world
```

**Step 4 — make the cert available inside the image build:**

Steps 1–3 fix the host's connection to Docker Hub, but `apt-get update` and
`pip install` *inside* the image build go through the same TLS-inspecting
proxy and need the cert too. Drop a copy into `corp-ca/` at the repo root:

```bash
cp /tmp/corp-root.pem ./corp-ca/corp-root.crt
```

The Dockerfile copies any `.crt` / `.pem` file in `corp-ca/` into the image's
system trust store and points `pip`, `requests`, and `curl` at it. Files in
that directory are gitignored, so your cert stays local.

Now rerun `./scripts/onboard.sh --build`.

**Step 5 — (`--hub` only) trust the cert *inside the published container*:**

The published image is built by CI on a clean network, so it carries no corp
CA — and unlike the build path, there's no image build to inject it into.
That's fine until a feature makes outbound TLS calls *from inside the
container*: Terraform-backed provisioning (cloud databases) runs
`terraform init`, which fetches providers from `registry.terraform.io` and
fails with:

```
terraform init failed: ... x509: certificate signed by unknown authority
```

Fix: start the hub stack with the corp-CA overlay, which bind-mounts the
host's system CA bundle (updated in step 2 above) read-only over the
container's, and sets `AWS_CA_BUNDLE` for boto3:

```bash
./scripts/onboard.sh --hub --corp-ca
# or manually:
docker compose -f docker-compose.hub.yml -f docker-compose.corp-ca.yml up -d
```

On a non-Linux host (no `/etc/ssl/certs/ca-certificates.crt`), point
`CORP_CA_BUNDLE` at a PEM file containing the public roots plus your corp CA.

### Stack starts but `/api/health` doesn't respond

```powershell
docker compose logs --tail 100 app
```

Common causes:

| Symptom in logs                                   | Likely cause                                      | Fix                                                                                          |
|---------------------------------------------------|---------------------------------------------------|----------------------------------------------------------------------------------------------|
| `InvalidClientTokenId` / `InvalidSignature`       | AWS access key wrong or rotated                   | Rerun `aws iam create-access-key`, then update via the reconfigure wizard (`/setup`)         |
| `AuthenticationFailed` from Azure                 | Azure SP secret wrong or expired                  | Regenerate with `az ad sp credential reset`, then update via the reconfigure wizard (`/setup`) |
| `connection refused` on port 5432                 | Postgres container not healthy                    | `docker compose ps`; check `db` container logs                                               |
| `Address already in use` on 8001                  | Another process is bound to 8001                  | Stop it, or change the port mapping in `docker-compose.yml`                                  |

### Login fails with "Invalid credentials"

- The admin account is created in **Step 1 of the setup wizard** on
  first run. Use the username and password you entered there.
- If you've forgotten the password, change it from **Settings → Security**
  while logged in, or reset the entire stack:
  ```bash
  docker compose down -v   # ⚠ wipes the database and all stored credentials
  ./scripts/onboard.sh     # brings it back up; wizard appears again on first visit
  ```

### JWT key file: backup and loss recovery

`.jwt_secret_key` at the repo root is the **root of trust** for all credentials
you store through the setup wizard. The app uses it to encrypt every integration
secret (AWS keys, Azure SP credentials, etc.) in the database.

**It cannot be migrated to a vault** in the community edition — it's the bootstrap
key that decrypts everything (including any vault credentials), so it must be
present at startup from the host. See [Protect and back up the JWT
key](after-first-run.md#protect-and-back-up-the-jwt-key) above and
[why](../secrets-management.md#why-the-jwt-root-key-cannot-be-migrated). (Removing the
on-disk key via cloud workload identity is a SaaS-edition feature.)

**Protect it:** back it up somewhere safe (password manager, encrypted drive), and
don't commit it to git (it's in `.gitignore`).

**If you lose it**, every stored credential is unrecoverable and the app will
refuse to start (the key file is required). Recovery procedure:

```bash
# 1. Stop the stack
docker compose down

# 2. Remove the old key and database volume (⚠ wipes all stored credentials)
rm .jwt_secret_key
docker volume rm vm-dashboard-community_pgdata   # adjust prefix to match 'docker volume ls'

# 3. Rerun the onboard script — it regenerates the key and the wizard reappears
./scripts/onboard.sh
```

**Rotating the key** is not currently supported without clearing the database.

### Where to file issues

Open a GitHub issue with:

1. The output of `.\scripts\Onboard-Dashboard.ps1` (copy the terminal)
2. The last 100 lines of `docker compose logs app`
3. Your OS / Docker Desktop / PowerShell versions
4. What you expected vs. what happened

**Do not paste `.env` contents** — they contain your cloud credentials.

---
