"""The HTTP surface: two audiences, two postures, one choke point.

The moderator surface is graded by the staff mandate — and the interesting
assertion is not "a moderator can read the queue" but "a MID moderator cannot
issue a ban". Per-app clearance is what makes a moderator staff without also
making them a billing administrator, and if the grades were not actually
enforced the whole authz chapter would be decoration.
"""
import pytest
from stapel_core.comm import mutate_and_emit

from stapel_moderation import services
from stapel_moderation.models import Case, CaseState, Sanction, VerdictDecision

pytestmark = pytest.mark.django_db

BASE = "/moderation/api/v1"


def _queued(llm_double, key="42", subject=None):
    llm_double["envelope"]["result"]["decision"] = "needs_review"
    with mutate_and_emit() as emit_event:
        case, _ = services.open_case(
            "listing",
            key,
            origin="submission",
            subject_user_id=subject,
            emit_event=emit_event,
        )
        services.start_screening(case, emit_event=emit_event)
    case.refresh_from_db()
    return case


# ── Intake ───────────────────────────────────────────────────────────


def test_a_member_files_a_report(content_double, llm_double, auth_client):
    response = auth_client.post(
        f"{BASE}/reports/",
        {
            "target_type": "listing",
            "target_key": "42",
            "reason_code": "spam",
            "good_faith": True,
        },
        format="json",
    )
    assert response.status_code == 201, response.data
    # A short reference to quote at support, not a handle to a case the
    # reporter may not read.
    assert len(response.data["case_ref"]) == 8
    assert Case.objects.count() == 1


def test_a_second_report_by_the_same_person_is_409(
    content_double, llm_double, auth_client
):
    llm_double["envelope"]["result"]["decision"] = "needs_review"
    body = {"target_type": "listing", "target_key": "42", "reason_code": "spam"}
    assert auth_client.post(f"{BASE}/reports/", body, format="json").status_code == 201
    second = auth_client.post(f"{BASE}/reports/", body, format="json")
    assert second.status_code == 409
    assert second.data["localizable_error"] == "error.409.moderation_already_reported"


def test_an_unknown_target_type_is_400(content_double, auth_client):
    response = auth_client.post(
        f"{BASE}/reports/",
        {"target_type": "spaceship", "target_key": "1", "reason_code": "spam"},
        format="json",
    )
    assert response.status_code == 400
    assert response.data["localizable_error"] == "error.400.moderation_unknown_target_type"


def test_a_missing_target_is_404_not_503(content_double, auth_client):
    response = auth_client.post(
        f"{BASE}/reports/",
        {"target_type": "listing", "target_key": "9999", "reason_code": "spam"},
        format="json",
    )
    assert response.status_code == 404
    assert response.data["localizable_error"] == "error.404.moderation_target_not_found"


def test_reporting_your_own_content_is_400(content_double, api_client, author_user):
    api_client.force_authenticate(user=author_user)
    response = api_client.post(
        f"{BASE}/reports/",
        {"target_type": "listing", "target_key": "42", "reason_code": "spam"},
        format="json",
    )
    assert response.status_code == 400
    assert response.data["localizable_error"] == "error.400.moderation_own_content"


def test_a_reason_needing_an_explanation_says_so(content_double, auth_client):
    response = auth_client.post(
        f"{BASE}/reports/",
        {"target_type": "listing", "target_key": "42", "reason_code": "harassment"},
        format="json",
    )
    assert response.status_code == 400
    assert response.data["localizable_error"] == "error.400.moderation_description_required"


def test_the_intake_is_closed_to_anonymous_callers(content_double, api_client):
    response = api_client.post(
        f"{BASE}/reports/",
        {"target_type": "listing", "target_key": "42", "reason_code": "spam"},
        format="json",
    )
    assert response.status_code in (401, 403)


# ── The public disclosure ────────────────────────────────────────────


def test_the_policy_disclosure_is_public(content_double, api_client):
    """A transparency disclosure that requires an account is not one."""
    response = api_client.get(f"{BASE}/policy")
    assert response.status_code == 200
    codes = {entry["code"] for entry in response.data["reasons"]}
    assert "spam" in codes and "fraud" in codes
    # System reasons are verdict vocabulary, not complaint vocabulary.
    assert "screening_unavailable" not in codes
    assert response.data["automated_means"]["enabled"] is True
    assert response.data["human_review"]["auto_resolve_after_seconds"] is None


