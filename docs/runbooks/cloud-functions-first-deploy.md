# Runbook — first Cloud Functions deploy (GCP → AWS → Azure)

The first end-to-end run of the Cloud Functions preview. Nothing in this feature has
been executed against a real cloud, so treat this as a bring-up: the goal of each
stage is to retire one class of risk, not to reach a working Entitle grant on the
first attempt. See [`../integrations/cloud-functions.md`](../integrations/cloud-functions.md)
for the reference and [`../design/cloud-functions.md`](../design/cloud-functions.md)
for why it is built this way.

**Order matters.** GCP first exercises the longest path (Cloud Build) and the
largest IAM surface, so it surfaces the most per attempt. AWS second is the fastest
loop. Azure last, because it carries the most independent ways to fail — doing it
after two clouds have proven the contract makes any failure unambiguously Azure's.

---

## 0. Before you start (10 minutes, no cloud)

1. **Enable the feature.** Settings → Preview features → **Cloud Functions**. The
   nav entry and `/api/functions` appear immediately; no restart.
2. **Confirm the image has the modules.** A published image missing a Terraform
   module fails only at deploy time, inside a job:
   ```bash
   docker compose exec app ls terraform/cloud_function
   ```
   Expect `aws_lambda  azure_function_app  gcp_cloudrun`.
3. **Confirm the vendored drivers are staged** (only needed for `db_grant`):
   ```bash
   docker compose exec app ls /opt/fn-vendor/linux-x86_64
   ```
   Expect `pymysql pytds OpenSSL cryptography cffi` and a `_cffi_backend*.so`.
   Missing → the image predates the vendor step; rebuild.
4. **Settings → Integrations → Cloud Functions** → set the package store for the
   cloud you are starting with. A cloud without one is offered in the deploy form
   but blocked, with the reason.

---

## 1. GCP — `echo_diag`, public

The longest path and the biggest IAM surface, so start here.

**Grant the service account these roles first.** A missing one surfaces as a 403
roughly 90 seconds into Cloud Build, *after* a successful plan — the least pleasant
failure in the feature:

```
roles/cloudfunctions.developer   roles/run.admin
roles/cloudbuild.builds.builder  roles/artifactregistry.writer
roles/storage.objectAdmin        roles/secretmanager.admin
roles/iam.serviceAccountUser     (on the runtime service account)
```

1. **Functions → Deploy function** → workload `echo_diag`, cloud `gcp`, network
   `public`. Expect 60–120 s: every GCP deploy runs Cloud Build.
2. Watch the job. `still creating` for a minute or two is normal here and the
   progress bar says so.
3. When it reports available, **Test invoke** with:
   ```json
   { "probe": [{ "host": "example.com", "port": 443 }] }
   ```
   Expect `dns: ok`, `connect: ok`, and `placement.gcp_service` populated.
4. **Prove the auth is real** — the one check worth doing by hand, since Test
   invoke always attaches the credential. Take the URL and secret from
   **Endpoint** (admin only):
   ```bash
   curl -sS -o /dev/null -w '%{http_code}\n' -X POST "$FN_URL" -d '{}'
   ```
   Expect `401`. Then with a wrong secret — expect a **byte-identical** 401.
   A `500 {"error":"function not configured"}` with **no** other field means the
   secret never reached the function's environment; that is the handler failing
   closed, which is correct behaviour for a misconfiguration.

   The same body **with** a `problem` field is a different thing: the caller got past
   auth and the *workload's* own settings are what is missing. `problem` names the
   setting. (An adapter used to raise instead, which dispatch turned into
   `500 {"error":"internal error"}` on every route — including `/check_config` —
   so an unconfigured function had no way to say what was wrong.)
5. **Destroy it** and confirm the row and the cloud resource both go.

> `terraform destroy` removes the function but **leaves the Artifact Registry
> images** Cloud Build pushed (`gcf-artifacts` in the region). Expected, not a bug —
> clean up separately if the storage cost matters.

**If step 1 fails at plan:** the module or provider. **If it fails ~90 s in:** IAM,
almost always. The Cloud Build log names the missing permission.

---

## 2. AWS — `echo_diag`, public then `vpc`

The fastest loop (~5 s deploys), so this is where to iterate on the VPC story.

1. Deploy `echo_diag`, cloud `aws`, network `public`. Repeat the curl matrix from
   step 1.4.
