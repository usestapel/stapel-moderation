"""comm Function providers of stapel-moderation.

Every Function carries a JSON schema in ``schemas/functions/``; tests run with
``VALIDATE_SCHEMAS`` on, so a payload drifting from its contract fails loudly.
Registration happens on import from ``apps.py:ready()``.

Two of these exist as a matched pair and must ship together:
``moderation.sanctions_by_users`` (the ``live_query`` half of the
``moderation.user_sanctions`` Projection) and ``moderation.sanctions_export``
(its ``source_of_truth``). Shipping a projection whose two halves do not exist
is the exact mistake ``stapel-shop`` made against ``stapel-reviews`` — a
declaration pointing at Functions nobody built — and the reason this module
publishes both in its first release.

What this module CALLS goes the other way and is never imported: each target
policy's ``content_function``, ``llm.complete``, ``cdn.describe``. There is
not one ``import stapel_listings`` in the package.
"""
from __future__ import annotations

import json
from pathlib import Path

from stapel_core.comm import function

_SCHEMAS_DIR = Path(__file__).resolve().parent / "schemas" / "functions"


def _schema(name: str) -> dict:
    return json.loads((_SCHEMAS_DIR / f"{name}.json").read_text(encoding="utf-8"))


@function("moderation.submit", schema=_schema("moderation.submit"))
def submit_function(payload: dict) -> dict:
    """Open (or find) a case for one target — the explicit submission path.

    For a module that has no event worth publishing, or one whose event the
    composite chose not to wire. Idempotent by state, like every other intake
    path: a second call about a target that already has an open case returns
    that case.
    """
    from stapel_core.comm import mutate_and_emit

    from . import services
    from .models import CaseOrigin

    with mutate_and_emit() as emit_event:
        case, created = services.open_case(
            payload["target_type"],
            str(payload["target_key"]),
            origin=payload.get("origin") or CaseOrigin.SUBMISSION,
            scope_key=str(payload.get("scope_key") or ""),
            emit_event=emit_event,
        )
        if created:
            services.start_screening(case, emit_event=emit_event)
    case.refresh_from_db()
    return {"case_id": str(case.id), "state": case.state, "created": created}


@function("moderation.check_sanctions", schema=_schema("moderation.check_sanctions"))
def check_sanctions_function(payload: dict) -> dict:
    """Is this user under an active sanction?

    The READ half of enforcement. This module deliberately does not intercept
    anybody's writes: it answers the question and the host's own view refuses
    the action (``authz.NotSanctioned`` is the DRF permission class for it).
    Gating publication inside ``stapel-listings`` would be a decision about
    the listings API, and that is its owner's to make.
    """
    from . import services

    return services.sanction_snapshot(
        payload["user_id"], scope=str(payload.get("scope") or "")
    )


@function("moderation.sanctions_by_users", schema=_schema("moderation.sanctions_by_users"))
def sanctions_by_users_function(payload: dict) -> dict:
    """Batch sanction state — the ``live_query`` half of the projection.

    Users with no active sanction are ABSENT from the answer rather than
    present with an empty list: that is the ``live_query`` contract in
    ``stapel_core.comm.projections.read()``, and a caller distinguishes
    "unsanctioned" from "unknown" by the key's absence.
    """
    from . import services

    out = {}
    for key in payload.get("keys") or []:
        snapshot = services.sanction_snapshot(key)
        if snapshot["sanctions"]:
            out[str(key)] = snapshot
    return out


@function("moderation.sanctions_export", schema=_schema("moderation.sanctions_export"))
def sanctions_export_function(payload: dict) -> dict:
    """Cursor-paged snapshot — the ``source_of_truth`` half of the projection.

    The response is ``{"rows": [...], "cursor": ..., "total": ...}`` and NOT
    ``{"items": ...}``. This is not a style choice: core's ``_iter_snapshot``
    reads ``resp.get("rows", [])``, so an items-shaped answer rebuilds the
    projection table to EMPTY and reports success while doing it (spec §22.1).
    Every row carries ``seq`` in unix milliseconds — the same clock as an
    Event timestamp — so a live fact arriving mid-rebuild supersedes its
    snapshot row instead of racing it.
    """
    from . import services

    return services.sanctions_export(
        cursor=payload.get("cursor"), limit=payload.get("limit")
    )


@function("moderation.case_status", schema=_schema("moderation.case_status"))
def case_status_function(payload: dict) -> dict:
    """The moderation standing of one target — the mirror of listings.status."""
    from .models import Case

    case = (
        Case.objects.filter(
            target_type=payload["target_type"], target_key=str(payload["target_key"])
        )
        .select_related("last_verdict")
        .order_by("-created_at")
        .first()
    )
    if case is None:
        return {
            "case_id": None,
            "state": "none",
            "decision": "",
            "reason_code": "",
            "decided_at": "",
        }
    verdict = case.last_verdict
    return {
        "case_id": str(case.id),
        "state": case.state,
        "decision": verdict.decision if verdict else "",
        "reason_code": verdict.reason_code if verdict else "",
        "decided_at": (
            case.resolved_at.isoformat() if case.resolved_at else ""
        ),
    }


@function("moderation.policy_disclosure", schema=_schema("moderation.policy_disclosure"))
def policy_disclosure_function(payload: dict) -> dict:
    """DSA Art. 15(1)(e) — the disclosure, generated from the registries.

    Compliance text as an artifact of code, not a prose copy that drifts out
    of sync with what the system actually does: the reasons come from the
    reason registry, the rules from the rule registry, and the automation
    facts from the settings that decide them.
    """
    from . import services

    return services.policy_disclosure(
        lang=str(payload.get("lang") or ""),
        target_type=str(payload.get("target_type") or ""),
    )


__all__ = [
    "case_status_function",
    "check_sanctions_function",
    "policy_disclosure_function",
    "sanctions_by_users_function",
    "sanctions_export_function",
    "submit_function",
]
