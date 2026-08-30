"""Action subscriptions of stapel-moderation.

Handlers are idempotent-minded — delivery is at-least-once (outbox retries,
broker redelivery) — and the transport is chosen by ``STAPEL_COMM``, not by
the code: in-process in a monolith, a bus consumer in microservices, the same
handler body either way.

Seven consumers, three of them subscriptions to this module's OWN facts. That
last part is the forms canon and it is deliberate: reacting to our own event
rather than notifying inline means the verdict row is committed before anyone
is told about it, and a notification outage can never roll back a moderation
decision.

``user.deleted`` and ``user.merged`` are the two halves of one account life
cycle and say opposite things: erase the person's identity, or carry their
history to the account that absorbed them. A sanction, in particular, follows
the person — otherwise a merge would be a one-click ban-evasion route.

Intake subscriptions are dynamic: every ``intake_events`` topic named by a
registered target type is subscribed at ``ready()``, so a composite that
registers ``listing`` gets ``listing.submitted`` wired and one that does not,
does not.
"""
from __future__ import annotations

import logging

from django.core.exceptions import ValidationError

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
    try:
        ModerationGDPRProvider().delete(user_id)
    except (ValidationError, ValueError, TypeError):
        # An id that cannot address a row here names no reporter to erase.
        # Django raises ValidationError (not ValueError) for a malformed UUID,
        # and an escaping exception is a poison pill: no redelivery repairs a
        # bad id, the bus just keeps handing it back.
        logger.error(
            "user.deleted with unusable user_id %r: %s", user_id, event.event_id
        )


def _drop_duplicate_reports(from_user_id, into_user_id) -> int:
    """Remove the guest's report where the survivor already reported the same
    target, and keep ``Case.report_count`` truthful.

    ``uniq_report_per_user`` says one person reports one target once. After
    the merge they ARE one person, so one report is the correct end state and
    a blind re-point would be an ``IntegrityError`` — i.e. a poison pill.
    """
    from django.db.models import F

    from .models import Case, Report

    already = set(
        Report.objects.filter(reporter_id=into_user_id).values_list(
            "target_type", "target_key"
        )
    )
    if not already:
        return 0
    dropped = 0
    for report in Report.objects.filter(reporter_id=from_user_id):
        if (report.target_type, report.target_key) not in already:
            continue
        case_id = report.case_id
        report.delete()
        Case.objects.filter(pk=case_id, report_count__gt=0).update(
            report_count=F("report_count") - 1
        )
        dropped += 1
        logger.warning(
            "user.merged: dropped the merged-away account's duplicate report on "
            "%s:%s — the surviving account had already reported it",
            report.target_type, report.target_key,
        )
    return dropped


def _drop_duplicate_appeals(from_user_id, into_user_id) -> int:
    """Remove the guest's appeal where the survivor already appealed the same
    case (``uniq_appeal_per_case_user``). Same reasoning as reports: one
    person, one appeal per case."""
    from .models import Appeal

    already = set(
        Appeal.objects.filter(appellant_id=into_user_id).values_list(
            "case_id", flat=True
        )
    )
    if not already:
        return 0
    dropped = Appeal.objects.filter(
        appellant_id=from_user_id, case_id__in=list(already)
    ).delete()[0]
    if dropped:
        logger.warning(
            "user.merged: dropped %s duplicate appeal(s) of the merged-away "
            "account — the surviving account had already appealed those cases",
            dropped,
        )
    return dropped


