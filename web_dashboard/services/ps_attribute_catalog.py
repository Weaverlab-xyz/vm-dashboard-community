"""Matching dashboard resources to Password Safe objects, and shaping their attributes.

Pure: stdlib plus the stdlib-only :mod:`tag_policy`. No config, no FastAPI, no httpx, so
the matching logic is unit-testable by file path — the same split
``services/ps_database_catalog`` keeps, and for the same reason. The I/O half lives in
``services/ps_api_service.read_attribute_inventory``.

**Why matching is not "join on IP" even though that is what it is for.**

Password Safe names a managed system after its ``HostName``, and this dashboard has written
four different things into that field across its onboarding paths — a bare VM name, an
``{env}-{vm}`` label, a PRA appliance URL, a private IP. Two production bugs came out of
that, including a managed account created on the wrong platform's system. So a name is not
a key here, and the operator asked for addresses specifically.

But addresses alone reach only half the estate, and it is worth being precise about which
half. Every cloud-native plugin onboarding (``ps_vm_hook`` lines 193/225/253) writes:

    IPAddress = "127.0.0.1"        a placeholder Password Safe REQUIRES on create
    HostName  = "<vm name>"
    DnsName   = "i-0abc123:us-east-1"  |  "<tenant>/<sub>/<rg>/<vm>"  |  "<proj>/<zone>/<vm>"

``DnsName`` there is a **connection locator, not an address** — which is why the
``DnsName > HostName > IPAddress`` order in ``ps_database_catalog`` is a connect-host
resolver and must not be borrowed as a match key. Those systems carry no usable address
anywhere, so an IP-only join finds **none** of them.

Hence two tiers, first hit wins:

  1. ``ps_managed_system_id`` — exact, and already recorded. ``ps_vm_hook`` writes it onto
     the deploy Job, and it is a column on ``cloud_databases`` and ``pov_environment_vms``.
     Zero I/O, no ambiguity, and it covers exactly the population tier 2 cannot.
  2. **IP** — for discovered Assets and ``ssh``-method systems, which do carry real
     addresses. This is the tier the operator asked for and it does the work for the
     records their Smart Rules act on.

There is deliberately **no name tier**. It would be redundant — the address-less population
*is* the tier-1 population — and it is the exact shape of the two bugs above.
"""
import ipaddress

from . import tag_policy

# What a matched row may carry. Closed, like ``ps_database_catalog.CANDIDATE_KEYS``.
MATCH_KEYS = (
    "state",        # str — one of STATE_*
    "basis",        # str — BASIS_ID | BASIS_IP | "" ; how the match was made
    "objects",      # list — MATCHED_OBJECT_KEYS, the Password Safe records behind it
    "attributes",   # list — tag_policy chips, [] unless state is ok
    "detail",       # str — operator-facing, "" when there is nothing to say
    "shared_with",  # int — how many OTHER resources matched the same object; 0 normally
)

MATCHED_OBJECT_KEYS = ("kind", "object_id", "name")

# The two Password Safe object kinds this reads. Assets carry the attributes Smart Rules
# filter on; managed systems are what the dashboard itself creates.
KIND_ASSET = "asset"
KIND_SYSTEM = "managed_system"

STATE_OK = "ok"                    # matched, and it has attributes
STATE_MATCHED_EMPTY = "none"       # matched, and it genuinely has no attributes
STATE_UNMATCHED = "unmatched"      # no Password Safe record corresponds to this resource
STATE_NOT_FETCHED = "not_fetched"  # matched, but its attributes were not read (over the cap)
STATE_AMBIGUOUS = "ambiguous"      # two records of one kind claim this resource
STATE_ERROR = "error"              # the read for THIS object failed

BASIS_ID = "id"
BASIS_IP = "ip"

MAX_TEXT = 256
MAX_ATTRIBUTES = 64
MAX_ADDRESSES = 8
# How many objects may have their attributes read in one pass. Attributes are a per-object
# call, so this is the ceiling on a page load. Applied AFTER matching, so it bounds the
# matched set rather than the estate.
MAX_ATTRIBUTE_FETCHES = 200


