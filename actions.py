"""Action subscriptions of stapel-moderation.

Handlers are idempotent-minded — delivery is at-least-once (outbox retries,
broker redelivery) — and the transport is chosen by ``STAPEL_COMM``, not by
the code: in-process in a monolith, a bus consumer in microservices, the same
handler body either way.

Six consumers, three of them subscriptions to this module's OWN facts. That
last part is the forms canon and it is deliberate: reacting to our own event
rather than notifying inline means the verdict row is committed before anyone
is told about it, and a notification outage can never roll back a moderation
decision.

Intake subscriptions are dynamic: every ``intake_events`` topic named by a
registered target type is subscribed at ``ready()``, so a composite that
registers ``listing`` gets ``listing.submitted`` wired and one that does not,
does not.
"""
from __future__ import annotations

import logging

from stapel_core.comm import on_action, subscribe_action

from . import events as own_events
from .services import SCREEN_TASK

logger = logging.getLogger(__name__)


# ── Intake: the host's facts ─────────────────────────────────────────


def handle_intake(event) -> None:
    """Open a case for whatever this topic says was submitted.

    Only the identifier is read. The content the event carries (``title``,
    ``description``, a chat message ``body``) is IGNORED on purpose: it is
    stale by the time a moderator opens the card, and a conversation has no
    business sitting in an outbox that has no retention. One read path, at the
    moment of reading.
    """
    from stapel_core.comm import mutate_and_emit

    from . import services
    from .models import CaseEventKind, CaseOrigin, CaseState
    from .registry import resolve_policy, target_type_for_event

    payload = event.payload or {}
    for target_type in target_type_for_event(event.event_type):
        policy = resolve_policy(target_type)
        target_key = payload.get(policy["id_field"]) or payload.get("target_key")
        if not target_key:
            logger.error(
                "moderation: %s carried no %s — no case opened",
                event.event_type,
                policy["id_field"],
            )
            continue
        with mutate_and_emit() as emit_event:
            case, created = services.open_case(
                target_type,
                str(target_key),
                origin=CaseOrigin.SUBMISSION,
                scope_key=str(payload.get("scope_key") or ""),
                emit_event=emit_event,
            )
            if created:
                services.start_screening(case, emit_event=emit_event)
            else:
                # A redelivered event, or a genuine resubmission after an
                # edit — and **the payload cannot tell them apart**. Neither
                # `listing.submitted` nor any other intake topic in the fleet
                # carries a revision token, so "re-screen on every delivery"
                # would turn an at-least-once bus into an unbounded LLM bill,
                # and could yank a case out from under the moderator holding
                # it. Both are audited; only a case still in OPEN (never
                # screened) is screened here. The explicit "look again" paths
                # are the moderator's rescan endpoint and the
                # `moderation.submit` Function — a decision, not an omission,
                # recorded as a delta on spec §5.3.
                services._log(case, CaseEventKind.RESUBMITTED, topic=event.event_type)
                if case.state == CaseState.OPEN:
                    services.start_screening(case, emit_event=emit_event)


#: Topics already wired to :func:`handle_intake`. Subscribing twice would
#: open two cases for one event, so the set is the idempotency guard.
_subscribed_topics: set = set()


def subscribe_intake_events() -> list:
    """Wire ``handle_intake`` to every topic a registered policy names.

    Called from ``apps.ready()`` for the settings layer, and AGAIN from
    ``register_target_type`` for the runtime layer. Both are needed, and the
    second is the interesting one: a target type registered after boot would
    otherwise be a policy with ``intake_events`` nobody listens to — the
    "declared but not connected" defect this module is supposed to be immune
    to, reproduced in its own extension seam.

    Returns the topics newly wired, so a caller can tell "nothing to do" from
    "wired something".
    """
    from .registry import get_target_types

    topics = sorted(
        {
            topic
            for policy in get_target_types().values()
            for topic in tuple((policy or {}).get("intake_events") or ())
        }
    )
    fresh = [topic for topic in topics if topic not in _subscribed_topics]
    for topic in fresh:
        subscribe_action(topic, handle_intake)
        _subscribed_topics.add(topic)
    return fresh


def reset_intake_subscriptions() -> None:
    """Tests only: forget which topics were wired.

    The handler stays subscribed in the action registry — dropping it there
    is the test harness's business — but the guard is cleared so a re-register
    in the next test wires again.
    """
    _subscribed_topics.clear()


# ── The screening ladder's end ───────────────────────────────────────


