"""A cloud POV is never provisioned with its idle timer silently off.

The dashboard is the idle timer on AWS: the reconcile pass stops a running POV only when
its row carries a `suspend_on_idle_seconds`. The provision request used to default that to
0 ("off"), and only the web form pre-filled an hour -- so an API caller, or a blueprint that
left it blank, built a POV that was never stopped and the only symptom was the invoice.

Pinned here: ABSENT is distinguishable from an explicit 0, and the default the server
applies is a window the idle check will actually honour.

Runs under pytest, or standalone:
    python tests/test_pov_default_idle_window.py
"""
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-pov-default-idle")
_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".db")
os.close(_DB_FD)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

from web_dashboard.api.pov import ProvisionRequest  # noqa: E402
from web_dashboard.services import lab_platforms, pov_cloud_env  # noqa: E402


def test_an_absent_window_is_not_an_explicit_off():
    assert ProvisionRequest(name="pov-a").suspend_on_idle_seconds is None
    assert ProvisionRequest(name="pov-a", suspend_on_idle_seconds=0).suspend_on_idle_seconds == 0


def test_the_default_is_a_window_the_idle_check_honours():
    d = pov_cloud_env.DEFAULT_IDLE_WINDOW_S
    assert pov_cloud_env.IDLE_MIN_WINDOW_S <= d <= pov_cloud_env.IDLE_MAX_WINDOW_S, d


def test_aws_is_a_platform_the_default_applies_to():
    assert lab_platforms.supports("aws", "dashboard_idle_suspend")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
