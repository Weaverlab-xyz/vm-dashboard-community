"""Every azure-mgmt-compute write must be built from SDK MODEL objects.

This bug has now shipped twice, from two different call sites, with the same
signature both times:

    (InvalidRequestContent) The request content was invalid and could not be
    deserialized: 'Could not find member 'hardware_profile' on object of type
    'ResourceDefinition'. Path 'hardware_profile', line 1, position 153.'

`azure-mgmt-compute` 38.x moved to the typespec `azure.core` models, and under that
SDK a **raw snake_case dict** handed to an operation is forwarded to ARM verbatim:
no snake_case -> camelCase mapping, and no nesting under `properties`. The older
msrest-based majors quietly coerced such a dict into the model, which is why the
same code worked for months and then broke on a rebuild that floated the pin.

The tell is nasty: the request is rejected by ARM, so it looks like a *cloud*
problem (bad size, bad region, a quota) rather than a client-side serialization
one, and only the call sites that used dicts break — so it reads as specific to
whatever feature owned that site (the image export, then the managed nodes) rather
than as one SDK-wide mistake.

A model-built payload serializes correctly on BOTH the old and new majors, so it is
the only shape that is safe across the pin range in requirements.txt. This test
asserts the shape structurally, because the Azure SDK is not installed in CI.

Run: python tests/test_azure_sdk_model_payloads.py   (or under pytest)
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_SERVICES = os.path.join(_ROOT, "web_dashboard", "services")

# `tags` and `location` are top-level ARM members whose names are identical in
# snake_case and on the wire, so a dict of only those serializes correctly either
# way -- `begin_update(rg, vm, {"tags": ...})` is legitimate. Anything else in a raw
# dict either needs case mapping, `properties` nesting, or both.
_WIRE_SAFE_KEYS = {"tags", "location"}

_CALL = re.compile(r"compute\.[a-z_]+\.begin_(?:create_or_update|update)\(")


def _sources():
    for name in sorted(os.listdir(_SERVICES)):
        if name.endswith(".py"):
            with open(os.path.join(_SERVICES, name), encoding="utf-8") as fh:
                yield name, fh.read()


def _spans(src, open_idx):
    """Walk the bracketed region opening at `open_idx`, returning
    ``(whole_region, top_level_items)`` -- string-literal aware, so a brace or paren
    inside a quoted value does not throw the balance off."""
    depth, quote, start, items = 0, "", open_idx + 1, []
    for i in range(open_idx, len(src)):
        ch = src[i]
        if quote:
            if ch == "\\":
                continue
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                items.append(src[start:i])
                return src[open_idx:i + 1], items
        elif ch == "," and depth == 1:
            items.append(src[start:i])
            start = i + 1
    return "", []


def _args(src, open_paren):
    """The top-level argument strings of the call whose '(' is at `open_paren`."""
    return _spans(src, open_paren)[1]


def _raw_dict_keys(arg):
    """The keys of `arg` when it is a dict literal, else None."""
    arg = arg.strip()
    if not arg.startswith("{"):
        return None
    return set(re.findall(r'"([a-z_]+)"\s*:', arg))


def _payload_keys(src, arg, call_start):
    """The keys of the payload, whether it is an inline dict literal or a local built
    a few lines above and passed by name -- the FIRST incident of this bug was the
    by-name shape (`snap_params = {...}`), so a detector that only sees inline dicts
    would have missed it."""
    inline = _raw_dict_keys(arg)
    if inline is not None:
        return inline
    name = arg.strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", name):
        return None
    assigns = list(re.finditer(rf"\n\s*{re.escape(name)}(?::\s*\w+)?\s*=\s*\{{",
                               src[:call_start]))
    if not assigns:
        return None
    brace = src.index("{", assigns[-1].start())
    return _raw_dict_keys(_spans(src, brace)[0])


def test_no_compute_write_is_handed_a_raw_dict_payload():
    offenders = []
    for name, src in _sources():
        for m in _CALL.finditer(src):
            args = _args(src, m.end() - 1)
            if not args:
                continue
            keys = _payload_keys(src, args[-1], m.start())
            if keys is None:
                continue
            if keys - _WIRE_SAFE_KEYS:
                line = src[:m.start()].count("\n") + 1
                offenders.append(f"{name}:{line} {m.group(0)} keys={sorted(keys)}")
    assert not offenders, (
        "azure-mgmt-compute writes are being handed a raw snake_case dict, which the "
        "track2 SDK sends to ARM verbatim -- Azure rejects the whole call with "
        "\"(InvalidRequestContent) ... Could not find member '<key>' on object of "
        "type 'ResourceDefinition'\". Build the payload from azure.mgmt.compute.models "
        "instead:\n  " + "\n  ".join(offenders))


def test_the_managed_node_launcher_builds_its_vm_from_models():
    """The Portainer AND Rancher Azure deploys both land in this one function, so a
    dict here takes out every managed node on Azure at once."""
    with open(os.path.join(_SERVICES, "azure_service.py"), encoding="utf-8") as fh:
        src = fh.read()
    body = src[src.index("def _run_vm_container_node_sync("):]
    body = body[:body.index("\nasync def ")]
    for model in ("VirtualMachine(", "HardwareProfile(", "StorageProfile(",
                  "OSProfile(", "NetworkProfile("):
        assert model in body, f"the node VM payload does not use {model[:-1]}"


def test_the_managed_node_data_disk_is_built_from_models():
    with open(os.path.join(_SERVICES, "azure_service.py"), encoding="utf-8") as fh:
        src = fh.read()
    body = src[src.index("def _ensure_node_data_disk_sync("):]
    body = body[:body.index("\nasync def ")]
    assert "Disk(" in body and "CreationData(" in body, (
        "the node's durable data disk is created from a raw dict, so the disk ensure "
        "fails before the VM is ever attempted")


def test_the_compute_pin_still_covers_the_typespec_majors():
    """A pin that floats into an untested major is how both incidents started. The
    code is model-built now so it is safe across the range -- this just documents
    that the range is deliberate and bounded."""
    with open(os.path.join(_ROOT, "web_dashboard", "requirements.txt"),
              encoding="utf-8") as fh:
        reqs = fh.read()
    m = re.search(r"^azure-mgmt-compute([^\n]*)$", reqs, re.M)
    assert m, "azure-mgmt-compute is not pinned in requirements.txt"
    assert "<" in m.group(1), (
        "azure-mgmt-compute has no upper bound, so a rebuild can float into a major "
        "whose serialization behaviour has never been exercised here")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