@on_action("user.merged")
def handle_user_merged(event) -> None:
    """Carry a merged-away account's moderation history to the survivor.

    Re-points every column this module keys by a user, in one transaction:

    * :class:`~stapel_moderation.models.Case` ``subject_user_id`` (whose
      content is under review) and ``claimed_by`` (the moderator's lease);
    * :class:`~stapel_moderation.models.Report` ``reporter_id``;
    * :class:`~stapel_moderation.models.Verdict` ``actor_id`` and
      :class:`~stapel_moderation.models.CaseEvent` ``actor_id`` — the audit
      trail keeps naming who decided;
    * :class:`~stapel_moderation.models.Sanction` ``subject_user_id``,
      ``issued_by`` and ``lifted_by``;
    * :class:`~stapel_moderation.models.Appeal` ``appellant_id`` and
      ``resolved_by``.

    **A sanction follows the person.** That is the load-bearing half. If a
    guest account under a ban could shed it by signing in with an
    authenticator an existing account holds, "merge" would be a one-click
    ban-evasion route; the progressive ladder's memory would reset with it.
    Carrying the row over is also what the ``user.merged`` contract asks for —
    every row this module owns, reassigned.

    :class:`~stapel_moderation.models.UserSanctionState` is deliberately NOT
    rewritten: it is the read-model of the ``moderation.user_sanctions``
    projection, written only by the projection runner from the owner's own
    facts. Instead, every sanction actually carried over is re-announced with
    ``moderation.sanction.issued`` inside the same transaction, which is a new
    and true fact about the survivor — the projection's ``apply`` recomputes
    that user's whole row from this module's tables, so a split topology
    learns about the carried ban rather than answering ``allowed`` from a row
    nobody refreshed. ``updated_at`` is advanced in the same update so the
    projection's ``seq`` ordering token moves forward and the announcement is
    not discarded as stale.

    Two uniqueness constraints make a blind re-point an ``IntegrityError``,
    which on this bus is a poison pill. Both resolve to "they are one person
    now, so one row is correct": the guest's duplicate report on a target the
    survivor already reported is dropped (and ``Case.report_count``
    decremented so the count stays truthful), as is the guest's duplicate
    appeal on a case the survivor already appealed. Both are logged.

    There is no ``MergeTargetNotReady`` here and there cannot be one: every
    actor in this module is a bare ``UUIDField``, never an FK to
    ``AUTH_USER_MODEL`` (models.py house rules), precisely so a moderation
    record survives the account it is about. Nothing has to exist locally for
    the survivor before their id can be written.

    Idempotent: a redelivery finds nothing left under the guest, reports zero
    rows and emits nothing.
    """
    from django.utils import timezone
    from stapel_core.comm import mutate_and_emit

    from .models import (
        Appeal,
        Case,
        CaseEvent,
        Report,
        Sanction,
        SanctionState,
        Verdict,
    )

    payload = event.payload or {}
    from_user_id = payload.get("from_user_id")
    into_user_id = payload.get("into_user_id")
    if not from_user_id or not into_user_id:
        logger.error(
            "user.merged without from/into user id: %s",
            getattr(event, "event_id", "?"),
        )
        return
    if str(from_user_id) == str(into_user_id):
        return

    #: model -> the column naming a user on it.
    owned = (
        (Case, "subject_user_id"),
        (Case, "claimed_by"),
        (Report, "reporter_id"),
        (Verdict, "actor_id"),
        (CaseEvent, "actor_id"),
        (Sanction, "subject_user_id"),
        (Sanction, "issued_by"),
        (Sanction, "lifted_by"),
        (Appeal, "appellant_id"),
        (Appeal, "resolved_by"),
    )

    with mutate_and_emit() as emit_event:
        # Both reads and the decision they feed happen inside the transaction
        # and before the first write, so a malformed id can never leave half
        # the history moved.
        try:
            owns_something = any(
                model.objects.filter(**{column: from_user_id}).exists()
                for model, column in owned
            )
            # The *into* id is coerced here, under the same guard: this module
            # holds no FK to the user table, so a probe against one of its own
            # columns is the only way to find out whether the survivor's id can
            # address a row at all — and a malformed one must not escape as a
            # poison pill either.
            Sanction.objects.filter(subject_user_id=into_user_id).exists()
        except (ValidationError, ValueError, TypeError):
            # Django raises ValidationError (not ValueError) for a malformed
            # UUID; an id that cannot address a row here names nothing, and an
            # escaping exception is a poison pill no redelivery repairs.
            logger.error(
                "user.merged with unusable user ids: %s",
                getattr(event, "event_id", "?"),
            )
            return
        if not owns_something:
            # Nothing to carry: the guest never appeared in a case here, or a
            # previous delivery already moved everything. Quiet by design —
            # this is also the at-least-once idempotency path.
            return

        dropped_reports = _drop_duplicate_reports(from_user_id, into_user_id)
        dropped_appeals = _drop_duplicate_appeals(from_user_id, into_user_id)

        carried_sanction_ids = list(
            Sanction.objects.filter(
                subject_user_id=from_user_id, state=SanctionState.ACTIVE
            ).values_list("id", flat=True)
        )

        moved = {}
        for model, column in owned:
            extra = (
                {"updated_at": timezone.now()}
                if (model is Sanction and column == "subject_user_id")
                else {}
            )
            moved[f"{model.__name__}.{column}"] = model.objects.filter(
                **{column: from_user_id}
            ).update(**{column: into_user_id}, **extra)

        # The read-model answers "is this user allowed?" per user id, and the
        # rows above moved without announcing anything. Re-announce each
        # carried sanction so the projection recomputes the SURVIVOR's row —
        # otherwise a split topology keeps saying they are unsanctioned.
        for sanction in Sanction.objects.filter(id__in=carried_sanction_ids):
            own_events.emit_sanction_issued(sanction, emit_event=emit_event)

    logger.info(
        "user.merged %s -> %s: moderation history carried over (%s); "
        "%s duplicate report(s) and %s duplicate appeal(s) dropped; "
        "%s active sanction(s) re-announced",
        from_user_id, into_user_id, moved,
        dropped_reports, dropped_appeals, len(carried_sanction_ids),
    )


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
    try:
        leased = list(
            Case.objects.filter(claimed_by=user_id, state=CaseState.CLAIMED)
        )
    except (ValidationError, ValueError, TypeError):
        # A malformed id leases nothing here; raising would loop the event.
        logger.error(
            "staff.role.revoked with unusable user_id %r: %s",
            user_id, event.event_id,
        )
        return
    for case in leased:
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

    try:
        case = Case.objects.filter(pk=(event.payload or {}).get("case_id")).first()
    except (ValidationError, ValueError, TypeError):
        case = None
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
    "handle_user_merged",
    "subscribe_intake_events",
]
