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


def test_a_flattened_remote_lookup_error_is_still_a_missing_target(
    content_double, user
):
    """The transport hop that turned a 404 into a twelve-day retry loop.

    ``__cause__`` survives an in-process call and nothing else. Over NATS,
    core rebuilds the owner's exception as
    ``FunctionCallError("function 'x' failed remotely: LookupError('…')")``
    with no cause attached — so ``_is_not_found`` said "not a 404", the case
    was treated as an outage, and the ladder retried a listing that was
    deleted at the end of a probe run. 207 events on a client stand, all of
    them this.

    The remote exception NAME is the one structured thing that survives, and
    it is read only in the ``failed remotely:`` tail, so prose cannot fake it.
    """
    from stapel_core.comm import CommError, function

    from stapel_moderation import services
    from stapel_moderation.registry import register_target_type

    @function("listings.flattened_content")
    def _flattened(payload):
        raise CommError(
            "function 'listings.moderation_content' failed remotely: "
            "LookupError('listing draft:71bde8564c2148e09eb0d2b3b8d8ab80 "
            "not found')"
        )

    register_target_type(
        "flat",
        {
            "id_field": "listing_id",
            "content_function": "listings.flattened_content",
            "verdict_event": "moderation.completed",
        },
    )

    with pytest.raises(services.TargetNotFound):
        services.fetch_content("flat", "draft:71bde856")


def test_a_remote_outage_is_still_an_outage(content_double, user):
    """The other direction, and the reason the match is not a substring hunt.

    A provider that says the words "not found" in prose — an upstream 404 it
    is reporting, a message mentioning a missing model — must NOT dismiss
    somebody's case. Only an exception repr in the ``failed remotely:`` tail
    counts.
    """
    from stapel_core.comm import CommError, function

    from stapel_moderation import services
    from stapel_moderation.registry import register_target_type

    @function("listings.outage_content")
    def _outage(payload):
        raise CommError(
            "function 'listings.moderation_content' failed remotely: "
            "ProviderError('upstream said the model was not found')"
        )

    register_target_type(
        "flaky",
        {
            "id_field": "listing_id",
            "content_function": "listings.outage_content",
            "verdict_event": "moderation.completed",
        },
    )

    with pytest.raises(services.ContentUnavailable):
        services.fetch_content("flaky", "42")


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
