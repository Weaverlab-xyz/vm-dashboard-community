"""Every shipped sample in examples/compose/ must pass the deploy-time validator.

Guarantees `examples/compose/*.yml` deploy cleanly via the Containers page (i.e.
`compose_service.parse_and_validate` accepts them — no unsupported keys sneak in).

Run: python tests/test_compose_samples.py   (or under pytest)
"""
import glob
import importlib.util
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MOD_PATH = os.path.join(_ROOT, "web_dashboard", "services", "compose_service.py")
_spec = importlib.util.spec_from_file_location("compose_service", _MOD_PATH)
compose_service = importlib.util.module_from_spec(_spec)
sys.modules["compose_service"] = compose_service  # dataclasses resolve __module__
_spec.loader.exec_module(compose_service)

_SAMPLES = sorted(glob.glob(os.path.join(_ROOT, "examples", "compose", "*.yml")))


def _validate_one(path):
    with open(path) as f:
        spec = compose_service.parse_and_validate(f.read())
    assert spec.services, f"{os.path.basename(path)} parsed to zero services"
    for svc in spec.services:
        assert svc.image, f"{os.path.basename(path)} service '{svc.name}' has no image"


def test_samples_exist():
    assert _SAMPLES, "no sample compose files found in examples/compose/"


def test_all_samples_validate():
    for path in _SAMPLES:
        _validate_one(path)  # raises ComposeError if a sample is non-conforming


if __name__ == "__main__":
    failures = 0
    if not _SAMPLES:
        print("FAIL: no samples found")
        sys.exit(1)
    for p in _SAMPLES:
        try:
            _validate_one(p)
            print(f"ok   {os.path.basename(p)}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {os.path.basename(p)}: {e}")
    sys.exit(1 if failures else 0)
