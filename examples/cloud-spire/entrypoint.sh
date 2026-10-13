#!/bin/sh
# Start the dashboard's SPIRE server as a sidecar. See server.conf beside this file.
#
# Every input is an environment variable, because that is the one secret channel every
# managed runtime has (ECS cannot mount a secret as a file). A *_FILE variant is read
# instead when the platform can mount one (ACA secret volumes, Cloud Run secret mounts,
# Kubernetes):
#
#   SPIRE_TRUST_DOMAIN       e.g. dashboard.example.com                        (required)
#   SPIRE_DATASTORE_URL      postgresql://user:pass@host:5432/spire?sslmode=require
#                                                                              (required)
#   SPIRE_UPSTREAM_CA_CERT   PEM of the upstream CA certificate                (required)
#   SPIRE_UPSTREAM_CA_KEY    PEM of its private key                            (required)
#   SPIRE_JWT_ISSUER         only for the dashboard's own identity             (optional)
#   SPIRE_RUN_UID            uid the server runs as; default 10001, the dashboard's.
#                            SPIRE makes the admin socket 0770, so the app can connect
#                            only as the same uid (or group).
#
# Starting as root is only to own the shared socket directory (ECS and Kubernetes create
# volumes root-owned) and to write the CA files 0600; the server itself never runs as root.
set -eu

die() { echo "spire-sidecar: $*" >&2; exit 64; }

# VAR or VAR_FILE, never both.
read_var() {
    name=$1
    eval "val=\${$name:-}"
    eval "file=\${${name}_FILE:-}"
    if [ -n "$val" ] && [ -n "$file" ]; then die "set $name or ${name}_FILE, not both"; fi
    if [ -n "$file" ]; then
        [ -r "$file" ] || die "${name}_FILE=$file is not readable"
        val=$(cat "$file")
    fi
    printf '%s' "$val"
}

SPIRE_TRUST_DOMAIN=$(read_var SPIRE_TRUST_DOMAIN)
SPIRE_DATASTORE_URL=$(read_var SPIRE_DATASTORE_URL)
CA_CERT=$(read_var SPIRE_UPSTREAM_CA_CERT)
CA_KEY=$(read_var SPIRE_UPSTREAM_CA_KEY)

[ -n "$SPIRE_TRUST_DOMAIN" ] || die "SPIRE_TRUST_DOMAIN is required, e.g. dashboard.example.com"
[ -n "$SPIRE_DATASTORE_URL" ] || die "SPIRE_DATASTORE_URL is required: a managed runtime has no disk that survives a restart, so the datastore must be PostgreSQL"
# Refuse to start without the upstream CA rather than fall back to keys that die with the
# container: that would re-key the trust domain on every restart and strand every agent
# that attested, with nothing in the logs saying why.
case "$CA_CERT" in *"BEGIN CERTIFICATE"*) ;; *) die "SPIRE_UPSTREAM_CA_CERT is not a PEM certificate (docs/spiffe.md#the-upstream-ca)";; esac
case "$CA_KEY" in *"PRIVATE KEY"*) ;; *) die "SPIRE_UPSTREAM_CA_KEY is not a PEM private key";; esac

RUN_UID=${SPIRE_RUN_UID:-10001}
SOCK_DIR=/run/spire-server
CA_DIR=/run/spire-ca
DATA_DIR=/var/lib/spire

# The image creates all three; a platform may mount an empty volume over any of them.
# mkdir under the default umask: under 077, `mkdir -p` would also create any missing
# PARENT 0700 root, and the server could not get through it to its own directory.
mkdir -p "$SOCK_DIR" "$CA_DIR" "$DATA_DIR"
( umask 077
  printf '%s\n' "$CA_CERT" > "$CA_DIR/upstream.crt"
  printf '%s\n' "$CA_KEY" > "$CA_DIR/upstream.key" )
# A stale socket from a previous run in the same pod would make the bind fail.
rm -f "$SOCK_DIR/api.sock"

# Nothing secret survives into the server's environment except the datastore URL, which
# -expandEnv needs.
unset SPIRE_UPSTREAM_CA_CERT SPIRE_UPSTREAM_CA_KEY SPIRE_UPSTREAM_CA_CERT_FILE \
      SPIRE_UPSTREAM_CA_KEY_FILE SPIRE_DATASTORE_URL_FILE SPIRE_TRUST_DOMAIN_FILE CA_KEY CA_CERT
export SPIRE_TRUST_DOMAIN SPIRE_DATASTORE_URL SPIRE_JWT_ISSUER="${SPIRE_JWT_ISSUER:-}"

# HOME follows the server, not the root shell that started it: with sslmode=require the
# Postgres driver stats $HOME/.postgresql/postgresql.crt for a client certificate, and
# under root's 0700 /root that stat is "permission denied" — fatal, not "no certificate".
export HOME="$DATA_DIR"

set -- /usr/local/bin/spire-server run -config /etc/spire/server.conf -expandEnv
if [ "$(id -u)" = "0" ]; then
    chown "$RUN_UID:$RUN_UID" "$SOCK_DIR" "$CA_DIR" "$CA_DIR/upstream.crt" \
        "$CA_DIR/upstream.key" "$DATA_DIR"
    # The app container (same uid) must reach the socket; nobody else needs to list it.
    chmod 0750 "$SOCK_DIR"
    if [ "$RUN_UID" = "10001" ]; then
        exec setuidgid spire "$@"
    fi
    exec chpst -u ":$RUN_UID:$RUN_UID" "$@"
fi
exec "$@"
