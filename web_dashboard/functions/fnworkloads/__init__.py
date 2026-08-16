"""Workload modules — exactly ONE is packaged per deployed function.

Each module exports ``NAME``, ``DESCRIPTION`` and ``handle(req, ctx) -> Response``,
and imports nothing but ``fnruntime`` and the standard library (plus, on AWS only,
``boto3``, which the Lambda runtime already ships). The packager copies the chosen
module to the zip root as ``workload.py``.

Keep the dashboard-side catalog in ``cloud_function_service.VALID_WORKLOADS`` in
sync — ``tests/test_function_samples.py`` asserts they match.
"""
