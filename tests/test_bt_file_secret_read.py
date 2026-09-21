"""A file-type Secrets Safe secret reads over `raw`, and never reads as empty.

`ps-cli secrets get` projects a **per-type** field set. The file one is metadata only --
`FileName` and `FileHash` -- with no content field at all, where a text secret gets
`Text` and a credential gets `Password`. `--decrypt` cannot help: it adds a query
parameter, and there is no payload field in the projection for it to fill.

So the old `read_bt_secrets_safe` probed four field names, found none of them, and
returned `""`. Not an error -- an **empty secret**, which is the worst available answer.
A `bt_safe://` reference to a certificate bundle resolved to the empty string and
whatever consumed it failed somewhere else entirely, with nothing pointing back here.

The contents live behind `GET Secrets-Safe/Secrets/{id}/file/download`, reached here
through `ps-cli raw` using the id the `secrets get` already returned.

**Three things about `raw` that are not obvious**, each pinned below, because all three
fail by returning a plausible string rather than by raising:

  * on a body it cannot parse as JSON it prints a **banner line first** and then the
    body, so the first line has to come off;
  * on an HTTP error it prints ``Status: <code>`` and the response body to stdout and
    **still exits 0**, so a 404 would otherwise be handed back AS the secret;
  * it returns ``response.text``, so a **binary** payload (PKCS#12, DER) arrives already
    mangled. That is refused rather than returned -- a corrupt bundle that looks like a
    value is worse than a clear failure.

The last one is a ps-cli limit, not an API one: the endpoint serves
`application/octet-stream` and is byte-faithful. Anything needing real bytes has to call
it off ps-cli entirely.

Runs under pytest, or standalone:  python tests/test_bt_file_secret_read.py
"""
import os
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.join(_HERE, "..")
sys.path.insert(0, _ROOT)

CONF = {}
CALLS = []


