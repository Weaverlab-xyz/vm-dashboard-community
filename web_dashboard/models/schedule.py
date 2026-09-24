"""The three fields a run form needs to book a job into a change window.

Declared once and mixed into each request model rather than retyped per form. There
are a dozen run forms and they are spread across eight routers; three string fields
copied by hand twelve times is how one of them ends up spelled `run_tz` and silently
never scheduling anything.

    class AWSDeployRequest(ScheduleRequestMixin, BaseModel):
        ...

    job_service.create_job(db, ..., **change_window_service.schedule_kwargs(
        db, **payload.schedule_fields()))

All three blank — the default — means "run now", and
``change_window_service.schedule_kwargs`` then returns an empty dict, so the
``create_job`` call is byte-for-byte what it was before the form gained a picker.

**These are queue state, not job parameters.** A request model whose whole body is
persisted into job metadata (``api/packer.py`` stores ``req.model_dump()``) must
exclude them with :data:`SCHEDULE_FIELDS`, or a replayed payload would carry the
booking that produced it — a job that reads as scheduled for a time in the past,
forever.
"""
from pydantic import BaseModel

#: The field names this mixin contributes, for ``model_dump(exclude=...)``.
SCHEDULE_FIELDS = frozenset({"run_at", "run_timezone", "change_window_id"})


class ScheduleRequestMixin(BaseModel):
    """Optional change-window booking on any request that creates a job."""

    # "YYYY-MM-DDTHH:MM", local to `run_timezone`. The browser's `datetime-local`
    # sends wall-clock text with no offset, which is why the zone travels beside it.
    run_at: str = ""
    # IANA name, e.g. "America/New_York". Blank resolves to UTC — never the server's
    # local zone, which is an accident of the container image.
    run_timezone: str = ""
    # A named window; its next occurrence supplies both the start and the deadline.
    change_window_id: str = ""

    def schedule_fields(self) -> dict:
        """The kwargs for ``change_window_service.schedule_kwargs``."""
        return {
            "run_at": self.run_at,
            "run_timezone": self.run_timezone,
            "change_window_id": self.change_window_id,
        }

    def is_scheduled(self) -> bool:
        """Whether the caller asked for anything other than "now"."""
        return bool((self.run_at or "").strip()
                    or (self.change_window_id or "").strip())
