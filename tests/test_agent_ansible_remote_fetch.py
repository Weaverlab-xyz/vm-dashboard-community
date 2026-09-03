"""Large assets travel to the target, not through the bundle.

An ``agent_ansible`` run carries its asset inside a signed job envelope capped at 256 KB,
so an installer cannot ride it — a 314 MB Resource Broker bootstrapper is three orders of
magnitude over. The fix is not a bigger envelope: the asset stays in object storage and the
generated play downloads it **on the target**, so the bytes never touch the dashboard, the
agent, or the envelope.

The properties worth pinning, because each was a real hazard while building it:

  * **The size decision is made from the LISTING, before anything is fetched.** The question
    is "is this too big to move through the dashboard"; answering it by moving the file
    through the dashboard would be the bug. A test that only checked the outcome would pass
    against an implementation that read 314 MB into memory first.
  * **The generated YAML parses.** A Windows destination puts a backslash in front of the
    filename, and inside a double-quoted YAML scalar ``\\B`` is an unknown escape that fails
    the whole play before Ansible sees it. Round-tripping the YAML is what catches that;
    eyeballing it does not.
  * **The URL is a secret.** It is a bearer token for the object until it expires, so it
    must reach the scrub list and must never be rendered into the playbook text.
  * **The refusals name the remedy.** "Too big" alone sends an operator looking for a
    setting to raise, and for a filesystem backend there is none.

Runs under pytest, or standalone:
    python tests/test_agent_ansible_remote_fetch.py
"""
import asyncio
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-remote-fetch-tests")

try:
    import yaml
    from web_dashboard.services import (agent_ansible_bundle as bundle,
                                        ansible_local_service as als,
                                        storage_service)
