"""Every shipped sample in examples/k8s/ must be a well-formed Kubernetes object.

These manifests are applied into managed clusters by hand (Portainer / kubectl
through PRA), so there is no deploy-time validator to lean on the way
examples/compose/ has `compose_service.parse_and_validate`. Instead we assert
each file parses as multi-doc YAML and every document carries the three fields
the API server requires — `apiVersion`, `kind`, and `metadata.name` — so a
malformed or truncated sample can't ship.

Run: python tests/test_k8s_samples.py   (or under pytest)
"""
import glob
import os

import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SAMPLES = sorted(glob.glob(os.path.join(_ROOT, "examples", "k8s", "*.yaml")))


def _validate_one(path):
    base = os.path.basename(path)
    with open(path) as f:
        docs = [d for d in yaml.safe_load_all(f) if d is not None]
    assert docs, f"{base} parsed to zero objects"
    for i, doc in enumerate(docs):
        assert isinstance(doc, dict), f"{base} doc {i} is not a mapping"
        assert doc.get("apiVersion"), f"{base} doc {i} missing apiVersion"
        assert doc.get("kind"), f"{base} doc {i} missing kind"
        name = (doc.get("metadata") or {}).get("name")
        assert name, f"{base} doc {i} ({doc.get('kind')}) missing metadata.name"


def test_samples_exist():
    assert _SAMPLES, "no sample manifests found in examples/k8s/"


def test_all_samples_validate():
    for path in _SAMPLES:
        _validate_one(path)


if __name__ == "__main__":
    import sys
    if not _SAMPLES:
        print("FAIL: no samples found")
        sys.exit(1)
    failures = 0
    for p in _SAMPLES:
        try:
            _validate_one(p)
            print(f"ok   {os.path.basename(p)}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {os.path.basename(p)}: {e}")
    sys.exit(1 if failures else 0)
