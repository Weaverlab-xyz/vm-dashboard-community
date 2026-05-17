#!/usr/bin/env python3
"""
Promote-runner entrypoint.

Runs inside a transient task (ECS Fargate / Azure ACI / GCP Cloud Run job).
Downloads a VM image from a presigned HTTPS source URL, optionally converts
its disk format with qemu-img, then uploads the result to a target-cloud
storage location ready for the cloud's native image-import API.

Same binary serves AWS / Azure / GCP target uploads — pick via `--target`.

Required env vars depend on `--target`:
  --target s3:    task IAM role provides S3 write to --dest-s3-bucket
                  (AWS_REGION must be set if not in the task role default).
  --target azure: AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET
                  + --dest-azure-account / --dest-azure-container.
  --target gcs:   GOOGLE_APPLICATION_CREDENTIALS path (or workload-identity
                  if the runner image grew that support) + --dest-gcs-bucket.

The source URL is presigned by the dashboard at task-launch time so this
container never needs source-side credentials.
"""
import argparse
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.request


def log(msg: str) -> None:
    # Flush so the orchestrator's log tail picks up progress in near-real-time
    # rather than buffering until exit.
    print(f"[promote-runner] {msg}", flush=True)


def download(url: str, dest: str) -> None:
    log(f"download {dest} <- {url[:80]}{'…' if len(url) > 80 else ''}")
    with urllib.request.urlopen(url) as src, open(dest, "wb") as out:
        # 8 MiB chunks keep memory bounded for multi-GB downloads.
        while True:
            chunk = src.read(8 * 1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
    size_mb = os.path.getsize(dest) / (1024 * 1024)
    log(f"download done ({size_mb:.1f} MiB)")


def convert(src: str, dst: str, target_format: str) -> None:
    log(f"convert {src} -> {dst} ({target_format})")
    subprocess.check_call(
        ["qemu-img", "convert", "-p", "-O", target_format, src, dst],
        stdout=sys.stdout,
        stderr=sys.stderr,
    )
    size_mb = os.path.getsize(dst) / (1024 * 1024)
    log(f"convert done ({size_mb:.1f} MiB)")


def upload_s3(local: str, bucket: str, key: str, region: str) -> None:
    import boto3
    log(f"upload s3://{bucket}/{key} (region={region})")
    s3 = boto3.client("s3", region_name=region) if region else boto3.client("s3")
    # upload_file uses multipart automatically for files > 8 MiB so multi-GB
    # objects work without manual chunking.
    s3.upload_file(local, bucket, key)
    log("upload done")


def upload_azure(local: str, account: str, container: str, blob_name: str) -> None:
    from azure.identity import ClientSecretCredential
    from azure.storage.blob import BlobServiceClient
    log(f"upload https://{account}.blob.core.windows.net/{container}/{blob_name}")
    cred = ClientSecretCredential(
        tenant_id=os.environ["AZURE_TENANT_ID"],
        client_id=os.environ["AZURE_CLIENT_ID"],
        client_secret=os.environ["AZURE_CLIENT_SECRET"],
    )
    svc = BlobServiceClient(
        account_url=f"https://{account}.blob.core.windows.net",
        credential=cred,
    )
    blob_client = svc.get_blob_client(container=container, blob=blob_name)
    with open(local, "rb") as f:
        blob_client.upload_blob(f, overwrite=True)
    log("upload done")


def upload_gcs(local: str, bucket: str, object_name: str) -> None:
    from google.cloud import storage as gcs
    log(f"upload gs://{bucket}/{object_name}")
    client = gcs.Client()
    blob = client.bucket(bucket).blob(object_name)
    blob.upload_from_filename(local)
    log("upload done")


def wrap_as_gcp_image_tar(raw_path: str, out_tar_gz_path: str) -> None:
    """GCP custom-image insert requires `rawDisk.source` to point at a
    .tar.gz that contains exactly one file named `disk.raw`. We move the
    converted raw file to that name (under /tmp/gcp-image/) and tar it.

    Doing this in the runner instead of the dashboard keeps the GCP-specific
    quirk localized — no other target cares about the wrapper format.
    """
    stage = "/tmp/gcp-image"
    os.makedirs(stage, exist_ok=True)
    disk_raw = os.path.join(stage, "disk.raw")
    # Move (not copy) so we don't double the disk footprint of a multi-GB raw.
    if os.path.abspath(raw_path) != os.path.abspath(disk_raw):
        log(f"renaming {raw_path} -> {disk_raw}")
        shutil.move(raw_path, disk_raw)
    log(f"writing tar.gz {out_tar_gz_path} (contents: disk.raw)")
    with tarfile.open(out_tar_gz_path, "w:gz") as tar:
        tar.add(disk_raw, arcname="disk.raw")
    size_mb = os.path.getsize(out_tar_gz_path) / (1024 * 1024)
    log(f"tar.gz done ({size_mb:.1f} MiB compressed)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source-url", required=True, help="presigned HTTPS URL of the source artefact")
    ap.add_argument("--source-format", required=True, help="vhd, raw, qcow2, vmdk")
    ap.add_argument("--target-format", required=True, help="vhd, raw, qcow2, vmdk")
    ap.add_argument("--target", required=True, choices=["s3", "azure", "gcs"])
    # S3 target
    ap.add_argument("--dest-s3-bucket")
    ap.add_argument("--dest-s3-key")
    ap.add_argument("--dest-s3-region", default=os.environ.get("AWS_REGION", ""))
    # Azure target
    ap.add_argument("--dest-azure-account")
    ap.add_argument("--dest-azure-container")
    ap.add_argument("--dest-azure-blob")
    # GCS target
    ap.add_argument("--dest-gcs-bucket")
    ap.add_argument("--dest-gcs-object")
    args = ap.parse_args()

    src_ext = args.source_format.lower()
    dst_ext = args.target_format.lower()
    src_path = f"/tmp/source.{src_ext}"
    dst_path = src_path if src_ext == dst_ext else f"/tmp/target.{dst_ext}"

    try:
        download(args.source_url, src_path)
        if src_ext != dst_ext:
            convert(src_path, dst_path, dst_ext)
        if args.target == "s3":
            if not (args.dest_s3_bucket and args.dest_s3_key):
                log("ERROR: --target s3 requires --dest-s3-bucket and --dest-s3-key")
                return 2
            upload_s3(dst_path, args.dest_s3_bucket, args.dest_s3_key, args.dest_s3_region)
        elif args.target == "azure":
            if not (args.dest_azure_account and args.dest_azure_container and args.dest_azure_blob):
                log("ERROR: --target azure requires --dest-azure-account / --dest-azure-container / --dest-azure-blob")
                return 2
            upload_azure(dst_path, args.dest_azure_account, args.dest_azure_container, args.dest_azure_blob)
        elif args.target == "gcs":
            if not (args.dest_gcs_bucket and args.dest_gcs_object):
                log("ERROR: --target gcs requires --dest-gcs-bucket and --dest-gcs-object")
                return 2
            # GCP custom-image insert requires the source to be a `.tar.gz`
            # whose single entry is `disk.raw`. The dashboard always tells us
            # `--target-format raw` for GCP targets; we wrap the result into
            # the expected tarball here before upload. Other targets (s3,
            # azure) take the raw file directly.
            if args.target == "gcs" and dst_ext == "raw":
                tar_path = "/tmp/target.tar.gz"
                wrap_as_gcp_image_tar(dst_path, tar_path)
                upload_path = tar_path
            else:
                upload_path = dst_path
            upload_gcs(upload_path, args.dest_gcs_bucket, args.dest_gcs_object)
        log("SUCCESS")
        return 0
    except subprocess.CalledProcessError as e:
        log(f"ERROR: subprocess failed (exit {e.returncode}): {e.cmd}")
        return 3
    except Exception as e:
        log(f"ERROR: {type(e).__name__}: {e}")
        return 4


if __name__ == "__main__":
    sys.exit(main())
