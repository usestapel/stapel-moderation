"""Service layer of stapel-moderation — every mutation and every fact.

Views, comm Functions, action handlers and task handlers all funnel here;
none of them writes a row of its own. Two rules hold throughout:

1. **A mutation and the fact announcing it are one transaction.** Everything
   goes through ``mutate_and_emit()``, so a verdict that is not announced
   cannot exist and an announced verdict cannot be missing.
2. **The module never calls a host back to mutate it.** Resolution emits
   ``moderation.completed`` and the target module applies it to itself (the
   reviews lesson). The action IS the fact.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from django.db import IntegrityError, transaction
from django.utils import timezone
from stapel_core.comm import mutate_and_emit

from . import events
from .models import (
    CASE_TRANSITIONS,
    OPEN_STATES,
    TERMINAL_DECISIONS,
    Appeal,
    AppealState,
    Case,
    CaseEvent,
    CaseEventKind,
    CaseOrigin,
    CaseState,
    Report,
    Sanction,
    SanctionKind,
    SanctionState,
    Verdict,
    VerdictDecision,
    VerdictSource,
)
from .registry import (
    REASON_SCREENING_UNAVAILABLE,
    UnknownReason,
    check_can_report,
    content_payload_key,
    reason_applies,
    resolve_policy,
    resolve_policy_lenient,
    resolve_reason,
)

logger = logging.getLogger(__name__)

#: comm-Task kind for the screening ladder.
SCREEN_TASK = "moderation.screen"


@contextmanager
def _emitting(emit_event):
    """Join the caller's ``mutate_and_emit`` block, or open one.

    Several services are called both standalone (an HTTP verdict) and from
    inside a bigger transaction (the screening task already holding the case
    row). Nesting ``mutate_and_emit`` would be correct but noisy at every call
    site; passing the handle down keeps "one mutation, one fact, one commit"
    true whichever way the service was entered.
    """
    if emit_event is not None:
        yield emit_event
    else:
        with mutate_and_emit() as handle:
            yield handle


# ── Errors ───────────────────────────────────────────────────────────


class ModerationError(Exception):
    """Base of every refusal this layer raises."""


class InvalidTransition(ModerationError):
    """A case was asked to move along an edge the FSM does not have."""


class CaseAlreadyResolved(ModerationError):
    """A second verdict was attempted on a resolved case."""


class CaseClaimedByAnother(ModerationError):
    """Another moderator holds a live lease on the case."""


class AlreadyReported(ModerationError):
    """This reporter already has a report against this target."""


class OwnContent(ModerationError):
    """A user tried to report their own content."""


class CannotReport(ModerationError):
    """The target policy's ``can_report`` callback said no."""


class TargetNotFound(ModerationError):
    """The target's ``content_function`` says it does not exist."""


class ContentUnavailable(ModerationError):
    """The target's content could not be read (the owner is down)."""


class InvalidDecision(ModerationError):
    """A decision word outside :class:`VerdictDecision`."""


class InvalidSanctionKind(ModerationError):
    """A sanction kind outside :class:`SanctionKind`."""


class SanctionNotActive(ModerationError):
    """Lifting a sanction that is not in force."""


class AppealNotAllowed(ModerationError):
    """The appeal cannot be opened or decided as asked."""


class SameActor(ModerationError):
    """The moderator who decided may not decide the appeal."""


# ── Content ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TargetContent:
    """What a ``*.moderation_content`` Function answered, normalized.

    The family is loose on purpose — a review has no title, a profile has no
    price — so every field has a benign empty default and only ``author_id``
    is load-bearing (it is what makes "you cannot report your own content"
    and "sanction the right person" answerable).
    """

    text: str = ""
    title: str = ""
    language: str = ""
    media: tuple = ()
    author_id: str = ""
    url: str = ""
    extra: dict = None

    @classmethod
    def from_result(cls, result) -> "TargetContent":
        result = result or {}
        known = {"text", "title", "language", "media", "author_id", "url"}
        return cls(
            text=str(result.get("text") or ""),
            title=str(result.get("title") or ""),
            language=str(result.get("language") or ""),
            media=tuple(result.get("media") or ()),
            author_id=str(result.get("author_id") or ""),
            url=str(result.get("url") or ""),
            extra={k: v for k, v in result.items() if k not in known},
        )

    def as_dict(self) -> dict:
        return {
            "text": self.text,
            "title": self.title,
            "language": self.language,
            "media": list(self.media),
            "author_id": self.author_id,
            "url": self.url,
            **(self.extra or {}),
        }


def fetch_content(target_type: str, target_key: str, *, policy: Optional[dict] = None) -> TargetContent:
    """Read a target's live content through its policy's ``content_function``.

    Not from the intake event, and not from a stored copy — the two things
    this module refuses to do. A moderator opening a case six hours later
    reads the target as it is now, and a re-screen screens what is there now.

    The payload key is the target module's OWN id name (``{"listing_id": ...}``,
    ``{"review_id": ...}``), declared per type as ``id_field``: both released
    upstreams shipped that spelling and neither built a tolerant reader
    (spec §22.2).

    "The target is gone" and "the owner could not answer" are DIFFERENT
    answers — :class:`TargetNotFound` (404) and :class:`ContentUnavailable`
    (503). Collapsing them would tell a reporter their target does not exist
    because a sibling service restarted. Telling them apart is what
    :func:`_is_not_found` does, and why it is not a one-liner.
    """
    from stapel_core.comm import CommError, call

    from .conf import moderation_settings

    policy = policy or resolve_policy(target_type)
    name = policy.get("content_function")
    if not name:
        raise ContentUnavailable(f"{target_type} declares no content_function")
    payload = {content_payload_key(policy): target_key}
    try:
        result = call(
            name, payload, timeout=float(moderation_settings.CONTENT_TIMEOUT_SECONDS)
        )
    except LookupError as exc:
        raise TargetNotFound(f"{target_type}:{target_key}") from exc
    except CommError as exc:
        # comm wraps a provider's exception in FunctionCallError, so the
        # owner's "no such row" arrives here as a transport-shaped failure.
        if _is_not_found(exc):
            raise TargetNotFound(f"{target_type}:{target_key}") from exc
        raise ContentUnavailable(str(exc)) from exc
    return TargetContent.from_result(result)


