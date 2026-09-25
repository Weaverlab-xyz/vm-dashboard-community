"""The become-method field: its allowlist, and the two copies of it staying equal.

Run standalone (``python tests/test_ansible_become.py``) like the other agent tests —
CI runs each file on its own and reads the exit code.
"""
import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from web_dashboard.services import ansible_become as ab          # noqa: E402
from web_dashboard.services import ansible_run_meta, agent_ansible_meta  # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else f"  → {detail!r}"))
    if not cond:
        FAILED.append(name)


# ── the allowlist ────────────────────────────────────────────────────────────
check("pbrun is offered", "pbrun" in ab.BECOME_METHODS, ab.BECOME_METHODS)
check("pmrun is offered", "pmrun" in ab.BECOME_METHODS, ab.BECOME_METHODS)
check("sudo is offered", "sudo" in ab.BECOME_METHODS, ab.BECOME_METHODS)

check("empty means 'leave Ansible's default'", ab.normalize("") == "")
check("None means the same", ab.normalize(None) == "")
check("case and padding are normalized", ab.normalize("  PBRun ") == "pbrun")

try:
    ab.normalize("rm -rf /")
    check("an unknown method is refused", False, "accepted")
except ab.BecomeError as exc:
    check("an unknown method is refused", True)
    check("the refusal lists what IS allowed", "pbrun" in str(exc), str(exc))

# A method is a plugin NAME. If this ever accepts something with a space or a slash, the
# field has stopped being a name and become a command fragment.
for bad in ("sudo -u root", "/usr/bin/pbrun", "pbrun;id", "become_exe=pbrun"):
    try:
        ab.normalize(bad)
        check(f"{bad!r} is refused", False, "accepted")
    except ab.BecomeError:
        check(f"{bad!r} is refused", True)


# ── apply_to ─────────────────────────────────────────────────────────────────
v = {}
ab.apply_to(v, "")
check("an unset method sets NO var", v == {}, v)

v = {}
ab.apply_to(v, "pbrun")
check("a set method sets ansible_become_method",
      v == {"ansible_become_method": "pbrun"}, v)

v = {"ansible_become_password": "x"}
ab.apply_to(v, "pbrun")
check("apply_to leaves the rest of the vars alone",
      v.get("ansible_become_password") == "x", v)


# ── the two copies of the list ───────────────────────────────────────────────
# The agent is a separate process and cannot import from web_dashboard, so it carries its
# own copy. A method the dashboard offers and the agent refuses is a run that fails only
# on agent-executed targets — parse the literal out rather than trust them to be edited
# together.
src = (ROOT / "runners" / "agent" / "agent.py").read_text(encoding="utf-8")
tree = ast.parse(src)
agent_methods = None
for node in ast.walk(tree):
    if (isinstance(node, ast.Assign)
            and any(getattr(t, "id", "") == "_BECOME_METHODS" for t in node.targets)):
        agent_methods = set(ast.literal_eval(node.value.args[0]))

check("the agent declares _BECOME_METHODS", agent_methods is not None)
check("the agent's allowlist equals the dashboard's",
      agent_methods == set(ab.BECOME_METHODS),
      (agent_methods or set()) ^ set(ab.BECOME_METHODS))


# ── the field survives a job-metadata round trip ─────────────────────────────
# Declared-but-undropped is the whole risk: a key missing from RUN_META_KEYS is written
# by the endpoint and then silently absent when the worker reconstructs the run.
check("become_method is in the local runner's meta keys",
      "become_method" in ansible_run_meta.RUN_META_KEYS)
check("become_method is in the agent runner's meta keys",
      "become_method" in agent_ansible_meta.RUN_META_KEYS)


class _Payload:
    def __init__(self, **kw):
        self.__dict__.update(kw)
        for key in ansible_run_meta.RUN_META_KEYS:
            self.__dict__.setdefault(key, ansible_run_meta._DEFAULTS[key])


meta = ansible_run_meta.run_meta(_Payload(become_method="pbrun"),
                                 description="d", asset_backend="")
check("run_meta carries the method", meta.get("become_method") == "pbrun", meta)
check("run_kwargs hands it back",
      ansible_run_meta.run_kwargs(meta).get("become_method") == "pbrun")

# A job queued before this field existed must resume exactly as that build ran it.
check("a pre-field job defaults to Ansible's own default",
      ansible_run_meta.run_kwargs({"asset": "x.yml"}).get("become_method") == "")

# The local runner is called with **run_kwargs, so a key the signature lacks is a
# TypeError inside a background worker where nobody is watching.
import inspect                                                    # noqa: E402
from web_dashboard.services import ansible_local_run_service as alrs  # noqa: E402
params = set(inspect.signature(alrs._run_job).parameters)
missing = set(ansible_run_meta.RUN_META_KEYS) - params
check("_run_job accepts every meta key", not missing, missing)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("ALL OK")
