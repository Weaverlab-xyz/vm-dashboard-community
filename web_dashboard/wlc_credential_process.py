"""AWS ``credential_process`` for Terraform and Packer runs on Workload Credentials.

Not run by hand. ``workload_credential_lease.aws_process_env`` writes an AWS profile whose
``credential_process`` is this script, and the AWS SDK inside Terraform's provider, its S3
state backend or Packer's Amazon plugin runs it whenever its cached credential is near the
``Expiration`` this prints. That is what lets a run outlast a short provisioning lease.

It runs as a script, in a fresh process, with the environment the subprocess inherited
from the dashboard (the database URL and the key that decrypts the lease), and serves the
same lease row the dashboard does. stdout carries the credential JSON and nothing else:
the SDK parses all of it. Anything else goes to stderr, which the SDK shows on failure.

Exit 0 with the credential, 1 with a reason on stderr.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cloud", default="aws")
    parser.add_argument("--purpose", required=True)
    args = parser.parse_args(argv)

    # Logs to stderr only, and quietly: stdout is the SDK's.
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    try:
        from web_dashboard.services import workload_credential_lease as lease
        out = lease.credential_process_output(args.cloud, args.purpose)
    except Exception as exc:  # noqa: BLE001 - the SDK shows stderr; say what failed
        print(f"vm-dashboard credential_process: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 1
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    # Run by path, so the package root is not on sys.path yet.
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.exit(main())