def _is_not_found(exc) -> bool:
    """Was this comm failure the owner saying "no such target"?

    Two shapes, because the two released upstreams disagree:
    ``listings.moderation_content`` raises ``LookupError`` (the documented
    contract of the ``*.moderation_content`` family) while
    ``reviews.moderation_content`` raises a bare ``ReviewNotFound(Exception)``.
    Rather than answer 503 to half the fleet, the name is accepted as
    evidence too — and the divergence is recorded as a delta on the spec, to
    be closed the honest way by ``ReviewNotFound`` subclassing ``LookupError``
    in its next minor.

    **Stated limitation.** ``__cause__`` only survives the in-process
    transport; over NATS or HTTP the owner's exception is flattened into a
    message string, so a missing target reads as an outage and the case is
    retried instead of dismissed. Retrying a target that is gone is the safe
    direction of that error (the screening task parks and a human looks),
    and closing it properly means structured error codes on comm — core's
    work, not a per-module string match on a remote message.
    """
    cause = exc.__cause__
    if cause is None:
        return False
    if isinstance(cause, LookupError):
        return True
    return type(cause).__name__.endswith("NotFound")


# ── Case lifecycle ───────────────────────────────────────────────────


def _log(case, kind, *, actor_id=None, from_state="", to_state="", **payload) -> CaseEvent:
    """Append one audit row. The only writer of :class:`CaseEvent` there is."""
    return CaseEvent.objects.create(
        case=case,
        kind=kind,
        from_state=from_state or "",
        to_state=to_state or "",
        actor_id=actor_id,
        payload=payload or {},
    )


def transition(case, to_state, *, actor_id=None, **payload) -> None:
    """Move ``case`` along a declared edge and audit it, in the caller's
    transaction. An undeclared edge raises rather than being assigned.

    This is the mechanism legacy's ``apply_moderation`` lacked: it validated a
    decision vocabulary and then assigned the status field directly, so the
    state machine existed in the docstring only.
    """
    if to_state == case.state:
        return
    allowed = CASE_TRANSITIONS.get(case.state, ())
    if to_state not in allowed:
        raise InvalidTransition(f"{case.state} -> {to_state}")
    from_state = case.state
    case.state = to_state
    case.save(update_fields=["state", "updated_at"])
    _log(
        case,
        CaseEventKind.STATE_CHANGED,
        actor_id=actor_id,
        from_state=from_state,
        to_state=to_state,
        **payload,
    )


def open_case(
    target_type: str,
    target_key: str,
    *,
    origin: str = CaseOrigin.REPORT,
    scope_key: str = "",
    severity: int = 0,
    subject_user_id=None,
    actor_id=None,
    emit_event=None,
) -> tuple[Case, bool]:
    """Find or open the live case for one target. Returns ``(case, created)``.

    The single entry point of all three intake paths (an ``intake_events``
    subscription, the ``moderation.submit`` Function, a POSTed report), and
    the place idempotency lives.

    **Idempotency is by state, not by event id.** ``select_for_update`` plus
    the partial unique constraint ``uniq_open_case_per_target`` mean a
    redelivered ``listing.submitted`` finds the open case and writes an audit
    row instead of opening a twin and starting a second screening. There is no
    processed-events table because the outbox has none, and a JSONB lookup on
    ``data__event_id`` without an index (the notifications approach) is the
    thing that does not scale.

    Must be called inside a transaction; ``emit_event`` is the caller's
    ``mutate_and_emit`` handle.
    """
    # Reporter-facing paths resolve strictly before they get here (an
    # unregistered type is a 400); a staff-opened manual case must not be
    # blocked by a de-registration (registry.resolve_policy_lenient).
    policy = (
        resolve_policy_lenient(target_type)
        if origin == CaseOrigin.MANUAL
        else resolve_policy(target_type)
    )
    existing = (
        Case.objects.select_for_update()
        .filter(target_type=target_type, target_key=target_key, state__in=OPEN_STATES)
        .first()
    )
    if existing is not None:
        if severity > existing.severity:
            existing.severity = severity
            existing.save(update_fields=["severity", "updated_at"])
        return existing, False

    case = Case(
        target_type=target_type,
        target_key=target_key,
        scope_key=scope_key or "",
        origin=origin,
        state=CaseState.OPEN,
        severity=max(int(severity), int(policy["severity_floor"])),
        subject_user_id=subject_user_id,
    )
    try:
        with transaction.atomic():
            case.save()
    except IntegrityError:
        # Lost the race against a concurrent opener. The constraint is the
        # arbiter, and the other transaction's case is the live one.
        case = (
            Case.objects.select_for_update()
            .filter(target_type=target_type, target_key=target_key, state__in=OPEN_STATES)
            .first()
        )
        if case is None:  # pragma: no cover — the constraint says otherwise
            raise
        return case, False

    _log(case, CaseEventKind.CREATED, actor_id=actor_id, origin=origin)
    events.emit_case_opened(case, emit_event=emit_event)  # emit-check: ok — open_case is documented as callable only inside the caller's mutate_and_emit block, and `emit_event` is that caller's handle
    return case, True


