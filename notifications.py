"""Outbound notifications of stapel-moderation.

Five letters, all through ``stapel_core.notifications.request_notification``:
the module owns no template, no SMTP, no push certificate and no channel. The
types are registered upstream in ``stapel-notifications`` 0.14.0
(``moderation.report_received``, ``moderation.sanction_issued``,
``moderation.appeal_resolved``, plus the extended ``listing_blocked`` and the
existing ``report_reviewed``), so a deployment on that version needs no
bridging entries at all.

**The variable-name trap.** A caller variable whose name collides with a short
translation key (``heading``, ``body``, ``cta``, ``warning``, ``subject``,
``push_title``, ``push_body``, ``footer_*``) is SILENTLY DROPPED by the
notifications merge. Every name below is chosen to dodge it — ``reason_label``
rather than ``warning``, ``appeal_note`` rather than ``body`` — and that is
the reason for the spelling, not house style.

**The cooldown.** Forty reports about one listing must not become forty
letters to its author. One notification per (type, recipient) per
``NOTIFY_COOLDOWN_SECONDS``; the gate is ``cache.add``, the atomic
compare-and-set, so concurrent verdicts still produce one letter.

Failures are logged, never raised: by the time any of this runs the decision
is committed, and a notification service being down is not a reason to lose a
moderation verdict.
"""
from __future__ import annotations

import logging

from django.core.cache import cache

logger = logging.getLogger(__name__)

#: Types this module requests. The first two already existed and had no
#: producer in the whole fleet until now; the last three arrived in
#: stapel-notifications 0.14.0 for exactly this module.
TYPE_REPORT_REVIEWED = "report_reviewed"
TYPE_LISTING_BLOCKED = "listing_blocked"
TYPE_REPORT_RECEIVED = "moderation.report_received"
TYPE_SANCTION_ISSUED = "moderation.sanction_issued"
TYPE_APPEAL_RESOLVED = "moderation.appeal_resolved"

REQUESTED_TYPES = (
    TYPE_REPORT_RECEIVED,
    TYPE_REPORT_REVIEWED,
    TYPE_LISTING_BLOCKED,
    TYPE_SANCTION_ISSUED,
    TYPE_APPEAL_RESOLVED,
)

_COOLDOWN_KEY = "stapel_moderation:notify:{kind}:{recipient}"


def appeal_url(case_id) -> str:
    """The appeal link for a decision, or ``""`` when the host set no template.

    Empty rather than invented (the listings ``LISTING_URL_TEMPLATE``
    precedent): a "how to appeal" link that 404s is worse than no link, and
    DSA Art. 17 is satisfied by a real address or not at all.
    """
    from .conf import moderation_settings

    template = moderation_settings.APPEAL_URL_TEMPLATE or ""
    return template.format(case_id=case_id) if template else ""


def reason_label_key(reason_code: str) -> str:
    """Translation key for a reason, resolved through the registry."""
    from .registry import UnknownReason, resolve_reason

    if not reason_code:
        return ""
    try:
        return resolve_reason(reason_code)["label_key"]
    except UnknownReason:
        return f"moderation.reason.{reason_code}.label"


def notify_report_received(report) -> bool:
    """DSA Art. 16(4) — acknowledge the complaint to whoever filed it."""
    if not report.reporter_id and not report.contact_email:
        return False
    return _request(
        TYPE_REPORT_RECEIVED,
        user_id=str(report.reporter_id) if report.reporter_id else None,
        email=report.contact_email or None,
        variables={
            "target_label": f"{report.target_type}:{report.target_key}",
            "case_ref": str(report.case_id)[:8],
        },
        cooldown_key=f"received:{report.id}",
    )


def notify_report_reviewed(report, decision: str) -> bool:
    """DSA Art. 16(5) — tell the complainant their report was decided.

    The upstream body used to say "we have taken action" unconditionally,
    which was a lie whenever the verdict was ``dismissed``. We now HAVE a
    decision word, so it travels as a variable and the 0.14.0 template can
    say the true thing.
    """
    if not report.reporter_id:
        return False
    return _request(
        TYPE_REPORT_REVIEWED,
        user_id=str(report.reporter_id),
        variables={"case_ref": str(report.case_id)[:8], "outcome_label": decision},
        cooldown_key=f"reviewed:{report.id}",
    )


