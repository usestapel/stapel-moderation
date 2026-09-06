"""Scheduled and background work of stapel-moderation.

Two very different things live here, and the difference is the point:

- **``moderation.screen``** is a comm-Task handler. It is the automatic
  screening ladder, and the primitive gives it what legacy assembled by hand
  from a Kafka primary, a Celery fallback and a retry beat: a transactional
  record, an atomic claim (a redelivered request is a no-op), bounded retries
  and a FAILED park that emits ``task.failed``.
- **the beat jobs** are plain callables a cron, a systemd timer or celery beat
  can run. Not one of them ever decides a case. ``sweep_stale_cases`` returns
  expired leases and stuck screenings to the QUEUE; it does not resolve
  anything, which is exactly where legacy's ``retry_stuck_moderation`` went
  wrong — it published unmoderated listings and swept ``needs_review`` into
  auto-approval on the same pass.

Celery is OPTIONAL: each function is importable and callable on its own, and
is additionally registered as a shared task under a stable name when celery
is installed.

Wire them into a host's beat schedule — importing from ``beat``, not from
here. This module reaches ``.services`` -> ``.models`` at import time, so a
settings file that imports it raises ``AppRegistryNotReady``::

    from stapel_moderation.beat import get_moderation_beat_schedule

    CELERY_BEAT_SCHEDULE = {**get_moderation_beat_schedule(), ...}
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone
from stapel_core.comm import task_handler

# Re-exported so a caller can catch it without importing two modules, and so
# the "raise, never return" contract is visible from this file.
from .screening import ScreeningUnavailable  # noqa: F401  (re-export)
from .services import SCREEN_TASK

logger = logging.getLogger(__name__)

# The names and the schedule live in `beat.py`, which imports settings and
# nothing else — this module cannot be imported from a settings file (see
# beat.py's docstring). Re-exported so the documented path keeps working.
from .beat import (  # noqa: E402,F401  (re-export)
    BEAT_TASK_NAMES,
    EXPIRE_TASK_NAME,
    PURGE_TASK_NAME,
    REARM_TASK_NAME,
    RESCREEN_TASK_NAME,
    SWEEP_TASK_NAME,
    get_moderation_beat_schedule,
)


# ── The screening task ───────────────────────────────────────────────


@task_handler(SCREEN_TASK)
def screen_case(payload: dict) -> dict:
    """Screen one case: read live content, run the screener, record a verdict.

    Idempotent by state. A redelivered request finds the case outside
    ``{open, screening}`` and returns a no-op rather than screening twice —
    the same composite guard legacy used (``select_for_update`` plus "is it
    still pending?") and the reason its double Kafka+Celery delivery was safe.

    **Failures are raised, never returned.** That is what buys the retry
    ladder: a returned failure envelope marks the task DONE. The three shapes
    that raise :class:`~stapel_moderation.screening.ScreeningUnavailable` are
    listed in ``screening.run_llm``; ``ContentUnavailable`` (the target module
    is down) raises for the same reason. What does NOT raise is
    ``TargetNotFound``: a target that no longer exists will not exist on the
    next attempt either, so the case is dismissed instead of being retried
    into the FAILED park.
    """
    from django.db import transaction

    from . import services
    from .conf import moderation_settings
    from .models import Case, CaseState, VerdictDecision, VerdictSource
    from .registry import resolve_policy
    from .screening import get_screener

    case_id = payload["case_id"]
    with transaction.atomic():
        case = Case.objects.select_for_update().filter(pk=case_id).first()
        if case is None:
            logger.warning("moderation.screen: no case %s", case_id)
            return {"skipped": "no_case"}
        if case.state not in (CaseState.OPEN, CaseState.SCREENING):
            # Redelivery, or a human got there first.
            return {"skipped": case.state}
        if case.state == CaseState.OPEN:
            services.transition(case, CaseState.SCREENING)
        Case.objects.filter(pk=case.pk).update(
            screen_attempts=case.screen_attempts + 1
        )
        case.refresh_from_db()

    policy = resolve_policy(case.target_type)

    if not services.target_is_addressable(case):
        # A draft case: its key is a synthetic `draft:<uuid>` that names no
        # row in any module, so the content call is guaranteed to come back
        # "no such target" — and, over a flattened transport, to come back
        # looking like an outage and be retried. Asked and answered here for
        # nothing, instead of nine times over the wire.
        logger.info(
            "moderation.screen: case %s has no addressable target (%s) — "
            "closing rather than screening a key nobody owns",
            case.id,
            case.target_key,
        )
        services.close_subject_gone(case)
        return {
            "decision": VerdictDecision.DISMISSED,
            "reason": "subject_gone",
        }

    from . import metrics as _content_metrics

    try:
        content = services.fetch_content(case.target_type, case.target_key, policy=policy)
    except services.ContentUnavailable as exc:
        # Counted where it happens: this raise leaves through the retry ladder
        # and never reaches the screener's own instrumentation below, which is
        # how 207 content failures on a client stand were invisible to every
        # screening metric the module had.
        _content_metrics.record_screen_failure(case.target_type, exc)
        raise
    except services.TargetNotFound:
        # Permanent: retrying cannot conjure the target back.
        logger.info("moderation.screen: target gone for case %s", case.id)
        services.resolve_case(
            case,
            decision=VerdictDecision.DISMISSED,
            source=VerdictSource.POLICY_DEFAULT,
            reason_code="target_not_found",
            note="The moderated target no longer exists.",
        )
        return {"decision": VerdictDecision.DISMISSED, "reason": "target_not_found"}

    reports = list(case.reports.values_list("reason_code", flat=True))
    screener = get_screener()
    # Timed and counted around the screener itself: a ScreeningUnavailable
    # propagates (that is what buys the retry ladder), so the failure is
    # counted here and re-raised rather than swallowed into a return.
    import time as _time

    from . import metrics as _metrics

    _started = _time.monotonic()
    try:
        result = screener(case, content, reports=reports)
    except Exception as exc:
        _metrics.record_screen(
            case.target_type,
            _metrics.OUTCOME_UNAVAILABLE,
            seconds=_time.monotonic() - _started,
        )
        _metrics.record_screen_failure(case.target_type, exc)
        raise
    _metrics.record_screen(
        case.target_type, result.decision, seconds=_time.monotonic() - _started
    )

    # Stamped here and nowhere else: this is the one moment the module can
    # say "the content was actually looked at". ``updated_at`` cannot answer
    # it — a claim, a report or a severity bump moves that too — and
    # ``tasks.rescreen_stuck_cases`` compares it against ``resubmitted_at``
    # to decide whether an edit has outrun its last screening.
    Case.objects.filter(pk=case.pk).update(last_screened_at=timezone.now())
    case.refresh_from_db()

    excerpt_chars = int(moderation_settings.EVIDENCE_EXCERPT_CHARS)
    verdict = services.resolve_case(
        case,
        decision=result.decision,
        source=result.source,
        reason_code=result.reason_code,
        note=result.rationale,
        confidence=result.confidence,
        evidence=result.evidence(content, excerpt_chars=excerpt_chars),
        model=result.model,
        usage=result.usage,
    )
    logger.info(
        "moderation.screen: case %s -> %s (%s)", case.id, verdict.decision, verdict.source
    )
    return {
        "case_id": str(case.id),
        "decision": verdict.decision,
        "source": verdict.source,
    }


def apply_screening_failure(case, *, error: str = "") -> None:
    """Apply ``ON_SCREENING_FAILURE`` to a case whose screening was parked.

    Called from the ``task.failed`` subscriber, once the comm ladder has spent
    ``SCREEN_MAX_ATTEMPTS``.

    **``hold`` now dead-letters instead of rendering a verdict (0.7.0).** It
    still holds — the case stays open, nothing is published on the strength of
    a screener that never answered — but it holds in ``DLQ``, carrying the
    class of the last error, and it records **no verdict at all**.

    What it used to do was write ``policy_default / needs_review /
    screening_unavailable`` and put the case in the human queue. Every word of
    that was defensible in isolation and the sentence was false: no machine
    had reviewed anything, so a machine verdict saying "a person must look"
    was a decision nobody made, generated into DSA Art. 17 statements of
    reasons, and dropped into a queue where it was indistinguishable from a
    screener that had genuinely abstained. On a client stand it produced 567
    machine ``needs_review`` verdicts, 122 queue rows no moderator could act
    on, and — because the queue looked like it was filling with work — twelve
    days of a 78% screening failure rate that nothing called an incident.

    ``"approve"`` and ``"reject"`` are unchanged and still write a verdict:
    those are real decisions an owner has chosen to make in advance, they act
    on the target, and a rejection has to be appealable. Each prints a system
    check saying which trade this deployment made.
    """
    from . import services
    from .conf import moderation_settings
    from .models import CaseEventKind, CaseState, VerdictDecision, VerdictSource
    from .registry import REASON_SCREENING_UNAVAILABLE

    if case.state == CaseState.RESOLVED:
        return
    error_class = services.error_class_of(_error_class_name(error))
    services._log(
        case, CaseEventKind.SCREEN_FAILED, error=error[:500], error_class=error_class
    )

    policy = (moderation_settings.ON_SCREENING_FAILURE or "hold").lower()
    if policy == "approve":
        decision = VerdictDecision.APPROVED
    elif policy == "reject":
        decision = VerdictDecision.REJECTED
    else:
        services.dead_letter_case(case, error=error, error_class=error_class)
        return

    services.resolve_case(
        case,
        decision=decision,
        source=VerdictSource.POLICY_DEFAULT,
        reason_code=REASON_SCREENING_UNAVAILABLE,
        note="Automatic screening was unavailable.",
    )


def _error_class_name(error: str) -> str:
    """The exception name out of a parked task's error string.

    The subscriber receives ``"ContentUnavailable(\"…\")"`` — the repr of what
    the handler raised — so the class is the head of it. Read rather than
    passed because ``task.failed`` carries a string and changing that is
    core's contract, not this module's.
    """
    head = str(error or "").split("(", 1)[0].strip()
    return head.rsplit(".", 1)[-1]


# ── Beat jobs ────────────────────────────────────────────────────────


def sweep_stale_cases() -> dict:
    """Return expired claims and stuck screenings to the queue.

    **Never renders a verdict.** A case a human has not reached is a case a
    human has not reached; ``AUTO_RESOLVE_STALE_QUEUE`` exists solely so that
    "we do not do this" is a setting somebody can read, and turning it on
    prints ``moderation.W002``.
    """
    from . import services
    from .conf import moderation_settings
    from .models import Case, CaseState

    now = timezone.now()
    released = 0
    for case in Case.objects.filter(
        state=CaseState.CLAIMED, claimed_until__lt=now
    ).iterator():
        try:
            services.release_case(case)
            released += 1
        except services.ModerationError:
            logger.exception("moderation: could not release stale claim %s", case.id)

    lease = int(moderation_settings.CLAIM_LEASE_SECONDS)
    cutoff = now - timedelta(seconds=lease)
    stalled = 0
    for case in Case.objects.filter(
        state=CaseState.SCREENING, updated_at__lt=cutoff
    ).iterator():
        try:
            services.queue_case(case, reason_code="screening_stalled")
            stalled += 1
        except services.ModerationError:
            logger.exception("moderation: could not queue stalled screening %s", case.id)

    auto = moderation_settings.AUTO_RESOLVE_STALE_QUEUE
    auto_resolved = 0
    if auto:
        # Opt-in only, and loudly declared (W002). The default None path never
        # reaches here, which is the whole design of this switch.
        from .models import VerdictDecision, VerdictSource

        stale_cutoff = now - timedelta(seconds=int(auto))
        for case in Case.objects.filter(
            state=CaseState.QUEUED, updated_at__lt=stale_cutoff
        ).iterator():
            services.resolve_case(
                case,
                decision=VerdictDecision.APPROVED,
                source=VerdictSource.POLICY_DEFAULT,
                reason_code="auto_resolved_stale",
                note="Auto-resolved: AUTO_RESOLVE_STALE_QUEUE is enabled.",
            )
            auto_resolved += 1

    logger.info(
        "moderation sweep: %s lease(s) released, %s stalled screening(s) queued, "
        "%s stale case(s) auto-resolved",
        released,
        stalled,
        auto_resolved,
    )
    return {"released": released, "stalled": stalled, "auto_resolved": auto_resolved}


def rescreen_stuck_cases() -> int:
    """Hand cases nothing else can move back to the screening ladder.

    The complement of :func:`sweep_stale_cases`, which fills the human queue
    (expired leases, stalled screenings) and never drains it. Three
    populations end up parked forever on a deployment whose queue is not
    staffed — which is every deployment on day one:

    * the machine **abstained** (``needs_review``) and no human came;
    * the owner **edited** the target while its case was still open, so the
      content changed under a case that had already been screened. Today that
      edit produces one ``RESUBMITTED`` audit row and nothing else, because
      ``open_case`` dedups on ``OPEN_STATES`` and ``handle_intake``
      re-screens only from ``OPEN``;
    * the screening seam **broke** and the case was dead-lettered (0.7.0).
      That one is waiting for a repair rather than for a person, and trying
      it again is how the sweep finds out the repair has landed.

    All three are the same request — *look at this again* — so all three get
    the same answer, and it is a re-screen, never a resolution. Nothing here
    decides a case, with one exception named below. That distinction is the
    whole reason this is a second job rather than a branch inside the sweep:
    legacy's ``retry_stuck_moderation`` swept ``needs_review`` into
    auto-approval on the same pass that retried, and published unmoderated
    listings for years.

    **The exception, and it decides nothing about content.** A case whose
    ``target_key`` cannot be handed to a content function at all — a draft
    case, whose key is synthetic — is CLOSED as ``dismissed /
    subject_gone`` instead of being re-screened. Not a judgement: there is
    no content to judge and no module to instruct, and the alternative is
    what this job did until 0.7.0, which was to spend the whole cap asking
    a sibling service about a key it has never owned.

    Three guards keep it from becoming a billing loop:

    **Backoff.** Attempt *n* waits ``RESCREEN_STUCK_AFTER * 2**n``. A case
    that stays stuck costs four screenings over about a week, not one per
    tick. A resubmission skips the wait — the content genuinely changed, and
    making an owner's edit sit out an exponential window is the defect, not
    the fix.

    **Coalescing.** ``resubmitted_at`` is a timestamp, not a counter, so five
    redeliveries of one event are one re-screen. At-least-once delivery does
    not become at-least-once billing.

    **A cap.** After ``RESCREEN_MAX_ATTEMPTS`` the case is ESCALATED: marked
    once, logged once, and left alone **in whatever state it was in** — a
    dead letter stays a dead letter, a queued case stays queued. A
    permanently failing case has to be *visible*, and a job that retries it
    forever is the opposite of visible — it looks like work is happening.

    CLAIMED cases are never touched: a moderator holding the lease outranks
    the clock.
    """
    from django.db import transaction

    from . import services
    from .conf import moderation_settings
    from .models import Case, CaseEventKind, CaseState

    now = timezone.now()
    window = int(moderation_settings.RESCREEN_STUCK_AFTER or 0)
    cap = int(moderation_settings.RESCREEN_MAX_ATTEMPTS or 0)
    if window <= 0 or cap <= 0:
        return 0

    started = 0
    escalated = 0
    closed = 0
    # QUEUED and DLQ. OPEN and SCREENING are the ladder's own business,
    # CLAIMED belongs to a person, RESOLVED is done.
    #
    # DLQ is here because a dead letter is a case waiting for a REPAIR, and
    # the cheapest way to find out whether the repair happened is to try
    # again — on the same backoff and under the same cap, so a seam that
    # stays broken costs four attempts over about a week rather than one per
    # tick, and an engineer who fixes the proxy does not also have to
    # remember to empty a queue.
    sweepable = (CaseState.QUEUED, CaseState.DLQ)
    candidates = Case.objects.filter(
        state__in=sweepable, escalated_at__isnull=True
    ).order_by("updated_at")
    for case_id in list(candidates.values_list("pk", flat=True)):
        unaddressable = False
        with transaction.atomic():
            case = Case.objects.select_for_update().filter(pk=case_id).first()
            # Re-read inside the lock: the sweep is not the only writer, and a
            # moderator may have claimed or resolved it since the id list.
            if case is None or case.state not in sweepable or case.escalated_at:
                continue

            if not services.target_is_addressable(case):
                # A draft case. Re-screening it is not "trying again": it is
                # asking a sibling module about a synthetic `draft:<uuid>` key
                # it has never owned, and the answer is fixed for all time.
                # This loop asked anyway until 0.7.0 — on a client stand, 69
                # draft cases spent nine attempts each and produced 207
                # failures that every log line called an outage. Closed
                # outside the lock, because `close_subject_gone` resolves and
                # emits in a transaction of its own.
                unaddressable = True
            else:
                resubmitted = case.resubmitted_at is not None and (
                    case.last_screened_at is None
                    or case.resubmitted_at > case.last_screened_at
                )
                if case.rescreen_attempts >= cap:
                    if resubmitted:
                        # An edit is new information, not a retry of the same
                        # question — but the cap still holds, so say so rather
                        # than screening past it.
                        logger.info(
                            "moderation: case %s was resubmitted past the "
                            "re-screen cap; it stays %s for a human",
                            case.id,
                            case.state,
                        )
                    case.escalated_at = now
                    case.save(update_fields=["escalated_at", "updated_at"])
                    services._log(
                        case,
                        CaseEventKind.ESCALATED,
                        attempts=case.rescreen_attempts,
                        reason_code="rescreen_cap_reached",
                        # Which queue it is stuck IN is the first thing an
                        # operator needs: escalated-in-DLQ is an engineer's
                        # problem, escalated-in-queue is a moderator's.
                        state=case.state,
                        error_class=case.last_error_class,
                    )
                    escalated += 1
                    continue

                if not resubmitted:
                    due = case.last_screened_at or case.updated_at
                    wait = timedelta(seconds=window * (2 ** case.rescreen_attempts))
                    if due > now - wait:
                        continue

                Case.objects.filter(pk=case.pk).update(
                    rescreen_attempts=case.rescreen_attempts + 1,
                    resubmitted_at=None,
                )
                case.refresh_from_db()
                services._log(
                    case,
                    CaseEventKind.RESCREENED,
                    attempt=case.rescreen_attempts,
                    reason_code="resubmitted" if resubmitted else "stuck_in_queue",
                )

        # Outside the row lock: both calls below open their own transaction
        # and emit, and holding a select_for_update across one would serialise
        # the whole sweep behind a single screening.
        if unaddressable:
            try:
                services.close_subject_gone(case)
                closed += 1
            except services.ModerationError:
                logger.exception(
                    "moderation: could not close unaddressable case %s", case.id
                )
            continue

        try:
            services.rescan_case(case)
            started += 1
        except services.ModerationError:
            logger.exception("moderation: could not re-screen stuck case %s", case.id)

    if started or escalated or closed:
        logger.info(
            "moderation re-screen sweep: %s case(s) sent back to the ladder, "
            "%s escalated, %s closed as subject_gone",
            started,
            escalated,
            closed,
        )
    return started


def rearm_active_sanctions() -> dict:
    """Re-set the blacklist key for every sanction that should still bite.

    Core's user blacklist is a cache entry with a TTL (7200s by default), so a
    thirty-day suspension outlives its own enforcement key. The row is the
    truth; this job is what keeps the cache agreeing with it. Run it well
    inside ``BLACKLIST_TTL_SECONDS`` — the shipped cadence is every 30 minutes
    against a 2-hour TTL, which survives three missed runs.
    """
    from django.db.models import Q

    from .conf import moderation_settings
    from .models import Sanction, SanctionState
    from .services import apply_sanction_enforcement

    now = timezone.now()
    kinds = list(moderation_settings.BLACKLIST_KINDS or [])
    rows = Sanction.objects.filter(state=SanctionState.ACTIVE, kind__in=kinds).filter(
        Q(expires_at__isnull=True) | Q(expires_at__gt=now)
    )
    rearmed = sum(1 for sanction in rows.iterator() if apply_sanction_enforcement(sanction))
    logger.info("moderation rearm: %s sanction(s) re-armed", rearmed)
    return {"rearmed": rearmed}


def expire_sanctions() -> dict:
    """Flip lapsed sanctions to ``expired`` and stop enforcing them."""
    from stapel_core.comm import mutate_and_emit

    from . import events
    from .models import Sanction, SanctionState
    from .services import _clear_enforcement

    now = timezone.now()
    expired = 0
    due = Sanction.objects.filter(
        state=SanctionState.ACTIVE, expires_at__isnull=False, expires_at__lte=now
    )
    for sanction in due.iterator():
        with mutate_and_emit() as emit_event:
            sanction.state = SanctionState.EXPIRED
            sanction.save(update_fields=["state", "updated_at"])
            events.emit_sanction_expired(sanction, emit_event=emit_event)
        _clear_enforcement(sanction.subject_user_id)
        expired += 1
    logger.info("moderation expiry: %s sanction(s) expired", expired)
    return {"expired": expired}


def purge_expired_cases() -> dict:
    """Destroy resolved cases and lapsed sanctions past their retention.

    Cases go at ``RETENTION_DAYS`` (365 — a full annual reporting cycle, so a
    DSA statement of reasons stays checkable for as long as anybody may ask
    about it). Sanctions go at ``SANCTION_RETENTION_DAYS`` (1095), because the
    progressive ladder IS memory and a ladder that forgets makes every third
    offence a first one.

    A case with a surviving sanction is not deleted: ``Sanction.case`` is
    PROTECTed on purpose, so the audit trail behind a live consequence cannot
    be purged out from under it.
    """
    from .conf import moderation_settings
    from .models import Case, CaseState, Sanction, SanctionState

    now = timezone.now()
    case_cutoff = now - timedelta(days=int(moderation_settings.RETENTION_DAYS))
    sanction_cutoff = now - timedelta(
        days=int(moderation_settings.SANCTION_RETENTION_DAYS)
    )

    sanctions = Sanction.objects.filter(
        state__in=(SanctionState.EXPIRED, SanctionState.LIFTED, SanctionState.OVERTURNED),
        updated_at__lt=sanction_cutoff,
    )
    sanctions_deleted = sanctions.count()
    sanctions.delete()

    cases = Case.objects.filter(
        state=CaseState.RESOLVED, resolved_at__lt=case_cutoff, sanctions__isnull=True
    )
    cases_deleted = cases.count()
    cases.delete()

    logger.info(
        "moderation retention purge: %s case(s), %s sanction(s)",
        cases_deleted,
        sanctions_deleted,
    )
    return {"cases": cases_deleted, "sanctions": sanctions_deleted}



try:  # pragma: no cover — exercised by whichever profile the host installs
    from celery import shared_task
except ImportError:
    pass
else:
    sweep_stale_cases = shared_task(name=SWEEP_TASK_NAME)(sweep_stale_cases)
    rescreen_stuck_cases = shared_task(name=RESCREEN_TASK_NAME)(rescreen_stuck_cases)
    rearm_active_sanctions = shared_task(name=REARM_TASK_NAME)(rearm_active_sanctions)
    expire_sanctions = shared_task(name=EXPIRE_TASK_NAME)(expire_sanctions)
    purge_expired_cases = shared_task(name=PURGE_TASK_NAME)(purge_expired_cases)


__all__ = [
    "BEAT_TASK_NAMES",
    "EXPIRE_TASK_NAME",
    "PURGE_TASK_NAME",
    "REARM_TASK_NAME",
    "RESCREEN_TASK_NAME",
    "SWEEP_TASK_NAME",
    "ScreeningUnavailable",
    "apply_screening_failure",
    "expire_sanctions",
    "get_moderation_beat_schedule",
    "purge_expired_cases",
    "rearm_active_sanctions",
    "rescreen_stuck_cases",
    "screen_case",
    "sweep_stale_cases",
]