def start_screening(case, *, emit_event=None) -> Optional[str]:
    """Queue the screening comm-Task for ``case``, or send it straight to a
    human when automation is off for this type or this deployment.

    The task record and its ``task.requested`` fact commit **with the case**,
    so a case can neither exist unannounced nor be announced without existing
    — the guarantee legacy bought with a Kafka primary plus a Celery fallback
    plus a retry beat, here supplied by one primitive.
    """
    from stapel_core.comm import start

    from .conf import moderation_settings

    policy = resolve_policy(case.target_type)
    if not policy["screen"] or not moderation_settings.SCREEN_ENABLED:
        _queue(
            case,
            reason_code=REASON_SCREENING_UNAVAILABLE if not moderation_settings.SCREEN_ENABLED else "",
            emit_event=emit_event,
        )
        return None

    transition(case, CaseState.SCREENING)
    task_id = start(
        SCREEN_TASK,
        {"case_id": str(case.id)},
        max_attempts=int(moderation_settings.SCREEN_MAX_ATTEMPTS),
        deadline_seconds=int(moderation_settings.SCREEN_DEADLINE_SECONDS),
        correlation_id=str(case.id),
    )
    Case.objects.filter(pk=case.pk).update(screen_task_id=task_id)
    case.screen_task_id = task_id
    _log(case, CaseEventKind.SCREEN_STARTED, task_id=task_id)
    return task_id


def _queue(case, *, reason_code: str = "", actor_id=None, emit_event=None) -> None:
    """Send a case to the human queue and say so."""
    if case.state != CaseState.QUEUED:
        transition(case, CaseState.QUEUED, actor_id=actor_id, reason_code=reason_code)
    events.emit_case_queued(case, reason_code=reason_code, emit_event=emit_event)


def queue_case(case, *, reason_code: str = "", actor_id=None) -> None:
    """Public wrapper of :func:`_queue` with its own transaction."""
    with mutate_and_emit() as emit_event:
        case = Case.objects.select_for_update().get(pk=case.pk)
        _queue(case, reason_code=reason_code, actor_id=actor_id, emit_event=emit_event)


# ── Complaints ───────────────────────────────────────────────────────


def submit_report(
    *,
    target_type: str,
    target_key: str,
    reporter_id,
    reason_code: str,
    description: str = "",
    good_faith: bool = False,
    contact_email: str = "",
    scope_key: str = "",
) -> tuple[Report, Case]:
    """Accept one complaint: open or join the case, record the report, count it.

    Validation order matters and is deliberate: registry questions first (they
    cost nothing and produce the clearest 400s), then the policy callback,
    then the content read that tells us who the author is — because "you
    cannot report your own content" cannot be answered before the target has
    been resolved, and resolving it is also how a 404 is produced.
    """
    policy = resolve_policy(target_type)
    reason = resolve_reason(reason_code)
    if reason["system"] or not reason_applies(reason, target_type):
        raise UnknownReason(reason_code)
    if reason["requires_description"] and not (description or "").strip():
        raise ValueError("description_required")
    if not check_can_report(
        policy, reporter_id=reporter_id, target_type=target_type, target_key=target_key
    ):
        raise CannotReport(f"{target_type}:{target_key}")

    content = fetch_content(target_type, target_key, policy=policy)
    if content.author_id and reporter_id and str(content.author_id) == str(reporter_id):
        raise OwnContent(target_key)

    now = timezone.now()
    with mutate_and_emit() as emit_event:
        case, _created = open_case(
            target_type,
            target_key,
            origin=CaseOrigin.REPORT,
            scope_key=scope_key,
            severity=reason["severity"],
            subject_user_id=content.author_id or None,
            emit_event=emit_event,
        )
        try:
            with transaction.atomic():
                report = Report.objects.create(
                    case=case,
                    target_type=target_type,
                    target_key=target_key,
                    reporter_id=reporter_id,
                    reason_code=reason_code,
                    description=description or "",
                    good_faith=bool(good_faith),
                    contact_email=contact_email or "",
                )
        except IntegrityError as exc:
            # uniq_report_per_user — a real constraint this time, not legacy's
            # unique_together that a second table quietly sidestepped.
            raise AlreadyReported(f"{target_type}:{target_key}") from exc

        Case.objects.filter(pk=case.pk).update(
            report_count=case.report_count + 1,
            first_reported_at=case.first_reported_at or now,
            subject_user_id=case.subject_user_id or (content.author_id or None),
        )
        case.refresh_from_db()
        _log(case, CaseEventKind.REPORTED, actor_id=reporter_id, reason_code=reason_code)
        events.emit_report_received(report, case, emit_event=emit_event)

        # A brand-new case earns a screening; joining an existing one does not
        # start a second (that is the whole idempotency point).
        if case.state == CaseState.OPEN:
            start_screening(case, emit_event=emit_event)
    return report, case


# ── Verdicts ─────────────────────────────────────────────────────────


def record_verdict(
    case,
    *,
    decision: str,
    source: str,
    reason_code: str = "",
    note: str = "",
    actor_id=None,
    confidence=None,
    evidence: Optional[dict] = None,
    model: str = "",
    usage: Optional[dict] = None,
) -> Verdict:
    """Append one verdict row. Never updates, never deletes."""
    if decision not in VerdictDecision.values:
        raise InvalidDecision(decision)
    verdict = Verdict.objects.create(
        case=case,
        decision=decision,
        source=source,
        actor_id=actor_id,
        reason_code=reason_code or "",
        note=note or "",
        confidence=confidence,
        evidence=evidence or {},
        model=model or "",
        usage=usage or {},
    )
    _log(
        case,
        CaseEventKind.VERDICT,
        actor_id=actor_id,
        decision=decision,
        source=source,
        reason_code=reason_code or "",
    )
    return verdict


