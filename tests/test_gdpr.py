"""GDPR: erase the person, keep the platform's own compliance record.

Spec §14 assertion 12. The asymmetry with stapel-forms is the whole point and
is argued in ``gdpr.py``: a form answer is the respondent's data, a moderation
case is a record the platform produced about a piece of content under a legal
obligation. Deleting a case because the complainant closed their account would
destroy the platform's own evidence at a stranger's request.
"""
import pytest
from stapel_core.bus import Event
from stapel_core.comm import deliver, mutate_and_emit

from stapel_moderation import services
from stapel_moderation.models import Appeal, Case, Report, SanctionKind, Verdict

pytestmark = pytest.mark.django_db


def test_erasure_nulls_the_reporter_and_keeps_the_case(
    content_double, llm_double, user, other_user
):
    """Assertion 12."""
    llm_double["envelope"]["result"]["decision"] = "needs_review"
    services.submit_report(
        target_type="listing",
        target_key="42",
        reporter_id=user.pk,
        reason_code="harassment",
        description="They will not stop.",
        contact_email="alice@example.test",
    )
    services.submit_report(
        target_type="listing",
        target_key="42",
        reporter_id=other_user.pk,
        reason_code="spam",
    )
    case = Case.objects.get()
    assert case.report_count == 2

    deliver(
        Event(
            event_type="user.deleted",
            service="auth",
            payload={"user_id": str(user.pk)},
        )
    )

    erased = Report.objects.get(reporter_id=None)
    assert erased.description == ""
    assert erased.contact_email == ""
    # The reason code survives: it is a fact about the content, not about
    # the person who noticed it.
    assert erased.reason_code == "harassment"

    case.refresh_from_db()
    assert Case.objects.count() == 1
    assert Verdict.objects.filter(case=case).exists()
    # The count stays truthful. "40 reports, 3 from erased accounts" is a
    # better fact than a count that silently shrinks.
    assert case.report_count == 2


def test_erasure_is_idempotent(content_double, llm_double, user):
    llm_double["envelope"]["result"]["decision"] = "needs_review"
    services.submit_report(
        target_type="listing", target_key="42", reporter_id=user.pk, reason_code="spam"
    )
    from stapel_moderation.gdpr import ModerationGDPRProvider

    provider = ModerationGDPRProvider()
    provider.delete(user.pk)
    provider.delete(user.pk)  # at-least-once redelivery must be harmless
    assert Report.objects.filter(reporter_id=None).count() == 1


def test_an_appeal_body_is_erased_with_its_author(
    content_double, llm_double, ts_lead, author_user
):
    with mutate_and_emit() as emit_event:
        case, _ = services.open_case(
            "listing",
            "42",
            origin="submission",
            subject_user_id=author_user.pk,
            emit_event=emit_event,
        )
    services.resolve_case(case, decision="rejected", actor_id=ts_lead.pk)
    case.refresh_from_db()
    services.open_appeal(
        case, appellant_id=author_user.pk, body="My name is Carol and I live at..."
    )

    from stapel_moderation.gdpr import ModerationGDPRProvider

    ModerationGDPRProvider().delete(author_user.pk)

    appeal = Appeal.objects.get()
    assert appeal.body == ""
    # The appeal itself survives: it is part of the Art. 20 record.
    assert appeal.state == "open"


def test_the_export_returns_all_three_roles(
    content_double, llm_double, ts_lead, user, author_user
):
    """A user is a complainant, a subject and an appellant, and all three are
    theirs to see. Cases about their content are NOT dumped whole: a case card
    holds other people's complaints, which are not this user's data."""
    llm_double["envelope"]["result"]["decision"] = "needs_review"
    services.submit_report(
        target_type="listing", target_key="42", reporter_id=user.pk, reason_code="spam"
    )
    case = Case.objects.get()
    services.issue_sanction(
        case=case,
        subject_user_id=user.pk,
        kind=SanctionKind.WARNING,
        reason_code="spam",
        issued_by=ts_lead.pk,
    )

    from stapel_moderation.gdpr import ModerationGDPRProvider

    export = ModerationGDPRProvider().export(user.pk)
    assert set(export) == {"reports", "sanctions", "appeals"}
    assert len(export["reports"]) == 1
    assert len(export["sanctions"]) == 1
    assert export["reports"][0]["reason_code"] == "spam"