@on_action("task.failed")
def handle_task_failed(event) -> None:
    """A parked ``moderation.screen`` task applies ``ON_SCREENING_FAILURE``.

    The task machinery has already retried to exhaustion by the time this
    arrives; what is left is the policy decision about a case nobody could
    screen. ``correlation_id`` carries the case id — which is why
    ``start()`` is called with it.
    """
    from .models import Case
    from .tasks import apply_screening_failure

    payload = event.payload or {}
    if payload.get("kind") != SCREEN_TASK:
        return
    case_id = payload.get("correlation_id")
    if not case_id:
        logger.error("task.failed for moderation.screen without correlation_id")
        return
    case = Case.objects.filter(pk=case_id).first()
    if case is None:
        logger.warning("task.failed for unknown moderation case %s", case_id)
        return
    apply_screening_failure(case, error=str(payload.get("error") or ""))


# ── Optional acknowledgement from a target module ────────────────────


@on_action("moderation.applied")
def handle_moderation_applied(event) -> None:
    """Record that a target module confirmed it applied our verdict.

    Nobody emits this today (neither listings 0.4.0 nor reviews 0.2.0), so the
    handler is the standing half of a two-sided mechanism. It costs one
    subscription and it means the day a target module starts acking, delivery
    assurance is a setting rather than a release.
    """
    from . import services
    from .models import Case, CaseEventKind

    payload = event.payload or {}
    case = Case.objects.filter(pk=payload.get("case_id")).first()
    if case is None:
        return
    applied = payload.get("applied", True)
    services._log(
        case,
        CaseEventKind.APPLIED if applied else CaseEventKind.APPLY_FAILED,
        target_type=payload.get("target_type") or case.target_type,
    )


# ── Housekeeping ─────────────────────────────────────────────────────


@on_action("user.deleted")
def handle_user_deleted(event) -> None:
    """Erase the complainant's identity from their reports (GDPR Art. 17)."""
    from .gdpr import ModerationGDPRProvider

    user_id = (event.payload or {}).get("user_id")
    if not user_id:
        logger.error("user.deleted event without user_id: %s", event.event_id)
        return
    ModerationGDPRProvider().delete(user_id)


@on_action("staff.role.revoked")
def handle_staff_role_revoked(event) -> None:
    """Release every case leased by a moderator who just lost their role.

    Small, and exactly the class of thing that gets declared and never wired:
    without it a claimed case waits out its whole lease for somebody who can
    no longer open it.
    """
    from . import services
    from .models import Case, CaseState

    user_id = (event.payload or {}).get("user_id")
    if not user_id:
        return
    for case in Case.objects.filter(
        claimed_by=user_id, state=CaseState.CLAIMED
    ).iterator():
        try:
            services.release_case(case)
        except services.ModerationError:
            logger.exception("moderation: could not release case %s", case.id)


# ── Our own facts drive the notifications ────────────────────────────


@on_action(own_events.MODERATION_COMPLETED)
def handle_own_verdict(event) -> None:
    """Notify the content's author when their content was taken down."""
    from .models import Case
    from .notifications import notify_content_blocked

    case = Case.objects.filter(pk=(event.payload or {}).get("case_id")).first()
    if case is None:
        return
    notify_content_blocked(case, event.payload or {})


@on_action(own_events.REPORT_REVIEWED)
def handle_own_report_reviewed(event) -> None:
    """Tell a complainant their report reached a decision (DSA Art. 16(5))."""
    from .models import Report
    from .notifications import notify_report_reviewed

    payload = event.payload or {}
    report = Report.objects.filter(pk=payload.get("report_id")).first()
    if report is None:
        return
    notify_report_reviewed(report, payload.get("decision") or "")


@on_action(own_events.REPORT_RECEIVED)
def handle_own_report_received(event) -> None:
    """Acknowledge a complaint to its submitter (DSA Art. 16(4))."""
    from .models import Report
    from .notifications import notify_report_received

    report = Report.objects.filter(pk=(event.payload or {}).get("report_id")).first()
    if report is None:
        return
    notify_report_received(report)


@on_action(own_events.SANCTION_ISSUED)
def handle_own_sanction_issued(event) -> None:
    """Tell the subject what happened to their account and how to appeal."""
    from .models import Sanction
    from .notifications import notify_sanction_issued

    sanction = Sanction.objects.filter(pk=(event.payload or {}).get("sanction_id")).first()
    if sanction is None:
        return
    notify_sanction_issued(sanction)


@on_action(own_events.APPEAL_RESOLVED)
def handle_own_appeal_resolved(event) -> None:
    """Tell the appellant how their appeal came out."""
    from .models import Appeal
    from .notifications import notify_appeal_resolved

    appeal = Appeal.objects.filter(pk=(event.payload or {}).get("appeal_id")).first()
    if appeal is None:
        return
    notify_appeal_resolved(appeal)


__all__ = [
    "handle_intake",
    "handle_moderation_applied",
    "handle_own_appeal_resolved",
    "handle_own_report_received",
    "handle_own_report_reviewed",
    "handle_own_sanction_issued",
    "handle_own_verdict",
    "handle_staff_role_revoked",
    "handle_task_failed",
    "handle_user_deleted",
    "subscribe_intake_events",
]
