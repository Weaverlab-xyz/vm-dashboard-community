#!/bin/sh
# Entrypoint for the dashboard image (app and worker). Starts as root only for the few
# things that need it, then runs the app as the unprivileged `dashboard` user
# (uid/gid 10001, the same ids the remote agent uses).
#
# Why root at all, briefly: two things about the host cannot be known when the image is
# built, and both need root to adapt to.
#
#   1. The Docker socket. The Ansible integration launches sibling containers through
#      /var/run/docker.sock, which the host owns as root:<docker group>. That group's id
#      differs per host (and Docker Desktop's socket is root:root), so `dashboard` is given
#      the socket's group here, whatever it is, instead of the operator having to find it
#      and set group_add in compose.
#   2. Volumes written by earlier releases, which ran as root. Their files are root-owned
#      and the app could no longer write them. The directories the app writes are handed
#      to `dashboard` here, once (an already-correct tree is left alone).
#
# Then setpriv drops to `dashboard` and execs the command, so gunicorn and the worker
# never hold root. Started with --user (Kubernetes runAsNonRoot, `docker run -u`), this
# script has nothing to fix and simply execs the command.
#
# DASHBOARD_RUN_AS_ROOT=1 keeps the old behaviour (everything as root). It exists as a
# way back if a deployment hits a permission problem this script does not handle; use it
# while reporting the problem, not as a setting.
set -eu

APP_USER=dashboard

if [ "$(id -u)" != "0" ]; then
    exec "$@"
fi

if [ "${DASHBOARD_RUN_AS_ROOT:-0}" = "1" ]; then
    echo "entrypoint: DASHBOARD_RUN_AS_ROOT=1, running as root" >&2
    if [ -f /root/.ssh/dev_dashboard_key ]; then
        install -m 600 /root/.ssh/dev_dashboard_key /root/.ssh/dev_key
    fi
    exec "$@"
fi

APP_HOME=$(getent passwd "$APP_USER" | cut -d: -f6)

APP_GID=$(id -g "$APP_USER")

# -- the Docker socket's group ------------------------------------------------------
# Passed to setpriv at exec time rather than written into /etc/group, so a restart of the
# same container recomputes it from the socket that is mounted now and nothing piles up.
# gid 0 is Docker Desktop's case (socket root:root, mode 0660). Membership of group root
# grants no root privilege, only group access to the few files that group owns.
GROUPS_ARG="$APP_GID"
SOCK=/var/run/docker.sock
if [ -S "$SOCK" ]; then
    sock_gid=$(stat -c %g "$SOCK")
    if [ "$sock_gid" != "$APP_GID" ]; then
        GROUPS_ARG="$APP_GID,$sock_gid"
    fi
fi

# -- directories the app writes -----------------------------------------------------
# Only those that persist in volumes or the image; temp files go to /tmp. A directory
# whose top is already owned by the app user is assumed migrated and not walked again.
for dir in /app/terraform/deployments /app/packer/builds /run/spiffe-tokens /runner-work; do
    if [ ! -d "$dir" ]; then continue; fi
    if [ "$(stat -c %u "$dir")" != "$(id -u "$APP_USER")" ]; then
        chown -R "$APP_USER:$APP_USER" "$dir"
    fi
done
# SQLite's default database is /app/vm_cli.db, and SQLite writes its journal next to it,
# so the directory itself (not its tree) belongs to the app user. A root-owned database
# file from an older release is handed over too.
chown "$APP_USER:$APP_USER" /app
for f in /app/vm_cli.db /app/vm_cli.db-journal /app/vm_cli.db-wal /app/vm_cli.db-shm; do
    if [ -e "$f" ]; then chown "$APP_USER:$APP_USER" "$f"; fi
done

# -- optional dev SSH key -------------------------------------------------------------
# A Windows host bind-mounts its key here as mode 0777, which ssh refuses; a private copy
# owned by the app user is what ssh is given.
if [ -f /root/.ssh/dev_dashboard_key ]; then
    install -d -m 700 -o "$APP_USER" -g "$APP_USER" "$APP_HOME/.ssh"
    install -m 600 -o "$APP_USER" -g "$APP_USER" /root/.ssh/dev_dashboard_key "$APP_HOME/.ssh/dev_key"
fi

export HOME="$APP_HOME" USER="$APP_USER" LOGNAME="$APP_USER"
exec setpriv --reuid="$APP_USER" --regid="$APP_GID" --groups="$GROUPS_ARG" --inh-caps=-all --no-new-privs -- "$@"