def _clean_text(value) -> str:
    """Strip C0/C1 control characters (except tab) and truncate.

    Mirrors ``ps_database_catalog._clean_text`` rather than importing it: both modules are
    stdlib-only on purpose so their boundary can be tested without the app, and the rule is
    four lines. The threat is the same one — an attribute name or value is operator-typed
    text from another product, and an ANSI escape in one would be replayed into a browser,
    and into a terminal the moment somebody copies it.
    """
    text = str(value if value is not None else "")
    cleaned = "".join(
        ch for ch in text
        if ch == "\t" or (ord(ch) >= 32 and not (0x7F <= ord(ch) <= 0x9F))
    ).strip()
    return cleaned[:MAX_TEXT]


# ── addresses ────────────────────────────────────────────────────────────────

# Fields a Password Safe object might carry an address in. All three are read as
# CANDIDATES rather than in preference order: the dashboard has put a real address in
# different ones over time, and a packed locator in others. Which is which is decided by
# whether it parses, below — never by which field it came from.
ADDRESS_FIELDS = ("IPAddress", "DnsName", "HostName")


def canonical_ip(value) -> str:
    """``value`` as one canonical address string, or ``""`` if it is not a usable key.

    Parsed with :mod:`ipaddress`, never a regex. ``managed_accounts.host_is_ip`` is an
    IPv4-only pattern that accepts ``999.999.999.999``; it is correct for its own job and
    is left alone, but it cannot be the basis for a join.

    Rejected as match keys, by PROPERTY rather than by a denylist that ages:

      * loopback — this is the ``127.0.0.1`` placeholder every plugin onboarding writes.
        Accepting it would collide every such system onto one key, which is worse than
        not matching them at all.
      * link-local — ``169.254.169.254`` is the cloud metadata endpoint and turns up in
        scraped inventory; it would collide every cloud VM in the estate.
      * unspecified, multicast, reserved — never a host's own address.

    RFC1918 is deliberately KEPT. Private addresses are what Password Safe knows hosts by;
    excluding them would leave nothing.

    Canonicalising folds ``::ffff:10.0.0.1`` onto ``10.0.0.1``, so a tenant storing one
    form and a page reporting the other still join.

    Note a zero-padded form like ``10.0.0.01`` is REFUSED, not folded: :mod:`ipaddress`
    rejects it deliberately because ``010`` is ambiguous between octal and decimal, and
    that ambiguity is a known source of address-confusion bugs. Refusing costs at most a
    missed match on a badly-entered record; guessing could match the wrong host.
    """
    text = str(value if value is not None else "").strip()
    if not text:
        return ""
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        # A hostname, or one of the packed locators described in the module docstring.
        # Not an error — most values in these fields are not addresses.
        return ""
    if (addr.is_loopback or addr.is_unspecified or addr.is_link_local
            or addr.is_multicast or addr.is_reserved):
        return ""
    # An IPv4-mapped IPv6 address is the same host as its IPv4 form; fold it so a page
    # that reports one and a tenant that stores the other still join.
    mapped = getattr(addr, "ipv4_mapped", None)
    return str(mapped or addr)


def addr_list(*values) -> list:
    """Usable addresses from ``values``, canonical, de-duplicated, order preserved.

    Order is display order only — the match consumes the whole set, so it cannot change a
    result. Capped so a guest with a pathological NIC count cannot bloat every row.
    """
    out = []
    for value in values:
        for item in (value if isinstance(value, (list, tuple, set)) else [value]):
            canon = canonical_ip(item)
            if canon and canon not in out:
                out.append(canon)
    return out[:MAX_ADDRESSES]


def object_addresses(obj) -> list:
    """Every usable address on one Password Safe object.

    All of :data:`ADDRESS_FIELDS`, not the first non-empty one. A dashboard-created system
    has a name in ``HostName`` and a locator in ``DnsName``; a discovered asset has a real
    address in ``IPAddress``. Reading all three and keeping what parses handles both
    without knowing which kind of object this is.
    """
    obj = obj or {}
    return addr_list(*[obj.get(field) for field in ADDRESS_FIELDS])


