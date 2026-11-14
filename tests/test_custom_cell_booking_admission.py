"""A custom demo cell's members are judged by the admission policy when the cell is BOOKED.

Before this, the gate ran only inside each cloud's create handler, at build time -- so a
cell the policy would refuse was booked anyway and failed member by member, possibly at
07:00 on the morning of the demo. Now ``custom_cell_service.launch`` runs the same gate on
every member before the cell row exists.

What this file holds in place:

  * **A refusal books nothing**, and names the member and the policy's reasons.
  * **needs_approval does not refuse a booking**: the member goes to approval when built.
  * **A booked cell is judged at its START**, so a weekday or hour rule answers for the
    build, not for the moment of booking.
  * **Admission off changes nothing.**
  * **One request builder per handler**, called by the handler AND the booking check, so
    the two cannot judge different requests.

The policy engine's verdict is the only thing stubbed; the gate, the builders and the
launch are real. Fixtures come from tests/test_custom_cell_lifecycle.py, which also stubs
the cloud at ``_create_member``.

Runs under pytest, or standalone:
    python tests/test_custom_cell_booking_admission.py
"""
import ast
import os
import sys
from datetime import timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import test_custom_cell_lifecycle as life  # noqa: E402  (its fixtures and cloud stubs)

from web_dashboard.database import CustomCell  # noqa: E402
from web_dashboard.services import admission_service  # noqa: E402
from web_dashboard.services import custom_cell_service as svc  # noqa: E402
from web_dashboard.utils.clock import utcnow  # noqa: E402

_ROOT = os.path.dirname(_HERE)
BIG = "n2-highmem-96"
HUGE_VM = {"kind": "vm", "cloud": "gcp", "label": "big", "count": 1,
           "spec": {"image_self_link": "projects/x/global/images/ubuntu",
                    "zone": "us-central1-a", "machine_type": BIG}}


class _Policy:
    """Admission ON, every action gated, and a verdict function of our choosing."""

    def __init__(self, verdict):
        self.verdict = verdict
        self.seen = []

    def __enter__(self):
        self._saved = (admission_service._enabled, admission_service.gated_actions,
                       admission_service.evaluate)
        admission_service._enabled = lambda: True
        admission_service.gated_actions = lambda: {
            "gcp:gce:deploy", "aws:ec2:deploy", "azure:vm:deploy",
            "k8s:provision", "clouddb:provision"}

        def _evaluate(action, context):
            self.seen.append((action, context))
            decision, reasons = self.verdict(action, context)
            return {"decision": decision,
                    "reasons": reasons if decision == "deny" else [],
                    "approval_reasons": reasons if decision == "needs_approval" else []}
        admission_service.evaluate = _evaluate
        return self

    def __exit__(self, *exc):
        (admission_service._enabled, admission_service.gated_actions,
         admission_service.evaluate) = self._saved


def _deny_big(action, ctx):
    if ctx["request"].get("instance_type") == BIG:
        return "deny", [f"instance type {BIG} is over the limit"]
    return "allow", []


def test_a_refused_member_refuses_the_booking_and_books_nothing():
    db = life._db()
    me = life._user(db, "demo-presenter")
    bp = life._bp(db, items=(life.MYSQL, HUGE_VM))
    before = db.query(CustomCell).count()
    with _Policy(_deny_big):
        try:
            life._launch(db, me, bp, name="toobig")
            raise AssertionError("a cell the policy refuses was booked")
        except svc.CellError as exc:
            msg = str(exc)
    assert "toobig-big-1" in msg and "over the limit" in msg, msg
    db.rollback()
    assert db.query(CustomCell).count() == before, "a refused booking left a cell row"
    assert not life.CALLS["created"], "a refused booking built a member"


def test_needs_approval_does_not_refuse_a_booking():
    db = life._db()
    me = life._user(db, "demo-presenter")
    with _Policy(lambda a, c: ("needs_approval", ["a second person signs off"])):
        cell = life._launch(db, me, life._bp(db, items=(life.MYSQL,)))
    assert cell.id and cell.status != "failed"


def test_a_booked_cell_is_judged_at_its_start():
    db = life._db()
    me = life._user(db, "demo-presenter")
    start = (utcnow() + timedelta(days=3)).replace(hour=7, minute=0, second=0, microsecond=0)
    with _Policy(lambda a, c: ("allow", [])) as pol:
        life._launch(db, me, life._bp(db, items=(life.MYSQL,)),
                     run_at=start.strftime("%Y-%m-%dT%H:%M"))
    nows = {c["now"]["weekday"] for _, c in pol.seen}
    assert nows == {start.strftime("%a").lower()}, (
        f"judged as of {nows}, not the start's weekday")
    assert {c["now"]["hour"] for _, c in pol.seen} == {7}


def test_every_member_is_judged_once_per_resource():
    db = life._db()
    me = life._user(db, "demo-presenter")
    with _Policy(lambda a, c: ("allow", [])) as pol:
        life._launch(db, me, life._bp(db), name="all")
    names = sorted(c["request"]["name"] for _, c in pol.seen)
    assert names == ["all-db", "all-gke", "all-web-1", "all-web-2"], names
    assert {a for a, _ in pol.seen} == {"gcp:gce:deploy", "k8s:provision",
                                       "clouddb:provision"}


def test_admission_off_changes_nothing():
    db = life._db()
    me = life._user(db, "demo-presenter")
    cell = life._launch(db, me, life._bp(db, items=(HUGE_VM,)))
    assert cell.id, "with admission control off a booking was refused"


def test_the_handler_and_the_booking_share_one_builder():
    """If the booking built its own request it could judge a different one from the build."""
    for module, fn in (("gcp", "deploy_admission"), ("aws", "deploy_admission"),
                       ("azure", "deploy_admission"), ("k8s", "provision_admission"),
                       ("cloud_databases", "provision_admission")):
        src = open(os.path.join(_ROOT, "web_dashboard", "api", f"{module}.py"),
                   encoding="utf-8").read()
        tree = ast.parse(src)
        assert any(isinstance(n, ast.FunctionDef) and n.name == fn for n in tree.body), (
            f"api/{module}.py has no {fn}")
        assert f"= {fn}(" in src, f"api/{module}.py's handler no longer calls {fn}"
        assert f'"{module}", "{fn}"' in open(
            os.path.join(_ROOT, "web_dashboard", "services", "custom_cell_service.py"),
            encoding="utf-8").read(), f"the booking check does not use api/{module}.{fn}"


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