def test_the_disclosure_reports_the_deployment_it_is_running_in(
    content_double, api_client, settings
):
    """Generated from the mechanism, not a prose copy that drifts."""
    settings.STAPEL_MODERATION = {
        "SCREEN_ENABLED": False,
        "ON_SCREENING_FAILURE": "approve",
    }
    response = api_client.get(f"{BASE}/policy")
    assert response.data["automated_means"]["enabled"] is False
    assert response.data["automated_means"]["on_unavailable"] == "approve"


# ── The mandate actually grades the console ──────────────────────────


def test_a_member_cannot_read_the_queue(content_double, llm_double, auth_client):
    _queued(llm_double)
    assert auth_client.get(f"{BASE}/cases").status_code == 403


def test_a_mid_moderator_reads_the_queue(
    content_double, llm_double, api_client, moderator
):
    _queued(llm_double)
    api_client.force_authenticate(user=moderator)
    response = api_client.get(f"{BASE}/cases")
    assert response.status_code == 200
    assert len(response.data) == 1
    assert response["X-Moderation-Next-Before"]


def test_a_mid_moderator_cannot_decide_or_ban(
    content_double, llm_double, api_client, moderator
):
    """The grade that matters: view MID, mutations HIGH. A moderator who can
    read the queue is not thereby a moderator who can hand out bans."""
    case = _queued(llm_double)
    api_client.force_authenticate(user=moderator)

    assert api_client.post(f"{BASE}/cases/{case.id}/claim").status_code == 403
    assert (
        api_client.post(
            f"{BASE}/cases/{case.id}/verdict",
            {"decision": "rejected"},
            format="json",
        ).status_code
        == 403
    )
    assert (
        api_client.post(
            f"{BASE}/sanctions",
            {"subject_user_id": str(moderator.pk), "kind": "banned"},
            format="json",
        ).status_code
        == 403
    )


def test_a_high_lead_works_the_whole_case(
    content_double, llm_double, lead_client, ts_lead, author_user
):
    case = _queued(llm_double, subject=author_user.pk)

    claimed = lead_client.post(f"{BASE}/cases/{case.id}/claim")
    assert claimed.status_code == 200
    assert claimed.data["state"] == CaseState.CLAIMED

    verdict = lead_client.post(
        f"{BASE}/cases/{case.id}/verdict",
        {
            "decision": "rejected",
            "reason_code": "counterfeit",
            "note": "Replica.",
            "sanction": {"kind": "posting_restricted", "duration_seconds": 3600},
        },
        format="json",
    )
    assert verdict.status_code == 201, verdict.data
    assert verdict.data["decision"] == "rejected"

    case.refresh_from_db()
    assert case.state == CaseState.RESOLVED
    assert Sanction.objects.filter(subject_user_id=author_user.pk).exists()


def test_a_claimed_case_refuses_a_second_claimant(
    content_double, llm_double, client_for, ts_lead
):
    from django.contrib.auth import get_user_model

    case = _queued(llm_double)
    assert client_for(ts_lead).post(f"{BASE}/cases/{case.id}/claim").status_code == 200

    other = get_user_model().objects.create_user(
        username="lead2", email="lead2@example.test", password="x", is_staff=True
    )
    other.staff_roles = ["ts_lead"]
    response = client_for(other).post(f"{BASE}/cases/{case.id}/claim")
    assert response.status_code == 409
    assert response.data["localizable_error"] == "error.409.moderation_case_claimed"


def test_a_resolved_case_refuses_a_second_verdict(
    content_double, llm_double, lead_client
):
    case = _queued(llm_double)
    lead_client.post(
        f"{BASE}/cases/{case.id}/verdict", {"decision": "approved"}, format="json"
    )
    second = lead_client.post(
        f"{BASE}/cases/{case.id}/verdict", {"decision": "rejected"}, format="json"
    )
    assert second.status_code == 409
    assert second.data["localizable_error"] == "error.409.moderation_case_resolved"