except Exception as exc:  # pragma: no cover — app deps missing
    try:
        import pytest
        pytest.skip(f"app dependencies unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

BOOTSTRAPPER = "BeyondTrust.Agents.Bootstrapper.exe"
BOOTSTRAPPER_BYTES = 329_665_864          # the real one, 314 MB
SIGNED = "https://bucket.s3.amazonaws.com/config-mgmt/x.exe?X-Amz-Signature=deadbeef"


class _Stub:
    """Swaps the storage_service entry points the chooser calls, and restores them."""

    def __init__(self, *, size, presign_backends=("s3", "azure_blob", "gcs"),
                 active="s3", presign_raises=None):
        self.size, self.active = size, active
        self.presign_backends, self.presign_raises = presign_backends, presign_raises
        self.fetched = []          # anything that read actual BYTES lands here
        self.sized = []

    def __enter__(self):
        self._saved = {k: getattr(storage_service, k) for k in
                       ("asset_size", "can_presign", "presigned_url", "active_backend",
                        "fetch_asset_in", "fetch_asset_b64")}

        async def asset_size(backend, name):
            self.sized.append((backend, name))
            return self.size

        async def presigned_url(backend, name, ttl=3600):
            if self.presign_raises:
                raise storage_service.StorageError(self.presign_raises)
            return SIGNED

        async def fetch_asset_in(backend, name):
            self.fetched.append(name)
            return b"x" * max(self.size, 0)

        async def fetch_asset_b64(name):
            self.fetched.append(name)
            import base64
            return base64.b64encode(b"x" * max(self.size, 0)).decode()

        storage_service.asset_size = asset_size
        storage_service.presigned_url = presigned_url
        storage_service.can_presign = lambda b: b in self.presign_backends
        storage_service.active_backend = lambda: self.active
        storage_service.fetch_asset_in = fetch_asset_in
        storage_service.fetch_asset_b64 = fetch_asset_b64
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            setattr(storage_service, k, v)


def _url(asset, backend="s3", *, prefetched=""):
    return asyncio.run(bundle._remote_fetch_url(asset, backend, prefetched_b64=prefetched))


# ── the decision ──────────────────────────────────────────────────────────────

def test_a_large_installer_is_left_in_storage_and_signed_for():
    with _Stub(size=BOOTSTRAPPER_BYTES) as st:
        assert _url(BOOTSTRAPPER) == SIGNED
        assert st.fetched == [], (
            "the chooser read the asset's BYTES to decide whether it was too big to read")


def test_the_size_comes_from_the_listing_not_from_a_download():
    """The ordering is the property, not just the answer."""
    with _Stub(size=BOOTSTRAPPER_BYTES) as st:
        _url(BOOTSTRAPPER)
        assert st.sized == [("s3", BOOTSTRAPPER)]
        assert st.fetched == []


def test_a_small_asset_still_travels_inside_the_bundle():
    with _Stub(size=4096) as st:
        assert _url("site.yml") == ""
        assert st.fetched == []          # the chooser declines; build() fetches later


def test_an_asset_at_the_threshold_is_still_embedded():
    with _Stub(size=bundle.MAX_EMBEDDED_ASSET_BYTES):
        assert _url(BOOTSTRAPPER) == ""
    with _Stub(size=bundle.MAX_EMBEDDED_ASSET_BYTES + 1):
        assert _url(BOOTSTRAPPER) == SIGNED


def test_an_agent_prefetched_asset_is_never_reconsidered():
    """The prefetch already happened at enqueue, bounded by that backend's own ceiling."""
    with _Stub(size=BOOTSTRAPPER_BYTES) as st:
        assert _url(BOOTSTRAPPER, "agent_local", prefetched="eA==") == ""
        assert st.sized == []


def test_a_missing_asset_falls_through_rather_than_guessing():
    """-1 means the listing did not have it; the embedding path reports the real error."""
    with _Stub(size=-1):
        assert _url(BOOTSTRAPPER) == ""


def test_a_listing_failure_falls_through_rather_than_refusing_the_run():
    with _Stub(size=0) as st:
        async def boom(backend, name):
            raise storage_service.StorageError("listing hiccup")
        storage_service.asset_size = boom
        assert _url(BOOTSTRAPPER) == ""


# ── the refusals, and what they say ───────────────────────────────────────────

def test_a_filesystem_backend_is_refused_by_naming_what_to_do():
    with _Stub(size=BOOTSTRAPPER_BYTES, presign_backends=()):
        try:
            _url(BOOTSTRAPPER, "agent_local")
            raise AssertionError("accepted an asset it cannot deliver")
        except bundle.BundleError as e:
            msg = str(e)
    assert "314.4 MB" in msg, msg
    assert "cloud backend" in msg, msg
    assert "agent_local" in msg, msg


def test_a_large_asset_of_an_undeliverable_type_names_the_type():
    """A .sh wants a file on the CONTROLLER, so "download it on the far end" is not a shape
    it has — say that, rather than offering a link that would not help."""
    with _Stub(size=BOOTSTRAPPER_BYTES):
        try:
            _url("enormous.sh")
            raise AssertionError("accepted a script it cannot wrap")
        except bundle.BundleError as e:
            msg = str(e)
    assert "script" in msg and "download link" in msg, msg


def test_a_signing_failure_surfaces_as_a_bundle_error():
    with _Stub(size=BOOTSTRAPPER_BYTES, presign_raises="no permission to sign"):
        try:
            _url(BOOTSTRAPPER)
            raise AssertionError("swallowed a signing failure")
        except bundle.BundleError as e:
            assert "no permission to sign" in str(e)


# ── the generated play ────────────────────────────────────────────────────────

def test_every_remote_fetch_play_is_valid_yaml():
    """The Windows path's backslash is an invalid escape in a double-quoted scalar, which
    fails the whole play at parse time. Round-trip rather than eyeball."""
    for asset in (BOOTSTRAPPER, "x.msi", "agent.rpm", "agent.deb"):
        plays = yaml.safe_load(als.generate_remote_fetch_playbook_yaml(asset))
        assert isinstance(plays, list) and plays[0]["tasks"], asset


def test_the_windows_destination_keeps_a_literal_backslash():
    plays = yaml.safe_load(als.generate_remote_fetch_playbook_yaml(BOOTSTRAPPER))
    assert plays[0]["vars"]["_asset_dest"] == \
        "{{ ansible_env.TEMP }}" + chr(92) + BOOTSTRAPPER


def test_the_play_names_a_variable_and_never_a_url():
    """A URL rendered into the play text would be echoed by anything that prints the play,
    and could not be scrubbed by value."""
    for asset in (BOOTSTRAPPER, "agent.rpm", "agent.deb"):
        text = als.generate_remote_fetch_playbook_yaml(asset)
        assert als.ASSET_URL_VAR in text
        assert "http://" not in text and "https://" not in text


def test_the_download_task_is_no_log():
    for asset in (BOOTSTRAPPER, "agent.rpm", "agent.deb"):
        plays = yaml.safe_load(als.generate_remote_fetch_playbook_yaml(asset))
        download = plays[0]["tasks"][0]
        assert download.get("no_log") is True, asset


def test_the_downloaded_file_is_removed_afterwards():
    """It is large and the target did not ask to keep it."""
    for asset in (BOOTSTRAPPER, "agent.rpm", "agent.deb"):
        names = [t["name"] for t in
                 yaml.safe_load(als.generate_remote_fetch_playbook_yaml(asset))[0]["tasks"]]
        assert any(n.startswith("Remove the downloaded") for n in names), asset


def test_installer_arguments_survive_the_switch_to_downloading():
    """A silent RB install needs INSTALLKEY and ZONE; losing them would turn a working run
    into one that hangs on an interactive prompt."""
    plays = yaml.safe_load(als.generate_remote_fetch_playbook_yaml(BOOTSTRAPPER))
    install = [t for t in plays[0]["tasks"] if "ansible.windows.win_package" in t][0]
    assert als.WINPKG_ARGS_VAR in install["ansible.windows.win_package"]["arguments"]


def test_the_windows_play_still_tolerates_a_reboot_required_exit_code():
    """win_package rather than win_command, so 3010 is not read as a failure."""
    plays = yaml.safe_load(als.generate_remote_fetch_playbook_yaml(BOOTSTRAPPER))
    assert any("ansible.windows.win_package" in t for t in plays[0]["tasks"])


def test_a_script_or_playbook_is_refused_by_the_generator_too():
    for asset in ("site.yml", "run.sh", "x.ps1"):
        try:
            als.generate_remote_fetch_playbook_yaml(asset)
            raise AssertionError(f"generated a download wrapper for {asset}")
        except ValueError:
            pass


def test_can_remote_fetch_agrees_with_the_generator():
    """Two readers of "is this deliverable by link" that disagree would produce a run
    refused by one and accepted by the other."""
    for asset in ("a.exe", "a.msi", "a.rpm", "a.deb", "a.sh", "a.ps1", "a.yml"):
        expected = als.can_remote_fetch(asset)
        try:
            als.generate_remote_fetch_playbook_yaml(asset)
            actual = True
        except ValueError:
            actual = False
        assert expected == actual, asset


# ── the ceiling itself ────────────────────────────────────────────────────────

def test_the_embed_ceiling_leaves_room_for_the_rest_of_the_bundle():
    """The asset is one tenant of the 256 KB: base64 costs it a third again, and the
    playbook, extra vars and connection material share the same budget."""
    assert bundle.MAX_EMBEDDED_ASSET_BYTES * 4 // 3 < bundle.MAX_BUNDLE_BYTES


def test_only_object_stores_can_sign():
    """Inherent, not an omission: a filesystem path has no signing authority and nothing
    serving it over HTTP."""
    assert storage_service.can_presign("s3")
    assert not storage_service.can_presign("local")
    assert not storage_service.can_presign("agent_local")


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
