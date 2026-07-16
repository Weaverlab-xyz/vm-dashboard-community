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


# Where the Dockerfile unpacks the bundled WALinuxAgent source.
WALINUXAGENT_SRC = "/opt/walinuxagent-src"

# Our --source-format vocabulary → the qemu/libguestfs disk-format name.
# VHD is "vpc" to qemu; auto-detection of VHD is unreliable, so we pass it
# explicitly to virt-customize.
_LIBGUESTFS_FORMAT = {"vhd": "vpc", "raw": "raw", "qcow2": "qcow2", "vmdk": "vmdk"}

# /etc/os-release ID values that can't run on Azure regardless of waagent.
# Amazon Linux (amzn) is built for EC2: waagent has no handler for it (so it
# never registers/reports), and its cloud-init is pinned to the Ec2 datasource
# + EC2 IMDS. The VM boots but provisioningState hangs at "Creating" forever.
# Failing the promote here turns a silent 20-min deploy hang into an instant,
# explanatory error.
_UNSUPPORTED_AZURE_DISTRO_IDS = {"amzn"}


def guest_os_release_id(disk_path: str, source_format: str) -> str:
    """Return the guest's /etc/os-release ID (lowercased), or "" if it can't be
    read. Uses libguestfs virt-cat — read-only, no boot. Never raises: a failed
    inspection must not block a promote (we just skip the compatibility check)."""
    fmt = _LIBGUESTFS_FORMAT.get((source_format or "").lower())
    cmd = ["virt-cat"]
    if fmt:
        cmd += ["--format", fmt]
    cmd += ["-a", disk_path, "/etc/os-release"]
    try:
        out = subprocess.check_output(cmd, stderr=sys.stderr, text=True)
    except Exception as e:
        log(f"could not read /etc/os-release ({e}); skipping Azure distro check")
        return ""
    for line in out.splitlines():
        if line.startswith("ID="):
            return line.split("=", 1)[1].strip().strip('"').lower()
    return ""


def assert_azure_supported_distro(disk_path: str, source_format: str) -> None:
    """Fail the promote fast if the source distro can't provision on Azure.

    Raises RuntimeError for a known-incompatible distro (e.g. Amazon Linux) so
    the operator gets an actionable message instead of a VM that boots but hangs
    at "Creating" until the deploy poller times out."""
    distro = guest_os_release_id(disk_path, source_format)
    if distro in _UNSUPPORTED_AZURE_DISTRO_IDS:
        raise RuntimeError(
            f"source image distro '{distro}' is not supported on Azure — its guest "
            "agent (waagent) can't provision there, so the VM would boot but hang at "
            "'Creating'. Build Azure-bound images from an Azure-endorsed distro "
            "(Ubuntu, RHEL, Rocky, Alma, Debian, SUSE)."
        )
    log(f"Azure distro check OK (id={distro or 'unknown'})")


def install_linux_agent(disk_path: str, source_format: str) -> None:
    """Offline-inject the Azure Linux Agent (waagent) into a Linux disk image.

    Foreign images (e.g. an AWS AMI) don't carry waagent. On Azure the VM boots
    and gets a private IP, but its ARM ``provisioningState`` never leaves
    ``Creating`` because nothing reports OS-provisioning complete — so the
    dashboard's deploy poller hangs until it times out. Baking waagent in here,
    during promotion, makes promoted Linux images provision natively on Azure.

    Uses libguestfs ``virt-customize`` (works in an unprivileged container) to:
      1. copy the bundled WALinuxAgent source into the guest,
      2. install + register the service via the guest's OWN Python — no guest
         network or package repo needed, so it's distro-agnostic, and
      3. deprovision (generalize): strip the source cloud's user/host keys so
         Azure re-creates the admin user + injects the SSH key from the VM's
         os_profile at deploy time.

    Raises on failure: a promoted Linux Azure image without waagent is broken
    in exactly the way this fixes, so we must not silently ship one.
    """
    fmt = _LIBGUESTFS_FORMAT.get((source_format or "").lower())
    log(f"inject waagent into {disk_path} (format={fmt or 'auto'})")
    # virt-customize requires --format to appear BEFORE -a (it applies to the
    # disk added by the -a that follows it).
    cmd = ["virt-customize"]
    if fmt:
        cmd += ["--format", fmt]
    cmd += ["-a", disk_path]
    cmd += [
        # Lands at /opt/walinuxagent-src inside the guest.
        "--copy-in", f"{WALINUXAGENT_SRC}:/opt",
        # Install via whatever Python the guest ships (py3 → py2 → py). waagent
        # supports both; --register-service wires up the systemd/init unit.
        # Always prepend the vendored deps (setuptools<80 + distro) to the guest's
        # PYTHONPATH:
        #   * distro is REQUIRED, not optional — WALinuxAgent's future.py falls
        #     back to `import distro` on py>=3.8 (platform.linux_distribution was
        #     removed), and a guest without it (e.g. Rocky/RHEL 9 minimal) dies at
        #     setup.py import with "NameError: name 'distro' is not defined".
        #   * pinning our setuptools<80 over any newer guest copy keeps the
        #     `setup.py install` command (--register-service) working (80 removed
        #     it), and covers minimal images that ship no setuptools at all.
        # SETUPTOOLS_USE_DISTUTILS=local is a no-op on older guests and makes
        # py3.12+ (no stdlib distutils) use setuptools' bundled distutils shim.
        "--run-command",
        "cd /opt/walinuxagent-src && "
        "export PYTHONPATH=/opt/walinuxagent-src/_vendor${PYTHONPATH:+:$PYTHONPATH} SETUPTOOLS_USE_DISTUTILS=local; "
        "for py in python3 python2 python; do "
        "if command -v $py >/dev/null 2>&1; then "
        "$py setup.py install --register-service && exit 0; exit 1; fi; done; "
        "echo 'no python interpreter in guest for waagent install' >&2; exit 1",
        # Belt-and-suspenders: ensure the unit is enabled under either name.
        "--run-command",
        "systemctl enable waagent.service 2>/dev/null || "
        "systemctl enable walinuxagent.service 2>/dev/null || true",
        # Generalize for re-imaging. -force skips the interactive confirm.
        "--run-command",
        "waagent -deprovision+user -force || "
        "/usr/sbin/waagent -deprovision+user -force || true",
        # Fix SELinux labels touched by the edits (no-op on non-SELinux guests).
        "--selinux-relabel",
    ]
    subprocess.check_call(cmd, stdout=sys.stdout, stderr=sys.stderr)
    log("waagent injection done")