# ── The card, and its failure branch ─────────────────────────────────


def test_the_card_carries_the_live_content(content_double, llm_double, lead_client):
    case = _queued(llm_double)
    response = lead_client.get(f"{BASE}/cases/{case.id}")
    assert response.status_code == 200
    assert response.data["content"]["available"] is True
    assert response.data["content"]["title"] == "A bicycle"


def test_an_unreadable_target_renders_a_failed_branch_not_an_empty_card(
    content_double, llm_double, lead_client
):
    """A moderator must never decide about content nobody showed them, and an
    empty card is indistinguishable from empty content."""
    case = _queued(llm_double)
    content_double["listing_id"] = "moved"

    response = lead_client.get(f"{BASE}/cases/{case.id}")
    assert response.status_code == 200
    assert response.data["content"]["available"] is False
    assert response.data["content"]["error"] == "target_not_found"


def test_the_card_shows_reports_verdicts_and_the_audit_trail(
    content_double, llm_double, lead_client, user
):
    llm_double["envelope"]["result"]["decision"] = "needs_review"
    services.submit_report(
        target_type="listing", target_key="42", reporter_id=user.pk, reason_code="spam"
    )
    case = Case.objects.get()

    card = lead_client.get(f"{BASE}/cases/{case.id}")
    assert len(card.data["reports"]) == 1
    assert card.data["reports"][0]["reason_code"] == "spam"
    assert len(card.data["verdicts"]) == 1

    events = lead_client.get(f"{BASE}/cases/{case.id}/events")
    assert events.status_code == 200
    kinds = [row["kind"] for row in events.data]
    assert "created" in kinds and "verdict" in kinds


def test_the_card_carries_the_dead_letter_stamps(content_double, llm_double, lead_client):
    """The card says what broke, in the same fields the queue row uses.

    Without them the console had to read the ``dead_lettered`` AUDIT EVENT to
    render "which seam broke" — history standing in for state, which stops
    being true the moment the case is revived.
    """
    case = _queued(llm_double)
    services.dead_letter_case(
        case, error="llm.complete unreachable", error_class="ScreeningUnavailable"
    )

    card = lead_client.get(f"{BASE}/cases/{case.id}")
    assert card.status_code == 200
    assert card.data["last_error_class"] == "ScreeningUnavailable"
    assert card.data["last_error"] == "llm.complete unreachable"
    assert card.data["dlq_at"] is not None
    assert card.data["escalated_at"] is None
    # The same values the list row carries, not a second rendering of them.
    row = lead_client.get(f"{BASE}/cases?state=dlq").data[0]
    for field in ("dlq_at", "last_error_class", "last_error", "escalated_at"):
        assert card.data[field] == row[field]


def test_a_case_that_never_failed_carries_the_stamps_empty(
    content_double, llm_double, lead_client
):
    """Declared and null — never an absent key a client has to guess about."""
    case = _queued(llm_double)

    card = lead_client.get(f"{BASE}/cases/{case.id}")
    assert card.data["dlq_at"] is None
    assert card.data["escalated_at"] is None
    assert card.data["last_error_class"] == ""
    assert card.data["last_error"] == ""


def test_the_audit_endpoint_is_read_only_by_route(content_double, llm_double, lead_client):
    """There is no write route to the audit log — not even a wrong one."""
    case = _queued(llm_double)
    assert lead_client.post(f"{BASE}/cases/{case.id}/events").status_code == 405


# ── Sanctions and appeals over HTTP ──────────────────────────────────


def test_a_lead_issues_and_lifts_a_standalone_sanction(
    content_double, lead_client, author_user
):
    issued = lead_client.post(
        f"{BASE}/sanctions",
        {
            "subject_user_id": str(author_user.pk),
            "kind": "suspended",
            "reason_code": "fraud",
            "duration_seconds": 3600,
        },
        format="json",
    )
    assert issued.status_code == 201, issued.data
    sanction_id = issued.data["id"]

    listed = lead_client.get(f"{BASE}/sanctions?subject_user_id={author_user.pk}")
    assert listed.status_code == 200 and len(listed.data) == 1

    lifted = lead_client.post(f"{BASE}/sanctions/{sanction_id}/lift", {}, format="json")
    assert lifted.status_code == 200
    assert lifted.data["state"] == "lifted"


