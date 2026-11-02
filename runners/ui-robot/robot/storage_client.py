"""Upload a file through the dashboard's active storage backend.

The robot never talks to S3 (or Azure, GCS, OCI) itself: it uses the chunked-upload API
documented in docs/operations/storage-management/uploading.md, so whichever backend is
active receives the file and the container needs no cloud credentials.

Auth is a service account's OAuth access token, sent as a bearer header. A service
account cannot use a personal access token (the dashboard refuses it at use), so the token
comes from POST /api/oauth/token with grant_type=client_credentials, authenticated either
by a SPIFFE JWT-SVID (the robot then stores no secret) or by a client id and secret. The
dashboard sets no auth cookie, so an unauthenticated request is anonymous and will 401.

This client is the standalone path. On the POV control node the design has the agent
upload instead, with its own signed identity: docs/profiles/pov/design/ui-robot.md.
"""
import os

import requests

MAX_RETRIES = 3


class StorageUploadError(RuntimeError):
    pass


class DashboardStorage:
    def __init__(self, base_url: str, token_factory, verify: bool | str = True):
        """`token_factory()` returns a fresh access token. It is called again after a 401,
        because a token outlives neither a slow 300 MB upload nor its own TTL."""
        self.base = base_url.rstrip("/")
        self._token_factory = token_factory
        self.session = requests.Session()
        self.session.verify = verify
        self._refresh_token()

    def _refresh_token(self) -> None:
        self.session.headers["Authorization"] = f"Bearer {self._token_factory()}"

    @staticmethod
    def _exchange(base_url: str, data: dict, auth, verify, scope: str) -> str:
        if scope:
            data = {**data, "scope": scope}
        resp = requests.post(f"{base_url.rstrip('/')}/api/oauth/token", data=data,
                             auth=auth, verify=verify)
        if not resp.ok:
            raise StorageUploadError(f"OAuth token: HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json()["access_token"]

    @classmethod
    def from_client_credentials(cls, base_url: str, client_id: str, client_secret: str,
                                verify: bool | str = True, scope: str = "") -> "DashboardStorage":
        return cls(base_url, lambda: cls._exchange(
            base_url, {"grant_type": "client_credentials"}, (client_id, client_secret),
            verify, scope), verify)

    @classmethod
    def from_spiffe_svid(cls, base_url: str, svid_file: str, client_id: str = "",
                         verify: bool | str = True, scope: str = "") -> "DashboardStorage":
        """Authenticate with a JWT-SVID read from `svid_file` (spiffe-helper keeps it
        current). The audience must be this dashboard's /api/oauth/token. The dashboard
        accepts each assertion once, so the file is re-read on every exchange."""
        def factory() -> str:
            with open(svid_file, encoding="utf-8") as fh:
                svid = fh.read().strip()
            if not svid:
                raise StorageUploadError(f"{svid_file} is empty; is spiffe-helper running?")
            data = {"grant_type": "client_credentials", "client_assertion": svid,
                    "client_assertion_type":
                        "urn:ietf:params:oauth:client-assertion-type:jwt-spiffe"}
            if client_id:
                data["client_id"] = client_id
            return cls._exchange(base_url, data, None, verify, scope)
        return cls(base_url, factory, verify)

    def _check(self, resp: requests.Response, what: str) -> dict:
        if not resp.ok:
            raise StorageUploadError(f"{what}: HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    def active_backend(self) -> dict:
        data = self._check(self.session.get(f"{self.base}/api/storage/backends"),
                           "list backends")
        for b in data["backends"]:
            if b["active"]:
                return b
        raise StorageUploadError("No active storage backend is configured on the dashboard.")

    def upload(self, path: str) -> str:
        """Chunked upload of `path`; returns the asset name. Aborts the upload on failure."""
        size = os.path.getsize(path)
        name = os.path.basename(path)
        backend = self.active_backend()
        if backend["via_agent"] or backend["id"] in ("local", "agent_local"):
            raise StorageUploadError(
                f"Active backend '{backend['id']}' is a filesystem backend with no chunked "
                f"lane; a {size} byte installer cannot be uploaded to it.")
        if size > backend["max_upload_bytes"]:
            raise StorageUploadError(
                f"{name} is {size} bytes; backend '{backend['id']}' accepts at most "
                f"{backend['max_upload_bytes']}.")

        begun = self._check(
            self.session.post(f"{self.base}/api/storage/upload/begin",
                              json={"filename": name, "size": size}),
            "begin upload")
        handle = begun["handle"]
        part_bytes = begun["part_bytes"]
        headers = {"X-Upload-Handle": handle, "Content-Type": "application/octet-stream"}
        try:
            parts = []
            with open(path, "rb") as fh:
                for n in range(1, begun["parts"] + 1):
                    chunk = fh.read(part_bytes)
                    parts.append(self._put_part(n, chunk, headers))
            self._check(
                self.session.post(f"{self.base}/api/storage/upload/commit",
                                  json={"handle": handle, "parts": parts}),
                "commit upload")
        except Exception:
            # Abort never fails server-side, but don't mask the original error if it does.
            try:
                self.session.post(f"{self.base}/api/storage/upload/abort",
                                  json={"handle": handle})
            except requests.RequestException:
                pass
            raise
        return name

    def _put_part(self, n: int, chunk: bytes, headers: dict) -> dict:
        last = ""
        refreshed = False
        for _ in range(MAX_RETRIES + 1):
            resp = self.session.put(f"{self.base}/api/storage/upload/part/{n}",
                                    data=chunk, headers=headers)
            if resp.ok:
                body = resp.json()
                # secret_findings is advisory (first part only); surface it, don't fail.
                if body.get("secret_findings"):
                    print(f"warning: secret scan flagged part {n}")
                return {"part": body["part"], "ref": body.get("ref", "")}
            if resp.status_code == 401 and not refreshed:
                refreshed = True
                self._refresh_token()
                headers = {**headers}
                continue
            if resp.status_code < 500:
                break
            last = resp.text[:300]
        raise StorageUploadError(f"stage part {n}: HTTP {resp.status_code}: {last or resp.text[:300]}")
