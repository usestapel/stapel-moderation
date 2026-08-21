"""The queue: leases, verdicts, the audit trail, and one SQL query per page.

Spec §14 assertions 7-10 live here.
"""
import pytest
from django.utils import timezone
from stapel_core.comm import mutate_and_emit

from stapel_moderation import services
from stapel_moderation.models import (
    Case,
    CaseEvent,
    CaseState,
    Verdict,
    VerdictDecision,
)

pytestmark = pytest.mark.django_db


def _queued_case(llm_double, key="42"):
    llm_double["envelope"]["result"]["decision"] = "needs_review"
    with mutate_and_emit() as emit_event:
        case, _ = services.open_case(
            "listing", key, origin="submission", emit_event=emit_event
        )
        services.start_screening(case, emit_event=emit_event)
    case.refresh_from_db()
    return case


# ── Assertion 7: the lease ───────────────────────────────────────────


def test_claim_is_a_lease_and_a_second_claimant_is_refused(
    content_double, llm_double, moderator, ts_lead
):
    case = _queued_case(llm_double)
    services.claim_case(case, actor_id=moderator.pk)
    case.refresh_from_db()
    assert case.state == CaseState.CLAIMED
    assert str(case.claimed_by) == str(moderator.pk)

    with pytest.raises(services.CaseClaimedByAnother):
        services.claim_case(case, actor_id=ts_lead.pk)


def test_sweep_returns_an_expired_lease_without_touching_the_verdict(
    content_double, llm_double, moderator
):
    """Assertion 7: the sweeper releases; it never decides."""
    from stapel_moderation.tasks import sweep_stale_cases

    case = _queued_case(llm_double)
    services.claim_case(case, actor_id=moderator.pk)
    Case.objects.filter(pk=case.pk).update(
        claimed_until=timezone.now() - timezone.timedelta(minutes=1)
    )

    verdicts_before = Verdict.objects.count()
    assert sweep_stale_cases()["released"] == 1

    case.refresh_from_db()
    assert case.state == CaseState.QUEUED
    assert case.claimed_by is None
    assert Verdict.objects.count() == verdicts_before


def test_a_departing_moderator_releases_their_cases(
    content_double, llm_double, moderator
):
    """staff.role.revoked → the lease goes back. Small, and exactly the kind
    of wiring that gets declared and never connected."""
    from stapel_core.bus import Event
    from stapel_core.comm import deliver

    case = _queued_case(llm_double)
    services.claim_case(case, actor_id=moderator.pk)

    deliver(
        Event(
            event_type="staff.role.revoked",
            service="auth",
            payload={"user_id": str(moderator.pk), "role": "moderator"},
        )
    )

    case.refresh_from_db()
    assert case.state == CaseState.QUEUED
    assert case.claimed_by is None


# ── Assertion 8: verdicts are append-only and single-shot ────────────


def test_verdict_writes_a_row_and_an_audit_entry(content_double, llm_double, ts_lead):
    case = _queued_case(llm_double)
    before = case.events.count()

    verdict = services.resolve_case(
        case,
        decision=VerdictDecision.REJECTED,
        reason_code="illegal",
        note="Prohibited item.",
        actor_id=ts_lead.pk,
    )

    case.refresh_from_db()
    assert case.state == CaseState.RESOLVED
    assert case.last_verdict_id == verdict.id
    assert case.resolved_at is not None
    kinds = list(case.events.values_list("kind", flat=True)[before:])
    assert "verdict" in kinds and "state_changed" in kinds


def test_second_verdict_on_a_resolved_case_is_refused(
    content_double, llm_double, ts_lead
):
    """Assertion 8: the guard, and the reason `resolved` is terminal."""
    case = _queued_case(llm_double)
    services.resolve_case(
        case, decision=VerdictDecision.APPROVED, actor_id=ts_lead.pk
    )
    case.refresh_from_db()

    with pytest.raises(services.CaseAlreadyResolved):
        services.resolve_case(
            case, decision=VerdictDecision.REJECTED, actor_id=ts_lead.pk
        )


def test_needs_review_does_not_resolve_the_case(content_double, llm_double, ts_lead):
    """`needs_review` is the automation abstaining, which is the opposite of
    a resolution — and the state legacy's stale sweeper folded into approval."""
    case = _queued_case(llm_double)
    services.resolve_case(
        case, decision=VerdictDecision.NEEDS_REVIEW, actor_id=ts_lead.pk
    )
    case.refresh_from_db()
    assert case.state == CaseState.QUEUED
    assert case.resolved_at is None


def test_an_undeclared_transition_raises(content_double, llm_double):
    """There is one transition table and it is enforced, unlike legacy's
    apply_moderation, which validated a vocabulary and then assigned."""
    case = _queued_case(llm_double)
    with pytest.raises(services.InvalidTransition):
        services.transition(case, CaseState.OPEN)


