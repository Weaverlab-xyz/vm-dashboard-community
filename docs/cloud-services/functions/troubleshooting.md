# Troubleshooting Cloud Functions

> **Audience:** operator · **Profile:** `demo` · **Read this when:** a function will not deploy, answers 401 or 403, or cannot reach what it should act on.

Part of [Cloud Functions](../functions.md).

**The deploy form says a cloud is blocked.** Its package store isn't configured —
the reason names the exact setting. Settings → Integrations → Cloud Functions.

**`echo_diag` reports `dns: timeout` on Azure.** Almost always a missing
`WEBSITE_DNS_SERVER`. The module sets it whenever a subnet is configured;
`vnet_route_all_enabled` fixes *routing* and does nothing for DNS, so a
correctly-routed function still cannot resolve `privatelink.*` without it.

**`echo_diag` reports `connect: timeout` but DNS is fine.** Routing or the security
group / NSG. On AWS, confirm the function's security group is allowed inbound at the
target. `connect: refused` is different and *good news* — the network path works and
nothing is listening on that port.

**`egress` probe times out but the private probe succeeds.** Expected on a
VPC-attached Lambda with no NAT. Harmless for workloads that only reach inward.

**AWS: every invoke of a `vpc`-mode function returns 500, but the deploy was clean.**
Auth failed closed. The handler resolves its bearer secret from **Secrets Manager at
cold start**, so a VPC-attached Lambda needs a path to that API — and a private
subnet with no NAT route has none. This is *not* only an outward-calling concern: it
breaks the front door for every workload. Give the VPC a `secretsmanager` **interface
endpoint** with private DNS (the sandbox scripts create one; `SANDBOX_SKIP_FN_VPCE=1`
opts out), or keep a NAT route up. It looks exactly like a wrong bearer secret.

**Azure: the app is up but every route 404s.** The v2 programming model registered
zero functions. `GET https://<app>.azurewebsites.net/api/health` is anonymous and
answers this in one request: a 200 means indexing worked and the problem is
elsewhere; anything else means the app didn't index. The module already sets
`FUNCTIONS_EXTENSION_VERSION=~4` and `AzureWebJobsFeatureFlags=EnableWorkerIndexing`,
which are the usual causes.

**Azure: code changes aren't taking effect.** Package blobs are content-addressed
(`function-packages/<fn_id>/<sha256>.zip`) specifically to prevent this. If you see
it, check that `WEBSITE_RUN_FROM_PACKAGE` points at the hash you expect.

**GCP: the deploy fails ~90 s in, after a successful plan.** Cloud Build. Usually a
missing IAM role — see [Prerequisites](../functions.md#prerequisites).

**GCP: destroy leaves things behind.** `terraform destroy` removes the function but
not the images Cloud Build pushed to Artifact Registry (`gcf-artifacts` in the
region). Clean those up separately if the storage cost matters.

**Every apply redeploys every function.** Shouldn't happen — packages are built
deterministically so an unchanged function is a no-op. If it does, run
`python tests/test_function_package.py`; the byte-stability assertion is there to
catch exactly this.
