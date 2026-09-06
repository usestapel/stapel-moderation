"""The three merge-registries of stapel-moderation.

**Target types** — the flagship seam, file-for-file the ``stapel-reviews``
idiom: built-ins (empty — the module knows no targets) <- settings
``STAPEL_MODERATION["TARGET_TYPES"]`` <- runtime ``register_target_type()``,
last layer wins, a policy of ``None`` REMOVES a type. A policy is a plain
dict, never an ABC: an interface with one implementation per host is a class
hierarchy pretending to be configuration.

**Reasons** — the complaint taxonomy. Unlike target types the built-ins are
NOT empty: "spam" means the same thing on a listing, a review, a chat message
and an avatar, whereas what a "listing" is only the host knows. One table of
reasons across every target type is what closes three legacy defects at once
(reasons that existed only for listings; a description silently erased when
the reason was not ``other``; a reason filter that silently dropped whole
target classes because two tables had different columns).

**Rules** — the deterministic first screening stage. Empty built-ins: a
banned-keyword list is jurisdiction- and vertical-specific, and a library
that ships one ships somebody else's censorship policy.

Policy callbacks are **comm Function names called by string** — the module
calls the host and never imports a host model. ``can_report`` fails OPEN when
unset (anyone authenticated may complain — the reviews ``can_review``
precedent); ``can_view_content`` fails OPEN too, because the moderator has
already passed the staff mandate and a second fail-closed gate would hide the
content from the person paid to look at it. Neither is a substitute for the
mandate in :mod:`stapel_moderation.authz`.
"""
from __future__ import annotations

from typing import Optional

# ── Target types ─────────────────────────────────────────────────────

#: Built-ins are EMPTY — the module is target-generic and knows no targets.
#: The composite registers listing/review/profile (moderation spec §16.8).
BUILTIN_TARGET_TYPES: dict[str, Optional[dict]] = {}

#: Runtime overrides. Kept separate from the settings layer so tests reset
#: without touching Django settings.
_runtime_target_types: dict[str, Optional[dict]] = {}


class UnknownTargetType(Exception):
    """Raised when a target_type is not registered by the host."""


class UnknownReason(Exception):
    """Raised when a reason_code is not in the reason registry."""


def register_target_type(name: str, policy: Optional[dict]) -> None:
    """Register/override a target type at runtime. ``policy=None`` removes a
    type a lower layer (built-ins / settings) provided.

    Wires the type's ``intake_events`` as a side effect. Without that a type
    registered after ``apps.ready()`` would declare topics nobody is
    listening to — "declared but not connected", in the module whose whole
    job is catching that. The subscription is idempotent, and it is skipped
    silently before the app registry is ready (import order during boot).
    """
    _runtime_target_types[name] = policy
    if policy is None:
        return
    try:
        from .actions import subscribe_intake_events

        subscribe_intake_events()
    except (ImportError, RuntimeError):  # pragma: no cover — boot ordering
        pass


def reset_target_types() -> None:
    """Tests only: drop runtime target-type overrides."""
    _runtime_target_types.clear()


def get_target_types() -> dict[str, dict]:
    """Effective registry: built-ins <- settings <- runtime, ``None`` removing
    a key. Only live (non-None) entries are returned."""
    from .conf import moderation_settings

    merged: dict[str, Optional[dict]] = dict(BUILTIN_TARGET_TYPES)
    for source in (moderation_settings.TARGET_TYPES or {}, _runtime_target_types):
        for name, policy in source.items():
            merged[name] = policy
    return {name: policy for name, policy in merged.items() if policy is not None}


def resolve_policy(target_type: str) -> dict:
    """Fully-resolved policy for ``target_type``, or :class:`UnknownTargetType`.

    Every key the services read is guaranteed present on the returned dict, so
    no call site ever writes ``policy.get(..., default)`` — the defaults live
    here, once.

    The gate key is called ``gate`` and not ``moderation`` (the reviews
    spelling) deliberately: inside a module named moderation, a policy key
    named ``moderation`` is unreadable. The one-off divergence is visible in
    the composite, which holds both dicts side by side.
    """
    from .conf import moderation_settings

    types = get_target_types()
    if target_type not in types:
        raise UnknownTargetType(target_type)
    raw = types[target_type] or {}
    return {
        # How the target enters moderation.
        "gate": raw.get("gate", moderation_settings.GATE_DEFAULT),
        "intake_events": tuple(raw.get("intake_events") or ()),
        "id_field": raw.get("id_field") or "target_key",
        # Where the content comes from. Mandatory — checks.E004 — UNLESS the
        # type is evidence-based (below), which is the one case where there
        # is no owner in the fleet to ask.
        "content_function": raw.get("content_function") or "",
        # Evidence-based type: the target's content is not served by anybody,
        # so a report carries the reporter's own snapshot of it and THAT is
        # what the screener and the moderator read. Declared per type rather
        # than inferred from a missing content_function, because "nobody
        # serves this" must be a statement, not an omission.
        "evidence": bool(raw.get("evidence", False)),
        # How the verdict reaches the target. Explicit None = "this target
        # consumes no verdict", a statement rather than an omission.
        "verdict_event": (
            raw["verdict_event"] if "verdict_event" in raw else "moderation.completed"
        ),
        "notification_types": dict(raw.get("notification_types") or {}),
        # Policy predicates (comm Function names).
        "can_report": raw.get("can_report"),
        "can_view_content": raw.get("can_view_content"),
        # Behavior.
        "reasons": list(raw.get("reasons") or ["*"]),
        "screen": bool(raw.get("screen", True)),
        "media": bool(raw.get("media", True)),
        "severity_floor": int(raw.get("severity_floor") or 0),
    }


