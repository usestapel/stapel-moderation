"""Intake: idempotency by state, and one case per target however it arrives.

Spec §14 assertions 1-3 live here. Each one closes a named legacy defect:
a redelivered event opened a second row, forty complaints opened forty queue
rows, and the "one report per user" rule was a ``unique_together`` a second
table simply sidestepped.
"""
import pytest
from stapel_core.bus import Event
from stapel_core.comm import deliver
from stapel_core.django.taskstore.models import TaskRecord

from stapel_moderation import services
from stapel_moderation.models import Case, CaseState, Report

pytestmark = pytest.mark.django_db


def _submit(listing_id="42"):
    """Deliver a listing.submitted the way the bus would.

    The event carries a title, and the module IGNORES it: content is read
    through content_function at the moment of screening, never from a payload
    that will be stale by the time a human opens the card.
    """
    deliver(
        Event(
            event_type="listing.submitted",
            service="listings",
            payload={"listing_id": listing_id, "title": "A bicycle"},
        )
    )


def test_redelivered_intake_event_opens_one_case_and_one_task(
    content_double, llm_double
):
    """Assertion 1: the same fact twice is one case and one screening.

    Idempotency is by STATE — a partial unique constraint over the open
    states plus select_for_update — not by a table of seen event ids, which
    the outbox does not have.
    """
    llm_double["envelope"]["result"]["decision"] = "needs_review"
    _submit()
    _submit()

    assert Case.objects.count() == 1
    assert TaskRecord.objects.filter(kind=services.SCREEN_TASK).count() == 1
    # The redelivery is audited rather than silently dropped: an operator
    # asking "why is this case here twice" gets an answer.
    case = Case.objects.get()
    assert case.events.filter(kind="resubmitted").count() == 1


def test_two_reporters_one_case(content_double, llm_double, user, other_user):
    """Assertion 2: forty complaints are one case with report_count = 40.

    Legacy kept one queue row per complaint; that was its defining scaling
    defect, and the reason a moderator saw the same listing forty times.
    """
    # The screener abstains, so the case stays in the human queue and the
    # second complaint joins it — which is the situation the count is for.
    llm_double["envelope"]["result"]["decision"] = "needs_review"

    services.submit_report(
        target_type="listing",
        target_key="42",
        reporter_id=user.pk,
        reason_code="spam",
    )
    services.submit_report(
        target_type="listing",
        target_key="42",
        reporter_id=other_user.pk,
        reason_code="fraud",
        description="Wants a deposit by bank transfer before any viewing.",
    )

    assert Case.objects.count() == 1
    case = Case.objects.get()
    assert case.report_count == 2
    assert Report.objects.filter(case=case).count() == 2
    # Severity climbs to the worst reason seen (fraud=3 over spam=1) — that
    # is what makes the queue's ORDER BY mean something.
    assert case.severity == 3


def test_same_reporter_twice_is_refused(content_double, llm_double, user):
    """Assertion 3: a real UniqueConstraint, not legacy's unique_together."""
    llm_double["envelope"]["result"]["decision"] = "needs_review"
    services.submit_report(
        target_type="listing", target_key="42", reporter_id=user.pk, reason_code="spam"
    )
    with pytest.raises(services.AlreadyReported):
        services.submit_report(
            target_type="listing",
            target_key="42",
            reporter_id=user.pk,
            reason_code="counterfeit",
        )
    assert Report.objects.count() == 1


def test_reporting_own_content_is_refused(content_double, llm_double, author_user):
    """The author is learned from content_function, never from the request."""
    with pytest.raises(services.OwnContent):
        services.submit_report(
            target_type="listing",
            target_key="42",
            reporter_id=author_user.pk,
            reason_code="spam",
        )


def test_unknown_target_type_is_refused(db, user):
    from stapel_moderation.registry import UnknownTargetType

    with pytest.raises(UnknownTargetType):
        services.submit_report(
            target_type="spaceship",
            target_key="1",
            reporter_id=user.pk,
            reason_code="spam",
        )


def test_missing_target_is_a_lookup_failure_not_an_outage(
    content_double, llm_double, user
):
    """TargetNotFound and ContentUnavailable are different answers.

    Collapsing them would tell a reporter their target does not exist because
    a sibling service restarted.
    """
    with pytest.raises(services.TargetNotFound):
        services.submit_report(
            target_type="listing",
            target_key="9999",
            reporter_id=user.pk,
            reason_code="spam",
        )


def test_reason_requiring_a_description_refuses_an_empty_one(
    content_double, llm_double, user
):
    """And the description is never silently erased, which legacy did."""
    with pytest.raises(ValueError, match="description_required"):
        services.submit_report(
            target_type="listing",
            target_key="42",
            reporter_id=user.pk,
            reason_code="harassment",
        )

    services.submit_report(
        target_type="listing",
        target_key="42",
        reporter_id=user.pk,
        reason_code="harassment",
        description="They keep messaging me after I asked them to stop.",
    )
    assert Report.objects.get().description.startswith("They keep messaging")


def test_report_after_resolution_opens_a_new_case(content_double, llm_double, user, other_user):
    """The partial constraint covers OPEN states only, so a resolved case
    does not block the next complaint about the same target."""
    _report, case = services.submit_report(
        target_type="listing", target_key="42", reporter_id=user.pk, reason_code="spam"
    )
    case.refresh_from_db()
    assert case.state == CaseState.RESOLVED  # the llm double approved it

    services.submit_report(
        target_type="listing",
        target_key="42",
        reporter_id=other_user.pk,
        reason_code="counterfeit",
    )
    assert Case.objects.count() == 2
