"""Appeals: the one backward edge, and the second pair of eyes.

DSA Art. 20 wants an internal complaint-handling system whose outcome can
actually change something. An appeal that files a letter and leaves the
target blocked is not a remedy, so an overturn here reopens the case along
its single ``resolved -> queued`` edge and re-resolves it — the target module
receives a fresh ``moderation.completed`` and un-blocks the thing.
"""
import pytest
from stapel_core.comm import mutate_and_emit

from stapel_moderation import services
from stapel_moderation.models import (
    AppealState,
    CaseState,
    SanctionKind,
    SanctionState,
    VerdictDecision,
    VerdictSource,
)

pytestmark = pytest.mark.django_db


def _rejected_case(content_double, llm_double, actor, author_user):
    llm_double["envelope"]["result"]["decision"] = "needs_review"
    with mutate_and_emit() as emit_event:
        case, _ = services.open_case(
            "listing",
            "42",
            origin="submission",
            subject_user_id=author_user.pk,
            emit_event=emit_event,
        )
        services.start_screening(case, emit_event=emit_event)
    case.refresh_from_db()
    services.resolve_case(
        case,
        decision=VerdictDecision.REJECTED,
        reason_code="counterfeit",
        note="Looks like a replica.",
        actor_id=actor.pk,
    )
    case.refresh_from_db()
    return case


def test_only_a_resolved_case_can_be_appealed(content_double, llm_double, author_user):
    llm_double["envelope"]["result"]["decision"] = "needs_review"
    with mutate_and_emit() as emit_event:
        case, _ = services.open_case(
            "listing", "42", origin="submission", emit_event=emit_event
        )
        services.start_screening(case, emit_event=emit_event)
    case.refresh_from_db()

    with pytest.raises(services.AppealNotAllowed, match="case_not_resolved"):
        services.open_appeal(case, appellant_id=author_user.pk, body="Please look again")


def test_one_appeal_per_person_per_case(content_double, llm_double, ts_lead, author_user):
    case = _rejected_case(content_double, llm_double, ts_lead, author_user)
    services.open_appeal(case, appellant_id=author_user.pk, body="It is licensed.")

    with pytest.raises(services.AppealNotAllowed, match="already_appealed"):
        services.open_appeal(case, appellant_id=author_user.pk, body="Again.")


def test_the_deciding_moderator_may_not_decide_the_appeal(
    content_double, llm_double, ts_lead, author_user
):
    """APPEAL_REQUIRES_DIFFERENT_ACTOR, default True. Independence is what
    makes Art. 20 mean anything; a one-moderator team turns it off knowingly
    rather than discovering that appeals rubber-stamp themselves."""
    case = _rejected_case(content_double, llm_double, ts_lead, author_user)
    appeal = services.open_appeal(
        case, appellant_id=author_user.pk, body="It is licensed."
    )

    with pytest.raises(services.SameActor):
        services.resolve_appeal(
            appeal, outcome=AppealState.OVERTURNED, actor_id=ts_lead.pk
        )


def test_the_switch_can_be_closed_deliberately(
    content_double, llm_double, ts_lead, author_user, settings
):
    case = _rejected_case(content_double, llm_double, ts_lead, author_user)
    appeal = services.open_appeal(case, appellant_id=author_user.pk, body="Licensed.")

    settings.STAPEL_MODERATION = {"APPEAL_REQUIRES_DIFFERENT_ACTOR": False}
    services.resolve_appeal(
        appeal, outcome=AppealState.UPHELD, actor_id=ts_lead.pk, note="Stands."
    )
    appeal.refresh_from_db()
    assert appeal.state == AppealState.UPHELD


def test_an_overturn_reopens_and_re_decides_the_case(
    content_double, llm_double, ts_lead, moderator, author_user, captured_events
):
    """The single backward edge, and the reason it exists."""
    case = _rejected_case(content_double, llm_double, ts_lead, author_user)
    appeal = services.open_appeal(
        case, appellant_id=author_user.pk, body="Here is the certificate."
    )
    captured_events.clear()

    services.resolve_appeal(
        appeal,
        outcome=AppealState.OVERTURNED,
        actor_id=moderator.pk,
        note="Certificate checks out.",
    )

    case.refresh_from_db()
    assert case.state == CaseState.RESOLVED
    last = case.verdicts.order_by("-created_at").first()
    assert last.decision == VerdictDecision.APPROVED
    assert last.source == VerdictSource.APPEAL

    # The target module hears about it: a remedy, not a letter.
    completed = [e for e in captured_events if e.event_type == "moderation.completed"]
    assert completed and completed[-1].payload["decision"] == "approved"
    assert case.events.filter(kind="reopened").exists()


