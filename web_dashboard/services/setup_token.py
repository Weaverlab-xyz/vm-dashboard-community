"""The one-time token that proves whoever finishes first-run setup can see the server.

Until setup is complete, ``POST /api/setup/complete`` and ``/api/setup/import`` create the
first administrator with no authentication -- whoever reaches a fresh instance first owns
it. On a host with a public address (a cloud VM, a port forwarded "just for a minute")
that is a race against scanners. Now both also need this token, which only someone with
access to the server can read:

  * ``SETUP_TOKEN`` in the environment, when the operator set one (``scripts/onboard.sh``
    generates it into ``.env``; automation and multi-replica installs set it explicitly);
  * otherwise a random token written to ``<data dir>/setup_token`` (mode 0600) at the
    first startup. The log says where the file is, never what is in it.

It is sent as the ``X-Setup-Token`` header. Once setup completes the file is deleted and
the check no longer applies: those routes then need an administrator, as before.
"""
import hmac
import logging
import os
import secrets
import tempfile
from typing import Optional

logger = logging.getLogger(__name__)

HEADER = "X-Setup-Token"
FILENAME = "setup_token"
_DEFAULT_DATA_DIR = "/app/data"     # the image's writable data directory (Dockerfile)

_announced = False


def _data_dir() -> str:
    """Where the token file lives: the image's data dir, else beside the SQLite database,
    else the working directory (a bare checkout on Postgres)."""
    from ..config import settings
    if os.path.isdir(_DEFAULT_DATA_DIR) and os.access(_DEFAULT_DATA_DIR, os.W_OK):
        return _DEFAULT_DATA_DIR
    url = settings.database_url or ""
    if url.startswith("sqlite:///"):
        path = url[len("sqlite:///"):]
        if path and path != ":memory:":
            return os.path.dirname(os.path.abspath(path))
    return os.getcwd()


def token_path() -> str:
    from ..config import settings
    return getattr(settings, "setup_token_file", "") or os.path.join(_data_dir(), FILENAME)


def _from_env() -> str:
    from ..config import settings
    return (getattr(settings, "setup_token", "") or "").strip()


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except FileNotFoundError:
        return ""


def _create(path: str) -> str:
    """Write a new token to ``path`` unless another process got there first.

    Every gunicorn worker runs startup, so two may race. The token is written to a temp
    file and hard-linked into place, which fails if the name exists: the loser reads the
    winner's token, and nobody ever reads a half-written file.
    """
    token = secrets.token_urlsafe(24)
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".setup_token.", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(token + "\n")
        try:
            os.link(tmp, path)
        except FileExistsError:
            return _read(path)
        return token
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def ensure() -> Optional[str]:
    """The token this instance expects, creating the file if needed; None if it cannot.

    Called at startup while setup is incomplete, and again by :func:`require`, so a file
    deleted by hand is recreated rather than locking setup out.
    """
    global _announced
    env = _from_env()
    if env:
        if not _announced:
            logger.warning("Setup is not complete. The setup wizard needs the setup token "
                           "from SETUP_TOKEN in this server's environment.")
            _announced = True
        return env
    path = token_path()
    try:
        token = _read(path) or _create(path)
    except OSError as exc:
        logger.error("Could not create the setup token file %s: %s. Set SETUP_TOKEN in "
                     "the environment instead.", path, exc)
        return None
    if not _announced:
        logger.warning("Setup is not complete. The setup wizard needs the one-time setup "
                       "token in %s on this server (docker compose exec app cat %s).",
                       path, path)
        _announced = True
    return token


def require(request) -> None:
    """403 unless ``request`` carries this instance's setup token."""
    from fastapi import HTTPException
    expected = ensure()
    sent = (request.headers.get(HEADER) or "").strip()
    if not expected or not sent or not hmac.compare_digest(sent.encode(), expected.encode()):
        raise HTTPException(
            status_code=403,
            detail="The setup token is missing or wrong. It is in the setup_token file in "
                   "the dashboard's data directory (docker compose exec app cat "
                   "/app/data/setup_token), or SETUP_TOKEN if you set one; the startup "
                   "log names the file.")


def discard() -> None:
    """Setup is done: the token is no longer needed, so it should not lie around."""
    global _announced
    _announced = False
    if _from_env():
        return
    try:
        os.unlink(token_path())
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("Could not delete the setup token file: %s", exc)