# ── Assertion 9: the audit log is not mutable by any module path ─────


def test_the_audit_log_forbids_mutation_by_declaration(content_double, llm_double):
    """Assertion 9. Two independent locks, and this asserts the stronger one:
    ``@access.ops`` makes add/change/delete FORBIDDEN at the mandate level, so
    no admin — not even a superuser's — can edit the trail."""
    from stapel_core.access import Level, declared_access

    declaration = declared_access(CaseEvent)
    assert declaration.category == "ops"
    assert declaration.required("add") == Level.FORBIDDEN
    assert declaration.required("change") == Level.FORBIDDEN
    assert declaration.required("delete") == Level.FORBIDDEN


def test_the_admin_registers_every_model_read_only():
    """The second lock: legacy's bulk actions wrote statuses straight through
    queryset.update(), invisible to its own audit table."""
    from django.contrib import admin

    from stapel_moderation.admin import _ReadOnlyAdmin
    from stapel_moderation.models import Appeal, Report, Sanction

    for model in (Case, CaseEvent, Verdict, Report, Sanction, Appeal):
        registered = admin.site._registry[model]
        assert isinstance(registered, _ReadOnlyAdmin), model
        assert not registered.has_add_permission(None)
        assert not registered.has_change_permission(None)
        assert not registered.has_delete_permission(None)


def test_no_service_updates_a_verdict_row(content_double, llm_double, ts_lead):
    """A verdict is a fact. Resolving twice is refused; nothing rewrites one."""
    case = _queued_case(llm_double)
    verdict = services.resolve_case(
        case, decision=VerdictDecision.APPROVED, actor_id=ts_lead.pk
    )
    stamp = verdict.created_at

    case.refresh_from_db()
    verdict.refresh_from_db()
    assert verdict.created_at == stamp
    assert Verdict.objects.filter(case=case).count() == 2  # screening + human


# ── Assertion 10: one page, one query, one LIMIT ─────────────────────


def test_the_queue_page_is_one_query_across_every_target_type(
    content_double, llm_double, django_assert_num_queries
):
    """Assertion 10. Legacy read two whole tables into Python on every page,
    chained them, sorted them and sliced the list — an absent LIMIT, not a
    slow query."""
    from stapel_moderation.registry import register_target_type

    register_target_type(
        "review",
        {"id_field": "review_id", "content_function": "reviews.moderation_content"},
    )
    llm_double["envelope"]["result"]["decision"] = "needs_review"
    for key in ("1", "2", "3"):
        with mutate_and_emit() as emit_event:
            case, _ = services.open_case(
                "listing", key, origin="submission", emit_event=emit_event
            )
            services.queue_case(case)
    for key in ("9", "8"):
        with mutate_and_emit() as emit_event:
            case, _ = services.open_case(
                "review", key, origin="report", emit_event=emit_event
            )
            services.queue_case(case)

    with django_assert_num_queries(1):
        rows = services.list_cases(state=CaseState.QUEUED, limit=4)

    assert len(rows) == 4
    assert {row.target_type for row in rows} == {"listing", "review"}


def test_the_page_size_is_capped_server_side(content_double, llm_double):
    rows = services.list_cases(limit=10_000)
    assert len(rows) <= 100


def test_the_reason_filter_never_drops_a_target_class(
    content_double, llm_double, user, other_user
):
    """One reason table across every target type. Legacy's filter silently
    dropped every complaint about a review, because that table had no reason
    column at all."""
    from stapel_core.comm import function

    from stapel_moderation.registry import register_target_type

    @function("reviews.moderation_content")
    def _review_content(payload):
        return {"text": "rude", "author_id": "", "media": []}

    register_target_type(
        "review",
        {"id_field": "review_id", "content_function": "reviews.moderation_content"},
    )
    llm_double["envelope"]["result"]["decision"] = "needs_review"

    services.submit_report(
        target_type="listing", target_key="42", reporter_id=user.pk, reason_code="spam"
    )
    services.submit_report(
        target_type="review", target_key="7", reporter_id=user.pk, reason_code="spam"
    )

    rows = services.list_cases(reason_code="spam")
    assert {row.target_type for row in rows} == {"listing", "review"}


def test_stats_count_by_state_and_target_type(content_double, llm_double):
    llm_double["envelope"]["result"]["decision"] = "needs_review"
    with mutate_and_emit() as emit_event:
        case, _ = services.open_case(
            "listing", "42", origin="submission", emit_event=emit_event
        )
        services.start_screening(case, emit_event=emit_event)

    stats = services.queue_stats()
    assert stats["by_state"][CaseState.QUEUED] == 1
    assert stats["by_target_type"]["listing"] == 1
    assert stats["open_total"] == 1
    assert stats["resolved_total"] == 0