def resolve_case(
    case,
    *,
    decision: str,
    source: str = VerdictSource.HUMAN,
    reason_code: str = "",
    note: str = "",
    actor_id=None,
    sanction: Optional[dict] = None,
    confidence=None,
    evidence: Optional[dict] = None,
    model: str = "",
    usage: Optional[dict] = None,
    emit_event=None,
) -> Verdict:
    """Close a case with a verdict, and act on the target by announcing it.

    Order inside the one transaction (spec §7.4):

    1. append the :class:`Verdict` and its audit row;
    2. move the case to ``resolved`` (or to ``queued`` for ``needs_review``,
       which is the automation abstaining, not a resolution);
    3. optionally issue a :class:`Sanction`;
    4. emit the policy's ``verdict_event`` — **this is the action on the
       target**; the target module owns its own consumer;
    5. emit ``moderation.report.reviewed`` per report, so every complainant's
       notification has a fact to hang on.

    Notifications are NOT requested here. They are a subscriber on this
    module's own facts (the forms canon): the verdict commits before anyone is
    told, and a notification outage can never roll back a moderation decision.
    """
    if decision not in VerdictDecision.values:
        raise InvalidDecision(decision)
    if case.state == CaseState.RESOLVED:
        raise CaseAlreadyResolved(str(case.id))

    policy = resolve_policy_lenient(case.target_type)
    with _emitting(emit_event) as emit_event:
        verdict = record_verdict(
            case,
            decision=decision,
            source=source,
            reason_code=reason_code,
            note=note,
            actor_id=actor_id,
            confidence=confidence,
            evidence=evidence,
            model=model,
            usage=usage,
        )

        if decision in TERMINAL_DECISIONS:
            transition(case, CaseState.RESOLVED, actor_id=actor_id, decision=decision)
            Case.objects.filter(pk=case.pk).update(
                resolved_at=timezone.now(),
                last_verdict=verdict,
                claimed_by=None,
                claimed_until=None,
            )
            case.refresh_from_db()
        else:
            # needs_review: the machine abstained. A human decides, and the
            # case is emphatically NOT closed — legacy's stale sweeper folded
            # exactly this state into auto-approval.
            Case.objects.filter(pk=case.pk).update(last_verdict=verdict)
            case.last_verdict = verdict
            _queue(case, reason_code=reason_code, emit_event=emit_event)

        if sanction:
            issue_sanction(
                case=case,
                subject_user_id=sanction.get("subject_user_id") or case.subject_user_id,
                kind=sanction["kind"],
                reason_code=sanction.get("reason_code") or reason_code,
                note=sanction.get("note") or note,
                duration_seconds=sanction.get("duration_seconds"),
                scope=sanction.get("scope") or "*",
                issued_by=actor_id,
                emit_event=emit_event,
            )

        verdict_topic = policy["verdict_event"]
        if verdict_topic:
            events.emit_moderation_completed(  # emit-check: ok — inside the _emitting() block opened at the top of resolve_case
                case, verdict, topic=verdict_topic, emit_event=emit_event
            )
        if decision in TERMINAL_DECISIONS:
            # Only a decision that CLOSES the case has reviewed anybody's
            # report. Announcing `needs_review` here would tell every
            # complainant their report was decided when the automation had
            # just asked for a human — and it would burn the notification
            # cooldown, so the real outcome letter would never be sent.
            for report_id in case.reports.values_list("id", flat=True):
                events.emit_report_reviewed(  # emit-check: ok — inside the _emitting() block opened at the top of resolve_case
                    str(report_id), case, decision, emit_event=emit_event
                )
    return verdict


# ── Queue operations ─────────────────────────────────────────────────


def claim_case(case, *, actor_id) -> Case:
    """Take a lease on a case. A live lease held by somebody else refuses."""
    from .conf import moderation_settings

    now = timezone.now()
    with transaction.atomic():
        case = Case.objects.select_for_update().get(pk=case.pk)
        if case.state == CaseState.RESOLVED:
            raise CaseAlreadyResolved(str(case.id))
        held = (
            case.claimed_by is not None
            and str(case.claimed_by) != str(actor_id)
            and case.claimed_until is not None
            and case.claimed_until > now
        )
        if held:
            raise CaseClaimedByAnother(str(case.claimed_by))
        lease = int(moderation_settings.CLAIM_LEASE_SECONDS)
        case.claimed_by = actor_id
        case.claimed_until = now + timedelta(seconds=lease)
        case.save(update_fields=["claimed_by", "claimed_until", "updated_at"])
        if case.state != CaseState.CLAIMED:
            transition(case, CaseState.CLAIMED, actor_id=actor_id)
        _log(case, CaseEventKind.CLAIMED, actor_id=actor_id)
    return case


def release_case(case, *, actor_id=None) -> Case:
    """Give a claimed case back to the queue."""
    with transaction.atomic():
        case = Case.objects.select_for_update().get(pk=case.pk)
        if case.state != CaseState.CLAIMED:
            return case
        case.claimed_by = None
        case.claimed_until = None
        case.save(update_fields=["claimed_by", "claimed_until", "updated_at"])
        transition(case, CaseState.QUEUED, actor_id=actor_id)
        _log(case, CaseEventKind.RELEASED, actor_id=actor_id)
    return case


def rescan_case(case, *, actor_id=None) -> Optional[str]:
    """Re-screen a case: back to the automation, keeping every past verdict.

    A resolved case is reopened through the appeal edge first — a rescan of a
    decided case is a decision to look again, and the audit has to show that.
    """
    with mutate_and_emit() as emit_event:
        case = Case.objects.select_for_update().get(pk=case.pk)
        if case.state == CaseState.RESOLVED:
            transition(case, CaseState.QUEUED, actor_id=actor_id, reason="rescan")
            _log(case, CaseEventKind.REOPENED, actor_id=actor_id, reason="rescan")
        elif case.state == CaseState.CLAIMED:
            transition(case, CaseState.QUEUED, actor_id=actor_id)
        Case.objects.filter(pk=case.pk).update(claimed_by=None, claimed_until=None)
        case.refresh_from_db()
        return start_screening(case, emit_event=emit_event)