2. **Now the VPC attachment**, which is the Phase 2 gate. Settings → Cloud
   Functions → set `aws_functions_subnet_ids` and
   `aws_functions_security_group_ids` (the security group must be one already
   allowed into your database's SG). Deploy again with network `vpc`, then probe a
   private endpoint:
   ```json
   { "probe": [{ "host": "<private-db-host>", "port": 3306 }] }
   ```
   Expect `connect: ok` — from an external caller, to an endpoint with no public
   route. That is the property the whole feature rests on.
3. **Read the `egress` block in the same response.** On a VPC-attached Lambda with
   no NAT it will time out. That is expected and only matters for workloads that
   call outward; `echo_diag` reports it precisely so you find out here rather than
   from a workload hanging.

| Symptom | Cause |
|---|---|
| function creates, every invoke fails with an ENI error | the VPC execution role did not attach — check the module applied `AWSLambdaVPCAccessExecutionRole` |
| `dns: ok`, `connect: timeout` | routing or the security group |
| `connect: refused` | **good news** — the path works, nothing is listening on that port |

---

## 3. Azure — `echo_diag`, public then `vnet`

Left until last deliberately. Five independent things can produce an app that boots,
serves the default landing page, and registers zero functions.

1. Deploy `echo_diag`, cloud `azure`, network `public`.
2. **Before anything else, curl the anonymous health route.** It needs no key and
   answers the only question worth asking first:
   ```bash
   curl -sS https://<app>.azurewebsites.net/api/health
   ```
   `{"ok":true}` → the v2 model indexed the function; any problem is elsewhere.
   Anything else → indexing failed. Check, in order: the vendored
   `azure-functions` is in the zip, `FUNCTIONS_EXTENSION_VERSION=~4`,
   `AzureWebJobsFeatureFlags=EnableWorkerIndexing`, and that
   `WEBSITE_RUN_FROM_PACKAGE` points at the blob hash you expect.
3. Repeat the curl matrix, remembering the route is under `/api` — the base URL the
   dashboard records is `https://<app>.azurewebsites.net/api` and the adapter routes
   append to it.
4. **VNet mode.** Set `azure_functions_subnet_id` — it must be delegated to
   `Microsoft.Web/serverFarms`, and it is a **different subnet** from the database
   one (which is delegated to the DB service). Redeploy with network `vpc`.
5. Probe the private database. **A `dns: timeout` here is almost always
   `WEBSITE_DNS_SERVER`**: `vnet_route_all_enabled` fixes routing and does nothing
   for DNS, so `privatelink.*` never resolves without it. The module sets it
   whenever a subnet is configured — if you see the timeout, check it survived.

> Plan SKU: `B1` (the default) supports VNet integration. `Y1` (Consumption) does
> **not** — the module refuses that combination at plan time rather than letting it
> deploy and fail mysteriously.

---

## 4. The Entitle path, granting nothing

Once one cloud works, prove the whole Entitle round trip before any real target is
involved.

1. Deploy the **`entitle_webhook_echo`** workload. It implements every Remote
   Adapter route with valid empty responses and grants nothing.
2. In Entitle, create a REST integration pointing at it. Paths (these are the
   defaults the adapters serve):
   ```
   get_assets_path           /get_assets
   get_actors_path           /get_actors
   get_all_permissions_path  /get_all_permissions
   create_actor_path         /create_actor
   delete_actor_path         /delete_actor
   give_access_path          /give_access
   revoke_access_path        /revoke_access
   ```
   Auth: `headers` → `{"Authorization": "Bearer <the function's secret>"}`.
3. Request access in Entitle and watch the function's log. The response body
   carries an `observed` block naming which contract fields arrived — that is the
   fastest way to see a mismatch.
4. **A 404 from the function lists the routes it actually serves.** Entitle's
   `*_path` fields are configurable, so a mismatch there is the most likely setup
   mistake by a wide margin.

> ⚠️ `entitle_rest_app_slug` defaults to `"rest api"` and is **unconfirmed against a
> real catalog**. If registration fails with a 404 "Application not found", check
> your tenant's `entitle_applications` data source and set the real slug in
> Settings → Integrations → Entitle.

---

## 5. A real grant, in dry run

1. Provision a **MySQL** database with **Register in Entitle** checked. The
   provision path routes MySQL and SQL Server to an adapter automatically (their
   native connectors cannot mint accounts); Postgres keeps its native connector.
2. Watch for the `clouddb_adapter_pair` job. It stages the admin credential in the
   cloud's secret store, deploys a `db_grant` function VNet/VPC-attached beside the
   database, and registers it.
3. The adapter is deployed **in dry run**. Request access in Entitle and read the
   response: it contains the exact SQL it *would* run, per connection, and touches
   nothing.
4. Only when that SQL looks right, set `FN_DB_DRY_RUN=0` on the function and
   request again.
5. **Immediately after arming it, call `POST /check_config`.** A dry run proves the
   SQL and nothing else — it opens no connection, so it cannot tell you whether the
   function can reach the database at all. Armed, `check_config` opens one real
   connection and reports TLS, the firewall, the VNet route or a wrong admin
   credential as a sentence. Skip this and the first thing you learn is
   `500 {"error": "internal error"}` on a colleague's real access request, with the
   reason only in the function's log stream.

**On Azure SQL specifically**, check the dry-run plan has **two** connections —
the login in `master`, the contained user in the target database. One connection
means the flavor resolved to `rds` and the grant would fail.

---

## Expect to tweak

This is a first bring-up of code that has never run in a cloud. The most likely
places to need adjustment, roughly in order:

1. GCP IAM roles (a 403 late in Cloud Build).
2. Azure function indexing (the `/api/health` check exists for this).
3. The Entitle application slug.
4. `WEBSITE_DNS_SERVER` / subnet delegation on the Azure private path.
5. The vendored driver set, if `db_grant` fails at import in the cloud.

Unit tests cover the contract, the packaging, the SQL and the guards; what they
cannot cover is any of the five above.
