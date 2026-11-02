"""CLI entry point.

  python -m robot installer   # PS: download RB installer + key, upload via the dashboard
  python -m robot integration # PRA: create the Password Safe integration

All inputs are environment variables so no secret appears in `ps` or the image.
"""
import json
import os
import stat
import sys

from playwright.sync_api import sync_playwright

from . import flows
from .storage_client import DashboardStorage


def load_bundle() -> None:
    """Fill the environment from the sealed bundle the agent wrote, when there is one.

    On a control node the agent hands logins over as a file, not as environment variables,
    because an environment variable is visible to `docker inspect` for the container's life.
    Anything already set explicitly wins, so running by hand is unchanged.
    """
    path = os.environ.get("ROBOT_BUNDLE_FILE", "")
    if not path:
        return
    with open(path, encoding="utf-8") as fh:
        for key, value in json.load(fh).items():
            os.environ.setdefault(str(key), str(value))


def env(name: str, required: bool = True) -> str:
    value = os.environ.get(name, "")
    if required and not value:
        sys.exit(f"Missing required environment variable {name}")
    return value


def _page(p):
    browser = p.chromium.launch(headless=os.environ.get("ROBOT_HEADED") != "1")
    return browser, browser.new_context(accept_downloads=True).new_page()


def _dashboard() -> DashboardStorage:
    """SPIFFE JWT-SVID when a file is given (no secret stored), else client credentials."""
    url = env("DASHBOARD_URL")
    verify = os.environ.get("DASHBOARD_CA_BUNDLE") or True
    scope = os.environ.get("DASHBOARD_SCOPE", "")
    svid_file = os.environ.get("DASHBOARD_SPIFFE_JWT_FILE", "")
    if svid_file:
        return DashboardStorage.from_spiffe_svid(
            url, svid_file, os.environ.get("DASHBOARD_CLIENT_ID", ""), verify, scope)
    return DashboardStorage.from_client_credentials(
        url, env("DASHBOARD_CLIENT_ID"), env("DASHBOARD_CLIENT_SECRET"), verify, scope)


def run_installer():
    # Fail on the cheap checks before driving a browser for ten minutes.
    storage = _dashboard()
    backend = storage.active_backend()
    print(f"Active storage backend: {backend['label']}")
    env_id = os.environ.get("POV_ENV_ID", "")
    key_file = "" if env_id else env("INSTALLER_KEY_FILE")
    with sync_playwright() as p:
        browser, page = _page(p)
        try:
            path, key = flows.fetch_resource_broker_installer(
                page, env("PS_URL"), env("PS_USER"), env("PS_PASSWORD"),
                env("PS_TOTP_SECRET", required=False), env("ROBOT_DOWNLOAD_DIR"))
        finally:
            browser.close()
    name = storage.upload(path)
    print(f"Uploaded {name} to the active storage backend")
    if env_id:
        # Straight to the POV's config store; the key is never printed or written to disk.
        storage.stage_resource_broker(env_id, name, key)
        print(f"Staged {name} and the installer key on POV {env_id}")
    else:
        # Standalone: owner-only file, never printed.
        fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                     stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w") as fh:
            fh.write(key)
        print(f"Installer key written to {key_file}")


def run_integration():
    with sync_playwright() as p:
        browser, page = _page(p)
        try:
            flows.create_pra_ps_integration(
                page, env("PRA_URL"), env("PRA_USER"), env("PRA_PASSWORD"),
                env("PRA_TOTP_SECRET", required=False),
                env("PS_URL"), env("PS_API_KEY"), env("PS_RUNAS_USER"))
        finally:
            browser.close()
    print("PRA <-> Password Safe integration created")


COMMANDS = {"installer": run_installer, "integration": run_integration}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        sys.exit(f"usage: python -m robot [{'|'.join(COMMANDS)}]")
    load_bundle()
    os.makedirs(os.environ.get("ROBOT_DOWNLOAD_DIR", "/work"), exist_ok=True)
    COMMANDS[sys.argv[1]]()
