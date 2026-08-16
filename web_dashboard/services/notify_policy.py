"""Pure policy for outbound notifications.

Answers *is this event worth sending*, *what does it say*, and *what key makes it
fire once* — and nothing else. Stdlib + ``config_service`` only: no Session, no
models, no HTTP client. That keeps it unit-testable by file path, mirroring
``expiry_policy.py``, and it is enforced by ``tests/test_notify_wiring.py``.

The pieces that touch the world live elsewhere:

  * ``services/notify_transports.py`` — payload shapes and the actual POST.
  * ``services/notification_service.py`` — the outbox, the drain loop, retry.
  * ``services/notify_scanner.py`` — the periodic condition scan.

Two properties matter here:

  * **Off by default, and dry-run by default even once on.** ``notifications_enabled``
    gates everything; ``notify_dry_run`` then still writes delivery rows and sends
    nothing. Enabling notifications against a live inventory without that second brake
    is an incident, not a rollout — the same reasoning behind ``resource_expiry_dry_run``.
  * **Every read goes through here rather than being captured at import**, so a Settings
    change takes effect on the next drain pass without restarting anything
    (``config_service``'s 5s process cache bounds the lag).
"""
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


# ── The event catalogue ──────────────────────────────────────────────────────
#
# event_type → default severity. Dotted rather than the flat snake_case the audit
# actions use (`resource_expiry_reaped`): audit actions are a flat greppable
# namespace, while notification types need hierarchical matching (`job.*`) the day
# per-event routing rules land. Do not rename the audit actions to match.
EVENT_SEVERITY = {
    "resource.expiring":     "warning",
    "resource.reaped":       "critical",
    "job.failed":            "warning",
    "cost.budget_exceeded":  "warning",
    "secret.stale":          "warning",
    "config.drift":          "warning",
    "notification.test":     "info",
}

# Shipped on by default. `notification.test` is absent deliberately — the test button
# bypasses filtering entirely, so listing it here would only let someone "turn off"
# a thing that ignores the setting.
DEFAULT_EVENT_TYPES = (
    "resource.expiring,resource.reaped,job.failed,"
    "cost.budget_exceeded,secret.stale,config.drift"
)

SEVERITY_ORDER = ("info", "warning", "critical")

# Retry schedule, indexed by attempts already made. Held here rather than in the
# drain loop because it is policy, and because a test can then assert the shape
# without importing anything that touches a database.
BACKOFF_SECONDS = (30, 120, 600, 1800)

# Ceiling on a Retry-After the remote asks us to honour. A webhook that says
# "come back in 6 hours" should not be able to park a row for 6 hours.
RETRY_AFTER_CAP_SECONDS = 1800

# A row left in `sending` this long is assumed to belong to a worker that died
# mid-POST and is returned to `pending`.
STALE_SENDING_SECONDS = 600

_MAX_SUBJECT_CHARS = 180
_MAX_DEDUPE_CHARS = 200

_DEFAULT_HTTP_TIMEOUT_S = 10
_DEFAULT_FLUSH_INTERVAL_S = 30
_DEFAULT_SCAN_INTERVAL_S = 3600
_DEFAULT_MAX_ATTEMPTS = 4
_DEFAULT_MAX_PER_FLUSH = 50
_DEFAULT_MAX_QUEUE = 500
_DEFAULT_RETENTION_DAYS = 30


@dataclass(frozen=True)
class NotificationEvent:
    """One thing worth telling somebody about.

    ``resource_id`` deliberately reuses ``inventory_service``'s id scheme
    (``job:<uuid>`` / ``clouddb:<id>`` / ``k8s:<id>``) so a delivery row can be mapped
    back to its resource by the code that already owns that mapping
    (``expiry_reaper._resolve_row``), and so a future "extend from the alert link"
    needs no new identifier.

    ``dedupe_bucket`` is for repeatable conditions: the scanner sets it to today's
    date so a sustained budget breach notifies once a day rather than once an hour.
    """
    event_type: str
    title: str
    body: str = ""
    severity: str = ""                    # blank = look up EVENT_SEVERITY
    resource_id: str = ""
    resource_kind: str = ""
    resource_name: str = ""
    cloud: str = ""
    region: str = ""
    workgroup: str = ""
    url: str = ""                         # dashboard-relative path, e.g. "/jobs/<id>"
    dedupe_bucket: str = ""
    fields: dict = field(default_factory=dict)

    def effective_severity(self) -> str:
        sev = (self.severity or "").strip().lower()
        if sev in SEVERITY_ORDER:
            return sev
        return EVENT_SEVERITY.get(self.event_type, "info")


