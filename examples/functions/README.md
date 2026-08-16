# Cloud Function examples

Sample request payloads for the built-in workloads, plus a template for writing your
own. See [docs/integrations/cloud-functions.md](../../docs/integrations/cloud-functions.md).

> **Note:** deployable handler source lives in `web_dashboard/functions/fnworkloads/`,
> **not here** — that tree is copied into the dashboard image, this one is not. To add
> a workload, copy `custom_handler.py` into `fnworkloads/` and rename it.

## Files

| File | What it is |
|---|---|
| `custom_handler.py` | Template for a new workload. Copy into `fnworkloads/`. |
| `echo_diag.request.json` | Probe a private database endpoint and check egress |
| `entitle_webhook_echo.request.json` | An Entitle Give Access payload |

## Calling a function

Get the URL and secret from **Functions → Endpoint** (admin), then:

```bash
curl -sS -X POST "$FN_URL" \
  -H "Authorization: Bearer $FN_SECRET" \
  -H 'content-type: application/json' \
  -d @echo_diag.request.json
```

Two checks worth running once per function, because they are the ones that catch a
misconfigured deployment:

```bash
# No credential → 401 with no detail
curl -sS -o /dev/null -w '%{http_code}\n' -X POST "$FN_URL" -d '{}'

# Wrong credential → a byte-identical 401
curl -sS -X POST "$FN_URL" -H "Authorization: Bearer wrong" -d '{}'
```

A `500 {"error":"function not configured"}` means the shared secret never reached the
function's environment. That is the handler failing **closed** — it will not serve an
unauthenticated request just because its secret is missing.

## Reading `echo_diag` output

| Result | Meaning |
|---|---|
| `dns: failed` | The resolver answered: no such name |
| `dns: timeout` | The resolver never answered — on Azure, usually a missing `WEBSITE_DNS_SERVER` |
| `dns: ok`, `connect: timeout` | Routing or the security group / NSG |
| `connect: refused` | The path works; nothing is listening on that port |
| `connect: ok` | Working |
| `egress.connect: timeout` | No outbound internet — a VPC-attached Lambda needs NAT |

`dns_ms` and `connect_ms` are reported separately because slow-but-working and broken
are different problems.
