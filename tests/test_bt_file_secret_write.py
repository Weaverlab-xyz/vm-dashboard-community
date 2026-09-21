"""Writing a file-type Secrets Safe secret, and never changing one into a text secret.

ps-cli infers a secret's TYPE from which body argument it is given: `--text` makes a
text secret, `-fp` makes a file one. There is no inline way to supply a file body --
`-fp` takes a path and the CLI reads it, and `raw` cannot substitute because uploading
is multipart while `raw` sends `json=`. So a real file, briefly, on disk.

Two things are pinned here, and the second is the one that would have done damage:

  * **Creating.** `write_bt_secrets_safe(..., as_file=True)` writes the value to a 0600
    file in a 0700 private directory, passes `-fp`, and removes the directory
    afterwards -- including when ps-cli fails, which is exactly when a leftover file
    holding a secret would go unnoticed. The file's NAME is deliberate, because
    Password Safe records the basename as the secret's `FileName` and a temp name
    would be what the console shows forever.

  * **Updating preserves the type.** `update_bt_secrets_safe` used to pass `--text`
    unconditionally. Against a file secret that does not merely fail -- it changes what
    the secret IS, and `read_bt_secrets_safe` routes on exactly that field. An operator
    editing a certificate bundle in Browse & Edit would have silently converted it, and
    every consumer reading it from the download endpoint would break, somewhere else,
    later. The existing type is read first and matched, with the recorded `FileName`
    carried over so the edit does not rename the file either.

Runs under pytest, or standalone:  python tests/test_bt_file_secret_write.py
"""
import os
import stat
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.join(_HERE, "..")
sys.path.insert(0, _ROOT)

CONF = {}
CALLS = []
SEEN = []


def _stub(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module


_stub("web_dashboard.services.config_service",
      get=lambda key, default="": CONF.get(key, default),
      get_bool=lambda key, default=False: bool(CONF.get(key, default)),
      set=lambda key, value: CONF.__setitem__(key, value),
      delete=lambda key: CONF.pop(key, None))
_stub("web_dashboard.services.workload_credentials_service",
      write_static=lambda name, value, folder="": None)
_stub("boto3", client=lambda service, **kw: None)

try:
    import pydantic  # noqa: F401
except ImportError:
    _stub("web_dashboard.config", settings=types.SimpleNamespace(
        aws_region="", gcp_project_id=""))

from web_dashboard.services import secrets_backend_service as sbs  # noqa: E402

PEM = "-----BEGIN CERTIFICATE-----\nMIIB...\n-----END CERTIFICATE-----"
FILE_ID = "11111111-2222-3333-4444-555555555555"

sbs._bt_cfg = lambda: ("ps.example", "Certificates")
sbs._bt_owner_id = lambda: "7"
sbs._resolve_bt_folder_id = lambda folder: "folder-guid"


def _install(get_result, fail=None):
    """Stub `_ps_run`, capturing what `-fp` pointed at WHILE the call was in flight --
    the file is gone by the time the assertion runs, which is the point of it."""
    CALLS.clear()
    SEEN.clear()

    def fake(args, timeout=30):
        CALLS.append(list(args))
        if "-fp" in args:
            path = args[args.index("-fp") + 1]
            SEEN.append({
                "path": path,
                "exists": os.path.exists(path),
                "name": os.path.basename(path),
                "body": open(path, encoding="utf-8").read() if os.path.exists(path) else None,
                "mode": stat.S_IMODE(os.stat(path).st_mode) if os.path.exists(path) else None,
                "dir_mode": stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode),
            })
            if fail:
                raise fail
        if args[0] == "secrets" and args[1] == "get":
            return get_result() if callable(get_result) else get_result
        if args[0] == "raw":
            return PEM
        return ""

    sbs._ps_run = fake


def _argv(verb):
    hits = [c for c in CALLS if len(c) > 1 and c[1] == verb]
    assert len(hits) == 1, f"expected one {verb}, got {CALLS}"
    return hits[0]


def _file_entry(**over):
    entry = {"Id": FILE_ID, "SecretType": "File", "FileName": "chain.pem",
             "FolderId": "folder-guid"}
    entry.update(over)
    return entry


# -- creating ------------------------------------------------------------------

def test_as_file_passes_fp_and_never_text():
    """The flag is `-fp` / `--file-path`. `ps-cli secrets -h` advertises it as
    `--path`, which is a different flag belonging to `secrets get` -- taking the
    service help at face value would build a call ps-cli rejects."""
    _install([_file_entry()])
    sbs.write_bt_secrets_safe("api-gateway-chain", PEM, as_file=True)
    argv = _argv("create-secret")
    assert "-fp" in argv, f"file write did not use -fp: {argv}"
    assert "--text" not in argv, f"file write also passed --text: {argv}"