def test_an_overturn_lifts_the_sanction_it_was_about(
    content_double, llm_double, ts_lead, moderator, author_user
):
    from stapel_core.django.jwt.authentication import is_user_blacklisted

    case = _rejected_case(content_double, llm_double, ts_lead, author_user)
    sanction = services.issue_sanction(
        case=case,
        subject_user_id=author_user.pk,
        kind=SanctionKind.SUSPENDED,
        issued_by=ts_lead.pk,
    )
    assert is_user_blacklisted(str(author_user.pk))

    appeal = services.open_appeal(
        case, appellant_id=author_user.pk, body="Wrong person.", sanction=sanction
    )
    services.resolve_appeal(
        appeal, outcome=AppealState.OVERTURNED, actor_id=moderator.pk
    )

    sanction.refresh_from_db()
    # `overturned`, not `lifted`: one is discretion, the other is the
    # platform having been wrong, and the distinction is the record.
    assert sanction.state == SanctionState.OVERTURNED
    assert not is_user_blacklisted(str(author_user.pk))


def test_an_upheld_appeal_changes_nothing_but_the_record(
    content_double, llm_double, ts_lead, moderator, author_user
):
    case = _rejected_case(content_double, llm_double, ts_lead, author_user)
    appeal = services.open_appeal(case, appellant_id=author_user.pk, body="Please.")
    verdicts_before = case.verdicts.count()

    services.resolve_appeal(
        appeal, outcome=AppealState.UPHELD, actor_id=moderator.pk, note="Stands."
    )

    case.refresh_from_db()
    assert case.state == CaseState.RESOLVED
    assert case.verdicts.count() == verdicts_before
    appeal.refresh_from_db()
    assert appeal.state == AppealState.UPHELD


def test_a_decided_appeal_is_not_decided_twice(
    content_double, llm_double, ts_lead, moderator, author_user
):
    case = _rejected_case(content_double, llm_double, ts_lead, author_user)
    appeal = services.open_appeal(case, appellant_id=author_user.pk, body="Please.")
    services.resolve_appeal(appeal, outcome=AppealState.UPHELD, actor_id=moderator.pk)

    with pytest.raises(services.AppealNotAllowed, match="already_resolved"):
        services.resolve_appeal(
            appeal, outcome=AppealState.OVERTURNED, actor_id=moderator.pk
        )


def _second_lead(username):
    """A HIGH lead who did NOT decide the case — Art. 20 independence."""
    from django.contrib.auth import get_user_model

    reviewer = get_user_model().objects.create_user(
        username=username, email=f"{username}@example.test", password="x", is_staff=True
    )
    reviewer.staff_roles = ["ts_lead"]
    return reviewer


def test_a_decided_appeal_answers_409_over_http_not_a_field_error(
    content_double, llm_double, client_for, ts_lead, author_user
):
    """A decided appeal is a STATE conflict, not a malformed outcome.

    It used to answer ``400 invalid_outcome``, which sends the console back
    to fix a field that was never wrong, and left the registered
    ``error.409.moderation_appeal_resolved`` unreachable.
    """
    case = _rejected_case(content_double, llm_double, ts_lead, author_user)
    appeal = services.open_appeal(case, appellant_id=author_user.pk, body="Please.")

    reviewer = client_for(_second_lead("lead5"))
    first = reviewer.post(
        f"/moderation/api/v1/appeals/{appeal.id}/resolve",
        {"outcome": "upheld"},
        format="json",
    )
    assert first.status_code == 200, first.data

    second = reviewer.post(
        f"/moderation/api/v1/appeals/{appeal.id}/resolve",
        {"outcome": "overturned"},
        format="json",
    )
    assert second.status_code == 409, second.data
    assert second.data["localizable_error"] == "error.409.moderation_appeal_resolved"


def test_an_outcome_word_that_does_not_exist_is_still_a_field_error(
    content_double, llm_double, client_for, ts_lead, author_user
):
    case = _rejected_case(content_double, llm_double, ts_lead, author_user)
    appeal = services.open_appeal(case, appellant_id=author_user.pk, body="Please.")

    response = client_for(_second_lead("lead6")).post(
        f"/moderation/api/v1/appeals/{appeal.id}/resolve",
        {"outcome": "sideways"},
        format="json",
    )
    assert response.status_code == 400, response.data
    assert response.data["localizable_error"] == "error.400.moderation_invalid_outcome"
