"""Evidence-based target types: complaints about content nobody serves.

Every other target in the fleet has an owner that answers
``*.moderation_content``. A chat message does not, and neither does a story
or a live-stream frame: by the time a moderator opens the case the thing may
not exist anywhere but in the complainant's screenshot. The module's answer
is a per-type declaration (``evidence: True``) plus the reporter's own
snapshot stored on the ``Report`` — and a hard rule that what is assembled
from it is marked as an attestation, never as content the platform read.
"""
import pytest
from django.core.management import call_command
from django.core.management.base import SystemCheckError

from stapel_moderation import services
from stapel_moderation.models import Case, CaseState, Report
from stapel_moderation.registry import register_target_type

pytestmark = pytest.mark.django_db


CHAT_POLICY = {
    "gate": "post",
    "evidence": True,
    "verdict_event": None,
    "screen": False,
    "media": False,
}


def _register_chat_type(**overrides):
    register_target_type("chat_message", {**CHAT_POLICY, **overrides})


def _evidence(author_id, text="Send the deposit to my card, we settle off-site."):
    return {
        "text": text,
        "author_id": str(author_id),
        "conversation_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
        "sent_at": "2026-08-24T10:00:00+00:00",
    }


# ── The declaration ──────────────────────────────────────────────────


def test_a_type_may_declare_evidence_instead_of_a_content_function(db):
    _register_chat_type()
    policy = services.resolve_policy("chat_message")
    assert policy["evidence"] is True
    assert policy["content_function"] == ""


def test_a_type_declaring_neither_is_still_an_error(db, settings):
    """E004 did not become optional — it grew one alternative."""
    settings.STAPEL_MODERATION = {"TARGET_TYPES": {"story": {"gate": "post"}}}
    with pytest.raises(SystemCheckError) as exc:
        call_command("check", fail_level="ERROR")
    assert "E004" in str(exc.value)


def test_declaring_both_sources_is_refused(db, settings):
    """One target, one source of truth — E007."""
    settings.STAPEL_MODERATION = {
        "TARGET_TYPES": {
            "story": {"evidence": True, "content_function": "stories.moderation_content"}
        }
    }
    with pytest.raises(SystemCheckError) as exc:
        call_command("check", fail_level="ERROR")
    assert "E007" in str(exc.value)


def test_an_evidence_type_is_not_reported_as_unreachable(db, settings):
    """W006 warns about a content function nobody provides. An evidence type
    names none, and its content source — the report — is right here."""
    settings.STAPEL_MODERATION = {"TARGET_TYPES": {"chat_message": dict(CHAT_POLICY)}}
    from stapel_moderation.checks import check_verdict_consumers

    warnings = check_verdict_consumers(None)
    assert not [w for w in warnings if "cannot be called here" in str(w.msg)]
    # verdict_event=None is still announced — the statement check stays.
    assert [w for w in warnings if "verdict_event=None" in str(w.msg)]


# ── Intake ───────────────────────────────────────────────────────────


def test_a_report_carries_the_snapshot_and_opens_a_case(db, user, author_user):
    _register_chat_type()
    report, case = services.submit_report(
        target_type="chat_message",
        target_key="m-1",
        reporter_id=user.pk,
        reason_code="fraud",
        description="Asked me to pay off-platform.",
        evidence=_evidence(author_user.pk),
    )
    assert report.evidence["text"].startswith("Send the deposit")
    assert case.state in (CaseState.OPEN, CaseState.QUEUED)
    # The subject is who the complainant says wrote it — the only answer that
    # exists for a target no service owns.
    assert str(case.subject_user_id) == str(author_user.pk)


def test_the_assembled_content_says_it_is_unverified(db, user, author_user):
    _register_chat_type()
    services.submit_report(
        target_type="chat_message",
        target_key="m-2",
        reporter_id=user.pk,
        reason_code="spam",
        evidence=_evidence(author_user.pk),
    )
    content = services.fetch_content("chat_message", "m-2")
    assert content.extra["source"] == "evidence"
    assert content.extra["verified"] is False
    assert content.text.startswith("Send the deposit")
    # Unknown keys ride along verbatim, the attachment-registry rule.
    assert content.extra["conversation_id"] == "3fa85f64-5717-4562-b3fc-2c963f66afa6"