# Where the Dockerfile stages the GCE guest agent: both arch binaries under
# amd64/ + arm64/, the arch-selecting launcher `google_guest_agent`, and the
# `google-guest-agent.service` systemd unit.
GCP_GUEST_AGENT_SRC = "/opt/gcp-guest-agent"


def install_gcp_guest_agent(disk_path: str, source_format: str) -> None:
    """Offline-inject the Google Compute Engine guest agent into a Linux image.

    Foreign images (e.g. an AWS AMI) don't carry google-guest-agent. GCP does
    NOT gate the instance's RUNNING state on a guest agent (unlike Azure), so the
    VM boots fine — but with no agent, `ssh-keys` instance metadata is never
    applied to ~/.ssh/authorized_keys, so key-based SSH (and the Password Safe
    GCP SSH-rotation plugin) silently never works. Baking the agent in during
    promotion fixes that.

    Uses libguestfs virt-customize (unprivileged container) to drop both the
    amd64 and arm64 binaries, our arch-selecting launcher at the unit's
    ExecStart path, and the systemd unit, then enable it. Distro-agnostic (the
    binary is static Go) and needs no guest network. Assumes a systemd guest.
    """
    fmt = _LIBGUESTFS_FORMAT.get((source_format or "").lower())
    log(f"inject google-guest-agent into {disk_path} (format={fmt or 'auto'})")
    cmd = ["virt-customize"]
    if fmt:
        cmd += ["--format", fmt]
    cmd += ["-a", disk_path]
    cmd += [
        "--mkdir", "/usr/lib/google-guest-agent",
        # Real per-arch binaries → /usr/lib/google-guest-agent/{amd64,arm64}/…
        "--copy-in", f"{GCP_GUEST_AGENT_SRC}/amd64:/usr/lib/google-guest-agent",
        "--copy-in", f"{GCP_GUEST_AGENT_SRC}/arm64:/usr/lib/google-guest-agent",
        # Arch-selecting launcher → /usr/bin/google_guest_agent (unit ExecStart).
        "--copy-in", f"{GCP_GUEST_AGENT_SRC}/google_guest_agent:/usr/bin",
        # systemd unit → /lib/systemd/system.
        "--copy-in", f"{GCP_GUEST_AGENT_SRC}/google-guest-agent.service:/lib/systemd/system",
        "--run-command",
        "chmod 0755 /usr/bin/google_guest_agent "
        "/usr/lib/google-guest-agent/amd64/google_guest_agent "
        "/usr/lib/google-guest-agent/arm64/google_guest_agent",
        # Enable at boot. `|| echo` (not hard-fail) so a non-systemd guest doesn't
        # break the bake — the agent just won't start, surfaced in the log.
        "--run-command",
        "systemctl enable google-guest-agent.service 2>/dev/null || "
        "echo 'WARNING: could not enable google-guest-agent.service (non-systemd guest?)' >&2",
        # Fix SELinux labels on the files we added (no-op on non-SELinux guests).
        "--selinux-relabel",
    ]
    subprocess.check_call(cmd, stdout=sys.stdout, stderr=sys.stderr)
    log("google-guest-agent injection done")


