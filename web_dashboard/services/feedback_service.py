"""In-product feedback: composing a report, and getting it to GitHub.

Two modes, and the mode is **derived from configuration, never configured**:

``direct``
    A repo and a token are both set. The server files the issue itself with
    ``POST /repos/{repo}/issues`` and hands the number straight back, so the submitter
    knows it landed before they close the dialog.

``compose``
    A repo is set and the token is blank. The server composes the identical title and
    body and returns a ``github.com/<repo>/issues/new?title=&body=`` link. Nothing is
    filed server-side and no credential exists on the instance -- which is the only
    honest shape for the public release, where a self-hoster files the issue under
    their own GitHub account and GitHub's own abuse controls do the limiting.

``off``
    No repo. The feature cannot do anything, whatever the flag says.

What this module deliberately does NOT do
-----------------------------------------
**It does not emit notifications.** ``tests/test_notify_wiring.py`` holds, statically,
that no ``emit_safe`` call site imports ``httpx``/``requests``/``urllib`` -- the whole
point of that rule being that emitting is an INSERT and never a network call. This
module is an HTTP client by definition, so if an operator should be told about new
feedback, the emit belongs in the router, which does no network work of its own.

**It does not redact.** See :func:`find_credentials`.
"""
from __future__ import annotations

import logging
import re
import urllib.parse
from typing import Optional

import httpx

from . import config_service, http_ca, secret_scan

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
GITHUB_WEB = "https://github.com"

#: ``owner/name``. Validated before it reaches an API path or a redirect -- see
#: :func:`validate_repo`.
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}$")

#: What the compose-mode body may carry. The deep link is a GET: GitHub itself is
#: fairly tolerant, but browsers and intervening proxies get unreliable past roughly
#: 2000 characters of URL and some answer 414. Everything here is percent-encoded,
#: which inflates newlines and punctuation a long way, so the cap is on the pre-encoded
#: body and is deliberately conservative. The FULL body is kept in the row regardless.
COMPOSE_BODY_LIMIT = 1500

CATEGORIES = ("bug", "idea", "docs", "other")

MAX_TITLE = 200
MAX_BODY = 8000
MAX_PAGE_PATH = 500

#: Short, because this runs inside a request. A GitHub outage must not hold a
#: threadpool worker for the httpx default.
_TIMEOUT = httpx.Timeout(connect=3.0, read=8.0, write=8.0, pool=3.0)


class FeedbackError(Exception):
    """A submission could not be filed. The message is safe to show the submitter."""


# -- Configuration -----------------------------------------------------------

def validate_repo(slug: str) -> str:
    """Return ``slug`` if it is a plausible ``owner/name``, else raise.

    Not cosmetic. The slug is interpolated into an ``api.github.com`` path in direct
    mode and into a ``github.com`` URL handed to the browser in compose mode, so an
    unvalidated value is path traversal against the API and an open redirect in the UI.
    Checked on the way out as well as on the way in, because the stored value could
    predate the check.
    """
    slug = (slug or "").strip().strip("/")
    if not _REPO_RE.match(slug):
        raise FeedbackError("The feedback repository is not a valid 'owner/name'.")
    return slug


def repo() -> str:
    from ..config import settings
    return (config_service.get("feedback_github_repo")
            or getattr(settings, "feedback_github_repo", "") or "").strip().strip("/")


def _token() -> str:
    from ..config import settings
    return (config_service.get("feedback_github_token")
            or getattr(settings, "feedback_github_token", "") or "").strip()


def labels() -> list:
    from ..config import settings
    raw = (config_service.get("feedback_labels")
           or getattr(settings, "feedback_labels", "") or "")
    return [p.strip() for p in raw.split(",") if p.strip()]


def mode() -> str:
    """One of ``direct``, ``compose`` or ``off``.

    ``off`` whenever there is no usable repo, including a stored value that fails
    validation -- better the button is absent than present and broken.
    """
    slug = repo()
    if not slug or not _REPO_RE.match(slug):
        return "off"
    return "direct" if _token() else "compose"


def guest_submissions_enabled() -> bool:
    from ..config import settings
    return config_service.get_bool(
        "feedback_guest_submissions",
        getattr(settings, "feedback_guest_submissions", True))


# -- Credential detection ----------------------------------------------------

def find_credentials(*parts: str) -> list:
    """Findings from ``secret_scan`` across every given piece of text.

    **secret_scan.scan_text is a detector, not a redactor.** It returns
    ``[{rule, line, match}]`` with the *match* redacted and hands the input string back
    untouched. Wiring it in as "the redaction pass" would file the credential verbatim
    into an issue while reporting that it had been handled -- so it is used here only to
    decide whether to REFUSE.

    Refusing rather than stripping is deliberate. A submission silently emptied of
    content and filed anyway is a side effect the submitter never asked for and cannot
    see; a refusal names the rule and the line while their text is still on screen and
    lets them fix it themselves.

    The ruleset is a safety net, not a guarantee: it does not know about Azure client
    secrets, bearer JWTs or connection strings. That is the other reason the target
    repo's visibility is stated in the form rather than assumed.
    """
    findings = []
    for text in parts:
        if text:
            findings.extend(secret_scan.scan_text(text))
    return findings


# -- Composition -------------------------------------------------------------

def compose_title(row) -> str:
    prefix = {"bug": "Bug", "idea": "Idea", "docs": "Docs"}.get(row.category, "Feedback")
    return ("[" + prefix + "] " + row.title)[:MAX_TITLE + 16]