def test_you_still_cannot_report_your_own_message(db, user):
    _register_chat_type()
    with pytest.raises(services.OwnContent):
        services.submit_report(
            target_type="chat_message",
            target_key="m-3",
            reporter_id=user.pk,
            reason_code="spam",
            evidence=_evidence(user.pk),
        )


def test_an_evidence_type_with_no_evidence_is_a_404_not_a_503(db, user):
    """Nothing is down: there is simply nothing to look at."""
    _register_chat_type()
    with pytest.raises(services.TargetNotFound):
        services.submit_report(
            target_type="chat_message",
            target_key="m-4",
            reporter_id=user.pk,
            reason_code="spam",
        )


def test_evidence_on_a_served_type_is_refused(db, user, content_double):
    """A snapshot next to a live content_function is a second, staler answer."""
    with pytest.raises(ValueError) as exc:
        services.submit_report(
            target_type="listing",
            target_key="42",
            reporter_id=user.pk,
            reason_code="spam",
            evidence={"text": "whatever"},
        )
    assert str(exc.value) == "evidence_invalid"


def test_oversized_evidence_is_refused_never_truncated(db, user, author_user, settings):
    _register_chat_type()
    settings.STAPEL_MODERATION = {"MAX_EVIDENCE_BYTES": 64}
    with pytest.raises(ValueError) as exc:
        services.submit_report(
            target_type="chat_message",
            target_key="m-5",
            reporter_id=user.pk,
            reason_code="spam",
            evidence=_evidence(author_user.pk, text="x" * 500),
        )
    assert str(exc.value) == "evidence_invalid"
    assert Report.objects.count() == 0
    assert Case.objects.count() == 0


# ── Later reads ──────────────────────────────────────────────────────


def test_a_later_read_uses_the_newest_attestation(db, user, other_user, author_user):
    """A case card opened six hours later, a re-screen and an appeal all read
    the freshest snapshot on file — the one thing this module stores instead
    of fetching, and only for types that have nothing to fetch."""
    _register_chat_type()
    services.submit_report(
        target_type="chat_message",
        target_key="m-6",
        reporter_id=user.pk,
        reason_code="spam",
        evidence=_evidence(author_user.pk, text="first"),
    )
    services.submit_report(
        target_type="chat_message",
        target_key="m-6",
        reporter_id=other_user.pk,
        reason_code="spam",
        evidence=_evidence(author_user.pk, text="second"),
    )
    assert services.fetch_content("chat_message", "m-6").text == "second"


def test_the_api_accepts_evidence_and_the_card_shows_it(
    db, client_for, user, author_user, lead_client
):
    _register_chat_type()
    response = client_for(user).post(
        "/moderation/api/v1/reports/",
        {
            "target_type": "chat_message",
            "target_key": "m-7",
            "reason_code": "harassment",
            "description": "Threats in a listing chat.",
            "evidence": _evidence(author_user.pk),
        },
        format="json",
    )
    assert response.status_code == 201, response.data

    case = Case.objects.get(target_type="chat_message", target_key="m-7")
    card = lead_client.get(f"/moderation/api/v1/cases/{case.id}")
    assert card.status_code == 200, card.data
    assert card.data["content"]["extra"]["source"] == "evidence"
    assert card.data["reports"][0]["evidence"]["text"].startswith("Send the deposit")


def test_the_api_refuses_evidence_the_policy_does_not_take(
    db, client_for, user, content_double
):
    response = client_for(user).post(
        "/moderation/api/v1/reports/",
        {
            "target_type": "listing",
            "target_key": "42",
            "reason_code": "spam",
            "evidence": {"text": "whatever"},
        },
        format="json",
    )
    assert response.status_code == 400
    assert response.data["localizable_error"] == "error.400.moderation_evidence_invalid"