# ── Sanctions ────────────────────────────────────────────────────────


def ladder_duration(subject_user_id, kind: str) -> Optional[int]:
    """Duration for the subject's next sanction of ``kind``, from the ladder.

    The n-th sanction takes the n-th entry and the last entry repeats, so a
    two-step ladder does not silently become permanent on the third strike.
    Shaped after stapel-auth's ``LockoutService.THRESHOLDS``: progressive
    discipline is configuration, not a hardwired policy.
    """
    from .conf import moderation_settings

    ladder = (moderation_settings.SANCTION_LADDER or {}).get(kind)
    if not ladder:
        return None
    seen = Sanction.objects.filter(subject_user_id=subject_user_id, kind=kind).count()
    index = min(seen, len(ladder) - 1)
    return ladder[index]


def issue_sanction(
    *,
    case,
    subject_user_id,
    kind: str,
    reason_code: str = "",
    note: str = "",
    duration_seconds=None,
    scope: str = "*",
    issued_by=None,
    emit_event=None,
) -> Sanction:
    """Issue a sanction, announce it, and make it bite.

    "Make it bite" is core's user blacklist, which every request path already
    checks — DRF authentication, the middleware (twice, once after refresh),
    channels, and the auth refresh endpoint — and which had **no producer at
    all** until this module. Deactivating the account instead would not touch
    a single live session: ``is_active=False`` is only consulted when a new
    token is issued, so a ban would take up to an access-token lifetime to
    mean anything.

    The blacklist is a cache key with a TTL, so the row remains the truth and
    ``tasks.rearm_active_sanctions`` keeps the key alive; see MODULE.md for
    the operational warning that core fails CLOSED when that cache is down.
    """
    if kind not in SanctionKind.values:
        raise InvalidSanctionKind(kind)
    if duration_seconds is None:
        duration_seconds = ladder_duration(subject_user_id, kind)

    now = timezone.now()
    expires_at = (
        now + timedelta(seconds=int(duration_seconds)) if duration_seconds else None
    )
    with _emitting(emit_event) as emit_event:
        sanction = Sanction.objects.create(
            case=case,
            subject_user_id=subject_user_id,
            kind=kind,
            scope=scope or "*",
            reason_code=reason_code or "",
            note=note or "",
            starts_at=now,
            expires_at=expires_at,
            state=SanctionState.ACTIVE,
            issued_by=issued_by,
        )
        _log(
            case,
            CaseEventKind.SANCTIONED,
            actor_id=issued_by,
            sanction_id=str(sanction.id),
            # Not `kind=`: that is _log's own positional parameter, and the
            # collision is a TypeError rather than a shadowed payload key.
            sanction_kind=kind,
        )
        events.emit_sanction_issued(sanction, emit_event=emit_event)  # emit-check: ok — inside the _emitting() block opened at the top of issue_sanction

    apply_sanction_enforcement(sanction)
    return sanction


def issue_standalone_sanction(
    *,
    subject_user_id,
    kind: str,
    reason_code: str = "",
    note: str = "",
    duration_seconds=None,
    scope: str = "*",
    issued_by=None,
    case=None,
    target_type: str = "",
    target_key: str = "",
) -> Sanction:
    """Sanction a user outside a verdict — and still leave one audit trail.

    ``Sanction.case`` is non-null and PROTECTed, so a sanction issued from the
    console with no case in hand opens a ``manual`` one. That is the mechanism
    behind "one audit trail, no side door": there is no shape of this API in
    which a ban exists without a case, a ``CaseEvent`` and a reason.
    """
    if case is None:
        with mutate_and_emit() as emit_event:
            case, _created = open_case(
                target_type or "user",
                target_key or str(subject_user_id),
                origin=CaseOrigin.MANUAL,
                subject_user_id=subject_user_id,
                actor_id=issued_by,
                emit_event=emit_event,
            )
    return issue_sanction(
        case=case,
        subject_user_id=subject_user_id,
        kind=kind,
        reason_code=reason_code,
        note=note,
        duration_seconds=duration_seconds,
        scope=scope,
        issued_by=issued_by,
    )


def apply_sanction_enforcement(sanction) -> bool:
    """Blacklist the subject when the sanction kind calls for it.

    Deliberately OUTSIDE the transaction: the cache is not transactional, and
    a cache write that cannot be rolled back must not run before the row it
    enforces is committed. A failure here is logged, not raised — the sanction
    row is the truth and ``rearm_active_sanctions`` will set the key on its
    next pass.
    """
    from .conf import moderation_settings

    if sanction.kind not in (moderation_settings.BLACKLIST_KINDS or []):
        return False
    from stapel_core.django.jwt.authentication import blacklist_user

    ttl = int(moderation_settings.BLACKLIST_TTL_SECONDS)
    stored = blacklist_user(str(sanction.subject_user_id), ttl=ttl)
    if not stored:
        logger.error(
            "moderation: sanction %s issued but the user blacklist refused the "
            "key — the subject keeps their live sessions until rearm succeeds",
            sanction.id,
        )
    return stored


