"""AWS Lambda entry point. Packaged at the zip root as ``aws_entry.py``;
``handler = "aws_entry.lambda_handler"`` in the Terraform module.

``workload`` is the single workload module the packager copied to the zip root.
"""
import workload
from fnruntime import adapters, dispatch


def lambda_handler(event, context):
    request = adapters.from_aws(event, context)
    response = dispatch.handle_request(request, workload)
    return adapters.to_aws(response)
