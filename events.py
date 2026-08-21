"""Emitted actions of stapel-moderation (transactional outbox, at-least-once).

Call sites are the service layer, always inside the mutating transaction
(outbox canon). Payload schemas live in ``schemas/emits/`` and are enforced in
tests via ``VALIDATE_SCHEMAS``.

**Ids only** (spec §5.2). A verdict fact carries the decision, the reason code
and a truncated statement of reasons — never the complaint text, never the
complainant's identity, never the model's reasoning, never ``evidence``. The
outbox has no retention and a durable bus fans out to every subscriber, so a
consumer that needs the case reads it over REST under a staff mandate.

``moderation.completed`` is deliberately NOT namespaced as
``moderation.case.completed``: it is the topic ``stapel-listings`` and
``stapel-reviews`` already consume, and the emitter does not get to rename the
contract it inherited.

Partition key is ``f"{target_type}:{target_key}"`` (the reviews shape), except
for sanctions and appeals, which key on the subject: everything about one user
must stay in order relative to everything else about that user.
"""
from __future__ import annotations

from stapel_core.comm import emit

CASE_OPENED = "moderation.case.opened"
CASE_QUEUED = "moderation.case.queued"
MODERATION_COMPLETED = "moderation.completed"
REPORT_RECEIVED = "moderation.report.received"
REPORT_REVIEWED = "moderation.report.reviewed"
SANCTION_ISSUED = "moderation.sanction.issued"
SANCTION_LIFTED = "moderation.sanction.lifted"
SANCTION_EXPIRED = "moderation.sanction.expired"
APPEAL_OPENED = "moderation.appeal.opened"
APPEAL_RESOLVED = "moderation.appeal.resolved"

#: Every topic this module produces — the list ``tests/test_comm.py`` walks to
#: prove that a schema exists for each, and that each has a call site.
EMITTED_TOPICS = (
    CASE_OPENED,
    CASE_QUEUED,
    MODERATION_COMPLETED,
    REPORT_RECEIVED,
    REPORT_REVIEWED,
    SANCTION_ISSUED,
    SANCTION_LIFTED,
    SANCTION_EXPIRED,
    APPEAL_OPENED,
    APPEAL_RESOLVED,
)


def target_key_of(case) -> str:
    """The partition key of a case's facts."""
    return f"{case.target_type}:{case.target_key}"


def emit_case_opened(case, *, emit_event=None) -> None:
    _emit(
        emit_event,
        CASE_OPENED,
        {
            "case_id": str(case.id),
            "target_type": case.target_type,
            "target_key": case.target_key,
            "scope_key": case.scope_key,
            "origin": case.origin,
            "severity": int(case.severity),
        },
        key=target_key_of(case),
    )


def emit_case_queued(case, *, reason_code: str = "", emit_event=None) -> None:
    _emit(
        emit_event,
        CASE_QUEUED,
        {
            "case_id": str(case.id),
            "target_type": case.target_type,
            "target_key": case.target_key,
            "reason_code": reason_code or "",
        },
        key=target_key_of(case),
    )


def emit_moderation_completed(case, verdict, *, topic: str = MODERATION_COMPLETED, emit_event=None) -> None:
    """The verdict fact — this IS the action on the target.

    ``topic`` comes from the target policy's ``verdict_event`` so a host can
    route one target type onto a private topic without forking; the default is
    the shared one both released consumers subscribe to.
    """
    from .conf import moderation_settings

    limit = int(moderation_settings.VERDICT_NOTE_WIRE_CHARS or 0)
    note = verdict.note or ""
    if limit and len(note) > limit:
        note = note[:limit]
    _emit(
        emit_event,
        topic,
        {
            "case_id": str(case.id),
            "target_type": case.target_type,
            "target_key": case.target_key,
            "decision": verdict.decision,
            "reason_code": verdict.reason_code or "",
            "note": note,
            "source": verdict.source,
            "decided_at": verdict.created_at.isoformat() if verdict.created_at else "",
        },
        key=target_key_of(case),
    )


def emit_report_received(report, case, *, emit_event=None) -> None:
    _emit(
        emit_event,
        REPORT_RECEIVED,
        {
            "report_id": str(report.id),
            "case_id": str(case.id),
            "target_type": case.target_type,
            "target_key": case.target_key,
            "reason_code": report.reason_code,
        },
        key=target_key_of(case),
    )


