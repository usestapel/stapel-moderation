"""Presenters for stapel-moderation — the DTO-building layer.

Presenter discipline (SWAP001/SWAP002 in ``stapel-verify``): views NEVER
instantiate a ``dto.py`` dataclass directly. Every envelope is built by a
presenter resolved through ``get_presenter(KEY, default=...)``, so a host can
reshape any response through ``STAPEL_SWAP`` without forking this module.

The two case presenters are separate classes on purpose (see ``dto.py``): the
summary is what a queue page carries, and it has never had a complaint text
field to accidentally keep.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from stapel_core.django.api.presenters import Presenter, PresenterField
from stapel_core.django.swappable import declare_swap, get_presenter

from .dto import ContentDTO
from .models import Appeal, Case, CaseEvent, Report, Sanction, Verdict

#: Where :func:`present_case_detail` parks the resolved content envelope so
#: the presenter can read it off the DAO. The read hits another module and
#: can fail, so it is resolved by the caller (which knows the actor) rather
#: than inside the presenter — but its RESULT is a declared field of the
#: card, not something grafted onto the response afterwards.
CONTENT_ATTR = "_stapel_moderation_content"

CASE_PRESENTER_KEY = "MODERATION_CASE_PRESENTER"
DEFAULT_CASE_PRESENTER = "stapel_moderation.presenters.CasePresenter"
CASE_DETAIL_PRESENTER_KEY = "MODERATION_CASE_DETAIL_PRESENTER"
DEFAULT_CASE_DETAIL_PRESENTER = "stapel_moderation.presenters.CaseDetailPresenter"
REPORT_PRESENTER_KEY = "MODERATION_REPORT_PRESENTER"
DEFAULT_REPORT_PRESENTER = "stapel_moderation.presenters.ReportPresenter"
VERDICT_PRESENTER_KEY = "MODERATION_VERDICT_PRESENTER"
DEFAULT_VERDICT_PRESENTER = "stapel_moderation.presenters.VerdictPresenter"
EVENT_PRESENTER_KEY = "MODERATION_EVENT_PRESENTER"
DEFAULT_EVENT_PRESENTER = "stapel_moderation.presenters.CaseEventPresenter"
SANCTION_PRESENTER_KEY = "MODERATION_SANCTION_PRESENTER"
DEFAULT_SANCTION_PRESENTER = "stapel_moderation.presenters.SanctionPresenter"
APPEAL_PRESENTER_KEY = "MODERATION_APPEAL_PRESENTER"
DEFAULT_APPEAL_PRESENTER = "stapel_moderation.presenters.AppealPresenter"

declare_swap(CASE_PRESENTER_KEY, DEFAULT_CASE_PRESENTER)
declare_swap(CASE_DETAIL_PRESENTER_KEY, DEFAULT_CASE_DETAIL_PRESENTER)
declare_swap(REPORT_PRESENTER_KEY, DEFAULT_REPORT_PRESENTER)
declare_swap(VERDICT_PRESENTER_KEY, DEFAULT_VERDICT_PRESENTER)
declare_swap(EVENT_PRESENTER_KEY, DEFAULT_EVENT_PRESENTER)
declare_swap(SANCTION_PRESENTER_KEY, DEFAULT_SANCTION_PRESENTER)
declare_swap(APPEAL_PRESENTER_KEY, DEFAULT_APPEAL_PRESENTER)


def _iso(moment) -> Optional[str]:
    return moment.isoformat() if moment else None


def _content_of(dao) -> ContentDTO:
    """The content envelope parked on the DAO, or the "nobody read it" branch.

    A caller that presents a case card without resolving the content gets the
    unavailable branch with a named reason — never a silently absent key.
    """
    content = getattr(dao, CONTENT_ATTR, None)
    if content is None:
        return ContentDTO(available=False, error="not_loaded")
    return content


class CasePresenter(Presenter):
    """Presents one row of the cross-target moderator queue.

    Example:
        {
            "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
            "target_type": "listing",
            "target_key": "412",
            "scope_key": "",
            "origin": "report",
            "state": "queued",
            "severity": 3,
            "report_count": 12,
            "subject_user_id": "5cc26b64-0717-4562-b3fc-2c963f66a001",
            "claimed_by": null,
            "claimed_until": null,
            "last_decision": "",
            "first_reported_at": "2026-08-21T10:00:00+00:00",
            "created_at": "2026-08-21T10:00:00+00:00",
            "updated_at": "2026-08-21T10:05:00+00:00",
            "resolved_at": null,
            "dlq_at": null,
            "last_error_class": "",
            "last_error": "",
            "escalated_at": null
        }
    """

    model = Case
    fields = ("target_type", "target_key", "scope_key", "origin", "state", "severity")
    custom_fields = {
        "id": PresenterField(type=str, source=lambda dao: str(dao.id)),
        "report_count": PresenterField(type=int, source=lambda dao: int(dao.report_count)),
        "subject_user_id": PresenterField(
            type=Optional[str],
            source=lambda dao: str(dao.subject_user_id) if dao.subject_user_id else None,
            default=None,
            help_text="Author of the moderated content, learned from content_function.",
        ),
        "claimed_by": PresenterField(
            type=Optional[str],
            source=lambda dao: str(dao.claimed_by) if dao.claimed_by else None,
            default=None,
        ),
        "claimed_until": PresenterField(
            type=Optional[str], source=lambda dao: _iso(dao.claimed_until), default=None,
            help_text="Lease expiry; past it the case returns to the queue.",
        ),
        "last_decision": PresenterField(
            type=str,
            source=lambda dao: dao.last_verdict.decision if dao.last_verdict_id else "",
            default="",
        ),
        "first_reported_at": PresenterField(
            type=Optional[str], source=lambda dao: _iso(dao.first_reported_at), default=None
        ),
        "created_at": PresenterField(type=str, source=lambda dao: dao.created_at.isoformat()),
        "updated_at": PresenterField(type=str, source=lambda dao: dao.updated_at.isoformat()),
        "resolved_at": PresenterField(
            type=Optional[str], source=lambda dao: _iso(dao.resolved_at), default=None
        ),
        # The dead-letter triad. On the SUMMARY rather than the detail card
        # because the DLQ tab is a list view: an engineer scanning it groups
        # by error class and sorts by age, and a tab that has to open every
        # row to learn which seam broke is a tab nobody opens twice.
        "dlq_at": PresenterField(
            type=Optional[str],
            source=lambda dao: _iso(dao.dlq_at),
            default=None,
            help_text="When screening gave up on this case. Null unless state is dlq.",
        ),
        "last_error_class": PresenterField(
            type=str,
            source=lambda dao: dao.last_error_class or "",
            default="",
            help_text=(
                "Class of the last screening failure - ContentUnavailable, "
                "ScreeningUnavailable, TargetNotFound, other. Closed vocabulary; "
                "group the DLQ tab by it."
            ),
        ),
        "last_error": PresenterField(
            type=str,
            source=lambda dao: dao.last_error or "",
            default="",
            help_text="The last failure's message, truncated. For a human to read.",
        ),
        "escalated_at": PresenterField(
            type=Optional[str],
            source=lambda dao: _iso(dao.escalated_at),
            default=None,
            help_text="Set when the automatic re-screen sweep spent its cap and stopped.",
        ),
    }


class ReportPresenter(Presenter):
    """Presents one complaint to a moderator.

    Example:
        {
            "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
            "reason_code": "fraud",
            "description": "Asks for payment by bank transfer off-platform.",
            "good_faith": true,
            "reporter_id": "5cc26b64-0717-4562-b3fc-2c963f66a001",
            "created_at": "2026-08-21T10:00:00+00:00"
        }
    """

    model = Report
    fields = ("reason_code", "description", "good_faith")
    custom_fields = {
        "id": PresenterField(type=str, source=lambda dao: str(dao.id)),
        "reporter_id": PresenterField(
            type=Optional[str],
            source=lambda dao: str(dao.reporter_id) if dao.reporter_id else None,
            default=None,
            help_text="Null once the reporter's account was erased.",
        ),
        "created_at": PresenterField(type=str, source=lambda dao: dao.created_at.isoformat()),
        "evidence": PresenterField(
            type=dict,
            source=lambda dao: dict(dao.evidence or {}),
            default=dict,
            help_text=(
                "The reporter's own snapshot of a target nobody serves "
                "(evidence-based target types). Unverified by construction: "
                "render it as what the complainant says they saw."
            ),
        ),
    }


class VerdictPresenter(Presenter):
    """Presents one append-only decision.

    Example:
        {
            "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
            "decision": "rejected",
            "source": "llm",
            "reason_code": "illegal",
            "note": "Offers a controlled substance.",
            "confidence": 0.93,
            "actor_id": null,
            "model": "medium@prompt1",
            "evidence": {"excerpt": "...", "media_refs": [], "matched_rules": []},
            "created_at": "2026-08-21T10:00:00+00:00"
        }
    """

    model = Verdict
    fields = ("decision", "source", "reason_code", "note", "confidence", "model")
    custom_fields = {
        "id": PresenterField(type=str, source=lambda dao: str(dao.id)),
        "actor_id": PresenterField(
            type=Optional[str],
            source=lambda dao: str(dao.actor_id) if dao.actor_id else None,
            default=None,
            help_text="Null for a machine verdict.",
        ),
        "evidence": PresenterField(type=dict, source=lambda dao: dao.evidence or {}),
        "created_at": PresenterField(type=str, source=lambda dao: dao.created_at.isoformat()),
    }


class CaseEventPresenter(Presenter):
    """Presents one audit row. Read-only everywhere, by declaration.

    Example:
        {
            "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
            "kind": "state_changed",
            "from_state": "screening",
            "to_state": "queued",
            "actor_id": null,
            "payload": {"reason_code": "screening_unavailable"},
            "created_at": "2026-08-21T10:00:00+00:00"
        }
    """

    model = CaseEvent
    fields = ("kind", "from_state", "to_state")
    custom_fields = {
        "id": PresenterField(type=str, source=lambda dao: str(dao.id)),
        "actor_id": PresenterField(
            type=Optional[str],
            source=lambda dao: str(dao.actor_id) if dao.actor_id else None,
            default=None,
            help_text="Null means the system acted.",
        ),
        "payload": PresenterField(type=dict, source=lambda dao: dao.payload or {}),
        "created_at": PresenterField(type=str, source=lambda dao: dao.created_at.isoformat()),
    }


class SanctionPresenter(Presenter):
    """Presents one account-level consequence.

    Example:
        {
            "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
            "case_id": "9aa1e5b0-0000-4000-8000-000000000001",
            "subject_user_id": "5cc26b64-0717-4562-b3fc-2c963f66a001",
            "kind": "suspended",
            "scope": "*",
            "reason_code": "fraud",
            "note": "Third offence.",
            "state": "active",
            "starts_at": "2026-08-21T10:00:00+00:00",
            "expires_at": "2026-09-20T10:00:00+00:00",
            "issued_by": "1111e5b0-0000-4000-8000-000000000001",
            "lifted_by": null,
            "lifted_at": null
        }
    """

    model = Sanction
    fields = ("kind", "scope", "reason_code", "note", "state")
    custom_fields = {
        "id": PresenterField(type=str, source=lambda dao: str(dao.id)),
        "case_id": PresenterField(type=str, source=lambda dao: str(dao.case_id)),
        "subject_user_id": PresenterField(
            type=str, source=lambda dao: str(dao.subject_user_id)
        ),
        "starts_at": PresenterField(type=str, source=lambda dao: dao.starts_at.isoformat()),
        "expires_at": PresenterField(
            type=Optional[str], source=lambda dao: _iso(dao.expires_at), default=None,
            help_text="Null means indefinite.",
        ),
        "issued_by": PresenterField(
            type=Optional[str],
            source=lambda dao: str(dao.issued_by) if dao.issued_by else None,
            default=None,
        ),
        "lifted_by": PresenterField(
            type=Optional[str],
            source=lambda dao: str(dao.lifted_by) if dao.lifted_by else None,
            default=None,
        ),
        "lifted_at": PresenterField(
            type=Optional[str], source=lambda dao: _iso(dao.lifted_at), default=None
        ),
    }


class AppealPresenter(Presenter):
    """Presents one appeal.

    Example:
        {
            "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
            "case_id": "9aa1e5b0-0000-4000-8000-000000000001",
            "sanction_id": null,
            "appellant_id": "5cc26b64-0717-4562-b3fc-2c963f66a001",
            "body": "The item is a licensed replica, here is the certificate.",
            "state": "open",
            "resolution_note": "",
            "resolved_by": null,
            "created_at": "2026-08-21T10:00:00+00:00",
            "resolved_at": null
        }
    """

    model = Appeal
    fields = ("body", "state", "resolution_note")
    custom_fields = {
        "id": PresenterField(type=str, source=lambda dao: str(dao.id)),
        "case_id": PresenterField(type=str, source=lambda dao: str(dao.case_id)),
        "sanction_id": PresenterField(
            type=Optional[str],
            source=lambda dao: str(dao.sanction_id) if dao.sanction_id else None,
            default=None,
        ),
        "appellant_id": PresenterField(type=str, source=lambda dao: str(dao.appellant_id)),
        "resolved_by": PresenterField(
            type=Optional[str],
            source=lambda dao: str(dao.resolved_by) if dao.resolved_by else None,
            default=None,
        ),
        "created_at": PresenterField(type=str, source=lambda dao: dao.created_at.isoformat()),
        "resolved_at": PresenterField(
            type=Optional[str], source=lambda dao: _iso(dao.resolved_at), default=None
        ),
    }


class CaseDetailPresenter(Presenter):
    """Presents one case card: the case, its complaints, verdicts and content.

    ``content`` is the target's live text as read through the type's
    ``content_function``. It is resolved by the caller (which knows who is
    asking) and handed to :func:`present_case_detail`, because a read that can
    fail must be able to say so without failing the whole card — but it is a
    declared field of this DTO, so the schema carries it like any other.

    Example:
        {
            "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
            "target_type": "listing",
            "target_key": "412",
            "scope_key": "",
            "origin": "report",
            "state": "queued",
            "severity": 3,
            "report_count": 2,
            "subject_user_id": "5cc26b64-0717-4562-b3fc-2c963f66a001",
            "claimed_by": null,
            "claimed_until": null,
            "created_at": "2026-08-21T10:00:00+00:00",
            "updated_at": "2026-08-21T10:05:00+00:00",
            "resolved_at": null,
            "reports": [],
            "verdicts": [],
            "sanctions": [],
            "appeals": [],
            "content": {
                "available": true,
                "error": "",
                "text": "Genuine Rolex, cash only, meet at the station.",
                "title": "Rolex Submariner",
                "language": "en",
                "media": ["cdn://photo/1"],
                "author_id": "5cc26b64-0717-4562-b3fc-2c963f66a001",
                "url": "https://example.test/listings/412",
                "extra": {}
            }
        }
    """

    model = Case
    fields = ("target_type", "target_key", "scope_key", "origin", "state", "severity")
    custom_fields = {
        "id": PresenterField(type=str, source=lambda dao: str(dao.id)),
        "report_count": PresenterField(type=int, source=lambda dao: int(dao.report_count)),
        "subject_user_id": PresenterField(
            type=Optional[str],
            source=lambda dao: str(dao.subject_user_id) if dao.subject_user_id else None,
            default=None,
        ),
        "claimed_by": PresenterField(
            type=Optional[str],
            source=lambda dao: str(dao.claimed_by) if dao.claimed_by else None,
            default=None,
        ),
        "claimed_until": PresenterField(
            type=Optional[str], source=lambda dao: _iso(dao.claimed_until), default=None
        ),
        "created_at": PresenterField(type=str, source=lambda dao: dao.created_at.isoformat()),
        "updated_at": PresenterField(type=str, source=lambda dao: dao.updated_at.isoformat()),
        "resolved_at": PresenterField(
            type=Optional[str], source=lambda dao: _iso(dao.resolved_at), default=None
        ),
        "reports": PresenterField(
            type=ReportPresenter, many=True, source=lambda dao: dao.reports.all()
        ),
        "verdicts": PresenterField(
            type=VerdictPresenter, many=True, source=lambda dao: dao.verdicts.all()
        ),
        "sanctions": PresenterField(
            type=SanctionPresenter, many=True, source=lambda dao: dao.sanctions.all()
        ),
        "appeals": PresenterField(
            type=AppealPresenter, many=True, source=lambda dao: dao.appeals.all()
        ),
        "content": PresenterField(
            type=ContentDTO,
            source=_content_of,
            help_text=(
                "The target's live content, read when the card was opened. "
                "A failed read is a rendered state: available=false carries "
                "the reason (no_content_function, forbidden, target_not_found, "
                "not_loaded, or the unavailability message)."
            ),
        ),
    }


# ── Swap-resolved accessors (the only way views build a DTO) ─────────


def present_case(case):
    return get_presenter(CASE_PRESENTER_KEY, default=DEFAULT_CASE_PRESENTER).present(case)


def present_case_detail(case, *, content=None):
    """Present one case card. ``content`` is the already-resolved envelope."""
    if content is not None:
        setattr(case, CONTENT_ATTR, content)
    return get_presenter(
        CASE_DETAIL_PRESENTER_KEY, default=DEFAULT_CASE_DETAIL_PRESENTER
    ).present(case)


def present_report(report):
    return get_presenter(REPORT_PRESENTER_KEY, default=DEFAULT_REPORT_PRESENTER).present(report)


def present_verdict(verdict):
    return get_presenter(VERDICT_PRESENTER_KEY, default=DEFAULT_VERDICT_PRESENTER).present(verdict)


def present_event(event):
    return get_presenter(EVENT_PRESENTER_KEY, default=DEFAULT_EVENT_PRESENTER).present(event)


def present_sanction(sanction):
    return get_presenter(
        SANCTION_PRESENTER_KEY, default=DEFAULT_SANCTION_PRESENTER
    ).present(sanction)


def present_appeal(appeal):
    return get_presenter(APPEAL_PRESENTER_KEY, default=DEFAULT_APPEAL_PRESENTER).present(appeal)


def present_report_result(report):
    from .dto import ReportResultDTO

    return ReportResultDTO(
        accepted=True,
        report_id=str(report.id),
        # A short reference to quote at support, not a handle to a case the
        # reporter may not read.
        case_ref=str(report.case_id)[:8],
    )


def present_case_page(cases, *, next_before=None):
    from .dto import CasePageDTO

    return CasePageDTO(
        items=[present_case(case) for case in cases],
        next_before=next_before.isoformat() if next_before else None,
    )


def present_stats(stats: Dict[str, Any]):
    from .dto import StatsDTO

    return StatsDTO(
        by_state=stats.get("by_state") or {},
        by_target_type=stats.get("by_target_type") or {},
        by_severity=stats.get("by_severity") or {},
        open_total=int(stats.get("open_total") or 0),
        resolved_total=int(stats.get("resolved_total") or 0),
        queue_total=int(stats.get("queue_total") or 0),
        dlq_total=int(stats.get("dlq_total") or 0),
        dlq_by_error_class=stats.get("dlq_by_error_class") or {},
    )


def present_policy_disclosure(disclosure: Dict[str, Any]):
    from .dto import PolicyDisclosureDTO

    return PolicyDisclosureDTO(
        lang=disclosure.get("lang") or "",
        reasons=disclosure.get("reasons") or [],
        rules=disclosure.get("rules") or [],
        automated_means=disclosure.get("automated_means") or {},
        human_review=disclosure.get("human_review") or {},
    )


def present_rescan_result(case, task_id):
    from .dto import RescanResultDTO

    return RescanResultDTO(case_id=str(case.id), state=case.state, task_id=task_id)


def present_content(content, *, available: bool = True, error: str = ""):
    """Build the content envelope, including its failure branch.

    A failed read is a rendered state, not an exception: the console shows
    ``failed`` with the reason, and the moderator knows they are looking at a
    card with no content rather than at content that happens to be empty.
    """
    from .dto import ContentDTO

    if not available:
        return ContentDTO(available=False, error=error)
    return ContentDTO(
        available=True,
        text=content.text,
        title=content.title,
        language=content.language,
        media=[str(m) for m in content.media],
        author_id=content.author_id,
        url=content.url,
        extra=content.extra or {},
    )


__all__: List[str] = [
    "APPEAL_PRESENTER_KEY",
    "CONTENT_ATTR",
    "CASE_DETAIL_PRESENTER_KEY",
    "CASE_PRESENTER_KEY",
    "EVENT_PRESENTER_KEY",
    "REPORT_PRESENTER_KEY",
    "SANCTION_PRESENTER_KEY",
    "VERDICT_PRESENTER_KEY",
    "AppealPresenter",
    "CaseDetailPresenter",
    "CaseEventPresenter",
    "CasePresenter",
    "ReportPresenter",
    "SanctionPresenter",
    "VerdictPresenter",
    "present_appeal",
    "present_case",
    "present_case_page",
    "present_case_detail",
    "present_content",
    "present_event",
    "present_policy_disclosure",
    "present_report",
    "present_report_result",
    "present_rescan_result",
    "present_sanction",
    "present_stats",
    "present_verdict",
]
