"""The PackerError body must read like the Live Output pane, not like CSV.

`packer build -machine-readable` emits one CSV row per line —
`timestamp,target,type,subtype,message` — and escapes every comma inside the
message with a token of its own. The job detail page has two panels fed from
the same stream:

  * Live Output, which goes through `_human_readable` and therefore shows the
    provisioner's sentence with real commas;
  * the Error panel, which shows `str(PackerError)`.

The Error panel used to join the RAW lines, so a failed build showed the
timestamp/target/`ui,error` prefix and Packer's comma-escape token mid-sentence
while the pane six inches above it showed the same sentence cleanly. That is a
presentation-only bug, invisible to every test that only checks the build
succeeds, and it only ever shows up on a build that FAILED — i.e. exactly when
someone is trying to read it.

`_human_readable` returns None for lines it suppresses, so the fix has two ways
to go wrong on its own: joining the string "None" into the message, or dropping
the plain-text lines that `packer init` produces (those are not CSV at all).

packer_service imports only stdlib at module level, so the helper is exercised
for real rather than asserted on as source text.

Run: python tests/test_packer_error_detail.py
"""
import importlib.util
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The escape Packer substitutes for a comma inside a machine-readable field.
COMMA_ESCAPE = "%!(PACKER_" + "COMMA)"


def _read(*parts):
    with open(os.path.join(_ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def _load(*parts):
    """Load a module straight off disk — packer_service is stdlib-only, so this
    works without the app's dependency set installed."""
    path = os.path.join(_ROOT, *parts)
    spec = importlib.util.spec_from_file_location(parts[-1][:-3] + "_probe", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ps = _load("web_dashboard", "services", "packer_service.py")

# The real shape of the line that started this: an OT sim cell Azure build whose
# provisioner refused a bad OT_FAAS/OT_ROLE combination, with two escaped commas.
REAL_FAILURE = (
    "1790354231,,ui,error,==> vm-dashboard.azure-arm.build: [ot-sim] ERROR: "
    "OT_FAAS applies to OT_ROLE=broker only — the cell is the plant floor and "
    "runs simulators" + COMMA_ESCAPE + " not the adapters" + COMMA_ESCAPE + " so "
    "there is nothing to publish to."
)


def test_escape_token_is_decoded():
    """The whole point: no comma-escape token survives into the Error panel."""
    detail = ps._error_detail([REAL_FAILURE])
    assert COMMA_ESCAPE not in detail, f"escape token left in the message: {detail!r}"
    assert "runs simulators, not the adapters, so" in detail, detail


def test_csv_prefix_is_stripped():
    """Timestamp/target/type/subtype are machine plumbing, not error text."""
    detail = ps._error_detail([REAL_FAILURE])
    assert detail.startswith("==> vm-dashboard.azure-arm.build:"), detail
    assert "1790354231" not in detail
    assert ",ui,error," not in detail


def test_error_panel_matches_live_output():
    """The two panels are fed by the same humanizing path, so they must agree —
    that equality is the invariant, not either string on its own."""
    assert ps._error_detail([REAL_FAILURE]) == ps._human_readable(REAL_FAILURE)


def test_suppressed_lines_are_dropped_not_stringified():
    """`_human_readable` returns None for noise; None must not be joined in."""
    noisy = [
        "1790354200,,version,1.11.2",
        "1790354201,,ui,say,Build started",
        "1790354202,,metadata,run-uuid,0f3c",
        "1790354203,vm-dashboard.azure-arm,artifact-count,1",
        REAL_FAILURE,
    ]
    detail = ps._error_detail(noisy)
    assert "None" not in detail, detail
    assert detail.splitlines() == ["Build started", ps._human_readable(REAL_FAILURE)]


def test_plain_text_lines_survive():
    """`packer init` output is not machine-readable — it must pass through."""
    plain = [
        "Installed plugin github.com/hashicorp/azure v2.1.7",
        "Error: 1 error occurred:",
        "\t* Unknown plugin source",
    ]
    assert ps._error_detail(plain).splitlines() == plain


def test_blank_lines_dropped_and_tail_limited():
    lines = []
    for i in range(40):
        lines.append("")
        lines.append(f"179035{i:04d},,ui,error,line {i}")
    detail = ps._error_detail(lines)
    assert "" not in detail.splitlines(), "blank lines should not pad the panel"
    assert len(detail.splitlines()) == 20, detail
    assert detail.splitlines()[-1] == "line 39"
    assert ps._error_detail(lines, limit=3).splitlines() == ["line 37", "line 38", "line 39"]


def test_run_build_uses_the_humanized_detail():
    """Pin the call site: a future edit must not go back to joining raw lines.

    The helper being correct is worthless if the raise stops calling it, and
    nothing else in the suite runs a real failing `packer build`."""
    src = _read("web_dashboard", "services", "packer_service.py")
    raise_at = src.index("packer build failed (exit")
    body = src[raise_at - 1200:raise_at + 200]
    assert "_error_detail(" in body, "the build-failure raise no longer humanizes its lines"
    assert '"\\n".join(errors[-20:])' not in src, "raw machine-readable join is back"


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
