"""GCP Cloud Run functions entry point.

Packaged at the zip root as **main.py** — the Python buildpack looks for that exact
filename — with ``entry_point = "main"`` in the Terraform module.

``request`` is a ``flask.Request``, but nothing here imports Flask: the adapter
duck-types it (see fnruntime.adapters.from_gcp).
"""
import workload
from fnruntime import adapters, dispatch


def main(request):
    normalized = adapters.from_gcp(request)
    response = dispatch.handle_request(normalized, workload)
    return adapters.to_gcp(response)