#: The policy a case falls back on when its target type is not (or no longer)
#: registered. No content to read, no verdict topic to publish, no automation
#: — a case that can still be audited, decided and closed by a person.
UNREGISTERED_POLICY = {
    "gate": "post",
    "intake_events": (),
    "id_field": "target_key",
    "content_function": "",
    "evidence": False,
    "verdict_event": None,
    "notification_types": {},
    "can_report": None,
    "can_view_content": None,
    "reasons": ["*"],
    "screen": False,
    "media": False,
    "severity_floor": 0,
}


def resolve_policy_lenient(target_type: str) -> dict:
    """:func:`resolve_policy`, but an unknown type yields the neutral policy.

    Used on the STAFF side only, and for the reason stapel-reviews 0.2.0
    documented from the other end (spec §22.7): a host that de-registers a
    target type must not be able to strand the platform's own open cases. A
    moderator has to be able to close a case about a type nobody moderates any
    more, and a manually issued sanction has to be able to open the case that
    carries its audit trail. Reporter-facing paths keep the strict function —
    a complaint about an unregistered type is a 400, as it should be.
    """
    try:
        return resolve_policy(target_type)
    except UnknownTargetType:
        return dict(UNREGISTERED_POLICY)


def target_type_for_event(topic: str) -> list[str]:
    """Every registered target type whose ``intake_events`` names ``topic``."""
    return [
        name
        for name, policy in get_target_types().items()
        if topic in tuple((policy or {}).get("intake_events") or ())
    ]


def content_payload_key(policy: dict) -> str:
    """The payload key the target's ``content_function`` expects the key under.

    The ``*.moderation_content`` family takes the target module's OWN id name
    (``{"listing_id": ...}``, ``{"review_id": ...}``) rather than a generic
    ``target_key`` — released that way in listings 0.4.0 and reviews 0.2.0
    (spec §21.5, §22.2). A tolerant two-spelling reader was deliberately not
    built on either side, so the caller declares the key: ``id_field``.
    """
    return policy.get("id_field") or "target_key"


# ── Reasons ──────────────────────────────────────────────────────────

#: The universal complaint taxonomy. ``severity`` orders the queue;
#: ``requires_description`` is what replaces legacy's silent erasure of a
#: description typed under a reason the serializer did not like;
#: ``applies_to`` narrows a reason to some target types; ``policy_clause``
#: names the terms-of-use clause quoted in the statement of reasons
#: (DSA Art. 17(3)) — the clause TEXT is the product's to write.
BUILTIN_REASONS: dict[str, Optional[dict]] = {
    "spam": {"severity": 1, "requires_description": False, "applies_to": ["*"]},
    "offensive": {"severity": 2, "requires_description": False, "applies_to": ["*"]},
    "harassment": {"severity": 3, "requires_description": True, "applies_to": ["*"]},
    "counterfeit": {"severity": 2, "requires_description": False, "applies_to": ["*"]},
    "fraud": {"severity": 3, "requires_description": True, "applies_to": ["*"]},
    "illegal": {"severity": 4, "requires_description": True, "applies_to": ["*"]},
    "adult": {"severity": 3, "requires_description": False, "applies_to": ["*"]},
    "personal_data": {"severity": 3, "requires_description": True, "applies_to": ["*"]},
    "off_platform_payment": {
        "severity": 2,
        "requires_description": False,
        "applies_to": ["*"],
    },
    "wrong_category": {"severity": 0, "requires_description": False, "applies_to": ["*"]},
    "other": {"severity": 1, "requires_description": True, "applies_to": ["*"]},
}