def lift_sanction(sanction, *, actor_id=None, state=SanctionState.LIFTED, note: str = "") -> Sanction:
    """Revoke an active sanction and stop enforcing it."""
    if sanction.state != SanctionState.ACTIVE:
        raise SanctionNotActive(str(sanction.id))
    now = timezone.now()
    with mutate_and_emit() as emit_event:
        sanction.state = state
        sanction.lifted_by = actor_id
        sanction.lifted_at = now
        if note:
            sanction.note = f"{sanction.note}\n{note}".strip()
        sanction.save(
            update_fields=["state", "lifted_by", "lifted_at", "note", "updated_at"]
        )
        _log(
            sanction.case,
            CaseEventKind.SANCTIONED,
            actor_id=actor_id,
            sanction_id=str(sanction.id),
            sanction_state=state,
        )
        events.emit_sanction_lifted(sanction, emit_event=emit_event)
    _clear_enforcement(sanction.subject_user_id)
    return sanction


def _clear_enforcement(subject_user_id) -> None:
    """Drop the blacklist key — but only when nothing else still warrants it.

    A user under two overlapping suspensions must not walk free because one of
    them was lifted, which is exactly the bug a bare ``unblacklist_user`` call
    would ship.
    """
    from stapel_core.django.jwt.authentication import unblacklist_user

    from .conf import moderation_settings

    kinds = list(moderation_settings.BLACKLIST_KINDS or [])
    still = active_sanctions(subject_user_id).filter(kind__in=kinds).exists()
    if not still:
        unblacklist_user(str(subject_user_id))


def active_sanctions(subject_user_id, *, scope: str = ""):
    """Live sanctions for a user, optionally narrowed to one scope.

    A sanction scoped ``"*"`` answers every scope question; a scoped one
    answers only its own. Expiry is evaluated in the query rather than trusted
    to the sweeper, so a lapsed sanction stops counting the moment it lapses
    even if the beat is late.
    """
    from django.db.models import Q

    now = timezone.now()
    qs = Sanction.objects.filter(
        subject_user_id=subject_user_id, state=SanctionState.ACTIVE
    ).filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
    if scope:
        qs = qs.filter(Q(scope="*") | Q(scope=scope))
    return qs


def sanction_snapshot(subject_user_id, *, scope: str = "") -> dict:
    """The ``{allowed, sanctions}`` answer both projection modes hand back."""
    rows = [
        {
            "kind": s.kind,
            "scope": s.scope,
            "expires_at": s.expires_at.isoformat() if s.expires_at else None,
            "reason_code": s.reason_code,
        }
        for s in active_sanctions(subject_user_id, scope=scope).order_by("-starts_at")
    ]
    blocking = {"posting_restricted", "suspended", "banned"}
    return {
        "allowed": not any(row["kind"] in blocking for row in rows),
        "sanctions": rows,
    }


# ── Appeals ──────────────────────────────────────────────────────────


def open_appeal(case, *, appellant_id, body: str, sanction=None) -> Appeal:
    """Register an appeal against a resolved case (DSA Art. 20)."""
    if case.state != CaseState.RESOLVED:
        raise AppealNotAllowed("case_not_resolved")
    with mutate_and_emit() as emit_event:
        try:
            with transaction.atomic():
                appeal = Appeal.objects.create(
                    case=case,
                    sanction=sanction,
                    appellant_id=appellant_id,
                    body=body,
                )
        except IntegrityError as exc:
            raise AppealNotAllowed("already_appealed") from exc
        _log(case, CaseEventKind.APPEALED, actor_id=appellant_id, appeal_id=str(appeal.id))
        events.emit_appeal_opened(appeal, emit_event=emit_event)
    return appeal


def resolve_appeal(
    appeal, *, outcome: str, actor_id, note: str = "", reason_code: str = ""
) -> Appeal:
    """Decide an appeal, and let an overturn actually undo the decision.

    ``APPEAL_REQUIRES_DIFFERENT_ACTOR`` (default True, DSA Art. 20 implies
    independence) refuses the moderator who decided the case. A one-moderator
    deployment turns it off knowingly rather than discovering that appeals
    silently rubber-stamp themselves.

    An overturn is not bookkeeping: the case is reopened along its single
    backward edge and re-resolved with the opposite decision, so the target
    module receives a fresh ``moderation.completed`` and un-blocks the thing.
    Without that, an appeal would be a letter, not a remedy.
    """
    from .conf import moderation_settings

    if appeal.state != AppealState.OPEN:
        raise AppealNotAllowed("already_resolved")
    if outcome not in (AppealState.UPHELD, AppealState.OVERTURNED, AppealState.WITHDRAWN):
        raise AppealNotAllowed(outcome)

    case = appeal.case
    if moderation_settings.APPEAL_REQUIRES_DIFFERENT_ACTOR and outcome != AppealState.WITHDRAWN:
        prior = (
            case.verdicts.exclude(actor_id=None)
            .values_list("actor_id", flat=True)
            .distinct()
        )
        if str(actor_id) in {str(a) for a in prior}:
            raise SameActor(str(actor_id))

    with mutate_and_emit() as emit_event:
        appeal.state = outcome
        appeal.resolved_by = actor_id
        appeal.resolution_note = note or ""
        appeal.resolved_at = timezone.now()
        appeal.save(
            update_fields=["state", "resolved_by", "resolution_note", "resolved_at"]
        )
        events.emit_appeal_resolved(appeal, emit_event=emit_event)

        if outcome == AppealState.OVERTURNED:
            case = Case.objects.select_for_update().get(pk=case.pk)
            if case.state == CaseState.RESOLVED:
                transition(case, CaseState.QUEUED, actor_id=actor_id, reason="appeal")
                _log(case, CaseEventKind.REOPENED, actor_id=actor_id, appeal_id=str(appeal.id))
            reversed_decision = _reverse(case)
            resolve_case(
                case,
                decision=reversed_decision,
                source=VerdictSource.APPEAL,
                reason_code=reason_code or "",
                note=note or "",
                actor_id=actor_id,
                emit_event=emit_event,
            )
    # Lifting the sanction is its own transaction and its own fact, after the
    # appeal has committed: the blacklist write it triggers is not
    # transactional, so it must never run before the row that justifies it.
    if outcome == AppealState.OVERTURNED and appeal.sanction_id:
        appeal.refresh_from_db()
        if appeal.sanction and appeal.sanction.state == SanctionState.ACTIVE:
            lift_sanction(
                appeal.sanction, actor_id=actor_id, state=SanctionState.OVERTURNED
            )
    return appeal


