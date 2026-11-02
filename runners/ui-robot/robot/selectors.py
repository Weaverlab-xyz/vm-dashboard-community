"""Every UI locator the flows use, in one place.

All values start as TODO. Record each flow once by hand with
`playwright codegen <url>` (see README.md) and paste the selectors here. A flow that hits
a TODO stops with the name of the locator, instead of clicking at random.
"""

TODO = "TODO"

PS = {
    "login_user":        TODO,
    "login_password":    TODO,
    "login_submit":      TODO,
    "login_totp":        TODO,   # leave TODO if the account has no MFA
    "nav_resource_broker": TODO,
    "installer_download": TODO,  # the control that starts the download
    "installer_key":     TODO,   # element whose text is the installer key
}

PRA = {
    "login_user":        TODO,
    "login_password":    TODO,
    "login_submit":      TODO,
    "login_totp":        TODO,
    "nav_integrations":  TODO,
    "add_ps_integration": TODO,
    "field_ps_url":      TODO,
    "field_api_key":     TODO,
    "field_runas_user":  TODO,
    "save":              TODO,
    "saved_toast":       TODO,   # something that only appears on success
}


def need(table: dict, key: str) -> str:
    value = table[key]
    if value == TODO:
        raise RuntimeError(
            f"Selector '{key}' has not been recorded yet. Run `playwright codegen` against "
            f"the real UI and fill it in robot/selectors.py.")
    return value