# ── the index ────────────────────────────────────────────────────────────────

def _object_id(obj, kind) -> str:
    keys = (("AssetID", "ID", "Id") if kind == KIND_ASSET
            else ("ManagedSystemID", "SystemId", "SystemID", "ID"))
    for key in keys:
        value = obj.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _object_name(obj) -> str:
    for key in ("AssetName", "SystemName", "Name", "HostName", "DnsName"):
        name = _clean_text(obj.get(key))
        if name:
            return name
    return ""


def build_index(assets=None, systems=None) -> dict:
    """Index Password Safe objects for matching.

    Returns ``{"objects": {ref: {...}}, "by_id": {system_id: ref}, "by_ip": {addr: [ref]}}``
    where ``ref`` is ``"<kind>:<object_id>"``.

    ``by_id`` holds managed systems only — it is keyed on the ``ps_managed_system_id`` the
    dashboard recorded, and an Asset has no such recorded id anywhere.
    """
    objects, by_id, by_ip = {}, {}, {}
    for kind, rows in ((KIND_ASSET, assets or []), (KIND_SYSTEM, systems or [])):
        for obj in rows:
            if not isinstance(obj, dict):
                continue
            object_id = _object_id(obj, kind)
            if not object_id:
                continue
            ref = f"{kind}:{object_id}"
            if ref in objects:
                continue
            addresses = object_addresses(obj)
            objects[ref] = {"kind": kind, "object_id": object_id,
                            "name": _object_name(obj), "addresses": addresses}
            if kind == KIND_SYSTEM:
                by_id.setdefault(object_id, ref)
            for addr in addresses:
                by_ip.setdefault(addr, [])
                if ref not in by_ip[addr]:
                    by_ip[addr].append(ref)
    return {"objects": objects, "by_id": by_id, "by_ip": by_ip}


# ── matching ─────────────────────────────────────────────────────────────────

def _coalesce(refs, objects) -> list:
    """One logical host per (kind) at an address.

    An Asset and a ManagedSystem describing the same machine at the same address is the
    NORMAL case — Password Safe holds both, and the attributes live on the Asset. Treating
    that pair as ambiguous would fire on the common case and render no chips at all. So
    the pair coalesces, and only two records of the SAME kind are a genuine conflict.
    """
    seen, out = set(), []
    for ref in refs:
        kind = objects.get(ref, {}).get("kind", "")
        if kind in seen:
            return []          # two of one kind — the caller reports ambiguity
        seen.add(kind)
        out.append(ref)
    return out


def match_refs(resource, index) -> tuple:
    """``(refs, basis)`` for one dashboard resource, or ``([], "")``.

    ``resource`` needs ``ps_system_id`` and/or ``ips`` — the two keys
    ``inventory_service`` puts on an item. Tier 1 wins outright when it hits: it is the id
    the dashboard itself recorded at onboarding, so no address can contradict it.
    """
    resource = resource or {}
    objects = index.get("objects", {})

    system_id = str(resource.get("ps_system_id") or "").strip()
    if system_id:
        ref = index.get("by_id", {}).get(system_id)
        if ref:
            return [ref], BASIS_ID

    refs = []
    for addr in addr_list(resource.get("ips")):
        for ref in index.get("by_ip", {}).get(addr, []):
            if ref not in refs:
                refs.append(ref)
    if not refs:
        return [], ""
    return refs, BASIS_IP