def test_the_body_reaches_disk_intact_and_private():
    """0600 in a 0700 directory, from `os.open` rather than a later chmod -- there is
    no window in which the file exists more readable than it should be."""
    _install([_file_entry()])
    sbs.write_bt_secrets_safe("api-gateway-chain", PEM, as_file=True)
    assert len(SEEN) == 1, "the -fp path was never inspected"
    seen = SEEN[0]
    assert seen["exists"], "the file did not exist when ps-cli was called"
    assert seen["body"] == PEM, f"body altered on the way to disk: {seen['body']!r}"
    # Windows maps only the write bit, so `os.open(..., 0o600)` reports 0o666 there
    # and the check would fail for a reason that says nothing about the code. The app
    # runs in a Linux container and CI is Linux, so this still bites where it counts.
    if os.name != "nt":
        assert seen["mode"] == 0o600, f"file mode is {oct(seen['mode'])}, not 0600"
        assert seen["dir_mode"] == 0o700, \
            f"dir mode is {oct(seen['dir_mode'])}, not 0700"


def test_the_temp_file_is_gone_afterwards():
    _install([_file_entry()])
    sbs.write_bt_secrets_safe("api-gateway-chain", PEM, as_file=True)
    assert not os.path.exists(SEEN[0]["path"]), "the secret was left on disk"
    assert not os.path.exists(os.path.dirname(SEEN[0]["path"])), "temp dir left behind"


def test_the_temp_file_is_gone_even_when_ps_cli_fails():
    """The case that matters. A failed write is when nobody goes looking, so a
    leftover file holding a certificate key would simply stay there."""
    _install([_file_entry()], fail=ValueError("ps-cli error: nope"))
    try:
        sbs.write_bt_secrets_safe("api-gateway-chain", PEM, as_file=True)
    except ValueError:
        pass
    else:
        raise AssertionError("the ps-cli failure was swallowed")
    assert SEEN, "ps-cli was never reached"
    assert not os.path.exists(SEEN[0]["path"]), "the secret survived a failed write"
    assert not os.path.exists(os.path.dirname(SEEN[0]["path"]))


def test_the_file_name_is_the_title_not_a_temp_name():
    """Password Safe records the basename as `FileName`, so `tmp8fq2x1` would be what
    the console shows for the life of the secret."""
    _install([_file_entry()])
    sbs.write_bt_secrets_safe("api-gateway-chain", PEM, as_file=True)
    assert SEEN[0]["name"] == "api-gateway-chain", SEEN[0]["name"]


def test_an_explicit_file_name_wins():
    _install([_file_entry()])
    sbs.write_bt_secrets_safe("api-gateway-chain", PEM, as_file=True,
                              file_name="chain.pem")
    assert SEEN[0]["name"] == "chain.pem", SEEN[0]["name"]


def test_a_separator_in_the_name_cannot_escape_the_temp_directory():
    """The title reaches here from an operator-supplied key, and it becomes a path
    component."""
    for hostile in ("../../etc/passwd", "a/b", "..", "/abs"):
        _install([_file_entry()])
        sbs.write_bt_secrets_safe("k", PEM, as_file=True, file_name=hostile)
        path, name = SEEN[0]["path"], SEEN[0]["name"]
        assert os.sep not in name and "/" not in name, f"separator survived: {name!r}"
        assert name not in ("", ".", ".."), f"degenerate name: {name!r}"
        assert os.path.dirname(os.path.abspath(path)).startswith(
            os.path.abspath(os.path.dirname(os.path.dirname(path)))), path


def test_the_default_is_still_a_text_secret():
    """Every existing caller passes no flag and must keep getting `--text`; a file
    secret costs an extra round trip on every read."""
    _install([{"Id": "x", "SecretType": "Text", "FolderId": "folder-guid"}])
    sbs.write_bt_secrets_safe("some_config_key", "a value")
    argv = _argv("create-secret")
    assert "--text" in argv and "-fp" not in argv, argv
    assert not SEEN, "a text write touched the disk"


# -- updating ------------------------------------------------------------------

def test_updating_a_file_secret_keeps_it_a_file_secret():
    """The silent-conversion guard. `--text` here would change the secret's TYPE, and
    the read path routes on that field."""
    _install(lambda: [_file_entry()])
    sbs.update_bt_secrets_safe("Certificates/api-gateway-chain", PEM)
    argv = _argv("create-secret")
    assert "-fp" in argv and "--text" not in argv, \
        f"an update converted a file secret to text: {argv}"


def test_updating_a_file_secret_keeps_its_recorded_file_name():
    """An edit should not rename the file either -- `FileName` is what the console
    and any consumer see."""
    _install(lambda: [_file_entry(FileName="bundle.pem")])
    sbs.update_bt_secrets_safe("Certificates/api-gateway-chain", PEM)
    assert SEEN[0]["name"] == "bundle.pem", SEEN[0]["name"]


def test_updating_a_text_secret_still_uses_text():
    _install(lambda: [{"Id": "x", "SecretType": "Text", "Text": PEM}])
    sbs.update_bt_secrets_safe("dashboard/thing", PEM)
    argv = _argv("create-secret")
    assert "--text" in argv and "-fp" not in argv, argv
    assert not SEEN


def test_a_trailing_newline_is_not_mistaken_for_a_failed_write():
    """ps-cli's stdout is stripped on the way back, so a stored PEM ending in a
    newline cannot read back with one. Comparing strictly would raise 'did not
    persist' on a write that persisted perfectly."""
    _install(lambda: [_file_entry()])
    sbs.update_bt_secrets_safe("Certificates/api-gateway-chain", PEM + "\n")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
