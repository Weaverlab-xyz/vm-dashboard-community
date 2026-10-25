# Writing your own workload

> **Audience:** operator · **Profile:** `demo` · **Read this when:** none of the shipped workloads does what you need and you want to add one.

Part of [Cloud Functions](../functions.md).

> **Workloads are repository files, not uploads.** There is deliberately **no
> upload path** for handler code: a workload is added by committing a file to
> `web_dashboard/functions/fnworkloads/` and shipping an image. This is not the
> same pipeline as [asset uploads](../../operations/storage-management.md#what-counts-as-an-asset)
> and the `.yml`/`.sh`/`.ps1` allowlist there does not apply.
>
> The reason is what these handlers hold. A workload runs with the database admin
> credential, or rights to write Azure role assignments, from inside your VPC. An
> endpoint that accepted arbitrary uploaded Python and then ran it with those
> credentials would be a remote-code-execution surface wearing a feature's clothes.
> Requiring a commit means every workload passes review, diff and CI — and it is
> also what makes the package **deterministic**, since the source is fixed at image
> build rather than varying per upload.
>
> **You do not have to upstream your workloads.** Run your own image: fork, add
> handlers under `fnworkloads/`, build, and deploy. Everything else — the packager, the
> Terraform modules, the adapter contract, the guards — works unchanged, and
> [provenance](../functions.md#provenance-what-is-actually-running) records which source produced each
> deployed function, so a fleet built from several images is still auditable.

Copy `examples/functions/custom_handler.py` into `fnworkloads/`, and implement one
function:

```python
from fnruntime.contract import Request, Response, Context

NAME = "my_workload"
DESCRIPTION = "One line, shown in the deploy form."

def handle(req: Request, ctx: Context) -> Response:
    return Response(200, {"ok": True})
```

Two rules:

- **Standard library only.** No pip dependencies (on AWS you may also use `boto3`,
  which the Lambda runtime ships — import it *inside* `handle`). This is what keeps
  a package ~30 KB and needs no build step.
- **Never log secrets.** Use `fnruntime.logs.emit`, which redacts automatically.

If a workload needs a cloud SDK only one runtime provides, add it to
`cloud_function_service._CLOUD_RESTRICTED`.
