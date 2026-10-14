"""Which use cases a POV tests: the "Use cases chosen" step of its Setup tab.

A POV's tenants say which products it CAN reach. They do not say what the evaluation is
FOR. A Password-Safe-only POV should test only Password Safe use cases; a POV of PRA plus
Password Safe is a Total PASM POV and tests both halves, including the cards that need the
two together. That is a decision the SE makes with the customer, so it is stored on the
row rather than derived from anything.

Two columns, on :class:`PovEnvironment` and on :class:`PovBlueprint` alike:

  * ``use_case_products`` -- a comma-separated subset of :data:`PRODUCTS`. NULL means
    nobody has chosen, which reads as "the whole catalog": every POV from before this
    landed keeps the checklist it had, and the ladder offers the step without locking
    anything behind it.
  * ``use_case_excluded`` -- a JSON list of card ids the SE unticked inside that choice.

This is the one place the checklist **subtracts**. The tenant mix still never does -- a
card whose product has no tenant renders ``out_of_scope`` with its reason, because "we
have not picked a tenant yet" is somebody's next click. A card outside the chosen use
cases is not: the SE has said the POV is not testing it, so it is hidden from the Use
cases tab, the customer's page and the summary, and is not counted in their totals.

Progress rows are never deleted by a scope change, so re-including a card brings its tick
and its notes back.
"""
from __future__ import annotations

import json

from . import pov_cards, pov_runbooks

PRODUCTS = pov_cards.POV_PRODUCTS

# The POV types an SE picks from, in the order the chips render. A preset is a VIEW over
# the product set and is never stored: two presets naming the same set are the same choice,
# and "Custom" is simply a set no preset names.
PRESETS = (
    {"key": "password_safe", "label": "Password Safe", "products": ("password_safe",)},
    {"key": "pra", "label": "PRA", "products": ("pra",)},
    {"key": "pasm", "label": "Total PASM (PRA + Password Safe)",
     "products": ("pra", "password_safe")},
    {"key": "entitle", "label": "Entitle", "products": ("entitle",)},
    {"key": "pasm_entitle", "label": "Total PASM + Entitle",
     "products": ("pra", "password_safe", "entitle")},
)

CUSTOM = "custom"

_PRODUCT_LABELS = {"pra": "PRA", "password_safe": "Password Safe", "entitle": "Entitle"}


class ScopeError(ValueError):
    """A refused scope, with the reason an operator can act on."""


def _ordered(products) -> tuple:
    """The products in catalog order, so one set always serialises one way."""
    chosen = set(products)
    return tuple(p for p in PRODUCTS if p in chosen)


def parse(row) -> tuple:
    """``(products, excluded)`` for a POV or blueprint row.

    ``products`` is ``None`` when nobody has chosen, never an empty set: an empty choice is
    refused on the way in, so an empty set here could only mean a corrupt row, and reading
    that as "nothing is in scope" would blank a customer's checklist.
    """
    raw = (getattr(row, "use_case_products", None) or "").strip()
    products = frozenset(p for p in raw.split(",") if p in PRODUCTS) if raw else None
    if products is not None and not products:
        products = None
    excluded: frozenset = frozenset()
    text = getattr(row, "use_case_excluded", None)
    if text:
        try:
            value = json.loads(text)
        except (TypeError, ValueError):
            value = []
        if isinstance(value, list):
            excluded = frozenset(str(v) for v in value if v)
    return products, excluded


def _known_card(card_id: str) -> bool:
    return (pov_cards.find_card(card_id)[1] is not None
            or pov_runbooks.find_card(card_id)[1] is not None)


def validate(products, excluded=()) -> tuple:
    """``(products, excluded)`` normalised, or :class:`ScopeError`.

    ``products`` of ``None`` is allowed and means "clear the choice". An empty list is not:
    a POV that tests nothing is not a POV, and storing it would hide every product card.
    """
    excluded = tuple(dict.fromkeys(str(c).strip() for c in (excluded or ()) if str(c).strip()))
    unknown_cards = [c for c in excluded if not _known_card(c)]
    if unknown_cards:
        raise ScopeError(f"no POV use case with id {', '.join(map(repr, unknown_cards))}")
    if products is None:
        return None, excluded
    products = [str(p).strip() for p in products if str(p).strip()]
    unknown = [p for p in products if p not in PRODUCTS]
    if unknown:
        raise ScopeError(f"unknown product {', '.join(map(repr, unknown))}; "
                         f"choose from {', '.join(PRODUCTS)}")
    if not products:
        raise ScopeError("choose at least one product for this POV to test")
    return _ordered(products), excluded


def write(row, products, excluded) -> None:
    """Store an already-validated scope on a POV or blueprint row. Does not commit."""
    row.use_case_products = ",".join(products) if products else None
    row.use_case_excluded = json.dumps(list(excluded)) if excluded else None


def includes(card: dict, products, excluded) -> bool:
    """Whether one described card (``pov_cards.describe_card``'s shape) is in the POV.

    Every product the card names has to be chosen, so the cards that need PRA and Password
    Safe together appear only on a Total PASM POV. A card naming no product is about the
    environment itself and is in every POV unless it was unticked.
    """
    if card.get("id") in excluded:
        return False
    if products is None:
        return True
    return all(p in products for p in card.get("products") or ())


def preset_of(products) -> str:
    """The preset key naming exactly this set, :data:`CUSTOM`, or ``""`` for no choice."""
    if products is None:
        return ""
    chosen = set(products)
    for preset in PRESETS:
        if set(preset["products"]) == chosen:
            return preset["key"]
    return CUSTOM


def label_of(products) -> str:
    """What the Setup step says the POV is: "Total PASM (PRA + Password Safe)", say."""
    key = preset_of(products)
    if not key:
        return ""
    for preset in PRESETS:
        if preset["key"] == key:
            return preset["label"]
    return " + ".join(_PRODUCT_LABELS[p] for p in _ordered(products))


def describe(row) -> dict:
    """The scope as the API serves it, beside the POV row and the use-cases payload."""
    products, excluded = parse(row)
    return {
        "products": list(_ordered(products)) if products is not None else None,
        "excluded": sorted(excluded),
        "preset": preset_of(products),
        "label": label_of(products),
    }