def test_a_sanction_survives_its_subject_being_erased(
    content_double, llm_double, ts_lead, author_user
):
    """Stated rather than papered over (gdpr.py): dropping the subject id
    would both unban the person and destroy the ladder's memory. A host with
    a legal basis lifts the sanction first."""
    with mutate_and_emit() as emit_event:
        case, _ = services.open_case(
            "listing", "42", origin="submission", emit_event=emit_event
        )
    services.issue_sanction(
        case=case,
        subject_user_id=author_user.pk,
        kind=SanctionKind.BANNED,
        issued_by=ts_lead.pk,
    )

    from stapel_moderation.gdpr import ModerationGDPRProvider

    ModerationGDPRProvider().delete(author_user.pk)

    from stapel_moderation.models import Sanction

    assert Sanction.objects.filter(subject_user_id=author_user.pk).exists()


def test_the_provider_is_registered_under_its_section():
    from stapel_core.gdpr import gdpr_registry

    assert "moderation" in gdpr_registry.sections


# ── Retention ────────────────────────────────────────────────────────


def test_the_purge_respects_two_different_clocks(
    content_double, llm_double, ts_lead, author_user
):
    """Cases 365 days, sanctions 1095: the progressive ladder IS memory, and
    a ladder that forgets makes every third offence a first one."""
    from django.utils import timezone

    from stapel_moderation.models import Sanction, SanctionState
    from stapel_moderation.tasks import purge_expired_cases

    with mutate_and_emit() as emit_event:
        old_case, _ = services.open_case(
            "listing", "1", origin="submission", emit_event=emit_event
        )
    services.resolve_case(old_case, decision="approved", actor_id=ts_lead.pk)
    Case.objects.filter(pk=old_case.pk).update(
        resolved_at=timezone.now() - timezone.timedelta(days=400)
    )

    with mutate_and_emit() as emit_event:
        kept_case, _ = services.open_case(
            "listing", "2", origin="submission", emit_event=emit_event
        )
    services.resolve_case(kept_case, decision="approved", actor_id=ts_lead.pk)
    sanction = services.issue_sanction(
        case=kept_case,
        subject_user_id=author_user.pk,
        kind=SanctionKind.WARNING,
        issued_by=ts_lead.pk,
    )
    Case.objects.filter(pk=kept_case.pk).update(
        resolved_at=timezone.now() - timezone.timedelta(days=400)
    )
    Sanction.objects.filter(pk=sanction.pk).update(
        state=SanctionState.EXPIRED,
        updated_at=timezone.now() - timezone.timedelta(days=400),
    )

    result = purge_expired_cases()
    assert result["cases"] == 1
    # The sanction is younger than 1095 days, so it stays — and its case
    # stays with it, because Sanction.case is PROTECTed. The audit trail
    # behind a live consequence cannot be purged out from under it.
    assert result["sanctions"] == 0
    assert Case.objects.filter(pk=kept_case.pk).exists()
    assert not Case.objects.filter(pk=old_case.pk).exists()


def test_a_sanctioned_case_is_never_purged(
    content_double, llm_double, ts_lead, author_user
):
    from django.utils import timezone

    from stapel_moderation.tasks import purge_expired_cases

    with mutate_and_emit() as emit_event:
        case, _ = services.open_case(
            "listing", "42", origin="submission", emit_event=emit_event
        )
    services.resolve_case(case, decision="rejected", actor_id=ts_lead.pk)
    services.issue_sanction(
        case=case,
        subject_user_id=author_user.pk,
        kind=SanctionKind.BANNED,
        issued_by=ts_lead.pk,
    )
    Case.objects.filter(pk=case.pk).update(
        resolved_at=timezone.now() - timezone.timedelta(days=5000)
    )

    purge_expired_cases()
    assert Case.objects.filter(pk=case.pk).exists()