def notify_content_blocked(case, payload: dict) -> bool:
    """DSA Art. 17 — a statement of reasons to the author of removed content.

    The type comes from the target policy's ``notification_types`` map, so a
    ``listing`` gets ``listing_blocked`` and another target type gets whatever
    the host registered — or nothing at all, when the map says ``None``. That
    is a policy statement, not an omission.
    """
    from .registry import resolve_policy

    if payload.get("decision") != "rejected":
        return False
    if not case.subject_user_id:
        return False
    policy = resolve_policy(case.target_type)
    notification_type = (policy["notification_types"] or {}).get("content_blocked")
    if not notification_type:
        return False

    title = case.target_key
    try:
        content = _safe_content(case, policy)
        title = content.title or content.text[:80] or case.target_key
    except Exception:  # noqa: BLE001 — a letter is not worth a failed read
        logger.info("moderation: could not read content title for case %s", case.id)

    return _request(
        notification_type,
        user_id=str(case.subject_user_id),
        variables={
            "listing_title": title,
            "reason_label": reason_label_key(payload.get("reason_code") or ""),
            "appeal_url": appeal_url(case.id),
        },
        cooldown_key=f"blocked:{case.subject_user_id}",
    )


def notify_sanction_issued(sanction) -> bool:
    """Tell the subject what their account got, and how to contest it."""
    expires = (
        sanction.expires_at.isoformat() if sanction.expires_at else ""
    )
    return _request(
        TYPE_SANCTION_ISSUED,
        user_id=str(sanction.subject_user_id),
        variables={
            "sanction_kind": sanction.kind,
            "reason_label": reason_label_key(sanction.reason_code),
            "expires_label": expires,
            "appeal_url": appeal_url(sanction.case_id),
        },
        cooldown_key=f"sanction:{sanction.subject_user_id}",
    )


def notify_appeal_resolved(appeal) -> bool:
    """Tell the appellant the outcome (and, when there is one, the reason)."""
    return _request(
        TYPE_APPEAL_RESOLVED,
        user_id=str(appeal.appellant_id),
        variables={
            "outcome_label": appeal.state,
            "appeal_note": appeal.resolution_note or "",
        },
        cooldown_key=f"appeal:{appeal.id}",
    )


def _safe_content(case, policy):
    from . import services

    return services.fetch_content(case.target_type, case.target_key, policy=policy)


def _request(
    notification_type: str,
    *,
    user_id=None,
    email=None,
    variables: dict,
    cooldown_key: str,
) -> bool:
    """One request, behind the per-recipient cooldown. Never raises."""
    from stapel_core.notifications import request_notification

    from .conf import moderation_settings

    cooldown = int(moderation_settings.NOTIFY_COOLDOWN_SECONDS or 0)
    if cooldown > 0:
        key = _COOLDOWN_KEY.format(kind=notification_type, recipient=cooldown_key)
        # add() is the atomic compare-and-set: only the caller that wins it
        # sends, so a burst of verdicts is one letter, not a burst of letters.
        if not cache.add(key, 1, cooldown):
            logger.debug(
                "moderation: %s to %s suppressed by cooldown",
                notification_type,
                cooldown_key,
            )
            return False
    try:
        return bool(
            request_notification(
                notification_type,
                user_id=user_id,
                email=email,
                variables=variables,
                source_service="moderation",
            )
        )
    except Exception:  # noqa: BLE001 — the verdict is committed; delivery is best-effort
        logger.exception(
            "moderation: could not request %s notification", notification_type
        )
        return False


__all__ = [
    "REQUESTED_TYPES",
    "TYPE_APPEAL_RESOLVED",
    "TYPE_LISTING_BLOCKED",
    "TYPE_REPORT_RECEIVED",
    "TYPE_REPORT_REVIEWED",
    "TYPE_SANCTION_ISSUED",
    "appeal_url",
    "notify_appeal_resolved",
    "notify_content_blocked",
    "notify_report_received",
    "notify_report_reviewed",
    "notify_sanction_issued",
    "reason_label_key",
]
