"""Notifications: the five letters, and the variable names that survive.

Two things are asserted here that nothing else can catch:

1. **Variable names dodge the merge trap.** stapel-notifications silently
   DROPS a caller variable whose name collides with a short translation key
   (``heading``, ``body``, ``cta``, ``warning``, ``subject``, ``push_*``).
   ``reason_label`` and ``appeal_note`` are spelled that way for that reason,
   and a rename back to the obvious word would break nothing visibly — the
   letter would just quietly lose its reason line.
2. **Nothing is notified inline.** Every letter is a subscriber on this
   module's own committed fact, so a notifications outage cannot roll back a
   verdict.
"""
import pytest
from stapel_core.comm import mutate_and_emit

from stapel_moderation import services
from stapel_moderation.models import SanctionKind, VerdictDecision

pytestmark = pytest.mark.django_db

#: The short translation keys a caller variable must never be named. Taken
#: from stapel-notifications' merge (services.py:362-378).
RESERVED_VARIABLE_NAMES = {
    "heading",
    "body",
    "cta",
    "warning",
    "subject",
    "push_title",
    "push_body",
    "footer_address",
    "footer_legal",
    "footer_manage",
    "footer_unsubscribe",
    "footer_consent",
}


def _of(sent, notification_type):
    return [item for item in sent if item["type"] == notification_type]


def test_no_caller_variable_collides_with_a_translation_key(
    content_double, llm_double, ts_lead, author_user, user, captured_notifications
):
    """One assertion covering every letter the module can send."""
    llm_double["envelope"]["result"]["decision"] = "needs_review"
    services.submit_report(
        target_type="listing", target_key="42", reporter_id=user.pk, reason_code="spam"
    )
    case = services.list_cases()[0]
    services.resolve_case(
        case,
        decision=VerdictDecision.REJECTED,
        reason_code="counterfeit",
        note="Replica.",
        actor_id=ts_lead.pk,
        sanction={"kind": SanctionKind.SUSPENDED, "duration_seconds": 3600},
    )

    assert captured_notifications, "no notification was requested at all"
    for item in captured_notifications:
        names = set(item.get("variables") or {})
        assert not (names & RESERVED_VARIABLE_NAMES), (
            f"{item['type']} passes {sorted(names & RESERVED_VARIABLE_NAMES)}, "
            "which the notifications merge silently drops"
        )


def test_the_complainant_is_acknowledged_then_told_the_outcome(
    content_double, llm_double, ts_lead, user, captured_notifications
):
    """DSA Art. 16(4) then 16(5)."""
    llm_double["envelope"]["result"]["decision"] = "needs_review"
    services.submit_report(
        target_type="listing", target_key="42", reporter_id=user.pk, reason_code="spam"
    )

    received = _of(captured_notifications, "moderation.report_received")
    assert len(received) == 1
    assert set(received[0]["variables"]) == {"target_label", "case_ref"}

    case = services.list_cases()[0]
    services.resolve_case(
        case, decision=VerdictDecision.DISMISSED, actor_id=ts_lead.pk
    )

    reviewed = _of(captured_notifications, "report_reviewed")
    assert len(reviewed) == 1
    # The upstream body used to say "we have taken action" unconditionally,
    # which was a lie on a dismissal. Now the decision travels as a variable.
    assert reviewed[0]["variables"]["outcome_label"] == "dismissed"


def test_the_author_gets_a_statement_of_reasons_with_an_appeal_link(
    content_double, llm_double, ts_lead, author_user, settings, captured_notifications
):
    """DSA Art. 17. The 0.14.0 listing_blocked carries reason_label and
    appeal_url, which it did not before this module needed them."""
    settings.STAPEL_MODERATION = {
        "APPEAL_URL_TEMPLATE": "https://example.test/appeals/{case_id}"
    }
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
    captured_notifications.clear()

    services.resolve_case(
        case,
        decision=VerdictDecision.REJECTED,
        reason_code="counterfeit",
        note="Replica.",
        actor_id=ts_lead.pk,
    )

    blocked = _of(captured_notifications, "listing_blocked")
    assert len(blocked) == 1
    variables = blocked[0]["variables"]
    assert variables["listing_title"] == "A bicycle"
    assert variables["reason_label"] == "moderation.reason.counterfeit.label"
    assert variables["appeal_url"].endswith(str(case.id))


