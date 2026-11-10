"""The CA bundle outbound HTTPS clients verify against.

Lifted out of ``notify_transports`` so the feedback client (and anything else that
talks to a third party over httpx) resolves the bundle the same way rather than
re-learning the problem. The original write-up, kept because the symptom is so
misleading:

    ``httpx`` builds its SSL context from ``certifi`` and does **not** read
    ``SSL_CERT_FILE`` / ``REQUESTS_CA_BUNDLE`` the way ``requests`` does -- yet this
    image sets both (Dockerfile) and ``docker-compose.corp-ca.yml`` mounts the host
    bundle over the container's. Behind a TLS-inspecting proxy that mismatch means
    every POST fails ``CERTIFICATE_VERIFY_FAILED`` while terraform and boto3 in the
    same container work fine, which is a miserable thing to debug.

Resolving the bundle explicitly also makes the behaviour independent of which httpx
minor is installed.

Returns a path when one of the env vars points at a real file, else ``True`` -- which
is httpx's own "verify normally" value. It never returns ``False``: turning
verification off is not something a helper should be able to do by accident.
"""
import os


def verify():
    """The ``verify=`` argument for an httpx client: a bundle path, or ``True``."""
    for var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        path = os.environ.get(var) or ""
        if path and os.path.isfile(path):
            return path
    return True
