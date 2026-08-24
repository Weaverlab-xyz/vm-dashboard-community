#!/usr/bin/env bash
# Generate the RSA key material the cloud-DB custom plugins use to protect the CURRENT
# login password in transit to a jump host. New passwords are never sent as ciphertext
# at all (the Azure plugins send a SCRAM-SHA-256 verifier), so this key only ever
# protects the login password.
#
# Two independent key pairs by default, one per cloud. The private keys end up on
# DIFFERENT hosts -- the Azure jump VM and the AWS ECS gateway host -- so sharing one
# pair would mean a compromise of either host also decrypts the other cloud's payloads.
# The Resource Broker holds both public files; that costs nothing.
#
# 4096-bit is required, not a preference: PKCS#1 v1.5 caps the plaintext at
# (keyBits/8 - 11) bytes, so 4096 allows 501-byte passwords where 2048 stops at 245.
#
# Each pair emits BOTH public encodings, because the two plugin families differ:
#   public_cert.cer - X.509 certificate. This is what the Azure Run Command plugins
#                     read at address field 7 (certPath).
#   public.pem      - bare SPKI public key extracted from that certificate. Our docs
#                     show the AWS SSM path as "...public_ssm.pem", which suggests this
#                     form for address field 5 -- but the AWS plugin spec was not
#                     available when this was written, so both are produced and you
#                     point the panel at whichever the plugin actually reads.
#
# Run this INSIDE the WSL/Linux filesystem, not on /mnt/c: DrvFs does not honour chmod,
# so private key files there cannot be restricted to 0600.
#
# Usage:  ./make-clouddb-plugin-keys.sh [output-dir]      (default ~/psplugin-keys)
set -euo pipefail

OUT_ROOT="${1:-$HOME/psplugin-keys}"

case "$OUT_ROOT" in
  /mnt/*)
    echo "REFUSING: $OUT_ROOT is a Windows drive mount. chmod does not stick there, so" >&2
    echo "the private keys could not be restricted to 0600. Use a path inside the Linux" >&2
    echo "filesystem (the default ~/psplugin-keys) and copy out only what you need." >&2
    exit 1 ;;
esac

command -v openssl >/dev/null || { echo "openssl not found" >&2; exit 1; }

umask 077
mkdir -p "$OUT_ROOT"
chmod 700 "$OUT_ROOT"

make_pair() {
  local cloud="$1" cn="$2" dir="$OUT_ROOT/$1"

  if [ -e "$dir/private.pem" ]; then
    echo "SKIP $cloud: $dir/private.pem already exists."
    echo "     Overwriting would orphan any copy already deployed to a broker or jump host."
    echo "     Delete the directory yourself if you really mean to re-key."
    return 0
  fi

  mkdir -p "$dir"
  chmod 700 "$dir"

  openssl rand -base64 32 > "$dir/passphrase.txt"
  openssl req -x509 -newkey rsa:4096 -sha256 -days 3650 \
    -subj "/CN=$cn" \
    -keyout "$dir/private.pem" -passout "file:$dir/passphrase.txt" \
    -out "$dir/public_cert.cer" 2>/dev/null
  openssl x509 -in "$dir/public_cert.cer" -pubkey -noout > "$dir/public.pem"
  chmod 600 "$dir"/*

  # Verify rather than assume: a mismatched pair fails at the FIRST ROTATION with an
  # opaque decrypt error, long after everything else looks healthy.
  local bits key_mod cert_mod
  bits=$(openssl rsa -in "$dir/private.pem" -passin "file:$dir/passphrase.txt" \
           -noout -text 2>/dev/null | sed -n 's/.*Private-Key: (\([0-9]*\) bit.*/\1/p')
  [ "$bits" = "4096" ] || { echo "$cloud: expected a 4096-bit key, got '${bits:-unknown}'" >&2; exit 1; }

  key_mod=$(openssl rsa -in "$dir/private.pem" -passin "file:$dir/passphrase.txt" \
              -noout -modulus 2>/dev/null | openssl sha256 | awk '{print $NF}')
  cert_mod=$(openssl x509 -in "$dir/public_cert.cer" -noout -modulus \
              | openssl sha256 | awk '{print $NF}')
  [ "$key_mod" = "$cert_mod" ] || { echo "$cloud: certificate does not match the private key" >&2; exit 1; }

  echo "OK   $cloud  4096-bit, cert matches key  ->  $dir"
}

echo "== generating =="
make_pair azure psplugin-clouddb-azure
make_pair aws   psplugin-clouddb-aws

cat <<PLACEMENT

== where each file goes ==

RESOURCE BROKER (both clouds, same broker -- these are PUBLIC, safe to copy freely)
  azure/public_cert.cer  -> the path you put in "Broker Cert Path"
                            (clouddb_ps_azure_cert_path, address field 7)
                            e.g. C:\\BeyondTrust\\certs\\public_cert.cer
  aws/public.pem         -> the path you put in "Public Key Path (on PS node)"
     (or aws/public_cert.cer) (clouddb_ps_ssm_public_key_path, address field 5)
                            e.g. C:\\Utils\\public_ssm.pem
                            Use whichever encoding the SSM plugin actually reads.

AZURE JUMP VM -- do NOT copy by hand. Paste into the settings panel instead:
  azure/private.pem      -> "Plugin Private Key (PEM)"   (full text, BEGIN/END included)
  azure/passphrase.txt   -> "Plugin Key Passphrase"
  The dashboard drops both onto clouddb-jumpoint at /root/psplugin (dir 700, files 600).
  Leaving these blank SILENTLY skips that drop: provisioning stays green and the first
  rotation fails to decrypt, with nothing in the job pointing at the cause.

AWS ECS GATEWAY HOST -- manual; the dashboard does not stage AWS key material:
  aws/private.pem        -> the ssm-user home on the shared gateway host
  aws/passphrase.txt     -> same directory
  A green provisioning job tells you nothing about whether this is right.

To read the Azure values for pasting (prints secrets -- do it in your own terminal):
  cat $OUT_ROOT/azure/private.pem
  cat $OUT_ROOT/azure/passphrase.txt

These directories hold unencrypted-at-rest private key material. Keep them, because
re-keying means touching the broker and both jump hosts again -- but keep them 0700.
PLACEMENT
