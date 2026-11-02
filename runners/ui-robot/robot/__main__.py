"""CLI entry point.

  python -m robot installer   # PS: download RB installer + key, upload via the dashboard
  python -m robot integration # PRA: create the Password Safe integration

All inputs are environment variables so no secret appears in `ps` or the image.
"""
import os
import stat
import sys

from playwright.sync_api import sync_playwright

from . import flows
from .storage_client import DashboardStorage


def env(name: str, required: bool = True) -> str:
    value = os.environ.get(name, "")
    if required and not value:
        sys.exit(f"Missing required environment variable {name}")
    return value


def _page(p):
    browser = p.chromium.launch(headless=os.environ.get("ROBOT_HEADED") != "1")
    return browser, browser.new_context(accept_downloads=True).new_page()


def run_installer():
    # Fail on the cheap checks before driving a browser for ten minutes.
    storage = DashboardStorage(env("DASHBOARD_URL"), env("DASHBOARD_TOKEN"),
                               verify=os.environ.get("DASHBOARD_CA_BUNDLE") or True)
    backend = storage.active_backend()
    print(f"Active storage backend: {backend['label']}")
    key_file = env("INSTALLER_KEY_FILE")
    with sync_playwright() as p:
        browser, page = _page(p)
        try:
            path, key = flows.fetch_resource_broker_installer(
                page, env("PS_URL"), env("PS_USER"), env("PS_PASSWORD"),
                env("PS_TOTP_SECRET", required=False), env("ROBOT_DOWNLOAD_DIR"))
        finally:
            browser.close()
    # Owner-only, never printed. The caller (or the dashboard) moves it to a secret store.
    fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w") as fh:
        fh.write(key)
    print(f"Installer key written to {key_file}")
    name = storage.upload(path)
    print(f"Uploaded {name} to the active storage backend")


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
    COMMANDS[sys.argv[1]]()