# ── Config access ────────────────────────────────────────────────────────────

def _cs():
    from . import config_service
    return config_service


def _flag(key: str, default: bool) -> bool:
    try:
        return _cs().get_bool(key, default)
    except Exception:                                  # pragma: no cover - defensive
        logger.debug("notify: could not read %s, using %s", key, default)
        return default


def _str(key: str, default: str = "") -> str:
    try:
        raw = _cs().get(key, "")
    except Exception:                                  # pragma: no cover - defensive
        raw = ""
    if raw in (None, ""):
        try:
            from ..config import settings
            raw = getattr(settings, key, default)
        except Exception:                              # pragma: no cover - defensive
            raw = default
    return str(raw or default).strip()


def _int(key: str, default: int) -> int:
    raw = _str(key, "")
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def enabled() -> bool:
    """Master switch. Off means nothing is emitted, drained, or scanned."""
    return _flag("notifications_enabled", False)


def dry_run() -> bool:
    """Record what would be sent and send nothing. Defaults ON.

    The per-endpoint test button ignores this — a test that doesn't send is not a test.
    """
    return _flag("notify_dry_run", True)


def base_url() -> str:
    """Absolute origin for deep links, e.g. ``https://dash.corp.example``.

    The worker has no request context, so it cannot derive this the way a template
    can from ``window.location.origin``. Blank means links are omitted rather than
    emitted as a broken relative path.
    """
    return _str("notify_base_url", "").rstrip("/")


def min_severity() -> str:
    sev = _str("notify_min_severity", "warning").lower()
    return sev if sev in SEVERITY_ORDER else "warning"


def event_types() -> frozenset:
    """The globally enabled event types, as a CSV — same shape as
    ``admission_gated_actions`` and ``resource_expiry_exempt_workgroups``."""
    return parse_event_types(_str("notify_event_types", "") or DEFAULT_EVENT_TYPES)


def parse_event_types(raw: str) -> frozenset:
    """CSV → set. Unknown tokens are kept, not rejected: a downgrade that no longer
    knows about an event type should quietly stop matching it, not 500 the panel."""
    return frozenset(t.strip() for t in (raw or "").split(",") if t.strip())


def http_timeout_s() -> int:
    return max(1, _int("notify_http_timeout_s", _DEFAULT_HTTP_TIMEOUT_S))


def flush_interval_seconds() -> int:
    return max(5, _int("notify_flush_interval_s", _DEFAULT_FLUSH_INTERVAL_S))


def scan_interval_seconds() -> int:
    """Cadence of the cost / secret-staleness / drift condition scan. Longer than the
    flush interval because those conditions change slowly and the cost read is billable."""
    return max(300, _int("notify_scan_interval_s", _DEFAULT_SCAN_INTERVAL_S))


def max_attempts() -> int:
    return max(1, min(len(BACKOFF_SECONDS) + 1, _int("notify_max_attempts", _DEFAULT_MAX_ATTEMPTS)))


def max_per_flush() -> int:
    return max(1, _int("notify_max_per_flush", _DEFAULT_MAX_PER_FLUSH))


def max_queue() -> int:
    """Ceiling on pending deliveries. Above it, emit() suppresses rather than queues —
    the brake that keeps "enable notifications" from becoming an outage."""
    return max(1, _int("notify_max_queue", _DEFAULT_MAX_QUEUE))


def retention_days() -> int:
    """0 = keep delivery rows forever."""
    return max(0, _int("notify_retention_days", _DEFAULT_RETENTION_DAYS))


# ── Gating ───────────────────────────────────────────────────────────────────

def severity_rank(severity: str) -> int:
    try:
        return SEVERITY_ORDER.index((severity or "").lower())
    except ValueError:
        return 0


def should_notify(event: NotificationEvent, *, endpoint_event_types=None) -> bool:
    """Whether this event passes the global gates and (optionally) an endpoint's filter.

    ``endpoint_event_types`` of None or an empty set means "inherit the global list",
    which is what almost every endpoint wants.
    """
    if not enabled():
        return False
    if severity_rank(event.effective_severity()) < severity_rank(min_severity()):
        return False
    if event.event_type not in event_types():
        return False
    if endpoint_event_types:
        return event.event_type in endpoint_event_types
    return True


