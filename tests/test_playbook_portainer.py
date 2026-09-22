"""The Portainer sample playbooks: the API traps they encode, and the Jinja that
does the work.

`examples/playbooks/portainer/` is how Portainer is configured from Config
Management — teams, environment access, and registering a Docker host as an Edge
environment. Four of the things these plays get right are not obvious from reading
them, and all four are silent when wrong:

  * `PUT /api/endpoints/{id}` REPLACES the access-policy map. Portainer's handler
    assigns the payload's TeamAccessPolicies straight over the stored one, so a PUT
    naming only the team being granted REVOKES every other team's access — and
    nothing errors. The plays read the environment and merge.
  * `POST /api/endpoints` is MULTIPART FORM. A JSON body comes back as a
    missing-name validation error that says nothing about the cause.
  * the runner image for an SSH target has NO Docker collection, so the agent is
    started by shelling out — `community.docker.docker_container` would fail at
    import on every run.
  * every request carries the API token in a header, so every task that makes one is
    `no_log` — which in turn means a mutating call's own failure detail is censored,
    which is why each play re-reads and asserts.

The Jinja that merges, matches and accumulates is rendered here against sample data
rather than eyeballed: a wrong expression in a playbook is discovered on a customer's
Portainer, not in CI.

Run: python tests/test_playbook_portainer.py   (or under pytest)
"""
import os
import sys

import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DIR = os.path.join(_ROOT, "examples", "playbooks", "portainer")

TEAM_ENSURE = "portainer-team-ensure.yml"
ENV_ACCESS = "portainer-env-access.yml"
EDGE_ENSURE = "portainer-edge-env-ensure.yml"
JIT_PREREQS = "portainer-jit-prereqs.yml"
NEW_PLAYS = (TEAM_ENSURE, ENV_ACCESS, EDGE_ENSURE, JIT_PREREQS)


def _text(name):
    return open(os.path.join(_DIR, name), encoding="utf-8").read()


def _plays(name):
    return yaml.safe_load(_text(name))


def _tasks(name, play_index=0):
    return _plays(name)[play_index].get("tasks") or []


def _task(name, task_name, play_index=0):
    for task in _tasks(name, play_index):
        if task.get("name") == task_name:
            return task
    raise AssertionError(f"{name}: no task named {task_name!r}")


def _uri_tasks(name):
    for index, play in enumerate(_plays(name)):
        for task in (play.get("tasks") or []):
            if "ansible.builtin.uri" in task:
                yield index, task


# ── the token is in every request, so every request is no_log ────────────────

def test_every_api_call_hides_the_token():
    for name in NEW_PLAYS:
        for _, task in _uri_tasks(name):
            headers = (task["ansible.builtin.uri"].get("headers") or {})
            if "X-API-Key" not in headers:
                continue
            assert task.get("no_log") is True, (
                f"{name}: task {task.get('name')!r} sends the API token without no_log")


def test_every_mutating_call_is_followed_by_a_read_back():
    """A no_log task's failure detail is censored, so 'it did not work' would arrive
    with no subject. Each play re-reads and asserts on what it finds."""
    for name in (TEAM_ENSURE, ENV_ACCESS, JIT_PREREQS):
        methods = [t["ansible.builtin.uri"].get("method") for _, t in _uri_tasks(name)]
        assert any(m in ("POST", "PUT") for m in methods), f"{name}: nothing mutates"
        last_write = max(i for i, m in enumerate(methods) if m in ("POST", "PUT"))
        assert "GET" in methods[last_write + 1:], (
            f"{name}: nothing is read back after the last write, so a censored "
            f"failure would be the whole report")
        asserts = [t for _, play in enumerate(_plays(name))
                   for t in (play.get("tasks") or [])
                   if "ansible.builtin.assert" in t]
        assert len(asserts) >= 2, f"{name}: the read-back is not asserted on"


# ── the access-policy map is merged, never replaced ──────────────────────────

def test_the_access_policy_is_read_before_it_is_written():
    """PUT /api/endpoints/{id} assigns the payload's map over the stored one."""
    for name in (ENV_ACCESS, JIT_PREREQS):
        seq = [(t["ansible.builtin.uri"].get("method"),
                t["ansible.builtin.uri"].get("url")) for _, t in _uri_tasks(name)]
        puts = [i for i, (m, u) in enumerate(seq) if m == "PUT"]
        assert puts, f"{name}: no PUT — nothing applies an access policy"
        gets_before = [i for i, (m, u) in enumerate(seq)
                       if m == "GET" and "endpoints" in (u or "") and i < puts[0]]
        assert gets_before, (
            f"{name}: the environment is written without being read first, so the "
            f"PUT would replace every other team's access")


