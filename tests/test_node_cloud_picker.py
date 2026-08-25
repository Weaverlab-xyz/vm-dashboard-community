"""The managed-node cloud picker, and the placement it implies.

The Portainer server and the Rancher management node were GCE-only: a container on a
COS VM, a GCE firewall rule as the gate, and ``gcp_*``-prefixed placement keys. Both
are now hosted on a cloud the operator picks, the way the Gateways tab already worked
— and the ways that can go wrong are worth pinning, because none of them announces
itself:

  * **A silent fallback to GCP.** If an unimplemented cloud resolved placement instead
    of raising, a node the operator asked for in AWS would be created in GCP while the
    form, the job and the row all said AWS. ``resolve_placement`` must refuse.
  * **A zone surviving a cloud switch.** A zone is a GCP concept in this shape (on AWS
    the subnet pins the AZ; Azure has none), so the deploy route rejects one on any
    other cloud rather than accepting a field that cannot take effect.
  * **A region surviving a cloud switch.** Region names do not cross clouds, so the
    form has to repoint its region list the instant the cloud changes — leaving the
    previous cloud's regions selectable is how you deploy into a region that does not
    exist.
  * **The two features drifting apart.** They share one implementation, so their config
    keys are derived from one rule; asserting the derivation is what stops one of them
    growing a private spelling.
  * **The picker disagreeing with Gateways.** Both offer "which cloud hosts this
    managed container", so they list the same clouds from one definition.

No DB or cloud SDK is needed — config reads are replaced with a dict-backed spy.

Run: python tests/test_node_cloud_picker.py   (or under pytest)
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

try:
    from web_dashboard.models.containers import (PortainerDeployRequest,
                                                 RancherDeployRequest)
    from web_dashboard.services import gateway_service
    from web_dashboard.services import managed_node_service as mns
except Exception as exc:  # pragma: no cover — deps absent outside CI
    try:
        import pytest
        pytest.skip(f"service import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

_SPECS = (mns.RANCHER, mns.PORTAINER)


class _Cfg:
    """Dict-backed stand-in for config_service, installed for one test."""

    def __init__(self, values):
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, "")

    def get_bool(self, key, default=False):
        v = self.values.get(key)
        if v in (None, ""):
            return default
        return str(v).lower() in ("1", "true", "yes", "on")

    def set(self, key, value):
        self.values[key] = value


class _patched_cfg:
    def __init__(self, **values):
        self.cfg = _Cfg(values)

    def __enter__(self):
        self.saved = (mns.config_service.get, mns.config_service.get_bool,
                      mns.config_service.set)
        mns.config_service.get = self.cfg.get
        mns.config_service.get_bool = self.cfg.get_bool
        mns.config_service.set = self.cfg.set
        return self.cfg

    def __exit__(self, *exc):
        (mns.config_service.get, mns.config_service.get_bool,
         mns.config_service.set) = self.saved
        return False


def _template() -> str:
    path = os.path.join(_ROOT, "web_dashboard", "templates", "containers", "index.html")
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _api_src() -> str:
    path = os.path.join(_ROOT, "web_dashboard", "api", "containers.py")
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# ── the cloud list ───────────────────────────────────────────────────────────

def test_the_node_picker_offers_the_same_clouds_as_the_gateway_picker():
    """Both answer "which cloud hosts this managed container", on the same page. Two
    lists would eventually disagree, and the one that is short is the one that silently
    stops offering a cloud the backend supports."""
    assert tuple(mns.CLOUDS) == tuple(gateway_service.CLOUDS), (
        f"managed_node_service.CLOUDS {mns.CLOUDS} != gateway_service.CLOUDS "
        f"{gateway_service.CLOUDS}")


def test_both_deploy_requests_accept_a_cloud():
    for model in (RancherDeployRequest, PortainerDeployRequest):
        assert "cloud" in model.model_fields, (
            f"{model.__name__} has no cloud field, so the picker's choice is dropped "
            f"before it reaches the job")
        # Blank means "where it already is", never a hard-coded default.
        assert model().cloud is None, (
            f"{model.__name__}.cloud defaults to a cloud rather than 'unchanged'")


# ── placement must refuse, not fall back ─────────────────────────────────────

def test_an_unimplemented_cloud_refuses_placement():
    """The dangerous failure: a node asked for in one cloud created in another. Every
    per-cloud entry point raises rather than resolving something GCP-shaped."""
    for spec in _SPECS:
        for cloud in ("aws", "azure"):
            try:
                with _patched_cfg(gcp_project_id="p"):
                    mns.resolve_placement(cloud, spec)
            except mns.ManagedNodeError as exc:
                assert cloud in str(exc), f"the error does not name the cloud: {exc}"
            else:
                raise AssertionError(
                    f"resolve_placement({cloud!r}, {spec.feature}) returned a placement "
                    f"instead of refusing — a node would be built in the wrong cloud")


def test_an_unknown_cloud_refuses_placement():
    for spec in _SPECS:
        try:
            with _patched_cfg(gcp_project_id="p"):
                mns.resolve_placement("digitalocean", spec)
        except mns.ManagedNodeError:
            pass
        else:
            raise AssertionError("an unknown cloud resolved a placement")


def test_gcp_placement_carries_the_cloud_neutral_fields():
    """Callers report and tear down a node without knowing its cloud, so every
    placement carries cloud / account / name / the ingress-rule name."""
    for spec in _SPECS:
        with _patched_cfg(gcp_project_id="proj-1", gcp_zone="us-central1-a"):
            p = mns.resolve_placement("gcp", spec)
        for key in ("cloud", "account", "region", "name", "firewall_name"):
            assert key in p, f"{spec.feature} gcp placement has no {key!r}"
        assert p["cloud"] == "gcp"
        assert p["account"] == "proj-1", p["account"]
        # account is the cloud-neutral alias; project_id stays for the GCP callers.
        assert p["project_id"] == "proj-1"
        assert p["firewall_name"].endswith("-allow-mgmt"), p["firewall_name"]


# ── the persisted cloud ──────────────────────────────────────────────────────

def test_the_node_cloud_defaults_to_gcp_and_round_trips():
    """Every node deployed before this key existed is a GCE VM, so an unset value has
    to mean gcp — anything else sends an existing install looking for its node in a
    cloud it was never deployed to."""
    for spec in _SPECS:
        with _patched_cfg() as cfg:
            assert mns.node_cloud(spec) == "gcp"
            mns.set_node_cloud(spec, "aws")
            assert cfg.values[spec.node_cloud_key] == "aws"
            assert mns.node_cloud(spec) == "aws"


def test_a_junk_persisted_cloud_reads_as_gcp_rather_than_crashing():
    for spec in _SPECS:
        with _patched_cfg(**{spec.node_cloud_key: "not-a-cloud"}):
            assert mns.node_cloud(spec) == "gcp"


def test_set_node_cloud_refuses_a_cloud_that_is_not_one():
    """A bad write here would make the node unreachable by every later read."""
    for spec in _SPECS:
        with _patched_cfg() as cfg:
            mns.set_node_cloud(spec, "nope")
            assert spec.node_cloud_key not in cfg.values, cfg.values


# ── key derivation, so the two features cannot drift ─────────────────────────

def test_the_config_keys_are_derived_from_one_rule():
    """Cloud-neutral behaviour keys are ``<feature>_<knob>``; per-cloud placement keys
    are ``<cloud>_<feature>_<knob>``. These are the names already in config.py, so the
    derivation is also the back-compat contract."""
    r, p = mns.RANCHER, mns.PORTAINER
    assert r.manual_cidrs_key == "rancher_allowed_source_cidrs"
    assert p.manual_cidrs_key == "portainer_allowed_source_cidrs"
    assert r.dashboard_cidr_key == "rancher_dashboard_egress_cidr"
    assert p.dashboard_cidr_key == "portainer_dashboard_egress_cidr"
    assert r.node_cloud_key == "rancher_node_cloud"
    assert p.node_cloud_key == "portainer_node_cloud"
    assert r.allow_open_key("gcp") == "gcp_rancher_allow_open"
    assert p.allow_open_key("gcp") == "gcp_portainer_allow_open"
    assert r.infra_key("gcp", "zone") == "gcp_rancher_zone"
    assert p.infra_key("gcp", "data_disk_gb") == "gcp_portainer_data_disk_gb"
    # And the new clouds follow the same rule with no extra plumbing.
    assert r.infra_key("aws", "instance_type") == "aws_rancher_instance_type"
    assert p.allow_open_key("azure") == "azure_portainer_allow_open"


def test_every_declared_cloud_has_an_allow_open_key_per_feature():
    """Fail-closed is per cloud, so a missing allow_open key would make one cloud's
    node impossible to open deliberately."""
    for spec in _SPECS:
        keys = {spec.allow_open_key(c) for c in mns.CLOUDS}
        assert len(keys) == len(mns.CLOUDS), f"{spec.feature} allow_open keys collide: {keys}"


def test_the_ports_differ_per_feature_and_are_not_empty():
    assert set(mns.RANCHER.ports) == {"80", "443"}, mns.RANCHER.ports
    assert set(mns.PORTAINER.ports) == {"9443", "8000"}, mns.PORTAINER.ports


# ── the API refuses what the form cannot express ─────────────────────────────

def test_a_zone_is_refused_on_a_cloud_that_has_none():
    """A zone only means something on GCP. Accepting one elsewhere would report a
    placement the deploy never used."""
    src = _api_src()
    assert src.count('A zone can only be chosen on GCP') == 2, (
        "one of the two node deploy routes accepts a zone on a cloud with no zones")


def test_the_deploy_routes_validate_the_region_against_the_chosen_cloud():
    """`region_catalog.validate("gcp", …)` on an AWS deploy would reject every valid
    AWS region and accept nothing."""
    src = _api_src()
    assert "region_catalog.validate(cloud, req.region)" in src, (
        "a node deploy route still validates the region against a hard-coded cloud")
    assert "region_catalog.validate(\"gcp\", req.region)" not in src, (
        "a node deploy route still hard-codes gcp for region validation")


def test_the_read_routes_survive_a_cloud_that_cannot_resolve():
    """The Containers tab has to render the setup card on a bare install. A placement
    that raises must not become a 500 on every request — that failure mode has shipped
    in this repo before."""
    src = _api_src()
    assert "def _safe_placement(" in src, (
        "the read routes resolve placement without a guard, so an unusable cloud 500s")


# ── the form ─────────────────────────────────────────────────────────────────

def test_both_deploy_forms_have_a_cloud_picker():
    src = _template()
    for model in ("deployForm", "portainerDeployForm"):
        assert f'x-model="{model}.cloud"' in src, (
            f"{model} has no cloud picker, so the node can only ever be redeployed "
            f"where it already is")


def test_the_zone_field_is_hidden_off_gcp_in_both_forms():
    src = _template()
    for model in ("deployForm", "portainerDeployForm"):
        assert f"""x-show="{model}.cloud === 'gcp'\"""" in src, (
            f"{model}'s zone field renders on clouds that have no zones")


def test_switching_cloud_clears_the_region_and_zone_in_both_forms():
    """Region names do not cross clouds and a zone is GCP-only, so carrying either
    over is a 400 the operator never asked for."""
    src = _template()
    for handler in ("rancherCloudChanged", "portainerCloudChanged"):
        assert f"{handler}()" in src, f"{handler} is not wired to the cloud select"
        body = src[src.index(f"{handler}() {{"):]
        body = body[:body.index("},")]
        assert ".region = ''" in body, f"{handler} keeps the previous cloud's region"
        assert ".zone = ''" in body, f"{handler} keeps the previous cloud's zone"


def test_both_node_tables_show_which_cloud_the_node_is_in():
    src = _template()
    assert src.count("""x-text="n.cloud || 'gcp'\"""") == 2, (
        "a node table does not show its cloud, so a relocated node is indistinguishable "
        "from one that never moved")


def test_the_teardown_calls_carry_the_nodes_cloud():
    """Teardown has to look in the cloud the node is actually in. Without this the
    stop button reaches for the persisted cloud, which a just-relocated node has
    already left."""
    src = _template()
    assert src.count("cloud=${encodeURIComponent(n.cloud || '')}") == 2, (
        "a node stop call does not pass the node's cloud")


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
