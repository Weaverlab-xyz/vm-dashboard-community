# Backup and Restore

> **Audience:** operator · **Profile:** `both` · **Read this when:** you want to back up the dashboard, restore it onto the same install or a rebuilt one, or find out what a restore needs besides the database.

Part of [Operations](../operations.md).

The dashboard's state is a PostgreSQL database plus the secret that decrypts it. **A database
backup on its own is not a backup.** Every stored credential in it is encrypted with a key that
lives outside the database, so a dump restored without that key comes back with the Settings
panel showing every secret as unset.

## What to back up

| What | Where it lives | Without it |
|---|---|---|
| **The database** | The `db` service's `pgdata` volume (`vmclidashboard`, user `dashboardadmin`) on Docker Compose; your `DATABASE_URL` database elsewhere | Everything: users, roles, jobs, inventory, settings, audit trail |
| **The JWT key** | `.jwt_secret_key` at the repo root, mounted as the `jwt_key` Docker secret; or `JWT_SECRET_KEY` / `JWT_SECRET_KEY_FILE` on a platform | Every stored credential reads as empty, and the app will not start without the key |
| **`CONFIG_ENCRYPTION_KEY`**, if you set it | An env var, `CONFIG_ENCRYPTION_KEY_FILE`, or `/run/secrets/config_encryption_key` | Same as above. When it is set it encrypts the stored secrets **instead of** the JWT-derived key, so it is the one that matters, and it cannot be rotated |
| **Your `.env`** | Repo root | The host-level settings you chose (ports, hostnames, `POSTGRES_PASSWORD`). Do not paste it into tickets: it holds credentials |
| **Terraform state** | `terraform-state/<job_id>/` in your storage backend (S3 / Blob / GCS) | The ability to destroy what a job built. See [Cloud hosting](../platform/cloud-hosting.md): there is no persistent volume for it |

Nothing else needs saving. The `runner_work` volume (`vmdash_runner_work`) is scratch space for
the transient kubectl and helm containers, and the Docker socket and source bind mounts are
not state.

Keep the key backup **separate from the database backup**, in a different place with different
access, or one stolen archive holds both halves. Both are needed to restore, so label them as
a pair and keep them from the same moment: the keys cannot be rotated today, so a key backup
stays valid, but a note of which database dump it goes with saves a search later.

## Back up

On Docker Compose, from the repo root:

```bash
docker compose exec db pg_dump -U dashboardadmin -d vmclidashboard -Fc -f /tmp/dashboard.dump
```

```bash
docker compose cp db:/tmp/dashboard.dump ./dashboard-$(date +%Y%m%d).dump
```

```bash
docker compose exec db rm /tmp/dashboard.dump
```

The dump is written inside the container and copied out rather than redirected from `exec`,
because a shell redirect corrupts a binary file in PowerShell. The dump is a consistent
snapshot taken while the dashboard runs; you do not need to stop anything to take it.

Then copy the key (and `CONFIG_ENCRYPTION_KEY` if you use it) somewhere other than the dump:

```bash
cp .jwt_secret_key /your/secure/location/jwt_secret_key.bak
```

On a managed platform (Azure Database for PostgreSQL, Cloud SQL, RDS, or a Postgres
container on Container Apps) use the platform's own backup and point-in-time restore for the
database; the dump above is the same idea if you have `pg_dump` reach. The key lives in your
platform's secret store, so confirm it is **also** recorded somewhere that survives deleting
the app.

## Restore onto the same install

Use this to roll back a bad change or recover a damaged database, with the **same** keys in
place.

1. Stop everything that writes to the database:

   ```bash
   docker compose stop app worker
   ```

2. Copy the dump in and restore it over the existing database:

   ```bash
   docker compose cp ./dashboard-20261104.dump db:/tmp/dashboard.dump
   ```

   ```bash
   docker compose exec db pg_restore -U dashboardadmin -d vmclidashboard --clean --if-exists --no-owner /tmp/dashboard.dump
   ```

   `--clean --if-exists` drops and recreates each object that is in the dump, so those tables
   come back with the dump's contents. An object created **after** the dump was taken is not in
   it and is left alone; to remove those too, restore into a freshly created empty database.
   Warnings about objects that did not exist are expected; an `ERROR` is not.

3. Start again and check:

   ```bash
   docker compose start app worker
   ```

   ```bash
   curl http://localhost:8001/api/health
   ```

On start the app runs `init_db`, which brings an older dump up to the current schema. Restoring
a dump taken on an older release into a newer image is therefore fine. The reverse is not: do
not restore a dump from a **newer** release into an older image.

## Restore onto a rebuilt or different host

1. Bring up the stack **without running the setup wizard**, but with the original keys in
   place first: write the saved `.jwt_secret_key` back to the repo root (and set
   `CONFIG_ENCRYPTION_KEY` if you used one) **before** the first start. The onboard script
   generates a fresh key if it finds none, and a fresh key cannot read the dump.
2. Start only the database, restore as above, then start `app` and `worker`.

Deploy the same release you backed up from, or a newer one. Use the same `POSTGRES_PASSWORD`
in `.env`, or the app cannot connect to the restored database.

### If the keys are gone

The dump still restores, but each stored secret reads as **empty** and the app logs
`encrypted with a key this process does not have`. Nothing raises, and the Settings panel just
shows fields unset. Recovery is to re-enter the credentials, or, if you have a plaintext export,
to import it: see [Config migration](../platform/config-migration.md). The same page explains
why moving configuration **between two different installs** takes an export rather than a
`pg_dump`: a different install has a different key. A `pg_dump` is the right tool only when the
keys travel with it, as above.

## Check a restore actually worked

A restored database that starts is not proof that the credentials survived. After every restore
(and when you first set up backups, restore into a spare stack to prove it):

1. `curl http://localhost:8001/api/health` returns `200`.
2. Sign in, and open **Settings**. A credential you know you set must show as set, not blank.
3. Run something that uses a stored credential, such as the cloud's connection test on the
   setup page.
4. Check the logs for the ERROR line named above:

   ```bash
   docker compose logs app | grep "encrypted with a key"
   ```

   Any hit means a key mismatch.

## Related

- [JWT key file: backup and loss recovery](../getting-started/troubleshooting.md#jwt-key-file-backup-and-loss-recovery), including what wiping and starting over involves
- [Protect and back up the JWT key](../getting-started/after-first-run.md#protect-and-back-up-the-jwt-key)
- [Config migration](../platform/config-migration.md): moving configuration between two installs
- [Cloud hosting](../platform/cloud-hosting.md): the keys and Terraform state on a platform
- [Storage Management](storage-management.md): the backend that holds Terraform state
