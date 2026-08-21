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

Wire them into a host's beat schedule::

    from stapel_moderation.tasks import get_moderation_beat_schedule

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

#: Stable names a beat schedule references (never renamed by a refactor).
SWEEP_TASK_NAME = "stapel_moderation.tasks.sweep_stale_cases"
REARM_TASK_NAME = "stapel_moderation.tasks.rearm_active_sanctions"
EXPIRE_TASK_NAME = "stapel_moderation.tasks.expire_sanctions"
PURGE_TASK_NAME = "stapel_moderation.tasks.purge_expired_cases"

BEAT_TASK_NAMES = (
    SWEEP_TASK_NAME,
    REARM_TASK_NAME,
    EXPIRE_TASK_NAME,
    PURGE_TASK_NAME,
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
    try:
        content = services.fetch_content(case.target_type, case.target_key, policy=policy)
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
    result = screener(case, content, reports=reports)

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

    Called from the ``task.failed`` subscriber. The default ``"hold"`` sends
    the case to the human queue with a ``needs_review`` verdict naming
    ``screening_unavailable``: **the human queue IS the fallback**, and
    nothing is published without a decision. ``"approve"`` and ``"reject"``
    exist because an owner is entitled to trade one risk for the other, and
    each prints a system check saying which trade this deployment made.
    """
    from . import services
    from .conf import moderation_settings
    from .models import CaseEventKind, CaseState, VerdictDecision, VerdictSource
    from .registry import REASON_SCREENING_UNAVAILABLE

    if case.state == CaseState.RESOLVED:
        return
    services._log(case, CaseEventKind.SCREEN_FAILED, error=error[:500])

    policy = (moderation_settings.ON_SCREENING_FAILURE or "hold").lower()
    if policy == "approve":
        decision = VerdictDecision.APPROVED
    elif policy == "reject":
        decision = VerdictDecision.REJECTED
    else:
        decision = VerdictDecision.NEEDS_REVIEW

    services.resolve_case(
        case,
        decision=decision,
        source=VerdictSource.POLICY_DEFAULT,
        reason_code=REASON_SCREENING_UNAVAILABLE,
        note="Automatic screening was unavailable.",
    )


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


def get_moderation_beat_schedule() -> dict:
    """Beat entries for every scheduled job, on the configured cadences."""
    from celery.schedules import crontab

    from .conf import moderation_settings

    return {
        "moderation-sweep-stale-cases": {
            "task": SWEEP_TASK_NAME,
            "schedule": crontab(**dict(moderation_settings.SWEEP_SCHEDULE or {})),
        },
        "moderation-rearm-sanctions": {
            "task": REARM_TASK_NAME,
            "schedule": crontab(**dict(moderation_settings.REARM_SCHEDULE or {})),
        },
        "moderation-expire-sanctions": {
            "task": EXPIRE_TASK_NAME,
            "schedule": crontab(**dict(moderation_settings.REARM_SCHEDULE or {})),
        },
        "moderation-retention-purge": {
            "task": PURGE_TASK_NAME,
            "schedule": crontab(**dict(moderation_settings.PURGE_SCHEDULE or {})),
        },
    }


try:  # pragma: no cover — exercised by whichever profile the host installs
    from celery import shared_task
except ImportError:
    pass
else:
    sweep_stale_cases = shared_task(name=SWEEP_TASK_NAME)(sweep_stale_cases)
    rearm_active_sanctions = shared_task(name=REARM_TASK_NAME)(rearm_active_sanctions)
    expire_sanctions = shared_task(name=EXPIRE_TASK_NAME)(expire_sanctions)
    purge_expired_cases = shared_task(name=PURGE_TASK_NAME)(purge_expired_cases)


__all__ = [
    "BEAT_TASK_NAMES",
    "EXPIRE_TASK_NAME",
    "PURGE_TASK_NAME",
    "REARM_TASK_NAME",
    "SWEEP_TASK_NAME",
    "ScreeningUnavailable",
    "apply_screening_failure",
    "expire_sanctions",
    "get_moderation_beat_schedule",
    "purge_expired_cases",
    "rearm_active_sanctions",
    "screen_case",
    "sweep_stale_cases",
]