def test_only_the_subject_may_appeal(
    content_double, llm_double, client_for, ts_lead, author_user, user
):
    case = _queued(llm_double, subject=author_user.pk)
    client_for(ts_lead).post(
        f"{BASE}/cases/{case.id}/verdict", {"decision": "rejected"}, format="json"
    )

    stranger = client_for(user).post(
        f"{BASE}/appeals/", {"case_id": str(case.id), "body": "Let me in"}, format="json"
    )
    assert stranger.status_code == 403
    assert stranger.data["localizable_error"] == "error.403.moderation_not_appellant"

    mine = client_for(author_user).post(
        f"{BASE}/appeals/",
        {"case_id": str(case.id), "body": "Here is the certificate."},
        format="json",
    )
    assert mine.status_code == 201
    assert mine.data["state"] == "open"


def test_an_appeal_is_decided_by_a_different_moderator(
    content_double, llm_double, client_for, ts_lead, author_user
):
    """DSA Art. 20 independence, over HTTP.

    Three actors, three clients: re-authenticating one shared client would
    quietly change who the earlier handles are, which is a false green
    waiting to happen in exactly the test that is supposed to prove two
    different people were involved.
    """
    from django.contrib.auth import get_user_model

    case = _queued(llm_double, subject=author_user.pk)
    decider = client_for(ts_lead)
    decider.post(
        f"{BASE}/cases/{case.id}/verdict", {"decision": "rejected"}, format="json"
    )

    appeal_id = client_for(author_user).post(
        f"{BASE}/appeals/", {"case_id": str(case.id), "body": "Licensed."}, format="json"
    ).data["id"]

    # The moderator who decided is refused ...
    refused = decider.post(
        f"{BASE}/appeals/{appeal_id}/resolve", {"outcome": "overturned"}, format="json"
    )
    assert refused.status_code == 403, refused.data
    assert refused.data["localizable_error"] == "error.403.moderation_same_actor"

    # ... a second pair of eyes is not.
    reviewer = get_user_model().objects.create_user(
        username="lead3", email="lead3@example.test", password="x", is_staff=True
    )
    reviewer.staff_roles = ["ts_lead"]
    resolved = client_for(reviewer).post(
        f"{BASE}/appeals/{appeal_id}/resolve",
        {"outcome": "overturned", "note": "Certificate checks out."},
        format="json",
    )
    assert resolved.status_code == 200, resolved.data
    assert resolved.data["state"] == "overturned"

    case.refresh_from_db()
    assert case.verdicts.order_by("-created_at").first().decision == (
        VerdictDecision.APPROVED
    )


def test_my_reports_and_my_appeals_are_scoped_to_me(
    content_double, llm_double, client_for, user, other_user
):
    llm_double["envelope"]["result"]["decision"] = "needs_review"
    mine_client = client_for(user)
    mine_client.post(
        f"{BASE}/reports/",
        {"target_type": "listing", "target_key": "42", "reason_code": "spam"},
        format="json",
    )

    mine = mine_client.get(f"{BASE}/reports/")
    assert len(mine.data) == 1

    theirs = client_for(other_user).get(f"{BASE}/reports/")
    assert theirs.data == []


def test_stats_answer_the_console_header(content_double, llm_double, lead_client):
    _queued(llm_double)
    response = lead_client.get(f"{BASE}/stats")
    assert response.status_code == 200
    assert response.data["open_total"] == 1
    assert response.data["by_target_type"]["listing"] == 1


def test_rescan_sends_a_decided_case_back_through_the_screener(
    content_double, llm_double, lead_client
):
    case = _queued(llm_double)
    lead_client.post(
        f"{BASE}/cases/{case.id}/verdict", {"decision": "approved"}, format="json"
    )
    case.refresh_from_db()
    assert case.state == CaseState.RESOLVED

    response = lead_client.post(f"{BASE}/cases/{case.id}/rescan")
    assert response.status_code == 202, response.data
    case.refresh_from_db()
    assert case.events.filter(kind="reopened").exists()
    # Every earlier verdict survives: the trail is append-only.
    assert case.verdicts.count() >= 3