def _reverse(case) -> str:
    """The decision an overturned appeal replaces the last one with."""
    last = case.verdicts.order_by("-created_at").first()
    if last is not None and last.decision == VerdictDecision.REJECTED:
        return VerdictDecision.APPROVED
    return VerdictDecision.DISMISSED


# ── Queue reads ──────────────────────────────────────────────────────


def list_cases(
    *,
    state: str = "",
    target_type: str = "",
    reason_code: str = "",
    severity_min=None,
    scope_key: str = "",
    subject_user_id=None,
    before=None,
    limit=None,
):
    """One keyset page of the cross-target queue.

    Cross-target **by construction**: one table, one index
    ``(state, severity, -created_at)``, one ``LIMIT`` in the database. Legacy
    read two whole tables into Python on every page, ``chain()``-ed them,
    ``sorted()`` them and sliced the list — which is not a slow query, it is
    an absent one. Resolving a pk by "try the first table, then the second"
    went with it.

    Ordering is ``(-created_at)`` with severity as the leading filter rather
    than a sort key, because a keyset cursor needs a single monotone column
    and ``created_at`` is the one every row has.
    """
    from .conf import moderation_settings

    cap = int(moderation_settings.MAX_PAGE_SIZE)
    limit = max(1, min(int(limit or cap), cap))

    qs = Case.objects.all()
    if state:
        qs = qs.filter(state=state)
    if target_type:
        qs = qs.filter(target_type=target_type)
    if scope_key:
        qs = qs.filter(scope_key=scope_key)
    if subject_user_id:
        qs = qs.filter(subject_user_id=subject_user_id)
    if severity_min is not None:
        qs = qs.filter(severity__gte=int(severity_min))
    if reason_code:
        # One reason table across every target type is what makes this
        # filter safe: legacy's version silently dropped every complaint
        # about a review, because that table had no reason column at all.
        qs = qs.filter(reports__reason_code=reason_code).distinct()
    if before:
        qs = qs.filter(created_at__lt=before)
    return list(qs.select_related("last_verdict").order_by("-created_at")[:limit])


def queue_stats() -> dict:
    """Counters for the console header and DSA Art. 24(1) reporting."""
    from django.db.models import Count

    by_state = {
        row["state"]: row["n"]
        for row in Case.objects.values("state").annotate(n=Count("id"))
    }
    by_target = {
        row["target_type"]: row["n"]
        for row in Case.objects.values("target_type").annotate(n=Count("id"))
    }
    by_severity = {
        str(row["severity"]): row["n"]
        for row in Case.objects.values("severity").annotate(n=Count("id"))
    }
    return {
        "by_state": by_state,
        "by_target_type": by_target,
        "by_severity": by_severity,
        "open_total": sum(by_state.get(s, 0) for s in OPEN_STATES),
        "resolved_total": by_state.get(CaseState.RESOLVED, 0),
    }


def list_sanctions(*, subject_user_id=None, state: str = "", before=None, limit=None):
    """One keyset page of sanctions."""
    from .conf import moderation_settings

    cap = int(moderation_settings.MAX_PAGE_SIZE)
    limit = max(1, min(int(limit or cap), cap))
    qs = Sanction.objects.all()
    if subject_user_id:
        qs = qs.filter(subject_user_id=subject_user_id)
    if state:
        qs = qs.filter(state=state)
    if before:
        qs = qs.filter(created_at__lt=before)
    return list(qs.order_by("-created_at")[:limit])


def list_appeals(*, state: str = "", appellant_id=None, before=None, limit=None):
    """One keyset page of appeals."""
    from .conf import moderation_settings

    cap = int(moderation_settings.MAX_PAGE_SIZE)
    limit = max(1, min(int(limit or cap), cap))
    qs = Appeal.objects.all()
    if state:
        qs = qs.filter(state=state)
    if appellant_id:
        qs = qs.filter(appellant_id=appellant_id)
    if before:
        qs = qs.filter(created_at__lt=before)
    return list(qs.order_by("-created_at")[:limit])


def list_reports(*, reporter_id, before=None, limit=None):
    """One keyset page of a user's own complaints."""
    from .conf import moderation_settings

    cap = int(moderation_settings.MAX_PAGE_SIZE)
    limit = max(1, min(int(limit or cap), cap))
    qs = Report.objects.filter(reporter_id=reporter_id)
    if before:
        qs = qs.filter(created_at__lt=before)
    return list(qs.order_by("-created_at")[:limit])


# ── GDPR ─────────────────────────────────────────────────────────────


def erase_user_reports(user_id) -> int:
    """Detach a user from their complaints and appeals. Returns rows touched.

    The complaint SURVIVES with its reason code and its case; only the person
    is removed. That keeps ``report_count`` truthful and keeps the platform's
    own compliance record intact, which is not the erasing user's to delete.
    The free-text description goes with the identity because a complaint
    someone wrote can name them.
    """
    from django.db.models import Q

    touched = Report.objects.filter(reporter_id=user_id).update(
        reporter_id=None, description="", contact_email=""
    )
    # An appeal body is the appellant's own prose about themselves.
    touched += Appeal.objects.filter(appellant_id=user_id).filter(
        ~Q(body="")
    ).update(body="")
    return touched


