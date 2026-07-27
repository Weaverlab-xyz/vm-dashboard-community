"""Unit tests: services/vm_naming — the name series behind count-based deploys.

The whole feature rests on one property: **N names in, N distinct VMs out**. Every
way that can break is an arithmetic mistake at a provider's length limit, so that is
what these pin.

The Azure case is the one worth reading. `azure_service.deploy_vm` derives the
in-guest hostname as `computer_name=vm_name[:15]` for Linux *and* Windows, so a
series that only differs past the 15th character produces distinct Azure resources
with the *same* hostname — and Entitle and Password Safe both key off hostname, so
onboarding silently attaches to the wrong box. `test_azure_names_stay_unique_after_
the_15_char_guest_truncation` is the regression guard for exactly that.

The browser mirrors `expand` in static/js/app.js to preview names before submit. The
fixtures in `test_shared_fixtures_with_the_js_preview` are the contract between the
two; change one side and this fails.

Loaded by file path so the module's stdlib-only promise is actually exercised — no
web_dashboard package import, no cloud SDKs, no pydantic.
Runs under pytest, or standalone:  python tests/test_vm_naming.py
"""
import importlib.util
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PATH = os.path.join(_ROOT, "web_dashboard", "services", "vm_naming.py")

_spec = importlib.util.spec_from_file_location("vm_naming", _PATH)
vn = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vn)


def test_the_canonical_case():
    assert vn.expand("web", 3, "aws") == ["web-01", "web-02", "web-03"]


def test_count_one_returns_the_base_verbatim():
    """The single-deploy path. No suffix, no truncation, no lowercasing — a count of
    1 must be byte-identical to what the endpoint did before counts existed."""
    assert vn.expand("web", 1, "aws") == ["web"]
    assert vn.expand("a-very-long-name-that-exceeds-fifteen", 1, "azure") == \
        ["a-very-long-name-that-exceeds-fifteen"]


def test_names_are_distinct_for_every_provider_at_every_count():
    for provider in ("aws", "azure", "gcp", "oci"):
        for count in range(2, vn.MAX_DEPLOY_COUNT + 1):
            names = vn.expand("web", count, provider)
            assert len(names) == count, (provider, count)
            assert len(set(names)) == count, (provider, count, names)


def test_truncation_trims_the_base_not_the_suffix():
    """Trimming the tail would collapse web-server-01/-02 into one name."""
    names = vn.expand("w" * 100, 3, "gcp")
    assert all(len(n) == 63 for n in names), [len(n) for n in names]
    assert [n[-3:] for n in names] == ["-01", "-02", "-03"]
    assert len(set(names)) == 3


def test_azure_names_stay_unique_after_the_15_char_guest_truncation():
    """azure_service.deploy_vm does computer_name=vm_name[:15]. If the series is not
    unique within 15 characters, two VMs share a hostname and Entitle/Password Safe
    onboard against the wrong one."""
    names = vn.expand("web-server-vm-cluster", 4, "azure")
    assert all(len(n) <= 15 for n in names), names
    assert len({n[:15] for n in names}) == 4, names


def test_aws_and_oci_never_truncate_a_realistic_name():
    for provider in ("aws", "oci"):
        names = vn.expand("a-perfectly-reasonable-instance-name", 5, provider)
        assert names[0] == "a-perfectly-reasonable-instance-name-01"


def test_truncation_does_not_leave_a_trailing_separator():
    """A cut landing on a hyphen would give web-server--01."""
    # 13 chars, so azure's 12-char budget cuts exactly on the trailing hyphen.
    names = vn.expand("web-server-x-", 2, "azure")
    assert "--" not in names[0], names
    assert names == ["web-server-x-01", "web-server-x-02"]


def test_a_base_with_no_room_left_is_rejected():
    try:
        vn.expand("ab", 2, "azure")
    except vn.VMNameError:
        pass
    else:
        raise AssertionError("expected VMNameError for a base under MIN_BASE_CHARS")


def test_gcp_rejects_a_non_rfc1035_base_rather_than_rewriting_it():
    for bad in ("Web Server", "WEB", "1web", "web_server", "-web"):
        try:
            vn.expand(bad, 2, "gcp")
        except vn.VMNameError:
            continue
        raise AssertionError(f"expected VMNameError for {bad!r}")


def test_gcp_expanded_names_are_legal_rfc1035():
    names = vn.expand("web-server", 3, "gcp")
    for n in names:
        assert vn._RFC1035.match(n), n


def test_aws_accepts_what_gcp_rejects():
    """Rejecting mixed case on AWS would invent a restriction the cloud does not have."""
    assert vn.expand("Web Server", 2, "aws") == ["Web Server-01", "Web Server-02"]


def test_suffix_width_never_drifts_from_the_cap():
    """Every count within MAX_DEPLOY_COUNT must read -01, and a raised cap must widen
    the padding automatically rather than silently colliding."""
    for count in range(1, vn.MAX_DEPLOY_COUNT + 1):
        assert vn.suffix_width(count) == 2, count
    assert vn.suffix_width(100) == 3
    assert vn.expand("web", 2, "aws")[0].endswith("-01")


def test_count_outside_the_allowed_range_is_rejected():
    for bad in (0, -1, vn.MAX_DEPLOY_COUNT + 1):
        try:
            vn.expand("web", bad, "aws")
        except vn.VMNameError:
            continue
        raise AssertionError(f"expected VMNameError for count={bad}")


def test_an_unknown_provider_is_rejected():
    try:
        vn.expand("web", 2, "nutanix")
    except vn.VMNameError:
        pass
    else:
        raise AssertionError("expected VMNameError for an unknown provider")


def test_start_offset_shifts_the_series():
    assert vn.expand("web", 2, "aws", start=5) == ["web-05", "web-06"]


def test_duplicates_is_case_insensitive():
    """Azure resource names are case-insensitive, so Web-01 and web-01 are one name."""
    assert vn.duplicates(["web-01", "web-02"]) == []
    assert vn.duplicates(["web-01", "WEB-01"]) == ["WEB-01"]
    assert vn.duplicates(["a", "a", "b", "b"]) == ["a", "b"]


def test_duplicates_ignores_blanks():
    assert vn.duplicates(["", "  ", "web"]) == []


def test_collisions_is_case_insensitive_and_sorted():
    assert vn.collisions(["web-01", "web-02"], {"web-01"}) == ["web-01"]
    assert vn.collisions(["WEB-01"], {"web-01"}) == ["WEB-01"]
    assert vn.collisions(["web-02", "web-01"], {"web-01", "web-02"}) == ["web-01", "web-02"]
    assert vn.collisions(["web-01"], set()) == []


def test_shared_fixtures_with_the_js_preview():
    """static/js/app.js mirrors expand() to preview names before submit. These are the
    cases both implementations must agree on — the JS side asserts the same table in
    tests/template_helpers_check.js."""
    assert vn.expand("web", 3, "gcp") == ["web-01", "web-02", "web-03"]
    assert vn.expand("web", 1, "gcp") == ["web"]
    assert vn.expand("verylongbasename", 2, "azure") == ["verylongbase-01", "verylongbase-02"]
    assert vn.expand("web-server", 2, "gcp") == ["web-server-01", "web-server-02"]


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
    sys.exit(1 if failures else 0)
