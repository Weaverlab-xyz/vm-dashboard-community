"""Unit tests for the Docker Compose parser/validator (compose_service).

Pure logic, no cloud/app dependencies. Runs under pytest, or standalone:
    python tests/test_compose_service.py
The module is loaded directly from its file so the test doesn't import the
whole web_dashboard package (which pulls in heavy cloud SDKs).
"""
import importlib.util
import os

try:
    import pytest
except ModuleNotFoundError:  # standalone fallback (no pytest installed)
    import contextlib

    class _Raises:
        def __init__(self, exc):
            self.exc = exc
            self.value = None

        def __enter__(self):
            return self

        def __exit__(self, et, ev, tb):
            assert et is not None and issubclass(et, self.exc), f"expected {self.exc.__name__}"
            self.value = ev
            return True

    class _Mark:
        @staticmethod
        def parametrize(_argnames, argvalues):
            def deco(fn):
                fn.pytestmark = [type("M", (), {"name": "parametrize", "args": (_argnames, argvalues)})()]
                return fn
            return deco

    class _Pytest:
        raises = staticmethod(lambda exc: _Raises(exc))
        mark = _Mark()

    pytest = _Pytest()

_MOD_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "web_dashboard", "services", "compose_service.py",
)
_spec = importlib.util.spec_from_file_location("compose_service", _MOD_PATH)
compose_service = importlib.util.module_from_spec(_spec)
import sys as _sys
_sys.modules["compose_service"] = compose_service  # dataclasses resolves __module__ via sys.modules
_spec.loader.exec_module(compose_service)

ComposeError = compose_service.ComposeError
parse_and_validate = compose_service.parse_and_validate


def test_parses_core_subset():
    spec = parse_and_validate(
        """
        version: "3.8"
        services:
          web:
            image: nginx:1.27
            command: nginx -g 'daemon off;'
            environment:
              FOO: bar
              EMPTY:
            ports:
              - "8080:80"
              - "53:53/udp"
            restart: always
            deploy:
              resources:
                limits:
                  cpus: "0.5"
                  memory: 512M
          sidecar:
            image: busybox
            command: ["sleep", "3600"]
            environment:
              - A=1
        """
    )
    assert [s.name for s in spec.services] == ["web", "sidecar"]
    web = spec.services[0]
    assert web.image == "nginx:1.27"
    assert web.command == ["nginx", "-g", "daemon off;"]
    assert ("FOO", "bar") in web.env and ("EMPTY", "") in web.env
    assert (8080, 80, "tcp") in web.ports
    assert (53, 53, "udp") in web.ports
    assert web.restart == "always"
    assert web.cpu == 0.5
    assert web.memory_mb == 512
    sidecar = spec.services[1]
    assert sidecar.command == ["sleep", "3600"]
    assert sidecar.env == [("A", "1")]


def test_port_shorthand_and_ip_form():
    spec = parse_and_validate(
        "services:\n  a:\n    image: x\n    ports:\n      - \"80\"\n      - \"127.0.0.1:9000:9000\"\n"
    )
    assert (80, 80, "tcp") in spec.services[0].ports
    assert (9000, 9000, "tcp") in spec.services[0].ports


def test_memory_units():
    assert compose_service._parse_memory("s", "1g") == 1024
    assert compose_service._parse_memory("s", "512m") == 512
    assert compose_service._parse_memory("s", "1073741824") == 1024
    assert compose_service._parse_memory("s", "256mb") == 256


@pytest.mark.parametrize("yaml_text, needle", [
    ("services:\n  a:\n    image: x\n    build: .\n", "build"),
    ("services:\n  a:\n    image: x\n    volumes:\n      - /data\n", "volumes"),
    ("services:\n  a:\n    image: x\n    depends_on:\n      - b\n", "depends_on"),
    ("networks:\n  n: {}\nservices:\n  a:\n    image: x\n", "networks"),
    ("services:\n  a:\n    image: x\n    deploy:\n      replicas: 3\n", "replicas"),
])
def test_rejects_unsupported_keys(yaml_text, needle):
    with pytest.raises(ComposeError) as ei:
        parse_and_validate(yaml_text)
    assert needle in str(ei.value)


def test_requires_image():
    with pytest.raises(ComposeError):
        parse_and_validate("services:\n  a:\n    command: echo hi\n")


def test_rejects_host_passthrough_env():
    with pytest.raises(ComposeError):
        parse_and_validate("services:\n  a:\n    image: x\n    environment:\n      - JUST_KEY\n")


def test_rejects_no_services():
    with pytest.raises(ComposeError):
        parse_and_validate("version: '3'\n")


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        # expand parametrize manually for standalone runs
        marks = getattr(fn, "pytestmark", [])
        cases = []
        for m in marks:
            if m.name == "parametrize":
                cases = m.args[1]
        if cases:
            for case in cases:
                try:
                    fn(*case)
                    print(f"ok   {fn.__name__}{case}")
                except Exception as e:  # noqa: BLE001
                    failures += 1
                    print(f"FAIL {fn.__name__}{case}: {e}")
        else:
            try:
                fn()
                print(f"ok   {fn.__name__}")
            except Exception as e:  # noqa: BLE001
                failures += 1
                print(f"FAIL {fn.__name__}: {e}")
    sys.exit(1 if failures else 0)
