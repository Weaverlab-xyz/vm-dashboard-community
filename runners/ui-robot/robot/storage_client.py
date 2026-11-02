"""Upload a file through the dashboard's active storage backend.

The robot never talks to S3 (or Azure, GCS, OCI) itself: it uses the chunked-upload API
documented in docs/operations/storage-management/uploading.md, so whichever backend is
active receives the file and the container needs no cloud credentials.

Auth is a dashboard personal access token, sent as a bearer header. The dashboard sets no
auth cookie, so an unauthenticated request is anonymous and will 401.
"""
import os

import requests

MAX_RETRIES = 3


class StorageUploadError(RuntimeError):
    pass


class DashboardStorage:
    def __init__(self, base_url: str, token: str, verify: bool | str = True):
        self.base = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"
        self.session.verify = verify

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
        for _ in range(MAX_RETRIES):
            resp = self.session.put(f"{self.base}/api/storage/upload/part/{n}",
                                    data=chunk, headers=headers)
            if resp.ok:
                body = resp.json()
                # secret_findings is advisory (first part only); surface it, don't fail.
                if body.get("secret_findings"):
                    print(f"warning: secret scan flagged part {n}")
                return {"part": body["part"], "ref": body.get("ref", "")}
            if resp.status_code < 500:
                break
            last = resp.text[:300]
        raise StorageUploadError(f"stage part {n}: HTTP {resp.status_code}: {last or resp.text[:300]}")