# ── The refusals the contract promised and never raised ──────────────


def test_the_content_read_is_asked_on_behalf_of_the_moderator(
    content_double, llm_double, lead_client, ts_lead
):
    """``can_view_content`` exists to answer "may THIS person read it".

    It was being asked with ``actor_id=None``, so a deployment gating the
    read per moderator saw every card requested by nobody — the gate was
    decorative and its refusal was unreachable.
    """
    from stapel_core.comm import function

    from stapel_moderation.registry import register_target_type

    seen = {}

    @function("listings.moderation_can_view")
    def _can_view(payload):
        seen.update(payload)
        return {"allowed": str(payload.get("actor_id")) == str(ts_lead.pk)}

    register_target_type(
        "listing",
        {
            "intake_events": ["listing.submitted"],
            "id_field": "listing_id",
            "content_function": "listings.moderation_content",
            "can_view_content": "listings.moderation_can_view",
            "verdict_event": "moderation.completed",
        },
    )

    case = _queued(llm_double)
    response = lead_client.get(f"{BASE}/cases/{case.id}")
    assert response.status_code == 200
    assert seen["actor_id"] == str(ts_lead.pk)
    assert response.data["content"]["available"] is True


def test_a_read_the_target_refuses_renders_the_forbidden_branch(
    content_double, llm_double, lead_client
):
    from stapel_core.comm import function

    from stapel_moderation.registry import register_target_type

    @function("listings.moderation_can_view")
    def _can_view(payload):
        return {"allowed": False}

    register_target_type(
        "listing",
        {
            "intake_events": ["listing.submitted"],
            "id_field": "listing_id",
            "content_function": "listings.moderation_content",
            "can_view_content": "listings.moderation_can_view",
            "verdict_event": "moderation.completed",
        },
    )

    case = _queued(llm_double)
    response = lead_client.get(f"{BASE}/cases/{case.id}")
    assert response.status_code == 200
    assert response.data["content"]["available"] is False
    assert response.data["content"]["error"] == "forbidden"


def test_a_reason_that_does_not_apply_here_is_not_an_unknown_reason(
    content_double, auth_client
):
    """Two different mistakes, two different remedies: the code is nonsense
    (fix the request) versus the form offered a choice this type never took
    (reload the policy)."""
    from stapel_moderation.registry import register_reason

    register_reason(
        "counterfeit",
        {"severity": 2, "requires_description": False, "applies_to": ["review"]},
    )

    response = auth_client.post(
        f"{BASE}/reports/",
        {"target_type": "listing", "target_key": "42", "reason_code": "counterfeit"},
        format="json",
    )
    assert response.status_code == 400, response.data
    assert response.data["localizable_error"] == (
        "error.400.moderation_reason_not_applicable"
    )

    unknown = auth_client.post(
        f"{BASE}/reports/",
        {"target_type": "listing", "target_key": "42", "reason_code": "no_such_reason"},
        format="json",
    )
    assert unknown.status_code == 400
    assert unknown.data["localizable_error"] == "error.400.moderation_unknown_reason"


def test_a_moderator_cannot_release_a_lease_somebody_else_holds(
    content_double, llm_double, client_for, ts_lead
):
    """One console tab must not yank a case out from under the person
    reading it. The system sweeper still may — it passes no actor."""
    from django.contrib.auth import get_user_model

    case = _queued(llm_double)
    assert client_for(ts_lead).post(f"{BASE}/cases/{case.id}/claim").status_code == 200

    other = get_user_model().objects.create_user(
        username="lead4", email="lead4@example.test", password="x", is_staff=True
    )
    other.staff_roles = ["ts_lead"]
    refused = client_for(other).post(f"{BASE}/cases/{case.id}/release")
    assert refused.status_code == 409, refused.data
    assert refused.data["localizable_error"] == "error.409.moderation_not_claimant"

    case.refresh_from_db()
    assert str(case.claimed_by) == str(ts_lead.pk)

    mine = client_for(ts_lead).post(f"{BASE}/cases/{case.id}/release")
    assert mine.status_code == 200
    case.refresh_from_db()
    assert case.claimed_by is None