def convert_to_fixed_vhd(src: str, dst: str) -> None:
    """Produce a FIXED-format VHD for Azure. Azure managed-image/disk creation
    rejects dynamic VHDs ("is of Dynamic VHD type. Please retry with fixed VHD
    type"), and qemu-img's `vpc` output is dynamic unless subformat=fixed.
    force_size=on keeps the virtual size exact so the footer size Azure reads
    back is correct (no CHS-geometry rounding). qemu-img auto-detects the source
    format, so this works whether the source is a dynamic VHD or raw."""
    log(f"convert to fixed VHD {src} -> {dst}")
    subprocess.check_call(
        ["qemu-img", "convert", "-p", "-O", "vpc", "-o", "subformat=fixed,force_size=on", src, dst],
        stdout=sys.stdout,
        stderr=sys.stderr,
    )
    size_mb = os.path.getsize(dst) / (1024 * 1024)
    log(f"fixed VHD done ({size_mb:.1f} MiB)")


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
    from azure.storage.blob import BlobServiceClient, BlobType
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
    # Azure managed-image / managed-disk creation requires the VHD to be stored
    # in a PAGE blob. upload_blob() defaults to a BLOCK blob, which Azure rejects
    # at image-create time: "(InvalidParameter) The source blob ... is not a page
    # blob." Page blobs must be 512-byte aligned — a valid fixed-format VHD always
    # is; fail with a clear message rather than a cryptic SDK error if the source
    # isn't (e.g. a dynamic VHD slipped through with no conversion).
    size = os.path.getsize(local)
    if size % 512 != 0:
        raise ValueError(
            f"VHD is {size} bytes — not a multiple of 512, so it can't be stored as an "
            "Azure page blob. The source must be a fixed-format VHD."
        )
    with open(local, "rb") as f:
        blob_client.upload_blob(
            f, blob_type=BlobType.PageBlob, length=size, overwrite=True, max_concurrency=4,
        )
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
    # GCP's image importer parses the tar itself (not via GNU tar) and rejects
    # PAX-format archives — Python tarfile's default. A >8GiB disk.raw exceeds the
    # ustar size limit, forcing a size extension that PAX encodes as a leading
    # `././@PaxHeader` entry, which GCP treats as an invalid extra file
    # ("INVALID_IMAGE_TAR: The tar archive is not a valid image"). Write GNU format
    # (matching GCP's documented `tar --format=oldgnu`) so the size is encoded
    # inline with no pax header.
    with tarfile.open(out_tar_gz_path, "w:gz", format=tarfile.GNU_FORMAT) as tar:
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
    # Azure only: bake the Azure Linux Agent into the image before upload so a
    # promoted foreign Linux image provisions natively on Azure. The dashboard
    # sets this for Linux images; Windows images bring their own agent.
    ap.add_argument("--install-linux-agent", action="store_true",
                    help="inject WALinuxAgent into a Linux disk (Azure target)")
    # GCS/GCP only: bake the GCE guest agent into the image so a promoted foreign
    # Linux image applies ssh-keys metadata (key-based SSH + PS rotation plugin).
    ap.add_argument("--install-gcp-guest-agent", action="store_true",
                    help="inject google-guest-agent into a Linux disk (GCS target)")
    args = ap.parse_args()

    src_ext = args.source_format.lower()
    dst_ext = args.target_format.lower()
    src_path = f"/tmp/source.{src_ext}"
    dst_path = src_path if src_ext == dst_ext else f"/tmp/target.{dst_ext}"

    try:
        download(args.source_url, src_path)
        # Azure is handled specially below (it always needs a fixed-format VHD,
        # normalised straight from the source), so skip the generic convert here.
        if src_ext != dst_ext and args.target != "azure":
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
            # Bake the Azure Linux Agent into the source disk BEFORE the fixed
            # VHD conversion, so the deprovision/generalize is captured in the
            # image Azure imports. Without this, promoted foreign Linux images
            # boot but never finish Azure OS provisioning (deploy hangs). First
            # reject distros that can't provision on Azure at all (e.g. Amazon
            # Linux) — fail fast rather than ship an image that hangs on deploy.
            if args.install_linux_agent:
                try:
                    assert_azure_supported_distro(src_path, args.source_format)
                except RuntimeError as e:
                    log(f"ERROR: {e}")
                    return 5
                install_linux_agent(src_path, args.source_format)
            # Azure managed-image/disk creation requires a FIXED-format VHD. The
            # source is frequently a dynamic VHD (and when source==target==vhd the
            # generic convert is skipped), so always normalise to a fixed VHD here,
            # straight from the downloaded source, before upload.
            azure_vhd = "/tmp/azure-fixed.vhd"
            convert_to_fixed_vhd(src_path, azure_vhd)
            upload_azure(azure_vhd, args.dest_azure_account, args.dest_azure_container, args.dest_azure_blob)
        elif args.target == "gcs":
            if not (args.dest_gcs_bucket and args.dest_gcs_object):
                log("ERROR: --target gcs requires --dest-gcs-bucket and --dest-gcs-object")
                return 2
            # Bake google-guest-agent into the (already raw-converted) disk before
            # the tar wrap, so a promoted foreign Linux image applies ssh-keys
            # metadata on GCP — otherwise key-based SSH silently never works.
            # dst_path is the raw disk here (--target-format is forced to raw).
            if args.install_gcp_guest_agent:
                install_gcp_guest_agent(dst_path, args.target_format)
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