# ── Read surfaces ────────────────────────────────────────────────────

#: Hard ceiling on an export page, whatever the caller asks for.
EXPORT_MAX_LIMIT = 2000


def sanctions_export(*, cursor=None, limit=None) -> dict:
    """Cursor-paged snapshot of every sanctioned user (projection rebuild).

    ``{"rows": [...], "cursor": ..., "total": ...}`` — the shape core's
    ``_iter_snapshot`` pages through. Keyset over ``subject_user_id``, so the
    snapshot stays stable under concurrent writes in a way an OFFSET never is;
    ``total`` is reported on the first page only, because a distinct count is
    not free.
    """
    from .conf import moderation_settings

    default = int(moderation_settings.EXPORT_PAGE_SIZE)
    limit = max(1, min(int(limit or default), EXPORT_MAX_LIMIT))

    now = timezone.now()
    from django.db.models import Q

    base = Sanction.objects.filter(state=SanctionState.ACTIVE).filter(
        Q(expires_at__isnull=True) | Q(expires_at__gt=now)
    )
    total = None
    if cursor is None:
        total = base.values("subject_user_id").distinct().count()
    else:
        base = base.filter(subject_user_id__gt=cursor)

    subjects = list(
        base.order_by("subject_user_id")
        .values_list("subject_user_id", flat=True)
        .distinct()[: limit + 1]
    )
    has_more = len(subjects) > limit
    subjects = subjects[:limit]

    rows = []
    for subject in subjects:
        snapshot = sanction_snapshot(subject)
        newest = (
            active_sanctions(subject).order_by("-updated_at").values_list("updated_at", flat=True).first()
        )
        rows.append(
            {
                "subject_user_id": str(subject),
                "allowed": snapshot["allowed"],
                "sanctions": snapshot["sanctions"],
                # Unix MILLISECONDS — the Event-timestamp clock (spec §22.1).
                "seq": int(newest.timestamp() * 1000) if newest else 0,
            }
        )
    return {
        "rows": rows,
        "cursor": str(subjects[-1]) if has_more and subjects else None,
        "total": total,
    }


def policy_disclosure(*, lang: str = "", target_type: str = "") -> dict:
    """Assemble the DSA Art. 15 disclosure from the registries and settings.

    Every claim in the answer is read off the mechanism that makes it true:
    whether an LLM screener is enabled, which reasons exist, which
    deterministic rules run, and what happens when the automation cannot
    answer. A prose paragraph would say the same thing until the day somebody
    changed a setting.
    """
    from .conf import moderation_settings
    from .registry import get_reasons, get_rules, rules_for_target

    reasons = get_reasons()
    if target_type:
        reasons = {
            code: entry
            for code, entry in reasons.items()
            if not entry["system"] and reason_applies(entry, target_type)
        }
        rules = rules_for_target(target_type)
    else:
        reasons = {code: entry for code, entry in reasons.items() if not entry["system"]}
        rules = [rule for _code, rule in sorted(get_rules().items())]

    on_failure = str(moderation_settings.ON_SCREENING_FAILURE or "hold")
    return {
        "lang": lang or "",
        "reasons": [
            {
                "code": entry["code"],
                "severity": entry["severity"],
                "requires_description": entry["requires_description"],
                "label_key": entry["label_key"],
                "description_key": entry["description_key"],
                "policy_clause": entry["policy_clause"],
            }
            for entry in sorted(reasons.values(), key=lambda e: e["code"])
        ],
        "rules": [
            {
                "code": rule["code"],
                "decision": rule["decision"],
                "severity": rule["severity"],
                "description_key": rule["description_key"],
            }
            for rule in rules
        ],
        "automated_means": {
            "enabled": bool(moderation_settings.SCREEN_ENABLED),
            "stages": (["rules"] if rules else []) + (
                ["llm"] if moderation_settings.SCREEN_ENABLED else []
            ),
            "model_size": moderation_settings.LLM_MODEL,
            "confidence_floor": float(moderation_settings.LLM_CONFIDENCE_FLOOR or 0),
            "on_unavailable": on_failure,
        },
        "human_review": {
            # The honest claim, computed rather than asserted: a queued case
            # is auto-resolved only when the host set a number here.
            "always_available": True,
            "auto_resolve_after_seconds": moderation_settings.AUTO_RESOLVE_STALE_QUEUE,
            "appeal_requires_different_actor": bool(
                moderation_settings.APPEAL_REQUIRES_DIFFERENT_ACTOR
            ),
        },
    }


__all__ = [
    "SCREEN_TASK",
    "AlreadyReported",
    "AppealNotAllowed",
    "CannotReport",
    "CaseAlreadyResolved",
    "CaseClaimedByAnother",
    "ContentUnavailable",
    "InvalidDecision",
    "InvalidSanctionKind",
    "InvalidTransition",
    "ModerationError",
    "OwnContent",
    "SameActor",
    "SanctionNotActive",
    "TargetContent",
    "TargetNotFound",
    "active_sanctions",
    "apply_sanction_enforcement",
    "claim_case",
    "fetch_content",
    "issue_sanction",
    "issue_standalone_sanction",
    "ladder_duration",
    "lift_sanction",
    "list_appeals",
    "list_cases",
    "list_reports",
    "list_sanctions",
    "open_appeal",
    "open_case",
    "queue_case",
    "queue_stats",
    "policy_disclosure",
    "record_verdict",
    "release_case",
    "resolve_appeal",
    "resolve_case",
    "rescan_case",
    "erase_user_reports",
    "sanction_snapshot",
    "sanctions_export",
    "start_screening",
    "submit_report",
    "transition",
]