def test_no_appeal_url_template_yields_no_invented_address(
    content_double, llm_double, ts_lead, author_user, captured_notifications
):
    """A "how to appeal" link that 404s is worse than no link."""
    from stapel_moderation.notifications import appeal_url

    assert appeal_url("abc") == ""


def test_an_approval_notifies_nobody_about_a_takedown(
    content_double, llm_double, ts_lead, author_user, captured_notifications
):
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
    captured_notifications.clear()

    services.resolve_case(
        case, decision=VerdictDecision.APPROVED, actor_id=ts_lead.pk
    )
    assert _of(captured_notifications, "listing_blocked") == []


def test_forty_reports_are_not_forty_letters(
    content_double, llm_double, ts_lead, author_user, captured_notifications
):
    """The invitation lesson: the protected resource is the author's inbox."""
    from stapel_moderation.notifications import notify_content_blocked

    llm_double["envelope"]["result"]["decision"] = "needs_review"
    with mutate_and_emit() as emit_event:
        case, _ = services.open_case(
            "listing",
            "42",
            origin="submission",
            subject_user_id=author_user.pk,
            emit_event=emit_event,
        )
    captured_notifications.clear()

    payload = {"decision": "rejected", "reason_code": "spam"}
    sent = [notify_content_blocked(case, payload) for _ in range(5)]
    assert sent.count(True) == 1
    assert len(_of(captured_notifications, "listing_blocked")) == 1


def test_a_policy_without_a_blocked_type_sends_nothing(
    content_double, llm_double, ts_lead, author_user, captured_notifications
):
    """``notification_types: {"content_blocked": None}`` is a statement, and
    the module respects it rather than picking a default letter."""
    from stapel_moderation.notifications import notify_content_blocked
    from stapel_moderation.registry import register_target_type

    register_target_type(
        "listing",
        {
            "id_field": "listing_id",
            "content_function": "listings.moderation_content",
            "notification_types": {"content_blocked": None},
        },
    )
    with mutate_and_emit() as emit_event:
        case, _ = services.open_case(
            "listing",
            "42",
            origin="submission",
            subject_user_id=author_user.pk,
            emit_event=emit_event,
        )

    assert notify_content_blocked(case, {"decision": "rejected"}) is False
    assert captured_notifications == []


def test_a_notification_outage_never_rolls_back_a_verdict(
    content_double, llm_double, ts_lead, author_user, monkeypatch
):
    """The forms canon: react to your own committed fact, never notify inline."""

    def _explode(*args, **kwargs):
        raise RuntimeError("notifications are down")

    monkeypatch.setattr(
        "stapel_core.notifications.request_notification", _explode, raising=False
    )

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
        case, decision=VerdictDecision.REJECTED, actor_id=ts_lead.pk
    )
    case.refresh_from_db()
    assert case.state == "resolved"


def test_the_sanctioned_user_is_told_how_to_appeal(
    content_double, llm_double, ts_lead, author_user, settings, captured_notifications
):
    settings.STAPEL_MODERATION = {
        "APPEAL_URL_TEMPLATE": "https://example.test/appeals/{case_id}"
    }
    with mutate_and_emit() as emit_event:
        case, _ = services.open_case(
            "listing", "42", origin="submission", emit_event=emit_event
        )
    captured_notifications.clear()

    services.issue_sanction(
        case=case,
        subject_user_id=author_user.pk,
        kind=SanctionKind.SUSPENDED,
        reason_code="fraud",
        duration_seconds=3600,
        issued_by=ts_lead.pk,
    )

    issued = _of(captured_notifications, "moderation.sanction_issued")
    assert len(issued) == 1
    variables = issued[0]["variables"]
    assert variables["sanction_kind"] == "suspended"
    assert variables["reason_label"] == "moderation.reason.fraud.label"
    assert variables["expires_label"]
    assert variables["appeal_url"].endswith(str(case.id))