def match(resource, index, attributes_by_ref=None, shared_counts=None,
          types=None) -> dict:
    """The :data:`MATCH_KEYS` payload for one resource. Never raises.

    ``attributes_by_ref`` maps a ref to its fetched attribute rows, or to ``None`` for an
    object that was matched but not read (over :data:`MAX_ATTRIBUTE_FETCHES`). A ref absent
    from the mapping entirely is ``not_fetched`` too — the distinction the UI must keep is
    between "has none" and "we did not look", and an empty cell cannot say both.
    """
    objects = index.get("objects", {})
    refs, basis = match_refs(resource, index)
    if not refs:
        return _payload(STATE_UNMATCHED, "", [], [],
                        "no Password Safe record matches this resource")

    coalesced = _coalesce(refs, objects)
    if not coalesced:
        names = ", ".join(sorted(objects.get(r, {}).get("name", "") or r for r in refs))
        return _payload(STATE_AMBIGUOUS, basis,
                        [objects[r] for r in refs if r in objects], [],
                        f"{len(refs)} Password Safe records claim this address: {names}")

    matched = [objects[r] for r in coalesced if r in objects]
    shared = max((int((shared_counts or {}).get(r, 1)) - 1 for r in coalesced), default=0)

    attributes_by_ref = attributes_by_ref or {}
    fetched, any_known = [], False
    for ref in coalesced:
        rows = attributes_by_ref.get(ref)
        if rows is None:
            continue
        any_known = True
        fetched.extend(rows)
    if not any_known:
        return _payload(STATE_NOT_FETCHED, basis, matched, [],
                        "attributes were not read for this record", shared)

    chips = to_chips(fetched, types)
    state = STATE_OK if chips else STATE_MATCHED_EMPTY
    return _payload(state, basis, matched, chips, "", shared)


def _payload(state, basis, objects, attributes, detail, shared=0) -> dict:
    return {"state": state, "basis": basis,
            "objects": [{k: o.get(k, "") for k in MATCHED_OBJECT_KEYS} for o in objects],
            "attributes": attributes, "detail": detail, "shared_with": int(shared)}


def type_names(rows) -> dict:
    """``{AttributeTypeID: name}`` from a ``GET AttributeTypes`` response.

    The vocabulary — ``Criticality``, ``Business Unit``, ``Geography`` — and the half of
    an attribute a Smart Rule actually filters on. See :func:`to_chips`.
    """
    out = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        type_id = row.get("AttributeTypeID")
        name = _clean_text(row.get("Name"))
        if type_id not in (None, "") and name:
            out[str(type_id)] = name
    return out


def to_chips(rows, types=None) -> list:
    """Attribute rows as render-ready chips, as ``<type> = <value>``.

    **BeyondInsight attributes are hierarchical, and this is easy to get backwards.**
    Verified against a live tenant 2026-09-23: an asset's attribute row looks like

        {"AttributeID": 10000, "AttributeTypeID": 10000, "ShortName": "Online", ...}

    and ``GET AttributeTypes`` returns ``{"AttributeTypeID": 3, "Name": "Criticality"}``.
    So ``ShortName`` is the **value** an operator picked, and the type is the **category**
    it was picked from — ``Criticality = High``, not ``High = ""``. Reading ShortName as
    the key produces chips like ``Online=`` with an empty value, which is both wrong and
    useless, and it hides the type a Smart Rule keys on.

    ``types`` maps AttributeTypeID to name (see :func:`type_names`). A row whose type is
    unknown falls back to showing the value alone rather than vanishing — a missing
    vocabulary entry should cost the label, not the fact.

    Delegates to ``tag_policy.normalise``, which already knows Password Safe calls these
    **attributes** (its ``_NOUN`` table) and returns the exact shape
    ``partials/tag_chips.html`` renders, so there is no second chip renderer.
    """
    types = types or {}
    flat = {}
    for row in (rows or [])[:MAX_ATTRIBUTES]:
        if not isinstance(row, dict):
            continue
        value = _clean_text(row.get("ShortName") or row.get("LongName"))
        if not value:
            continue
        key = _clean_text(types.get(str(row.get("AttributeTypeID")), ""))
        if key:
            flat[key] = value
        else:
            # No type name for it: show the value as a bare label. It still says which
            # attribute is set, which is the fact worth keeping.
            flat[value] = ""
    return tag_policy.normalise(flat, "passwordsafe")