def test_the_put_body_only_names_the_access_policy():
    """Every other field of the update payload is optional and an omitted one is left
    alone; naming more would let this play disturb an environment's URL or TLS."""
    for name in (ENV_ACCESS, JIT_PREREQS):
        for _, task in _uri_tasks(name):
            spec = task["ansible.builtin.uri"]
            if spec.get("method") != "PUT":
                continue
            assert list(spec.get("body") or {}) == ["TeamAccessPolicies"], (
                f"{name}: the PUT body carries more than the access policy: "
                f"{list(spec.get('body') or {})}")


def test_the_merge_keeps_every_other_team():
    """The finding, rendered: an existing policy survives a grant."""
    expr = _task(ENV_ACCESS, "Build the new access-policy map")
    template = expr["ansible.builtin.set_fact"]["new_policies"]
    current = {"7": {"RoleId": 0}}
    out = _render(template, {
        "_current": current, "state": "present",
        "team_row": {"Id": 12}, "team_role_id": 0,
    })
    assert out == {"7": {"RoleId": 0}, "12": {"RoleId": 0}}, out


def test_the_merge_can_take_access_away_without_touching_the_rest():
    expr = _task(ENV_ACCESS, "Build the new access-policy map")
    template = expr["ansible.builtin.set_fact"]["new_policies"]
    out = _render(template, {
        "_current": {"7": {"RoleId": 0}, "12": {"RoleId": 0}}, "state": "absent",
        "team_row": {"Id": 12}, "team_role_id": 0,
    })
    assert out == {"7": {"RoleId": 0}}, out


def test_the_team_id_is_a_string_key():
    """Portainer keys the map by team id as a STRING; an int key would add a second
    entry for the same team rather than updating it."""
    expr = _task(ENV_ACCESS, "Build the new access-policy map")
    out = _render(expr["ansible.builtin.set_fact"]["new_policies"], {
        "_current": {}, "state": "present", "team_row": {"Id": 3}, "team_role_id": 0,
    })
    assert list(out) == ["3"] and isinstance(list(out)[0], str), out


def test_the_bulk_grant_merges_per_environment():
    """jit-prereqs writes one PUT per environment, each merged onto ITS own map."""
    task = _task(JIT_PREREQS, "Grant the team access to each environment")
    template = task["ansible.builtin.uri"]["body"]["TeamAccessPolicies"]
    out = _render(template, {
        "item": {"Id": 1, "Name": "local", "TeamAccessPolicies": {"7": {"RoleId": 0}}},
        "team_row": {"Id": 12}, "team_role_id": 0,
    })
    assert out == {"7": {"RoleId": 0}, "12": {"RoleId": 0}}, out


# ── teams are matched the way Portainer and the dashboard match them ─────────

def test_missing_teams_is_case_insensitive():
    """Creating 'platform' beside an existing 'Platform' would publish two Entitle
    assets that look like one."""
    task = _task(TEAM_ENSURE, "Work out which teams are missing")
    template = task["ansible.builtin.set_fact"]["missing_teams"]
    have_lower = _render(task["vars"]["_have_lower"],
                         {"teams_before": {"json": [{"Name": "Platform"}]}})
    missing = []
    for item in ["platform", "DBAs"]:
        missing = _render(template, {"missing_teams": missing, "item": item,
                                     "_have_lower": have_lower})
    assert missing == ["DBAs"], missing


def test_the_create_only_runs_for_the_missing_ones():
    task = _task(TEAM_ENSURE, "Create the missing teams")
    assert task.get("loop") == "{{ missing_teams }}", task.get("loop")
    assert task["ansible.builtin.uri"]["body"] == {"Name": "{{ item }}"}


def test_the_singleton_case_is_not_forgotten():
    """team_name and team_names both feed one list; a play that read only the plural
    would silently do nothing for the single-team call the docs lead with."""
    template = _task(TEAM_ENSURE, "Collect the wanted team names")
    expr = template["ansible.builtin.set_fact"]["wanted_teams"]
    assert _render(expr, {"team_names": [], "team_name": "Platform"}) == ["Platform"]
    assert _render(expr, {"team_names": ["A"], "team_name": ""}) == ["A"]
    assert _render(expr, {"team_names": ["A"], "team_name": "B"}) == ["A", "B"]


# ── the Edge environment ─────────────────────────────────────────────────────

def test_the_environment_create_is_multipart():
    """Portainer's handler reads every field with RetrieveMultiPartFormValue, so a
    JSON body arrives as a missing-name validation error."""
    spec = _task(EDGE_ENSURE, "Create the Edge environment")["ansible.builtin.uri"]
    assert spec.get("body_format") == "form-multipart", spec.get("body_format")
    body = spec["body"]
    assert body["EndpointCreationType"] == "4", "4 is the Edge-agent creation type"
    # Not TagIDs (the Go struct's spelling), and not "" — it is parsed as JSON.
    assert body["TagIds"] == "[]", body.get("TagIds")