#: Reason codes the module itself produces. They are verdict reasons, not
#: complaint reasons — a user never picks them, but a statement of reasons
#: and a policy disclosure must be able to name them.
REASON_SCREENING_UNAVAILABLE = "screening_unavailable"
REASON_SCREENING_HELD = "screening_held"
REASON_LOW_CONFIDENCE = "low_confidence"
REASON_MEDIA_UNAVAILABLE = "media_unavailable"
#: The case's subject cannot be addressed at all — a draft case, whose
#: ``target_key`` is a synthetic ``draft:<uuid>`` and names no row anywhere,
#: or a target its owner has deleted. Nothing can be screened and nothing can
#: be acted on, so the case is DISMISSED under this code rather than retried
#: against a key that will never resolve.
REASON_SUBJECT_GONE = "subject_gone"
#: The screening seam failed and kept failing. Carried by the DLQ audit row,
#: never by a verdict: "we could not check" is not a decision about content.
REASON_SCREENING_FAILED = "screening_failed"

_SYSTEM_REASONS: dict[str, dict] = {
    REASON_SCREENING_UNAVAILABLE: {
        "severity": 0,
        "requires_description": False,
        "applies_to": ["*"],
        "system": True,
    },
    REASON_SCREENING_HELD: {
        "severity": 0,
        "requires_description": False,
        "applies_to": ["*"],
        "system": True,
    },
    REASON_LOW_CONFIDENCE: {
        "severity": 0,
        "requires_description": False,
        "applies_to": ["*"],
        "system": True,
    },
    REASON_MEDIA_UNAVAILABLE: {
        "severity": 0,
        "requires_description": False,
        "applies_to": ["*"],
        "system": True,
    },
    REASON_SUBJECT_GONE: {
        "severity": 0,
        "requires_description": False,
        "applies_to": ["*"],
        "system": True,
    },
    REASON_SCREENING_FAILED: {
        "severity": 0,
        "requires_description": False,
        "applies_to": ["*"],
        "system": True,
    },
}

_runtime_reasons: dict[str, Optional[dict]] = {}


def register_reason(code: str, entry: Optional[dict]) -> None:
    """Register/override a reason at runtime. ``entry=None`` removes it."""
    _runtime_reasons[code] = entry


def reset_reasons() -> None:
    """Tests only: drop runtime reason overrides."""
    _runtime_reasons.clear()


def get_reasons() -> dict[str, dict]:
    """Effective reason registry, defaults filled in, ``None`` removing."""
    from .conf import moderation_settings

    merged: dict[str, Optional[dict]] = dict(BUILTIN_REASONS)
    for source in (moderation_settings.REASONS or {}, _runtime_reasons):
        for code, entry in source.items():
            merged[code] = entry
    # System reasons are last and unremovable: a host that deleted
    # "screening_unavailable" would make the hold path unable to name why it
    # held, which is not a configuration choice, it is a broken module.
    merged.update(_SYSTEM_REASONS)
    return {
        code: _resolved_reason(code, entry)
        for code, entry in merged.items()
        if entry is not None
    }


def _resolved_reason(code: str, raw: dict) -> dict:
    return {
        "code": code,
        "severity": int(raw.get("severity") or 0),
        "requires_description": bool(raw.get("requires_description", False)),
        "applies_to": list(raw.get("applies_to") or ["*"]),
        "label_key": raw.get("label_key") or f"moderation.reason.{code}.label",
        "description_key": (
            raw.get("description_key") or f"moderation.reason.{code}.description"
        ),
        "policy_clause": raw.get("policy_clause") or "",
        "system": bool(raw.get("system", False)),
    }


def resolve_reason(code: str) -> dict:
    """One resolved reason entry, or :class:`UnknownReason`."""
    reasons = get_reasons()
    if code not in reasons:
        raise UnknownReason(code)
    return reasons[code]


def reason_applies(reason: dict, target_type: str) -> bool:
    """Whether ``reason`` may be used against ``target_type``."""
    applies = reason.get("applies_to") or ["*"]
    return "*" in applies or target_type in applies


def reasons_for_target(target_type: str) -> dict[str, dict]:
    """The reasons a reporter may choose for ``target_type``.

    Intersects the reason registry's own ``applies_to`` with the target
    policy's ``reasons`` allowlist, and never offers a system reason: those
    are verdict vocabulary, not complaint vocabulary.
    """
    policy_reasons = resolve_policy(target_type)["reasons"]
    allow_all = "*" in policy_reasons
    return {
        code: entry
        for code, entry in get_reasons().items()
        if not entry["system"]
        and reason_applies(entry, target_type)
        and (allow_all or code in policy_reasons)
    }


# ── Rules ────────────────────────────────────────────────────────────