def unmatched(index, matched_refs, cap=100) -> list:
    """Password Safe records that correspond to nothing the dashboard knows about.

    Usually a decommissioned host whose record was never cleaned up. Returned as
    :data:`MATCHED_OBJECT_KEYS` plus its addresses; no attributes are read for these, which
    is what keeps the per-object fetch proportional to the page rather than to the tenant.
    """
    seen = set(matched_refs or ())
    out = []
    for ref, obj in sorted((index.get("objects") or {}).items()):
        if ref in seen:
            continue
        out.append({**{k: obj.get(k, "") for k in MATCHED_OBJECT_KEYS},
                    "addresses": obj.get("addresses", [])})
        if len(out) >= cap:
            break
    return out


# ── the vocabulary (Phase 2: assigning) ──────────────────────────────────────
#
# An attribute is NOT free text. A type — `Criticality`, `Business Unit`, `Geography` —
# owns a fixed set of values, each with its own AttributeID, and assigning one to an asset
# means naming that id. So the editor is a PICKER over this vocabulary, not a key/value
# box like the cloud tag editor: there is no way to invent a value, and a typo is not
# expressible. That is why nothing here needs a charset guard the way the Proxmox tag
# verb did.

VOCABULARY_KEYS = ("type_id", "name", "read_only", "values")
VOCABULARY_VALUE_KEYS = ("attribute_id", "value")


def build_vocabulary(types, values_by_type) -> list:
    """``AttributeTypes`` plus each one's values, as a closed, render-ready shape.

    ``read_only`` is carried from the tenant rather than inferred. Some types genuinely
    are — ``Criticality`` is ``IsReadOnly: true`` in the tenant this was built against —
    and offering a picker that Password Safe will refuse is a promise the page cannot
    keep. :func:`assert_assignable` is the half that enforces it.
    """
    out = []
    for row in types or []:
        if not isinstance(row, dict):
            continue
        type_id = row.get("AttributeTypeID")
        name = _clean_text(row.get("Name"))
        if type_id in (None, "") or not name:
            continue
        values = []
        for value_row in (values_by_type or {}).get(str(type_id), []) or []:
            if not isinstance(value_row, dict):
                continue
            attribute_id = value_row.get("AttributeID")
            label = _clean_text(value_row.get("ShortName") or value_row.get("LongName"))
            if attribute_id in (None, "") or not label:
                continue
            values.append({"attribute_id": str(attribute_id), "value": label})
        out.append({"type_id": str(type_id), "name": name,
                    "read_only": bool(row.get("IsReadOnly")),
                    "values": values[:MAX_ATTRIBUTES]})
    out.sort(key=lambda t: t["name"].lower())
    return out


def find_value(vocabulary, attribute_id):
    """``(type, value)`` for one attribute id, or ``(None, None)``.

    The lookup the write path validates against: an id the vocabulary does not contain is
    refused rather than forwarded, so a caller cannot assign something this tenant has
    never heard of and read the provider's error back as the explanation.
    """
    wanted = str(attribute_id or "").strip()
    for type_row in vocabulary or []:
        for value in type_row.get("values", []):
            if value["attribute_id"] == wanted:
                return type_row, value
    return None, None


def assert_assignable(vocabulary, attribute_id) -> tuple:
    """``(type, value)`` for an assignable attribute, or raise :class:`ValueError`.

    Two refusals, both naming the reason, because both are things an operator can act on:

      * an id this tenant does not have — usually a stale page, or a vocabulary edited in
        the console since the picker was loaded;
      * a type the tenant marks READ-ONLY. Password Safe would refuse it anyway; catching
        it here turns a provider error into a sentence, and stops a bulk apply from
        failing identically on every target.
    """
    type_row, value = find_value(vocabulary, attribute_id)
    if type_row is None:
        raise ValueError(
            f"attribute {attribute_id!r} is not in this Password Safe's vocabulary — "
            f"reload the page if it was added or removed in the console")
    if type_row["read_only"]:
        raise ValueError(
            f"'{type_row['name']}' is read-only in this Password Safe, so "
            f"'{value['value']}' cannot be assigned from here — change it in the console")
    return type_row, value
