"""The two browser flows. Everything else is done through the PS / PRA / dashboard APIs.

Credentials come from the caller (env vars in __main__); nothing here logs a secret.
"""
import os

import pyotp

from .selectors import PRA, PS, need


def _login(page, url: str, table: dict, user: str, password: str, totp_secret: str = ""):
    page.goto(url)
    page.fill(need(table, "login_user"), user)
    page.fill(need(table, "login_password"), password)
    page.click(need(table, "login_submit"))
    if totp_secret:
        page.fill(need(table, "login_totp"), pyotp.TOTP(totp_secret).now())
        page.keyboard.press("Enter")
    page.wait_for_load_state("networkidle")


def fetch_resource_broker_installer(page, url, user, password, totp_secret, download_dir):
    """Log into Password Safe, download the RB installer, read the installer key.

    Returns (installer_path, installer_key).
    """
    _login(page, url, PS, user, password, totp_secret)
    page.click(need(PS, "nav_resource_broker"))
    with page.expect_download(timeout=10 * 60 * 1000) as dl:
        page.click(need(PS, "installer_download"))
    download = dl.value
    path = os.path.join(download_dir, download.suggested_filename)
    download.save_as(path)
    key = page.inner_text(need(PS, "installer_key")).strip()
    if not key:
        raise RuntimeError("Installer key element was empty.")
    return path, key


def create_pra_ps_integration(page, url, user, password, totp_secret,
                              ps_url, ps_api_key, ps_runas_user):
    """Log into PRA and create the Password Safe integration (not available via API)."""
    _login(page, url, PRA, user, password, totp_secret)
    page.click(need(PRA, "nav_integrations"))
    page.click(need(PRA, "add_ps_integration"))
    page.fill(need(PRA, "field_ps_url"), ps_url)
    page.fill(need(PRA, "field_api_key"), ps_api_key)
    page.fill(need(PRA, "field_runas_user"), ps_runas_user)
    page.click(need(PRA, "save"))
    page.wait_for_selector(need(PRA, "saved_toast"), timeout=30_000)
