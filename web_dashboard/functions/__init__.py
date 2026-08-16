"""Deployable cloud-function source (Cloud Functions preview feature).

Unlike every other package under ``web_dashboard/``, the code in here does NOT run
in the dashboard process — it is packaged into a zip by
``services/cloud_function_package.py`` and executed inside AWS Lambda / an Azure
Function App / a GCP Cloud Run function.

Layout — every subpackage here is zip material:

    fnruntime/    the portable contract: adapters, auth, logging, dispatch
    fnworkloads/  one module per workload; exactly one is packaged per function
    fnentry/      the per-cloud shim that lands at the zip root

Two rules that are easy to get wrong:

1. **Stdlib only.** ``fnruntime`` imports nothing outside the standard library, and
   a workload may additionally use only what its cloud's runtime already ships
   (Lambda has boto3; nothing else is guaranteed). Nothing here may import from
   ``web_dashboard`` — none of it is in the zip.
2. **Absolute imports.** In the zip, ``fnruntime/`` sits at the root and the chosen
   workload is copied in as ``workload.py``, so relative imports would break. Every
   module here therefore imports ``fnruntime.x`` absolutely.

Rule 2 is why importing this package puts its own directory on ``sys.path``: it
makes ``import fnruntime`` resolve in-repo (tests, the packager) exactly as it does
inside the zip, so there is ONE import style and no rewriting at package time. The
names are prefixed ``fn*`` precisely because they become top-level.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