def backoff_seconds(attempts: int) -> int:
    """Delay before retry number ``attempts`` (0-based: 0 = after the first failure)."""
    idx = max(0, min(len(BACKOFF_SECONDS) - 1, attempts))
    return BACKOFF_SECONDS[idx]


def retry_after_seconds(header_value, *, default: int) -> int:
    """Parse a Retry-After header, capped. Accepts the delta-seconds form only —
    the HTTP-date form is legal but no chat platform sends it."""
    try:
        secs = int(str(header_value).strip())
    except (TypeError, ValueError):
        return default
    return max(1, min(RETRY_AFTER_CAP_SECONDS, secs))


# ── Rendering ────────────────────────────────────────────────────────────────

def clean_subject(text: str) -> str:
    """One line, bounded length. CR/LF is stripped rather than escaped: the subject
    flows into a Slack ``text`` and a Teams ``TextBlock``, and a resource name that
    someone pasted a newline into should not become two messages' worth of layout."""
    flat = " ".join(str(text or "").split())
    if len(flat) > _MAX_SUBJECT_CHARS:
        flat = flat[:_MAX_SUBJECT_CHARS - 1].rstrip() + "…"
    return flat


def absolute_url(path: str) -> str:
    """Join a dashboard-relative path onto ``notify_base_url``. Empty when either side
    is missing — a caller must treat "" as "omit the link", not as a valid URL."""
    root = base_url()
    p = (path or "").strip()
    if not root or not p:
        return ""
    if p.startswith("http://") or p.startswith("https://"):
        return p
    return f"{root}/{p.lstrip('/')}"


def event_facts(event: NotificationEvent) -> list:
    """The (label, value) pairs every channel renders, in one place so the plain-text
    body, the Slack block and the Teams FactSet cannot drift apart."""
    pairs = []
    if event.resource_kind:
        pairs.append(("Kind", event.resource_kind))
    if event.cloud:
        loc = f"{event.cloud} / {event.region}" if event.region else event.cloud
        pairs.append(("Cloud", loc))
    if event.workgroup:
        pairs.append(("Workgroup", event.workgroup))
    for k, v in (event.fields or {}).items():
        if v not in (None, ""):
            pairs.append((str(k), str(v)))
    return pairs


def render(event: NotificationEvent) -> tuple:
    """→ ``(subject, body)``. Plain text; each channel decorates it its own way."""
    label = event.resource_name or event.event_type
    subject = clean_subject(f"[{event.effective_severity().upper()}] {event.title}")
    lines = [event.body.strip()] if event.body and event.body.strip() else []
    facts = event_facts(event)
    if facts:
        width = max(len(k) for k, _ in facts)
        lines.append("\n".join(f"{k.ljust(width)}  {v}" for k, v in facts))
    link = absolute_url(event.url)
    if link:
        lines.append(link)
    body = "\n\n".join(lines) if lines else label
    return subject, body


# ── Dedupe ───────────────────────────────────────────────────────────────────

def dedupe_key(event: NotificationEvent, endpoint_id: str) -> str:
    """The value behind ``UNIQUE(notification_deliveries.dedupe_key)``.

    This constraint — not an in-process set — is what makes an event fire once across
    two gunicorn app workers and three worker replicas. Anything that should repeat
    (a daily budget breach) has to say so via ``dedupe_bucket``; the default is
    fire-once-forever per resource per endpoint.
    """
    subject_part = event.resource_id or hashlib.sha256(
        event.title.encode("utf-8", "replace")).hexdigest()[:16]
    parts = [event.event_type, subject_part, endpoint_id or "-"]
    if event.dedupe_bucket:
        parts.append(event.dedupe_bucket)
    key = ":".join(parts)
    if len(key) > _MAX_DEDUPE_CHARS:
        # Hash the whole thing rather than truncating: two long keys sharing a prefix
        # would collide, and a collision here means a notification silently vanishes.
        key = f"{event.event_type}:{hashlib.sha256(key.encode()).hexdigest()}"
    return key[:_MAX_DEDUPE_CHARS]


def day_bucket(now: Optional[datetime] = None) -> str:
    """``dedupe_bucket`` value for a condition that should re-notify once per day."""
    return (now or datetime.utcnow()).strftime("%Y-%m-%d")