def compose_body(row) -> str:
    """The issue body: what they said, then where they said it from.

    The context block is what makes a report actionable, and every field in it except
    ``page_path`` was read server-side -- a client that could set its own version could
    misattribute a bug to a build that never had it.

    No email address and no token ever appears here. ``submitted_by`` is the username
    the instance knows; on a POV instance that is a prospect's ephemeral login, which
    is why the guest's POV is named instead of anything about the person.
    """
    lines = [row.body.strip(), "", "---", "", "| | |", "|---|---|"]
    ctx = [
        ("Category", row.category),
        ("Page", row.page_path or "-"),
        ("Version", row.app_version or "unknown"),
        ("Commit", (row.commit or "")[:12] or "unknown"),
        ("Profile", row.instance_profile or "-"),
        ("Submitted by", row.submitted_by + " (" + row.submitter_kind + ")"),
    ]
    if row.pov_env_id:
        ctx.append(("POV", row.pov_env_id))
    lines += ["| " + k + " | " + str(v) + " |" for k, v in ctx]
    return "\n".join(lines)


def compose_url(slug: str, title: str, body: str) -> tuple:
    """Return ``(url, truncated)`` for the prefilled new-issue form.

    The body is capped (:data:`COMPOSE_BODY_LIMIT`) because the link is a GET and a
    pasted traceback will otherwise push it past what a browser or proxy will carry --
    a 414, or worse a silently truncated issue, on the one path that exists for people
    with no other channel. The caller keeps the full text in the row and the UI says so.
    """
    slug = validate_repo(slug)
    truncated = len(body) > COMPOSE_BODY_LIMIT
    if truncated:
        body = (body[:COMPOSE_BODY_LIMIT].rstrip()
                + "\n\n_[truncated for the link - the full text was saved in the "
                  "dashboard and an operator can see it there]_")
    qs = urllib.parse.urlencode({"title": title, "body": body})
    return GITHUB_WEB + "/" + slug + "/issues/new?" + qs, truncated


# -- GitHub ------------------------------------------------------------------

def _headers(token: str) -> dict:
    return {
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "vm-dashboard-feedback",
    }


def file_issue(title: str, body: str, *, slug: Optional[str] = None,
               token: Optional[str] = None) -> dict:
    """File the issue and return ``{number, url}``. Raises :class:`FeedbackError`.

    Synchronous on purpose: every route that calls this is a plain ``def``, so
    Starlette runs it in the threadpool where a blocking call belongs. An ``async def``
    route doing this would put the whole thing on the event loop, which is the failure
    ``tests/test_sync_db_routes_off_the_event_loop`` exists to prevent.

    GitHub's own error body is logged but never returned: it can carry the repo name
    and the token's scopes, and the person reading the message may be a prospect.
    """
    slug = validate_repo(slug if slug is not None else repo())
    token = token if token is not None else _token()
    if not token:
        raise FeedbackError("No GitHub token is configured.")

    payload = {"title": title, "body": body}
    tags = labels()
    if tags:
        payload["labels"] = tags

    try:
        with httpx.Client(timeout=_TIMEOUT, verify=http_ca.verify(),
                          follow_redirects=False) as client:
            resp = client.post(GITHUB_API + "/repos/" + slug + "/issues",
                               json=payload, headers=_headers(token))
    except httpx.HTTPError as exc:
        logger.warning("feedback: GitHub unreachable: %s: %s", type(exc).__name__, exc)
        raise FeedbackError(
            "Could not reach GitHub. Your feedback was saved here.") from exc

    if resp.status_code in (401, 403):
        logger.error("feedback: GitHub refused the token (%s): %s",
                     resp.status_code, resp.text[:400])
        raise FeedbackError("The configured GitHub token was refused. "
                            "Your feedback was saved here.")
    if resp.status_code == 404:
        logger.error("feedback: repo %s not found or not visible to the token", slug)
        raise FeedbackError("The feedback repository could not be found. "
                            "Your feedback was saved here.")
    if resp.status_code >= 300:
        logger.error("feedback: GitHub returned %s: %s",
                     resp.status_code, resp.text[:400])
        raise FeedbackError("GitHub rejected the report. "
                            "Your feedback was saved here.")

    try:
        data = resp.json()
        return {"number": int(data["number"]), "url": str(data["html_url"])}
    except Exception as exc:  # noqa: BLE001 - a 201 with an unreadable body
        logger.error("feedback: could not read GitHub's response: %s", exc)
        raise FeedbackError("GitHub accepted the report but its reply "
                            "could not be read.") from exc


def repo_is_private(*, slug: Optional[str] = None,
                    token: Optional[str] = None) -> Optional[bool]:
    """Whether the target repo is private, or ``None`` if that cannot be determined.

    Shown in the form so a submitter knows, while they are typing, whether their words
    are about to become world-readable. ``None`` is rendered as "unknown" rather than
    guessed: claiming a public repo is private is the one wrong answer here.

    Unauthenticated when no token is set, which is the compose-mode case -- a private
    repo then simply 404s and the answer is ``None``.
    """
    try:
        slug = validate_repo(slug if slug is not None else repo())
    except FeedbackError:
        return None
    token = token if token is not None else _token()
    headers = _headers(token) if token else {
        "Accept": "application/vnd.github+json",
        "User-Agent": "vm-dashboard-feedback",
    }
    try:
        with httpx.Client(timeout=_TIMEOUT, verify=http_ca.verify(),
                          follow_redirects=False) as client:
            resp = client.get(GITHUB_API + "/repos/" + slug, headers=headers)
        if resp.status_code >= 300:
            return None
        return bool(resp.json().get("private"))
    except Exception as exc:  # noqa: BLE001 - advisory only, never fails a submission
        logger.debug("feedback: could not read repo visibility: %s", exc)
        return None