def _stub(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module


# Stubbed unconditionally, as the sibling suites do: gating on whether boto3 / azure /
# google happen to be installed is how a suite passes locally and fails in CI.
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

FILE_ID = "11111111-2222-3333-4444-555555555555"
PEM = "-----BEGIN CERTIFICATE-----\nMIIB...\n-----END CERTIFICATE-----"


def _install(entry, body):
    """Point `_ps_run` at one `secrets get` result and one `raw` body."""
    CALLS.clear()

    def fake(args, timeout=30):
        CALLS.append(list(args))
        if args[0] == "secrets":
            return [entry] if entry is not None else []
        if args[0] == "raw":
            if isinstance(body, Exception):
                raise body
            return body
        raise AssertionError(f"unexpected ps-cli call: {args}")

    sbs._ps_run = fake


def _file_entry(**over):
    entry = {"Id": FILE_ID, "Title": "api-gateway-chain", "SecretType": "File",
             "FileName": "chain.pem", "FileHash": "abc123"}
    entry.update(over)
    return entry


def _raw_calls():
    return [c for c in CALLS if c and c[0] == "raw"]


def test_a_file_secret_is_fetched_over_raw_from_the_download_endpoint():
    """The whole point. The argv is pinned because the endpoint path is the one thing
    here that cannot be inferred from a failure -- a wrong path returns a 404 body,
    which the status guard turns into an error that looks like a permissions problem."""
    _install(_file_entry(), PEM)
    got = sbs.read_bt_secrets_safe("Certificates/api-gateway-chain")
    assert got == PEM, f"file secret did not come back: {got!r}"
    raw = _raw_calls()
    assert len(raw) == 1, f"expected exactly one raw call, got {CALLS}"
    assert raw[0] == ["raw", "GET", f"Secrets-Safe/Secrets/{FILE_ID}/file/download"], \
        f"wrong raw argv: {raw[0]}"


def test_the_non_json_banner_line_is_stripped_off_the_body():
    """`raw` prints 'Response is not valid JSON:' and THEN the body. Returning the
    banner as part of a PEM would produce a file that looks right in a diff and fails
    to parse everywhere."""
    _install(_file_entry(), "Response is not valid JSON:\n" + PEM)
    assert sbs.read_bt_secrets_safe("Certificates/api-gateway-chain") == PEM


def test_an_http_error_is_raised_and_not_returned_as_the_secret():
    """`raw` prints `Status: 404` to stdout and exits 0, so nothing upstream of here
    can tell this from a value. Without this guard a missing secret resolves to the
    literal string 'Status: 404 ...'."""
    _install(_file_entry(), "Status: 404\nResponse: {\"message\":\"Not found\"}")
    try:
        got = sbs.read_bt_secrets_safe("Certificates/gone")
        raise AssertionError(f"a 404 body was returned as the secret: {got!r}")
    except ValueError as exc:
        assert "404" in str(exc), f"the error does not quote the status: {exc}"


def test_a_binary_payload_is_refused_rather_than_returned_corrupt():
    """A PKCS#12 through `response.text` is already broken when we see it. Returning it
    would write a corrupt .pfx that fails much later, somewhere unrelated."""
    for damaged in ("��\x02chain", "MII\x00\x00binary"):
        _install(_file_entry(), damaged)
        try:
            got = sbs.read_bt_secrets_safe("Certificates/bundle-p12")
            raise AssertionError(f"corrupt bytes returned as a value: {got!r}")
        except ValueError as exc:
            assert "PEM" in str(exc), \
                f"the refusal does not say what to do instead: {exc}"


def test_a_json_file_secret_comes_back_as_text():
    """`raw` pretty-prints a JSON body, so `_ps_run` parses it and we never see a
    string. Re-serialising is the honest answer; returning '' or a dict is not."""
    _install(_file_entry(), {"user": "svc", "token": "t"})
    got = sbs.read_bt_secrets_safe("Certificates/creds-json")
    assert '"user"' in got and '"svc"' in got, f"JSON body lost: {got!r}"


def test_a_file_secret_with_no_id_fails_loudly():
    """The id is the only handle the download endpoint takes. Without it there is
    nothing to fetch, and returning '' would put us back where we started."""
    _install(_file_entry(Id=""), PEM)
    try:
        sbs.read_bt_secrets_safe("Certificates/api-gateway-chain")
        raise AssertionError("a file secret with no Id returned instead of raising")
    except ValueError as exc:
        assert "Id" in str(exc)


def test_text_and_credential_secrets_do_not_touch_the_raw_path():
    """The regression that matters in the other direction: every existing read is a
    text or credential secret, and none of them should now cost a second round trip."""
    for entry, expected in (
            ({"Id": "x", "SecretType": "Text", "Text": "hello"}, "hello"),
            ({"Id": "x", "SecretType": "Credential", "Password": "pw"}, "pw"),
            # SecretType absent entirely — older entries, and the field is not
            # load-bearing for these two because the payload field decides.
            ({"Id": "x", "Text": "legacy"}, "legacy")):
        _install(entry, PEM)
        assert sbs.read_bt_secrets_safe("dashboard/thing") == expected
        assert not _raw_calls(), f"a non-file secret made a raw call: {CALLS}"


def test_an_empty_listing_still_reads_as_empty():
    """Unchanged behaviour, pinned so the new branch cannot start raising on a secret
    that simply is not there -- callers treat '' as absent."""
    _install(None, PEM)
    assert sbs.read_bt_secrets_safe("dashboard/missing") == ""


def test_the_service_verb_pair_is_one_ps_cli_actually_has():
    """`raw GET` has to survive tests/test_pscli_grammar.py's table, which is what
    stops this from drifting into an invented call."""
    sys.path.insert(0, _HERE)
    import test_pscli_grammar as grammar
    assert "raw" in grammar.VERBS and "GET" in grammar.VERBS["raw"]
    argvs = [argv for _, argv in grammar._all_argvs()]
    assert ["raw", "GET", "<var>"] in argvs or any(
        a[:2] == ["raw", "GET"] for a in argvs), \
        "the grammar guard is not seeing the new raw GET call"


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
