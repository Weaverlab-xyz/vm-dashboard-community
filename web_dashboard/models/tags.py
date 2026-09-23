"""The wire shape of one tag / label / attribute chip.

Shared by every resource listing that shows tags, so the four clouds and the hypervisor
pages cannot come to disagree about the field names. Built by
``services/tag_policy.normalise`` — that module owns the classification, the colour index
and the ordering; this is only the contract.
"""
from typing import Optional

from pydantic import BaseModel


class TagChip(BaseModel):
    key: str
    # None — not "" — for a tag that genuinely has no value: a Proxmox tag, a vSphere
    # category. The page renders those as a bare key rather than inventing `prod=`.
    value: Optional[str] = None
    # "system" | "identity" | "user" — see services/tag_policy. Decides the chip's colour
    # and, in Phase 2, whether it is editable at all.
    cls: str = "user"
    # Palette index for a "user" chip, else None. Computed server-side so one key is one
    # colour on every page; static/js/app.js TAG_TONES is the lookup table.
    tone: Optional[int] = None
    # Tooltip: what this platform calls a tag, plus — for a system chip — why it is
    # locked. The place an operator learns that GCP says "label" and Password Safe says
    # "attribute" for the same idea.
    title: str = ""