#: Empty by design. A shipped keyword list is somebody else's speech policy,
#: and the one thing worse than no deterministic filter is an invisible one.
BUILTIN_RULES: dict[str, Optional[dict]] = {}

_runtime_rules: dict[str, Optional[dict]] = {}


def register_rule(code: str, entry: Optional[dict]) -> None:
    """Register/override a screening rule at runtime. ``None`` removes it."""
    _runtime_rules[code] = entry


def reset_rules() -> None:
    """Tests only: drop runtime rule overrides."""
    _runtime_rules.clear()


def get_rules() -> dict[str, dict]:
    """Effective rule registry, defaults filled in, ``None`` removing."""
    from .conf import moderation_settings

    merged: dict[str, Optional[dict]] = dict(BUILTIN_RULES)
    for source in (moderation_settings.RULES or {}, _runtime_rules):
        for code, entry in source.items():
            merged[code] = entry
    return {
        code: {
            "code": code,
            "pattern": str(raw.get("pattern") or ""),
            "decision": raw.get("decision") or "rejected",
            "severity": int(raw.get("severity") or 0),
            "reason_code": raw.get("reason_code") or code,
            "description_key": (
                raw.get("description_key") or f"moderation.rule.{code}.description"
            ),
            "applies_to": list(raw.get("applies_to") or ["*"]),
        }
        for code, raw in merged.items()
        if raw is not None
    }


def rules_for_target(target_type: str) -> list[dict]:
    """Rules applicable to ``target_type``, in stable code order."""
    return [
        rule
        for code, rule in sorted(get_rules().items())
        if "*" in rule["applies_to"] or target_type in rule["applies_to"]
    ]


def reset_registries() -> None:
    """Tests only: drop every runtime override in one call."""
    reset_target_types()
    reset_reasons()
    reset_rules()


# ── Policy callbacks (comm Functions, called by name) ────────────────


def _read_bool(result) -> bool:
    """Normalize a callback result: a bare bool or ``{"allowed": bool}``."""
    if isinstance(result, dict) and "allowed" in result:
        return bool(result["allowed"])
    return bool(result)


def check_can_report(policy: dict, *, reporter_id, target_type: str, target_key: str) -> bool:
    """Ask the type's ``can_report`` callback whether the reporter may file.

    No callback (``None``) means unrestricted — any authenticated user may
    report anything of this type (the reviews ``can_review`` fail-open shape).
    That is what turns legacy's hardwired oddity "only the recipient of a
    review may report it" into a composite policy. A callback that raises is
    NOT swallowed into a permissive default: a broken gate blocks the write.
    """
    name = policy.get("can_report")
    if not name:
        return True
    from stapel_core.comm import call

    return _read_bool(
        call(
            name,
            {
                "reporter_id": str(reporter_id),
                "target_type": target_type,
                "target_key": target_key,
            },
        )
    )


def check_can_view_content(policy: dict, *, actor_id, target_type: str, target_key: str) -> bool:
    """Ask the type's ``can_view_content`` callback about a moderator's read.

    Fail-OPEN when unset, unlike the reviews ``can_moderate`` twin, and the
    asymmetry is the point: by the time this is asked the caller has already
    cleared the staff mandate (``Case`` view = MID). A second fail-closed gate
    would blank the card for the very person the mandate admitted, and a
    moderator deciding about content nobody showed them is the failure this
    module exists to prevent.
    """
    name = policy.get("can_view_content")
    if not name:
        return True
    from stapel_core.comm import call

    return _read_bool(
        call(
            name,
            {
                "actor_id": str(actor_id) if actor_id else "",
                "target_type": target_type,
                "target_key": target_key,
            },
        )
    )


__all__ = [
    "BUILTIN_TARGET_TYPES",
    "BUILTIN_REASONS",
    "BUILTIN_RULES",
    "REASON_MEDIA_UNAVAILABLE",
    "REASON_SCREENING_FAILED",
    "REASON_SCREENING_UNAVAILABLE",
    "REASON_SCREENING_HELD",
    "REASON_SUBJECT_GONE",
    "REASON_LOW_CONFIDENCE",
    "UnknownReason",
    "UnknownTargetType",
    "check_can_report",
    "check_can_view_content",
    "content_payload_key",
    "get_reasons",
    "get_rules",
    "get_target_types",
    "reason_applies",
    "reasons_for_target",
    "register_reason",
    "register_rule",
    "register_target_type",
    "reset_reasons",
    "reset_registries",
    "reset_rules",
    "reset_target_types",
    "resolve_policy",
    "resolve_policy_lenient",
    "resolve_reason",
    "rules_for_target",
    "target_type_for_event",
]