def test_the_agent_identity_is_deterministic():
    """Portainer only assigns an EdgeID under EnforceEdgeID. Left blank, every agent
    registers as a duplicate of the others; regenerated per run, a re-run adds a
    second environment for one host."""
    expr = _task(EDGE_ENSURE, "Settle the agent identity")["ansible.builtin.set_fact"]
    assert "to_uuid" in expr["_edge_id"], expr["_edge_id"]
    assert "EdgeID" in expr["_edge_id"], "an id Portainer already assigned must win"


def test_the_key_is_checked_before_the_host_is_touched():
    """An environment with no Edge key cannot be joined; the join command would fail
    on the host with a message about the agent, not about this."""
    names = [t.get("name") for t in _tasks(EDGE_ENSURE, 0)]
    assert "Confirm there is an Edge key to join with" in names
    assert names.index("Confirm there is an Edge key to join with") < len(names)


def test_the_agent_is_started_by_shelling_out():
    """The runner image for an SSH target ships no Docker collection at all."""
    text = _text(EDGE_ENSURE)
    assert "community.docker" not in text, (
        "community.docker is not in the runner image for a VM target")
    task = _task(EDGE_ENSURE, "Start the Edge agent", play_index=1)
    argv = task["ansible.builtin.command"]["argv"]
    assert argv[0] == "docker" and "run" in argv
    assert task.get("no_log") is True, "the Edge key is a credential for the environment"


def test_the_agent_polls_insecurely_by_default():
    """The managed node serves a self-signed certificate on 9443. Without this the
    agent's first poll fails verification and the environment never comes up, with no
    error anywhere an operator would look."""
    task = _task(EDGE_ENSURE, "Start the Edge agent", play_index=1)
    argv = " ".join(task["ansible.builtin.command"]["argv"])
    assert "EDGE_INSECURE_POLL" in argv
    play_vars = _plays(EDGE_ENSURE)[1].get("vars") or {}
    assert play_vars.get("insecure_poll") is True


def test_the_host_play_checks_docker_before_it_runs_anything():
    names = [t.get("name") for t in _tasks(EDGE_ENSURE, 1)]
    assert names.index("Confirm Docker is usable") < names.index("Start the Edge agent")
    probe = _task(EDGE_ENSURE, "Check Docker is usable", play_index=1)
    assert probe.get("changed_when") is False and probe.get("failed_when") is False


# ── each play says which target family to run it against ─────────────────────

def test_each_play_names_its_target():
    """The run form's target picker decides which runner and which connection the play
    gets; 'Portainer' and 'a VM' are different runs, and picking the wrong one fails
    in a way that reads as a broken playbook."""
    for name in (TEAM_ENSURE, ENV_ACCESS, JIT_PREREQS):
        assert "# Target: Portainer" in _text(name), f"{name}: no Portainer target line"
    assert "# Target: a VM" in _text(EDGE_ENSURE), (
        "the Edge play installs an agent ON a host, so it is a VM run")


def test_the_localhost_plays_are_localhost_plays():
    for name in (TEAM_ENSURE, ENV_ACCESS, JIT_PREREQS):
        play = _plays(name)[0]
        assert play["hosts"] == "localhost" and play["connection"] == "local", name


# ── a tiny Ansible-flavoured Jinja, so the expressions above can be rendered ──

def _render(template, variables):
    """Render one playbook expression with the filters Ansible adds to Jinja."""
    import jinja2

    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.filters["combine"] = lambda base, other, **_: {**(base or {}), **(other or {})}
    env.filters["dict2items"] = lambda d, key_name="key", value_name="value": [
        {key_name: k, value_name: v} for k, v in (d or {}).items()]
    env.filters["items2dict"] = lambda items, key_name="key", value_name="value": {
        i[key_name]: i[value_name] for i in items}
    env.filters["difference"] = lambda a, b: [x for x in a if x not in b]
    env.filters["to_uuid"] = lambda s: "uuid-of-" + str(s)
    env.filters["bool"] = lambda v: str(v).strip().lower() in ("1", "true", "yes", "on")
    out = env.from_string(template).render(**variables)
    # Ansible turns a rendered structure back into Python; ast.literal_eval is the
    # honest stand-in, and a plain string simply stays one.
    import ast
    try:
        return ast.literal_eval(out.strip())
    except (ValueError, SyntaxError):
        return out.strip()


if __name__ == "__main__":
    failures = 0
    for key, fn in sorted(globals().items()):
        if key.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {key}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {key}: {exc}")
    sys.exit(1 if failures else 0)