def emit_report_reviewed(report_id: str, case, decision: str, *, emit_event=None) -> None:
    _emit(
        emit_event,
        REPORT_REVIEWED,
        {
            "report_id": str(report_id),
            "case_id": str(case.id),
            "decision": decision,
        },
        key=target_key_of(case),
    )


def emit_sanction_issued(sanction, *, emit_event=None) -> None:
    _emit(
        emit_event,
        SANCTION_ISSUED,
        {
            "sanction_id": str(sanction.id),
            "case_id": str(sanction.case_id),
            "subject_user_id": str(sanction.subject_user_id),
            "kind": sanction.kind,
            "scope": sanction.scope,
            "starts_at": sanction.starts_at.isoformat() if sanction.starts_at else "",
            "expires_at": sanction.expires_at.isoformat() if sanction.expires_at else None,
            "reason_code": sanction.reason_code or "",
            # Ordering token for the user_sanctions projection: unix
            # milliseconds, the same unit and clock as an Event timestamp and
            # as the export snapshot's `seq`.
            "seq": _seq(sanction.updated_at),
        },
        key=str(sanction.subject_user_id),
    )


def emit_sanction_lifted(sanction, *, emit_event=None) -> None:
    _emit(
        emit_event,
        SANCTION_LIFTED,
        {
            "sanction_id": str(sanction.id),
            "subject_user_id": str(sanction.subject_user_id),
            "lifted_at": sanction.lifted_at.isoformat() if sanction.lifted_at else "",
            "seq": _seq(sanction.updated_at),
        },
        key=str(sanction.subject_user_id),
    )


def emit_sanction_expired(sanction, *, emit_event=None) -> None:
    _emit(
        emit_event,
        SANCTION_EXPIRED,
        {
            "sanction_id": str(sanction.id),
            "subject_user_id": str(sanction.subject_user_id),
            "expired_at": sanction.updated_at.isoformat() if sanction.updated_at else "",
            "seq": _seq(sanction.updated_at),
        },
        key=str(sanction.subject_user_id),
    )


def emit_appeal_opened(appeal, *, emit_event=None) -> None:
    _emit(
        emit_event,
        APPEAL_OPENED,
        {
            "appeal_id": str(appeal.id),
            "case_id": str(appeal.case_id),
            "appellant_id": str(appeal.appellant_id),
        },
        key=str(appeal.case_id),
    )


def emit_appeal_resolved(appeal, *, emit_event=None) -> None:
    _emit(
        emit_event,
        APPEAL_RESOLVED,
        {
            "appeal_id": str(appeal.id),
            "case_id": str(appeal.case_id),
            "outcome": appeal.state,
        },
        key=str(appeal.case_id),
    )


def _seq(moment) -> int:
    """Unix milliseconds — the projection ordering unit (spec §22.1)."""
    return int(moment.timestamp() * 1000) if moment else 0


def _emit(emit_event, topic: str, payload: dict, *, key: str) -> None:
    """Emit through the caller's ``mutate_and_emit`` handle when it has one.

    Every fact this module publishes belongs to a transaction that is writing
    a row, so the normal path is the handle; the bare ``emit`` fallback exists
    for the one caller that is already inside an ``atomic()`` opened by
    somebody else (the task handler joining the case's transaction).
    """
    if emit_event is not None:
        emit_event(topic, payload, key=key)
    else:
        emit(topic, payload, key=key)


__all__ = [
    "APPEAL_OPENED",
    "APPEAL_RESOLVED",
    "CASE_OPENED",
    "CASE_QUEUED",
    "EMITTED_TOPICS",
    "MODERATION_COMPLETED",
    "REPORT_RECEIVED",
    "REPORT_REVIEWED",
    "SANCTION_EXPIRED",
    "SANCTION_ISSUED",
    "SANCTION_LIFTED",
    "emit_appeal_opened",
    "emit_appeal_resolved",
    "emit_case_opened",
    "emit_case_queued",
    "emit_moderation_completed",
    "emit_report_received",
    "emit_report_reviewed",
    "emit_sanction_expired",
    "emit_sanction_issued",
    "emit_sanction_lifted",
    "target_key_of",
]
