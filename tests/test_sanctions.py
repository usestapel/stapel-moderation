"""Sanctions: the ladder, the teeth, and the projection's two halves.

Spec §14 assertion 11 lives here. The teeth are core's user blacklist — the
key that DRF authentication, the middleware (twice), channels and the auth
refresh endpoint all already check, and that had **no producer anywhere in
the fleet** until this module. Deactivating the account instead would touch
no live session at all: ``is_active`` is only consulted when a new token is
issued.
"""
import pytest
from django.core.cache import cache
from stapel_core.comm import mutate_and_emit

from stapel_moderation import services
from stapel_moderation.models import (
    Sanction,
    SanctionKind,
    SanctionState,
    VerdictDecision,
)

pytestmark = pytest.mark.django_db

BLACKLIST_KEY = "user_blacklisted:{user_id}"


def _blacklisted(user_id) -> bool:
    return cache.get(BLACKLIST_KEY.format(user_id=user_id)) is not None


def _case(key="42"):
    with mutate_and_emit() as emit_event:
        case, _ = services.open_case(
            "listing", key, origin="submission", emit_event=emit_event
        )
    return case


# ── Assertion 11: the blacklist is set, cleared and re-armed ─────────


def test_a_suspension_sets_the_blacklist_key(content_double, author_user, ts_lead):
    case = _case()
    services.issue_sanction(
        case=case,
        subject_user_id=author_user.pk,
        kind=SanctionKind.SUSPENDED,
        reason_code="fraud",
        duration_seconds=3600,
        issued_by=ts_lead.pk,
    )
    assert _blacklisted(author_user.pk)


def test_a_warning_does_not(content_double, author_user, ts_lead):
    """Only the kinds in BLACKLIST_KINDS kill sessions. A warning is a letter."""
    case = _case()
    services.issue_sanction(
        case=case,
        subject_user_id=author_user.pk,
        kind=SanctionKind.WARNING,
        issued_by=ts_lead.pk,
    )
    assert not _blacklisted(author_user.pk)


def test_lifting_clears_the_key(content_double, author_user, ts_lead):
    case = _case()
    sanction = services.issue_sanction(
        case=case,
        subject_user_id=author_user.pk,
        kind=SanctionKind.BANNED,
        issued_by=ts_lead.pk,
    )
    assert _blacklisted(author_user.pk)

    services.lift_sanction(sanction, actor_id=ts_lead.pk)
    assert not _blacklisted(author_user.pk)


def test_lifting_one_of_two_does_not_free_the_user(
    content_double, author_user, ts_lead
):
    """The bug a bare unblacklist_user() call would ship."""
    first = services.issue_sanction(
        case=_case("1"),
        subject_user_id=author_user.pk,
        kind=SanctionKind.SUSPENDED,
        issued_by=ts_lead.pk,
    )
    services.issue_sanction(
        case=_case("2"),
        subject_user_id=author_user.pk,
        kind=SanctionKind.BANNED,
        issued_by=ts_lead.pk,
    )

    services.lift_sanction(first, actor_id=ts_lead.pk)
    assert _blacklisted(author_user.pk)


def test_rearm_restores_a_key_the_cache_ttl_dropped(
    content_double, author_user, ts_lead
):
    """The row is the truth; the cache key is enforcement with a clock on it.

    Without the beat job every suspension silently stops being enforced after
    BLACKLIST_TTL_SECONDS while the row still reads 'active' — which is why
    checks.W004 calls that out by name.
    """
    from stapel_moderation.tasks import rearm_active_sanctions

    case = _case()
    services.issue_sanction(
        case=case,
        subject_user_id=author_user.pk,
        kind=SanctionKind.SUSPENDED,
        issued_by=ts_lead.pk,
    )
    cache.delete(BLACKLIST_KEY.format(user_id=author_user.pk))
    assert not _blacklisted(author_user.pk)

    assert rearm_active_sanctions()["rearmed"] == 1
    assert _blacklisted(author_user.pk)


def test_expiry_flips_the_row_and_drops_the_key(content_double, author_user, ts_lead):
    from django.utils import timezone

    from stapel_moderation.tasks import expire_sanctions

    case = _case()
    sanction = services.issue_sanction(
        case=case,
        subject_user_id=author_user.pk,
        kind=SanctionKind.SUSPENDED,
        duration_seconds=3600,
        issued_by=ts_lead.pk,
    )
    Sanction.objects.filter(pk=sanction.pk).update(
        expires_at=timezone.now() - timezone.timedelta(seconds=1)
    )

    assert expire_sanctions()["expired"] == 1
    sanction.refresh_from_db()
    assert sanction.state == SanctionState.EXPIRED
    assert not _blacklisted(author_user.pk)


# ── The ladder ───────────────────────────────────────────────────────


def test_the_ladder_escalates_and_then_repeats(content_double, author_user, ts_lead):
    """The n-th sanction takes the n-th rung; the last rung repeats, so a
    two-step ladder does not silently become permanent on the third strike."""
    assert services.ladder_duration(author_user.pk, "posting_restricted") == 86400

    services.issue_sanction(
        case=_case("1"),
        subject_user_id=author_user.pk,
        kind=SanctionKind.POSTING_RESTRICTED,
        issued_by=ts_lead.pk,
    )
    assert services.ladder_duration(author_user.pk, "posting_restricted") == 604800

    services.issue_sanction(
        case=_case("2"),
        subject_user_id=author_user.pk,
        kind=SanctionKind.POSTING_RESTRICTED,
        issued_by=ts_lead.pk,
    )
    services.issue_sanction(
        case=_case("3"),
        subject_user_id=author_user.pk,
        kind=SanctionKind.POSTING_RESTRICTED,
        issued_by=ts_lead.pk,
    )
    assert services.ladder_duration(author_user.pk, "posting_restricted") == 2592000
    assert services.ladder_duration(author_user.pk, "warning") is None


def test_a_verdict_can_carry_its_consequence(content_double, llm_double, ts_lead, author_user):
    """One request, one transaction: take it down and suspend the seller."""
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
        reason_code="fraud",
        actor_id=ts_lead.pk,
        sanction={"kind": SanctionKind.SUSPENDED, "duration_seconds": 3600},
    )

    sanction = Sanction.objects.get()
    assert str(sanction.subject_user_id) == str(author_user.pk)
    assert sanction.case_id == case.id
    assert _blacklisted(author_user.pk)
    assert case.events.filter(kind="sanctioned").exists()


def test_a_standalone_sanction_still_gets_a_case(content_double, author_user, ts_lead):
    """Sanction.case is non-null and PROTECTed: there is no shape of this API
    in which a ban exists without a case, a CaseEvent and a reason."""
    sanction = services.issue_standalone_sanction(
        subject_user_id=author_user.pk,
        kind=SanctionKind.BANNED,
        reason_code="illegal",
        issued_by=ts_lead.pk,
    )
    assert sanction.case_id is not None
    assert sanction.case.origin == "manual"
    assert sanction.case.events.filter(kind="sanctioned").exists()


def test_an_unknown_kind_is_refused(content_double, author_user, ts_lead):
    with pytest.raises(services.InvalidSanctionKind):
        services.issue_sanction(
            case=_case(),
            subject_user_id=author_user.pk,
            kind="exile",
            issued_by=ts_lead.pk,
        )


# ── The read surface and its projection ──────────────────────────────


def test_check_sanctions_answers_allowed_and_the_list(
    content_double, author_user, ts_lead
):
    from stapel_core.comm import call

    assert call("moderation.check_sanctions", {"user_id": str(author_user.pk)}) == {
        "allowed": True,
        "sanctions": [],
    }

    services.issue_sanction(
        case=_case(),
        subject_user_id=author_user.pk,
        kind=SanctionKind.POSTING_RESTRICTED,
        reason_code="spam",
        duration_seconds=3600,
        issued_by=ts_lead.pk,
    )
    answer = call("moderation.check_sanctions", {"user_id": str(author_user.pk)})
    assert answer["allowed"] is False
    assert answer["sanctions"][0]["kind"] == "posting_restricted"


def test_scoped_sanctions_only_answer_their_own_scope(
    content_double, author_user, ts_lead
):
    services.issue_sanction(
        case=_case(),
        subject_user_id=author_user.pk,
        kind=SanctionKind.POSTING_RESTRICTED,
        scope="listing",
        issued_by=ts_lead.pk,
    )
    assert services.sanction_snapshot(author_user.pk, scope="listing")["allowed"] is False
    assert services.sanction_snapshot(author_user.pk, scope="review")["allowed"] is True


def test_the_export_answers_rows_not_items(content_double, author_user, ts_lead):
    """Spec §22.1, the mistake that costs a whole projection table.

    core's ``_iter_snapshot`` reads ``resp["rows"]``. An ``{items, cursor}``
    answer rebuilds the table to EMPTY and reports success while doing it.
    """
    from stapel_core.comm import call

    services.issue_sanction(
        case=_case(),
        subject_user_id=author_user.pk,
        kind=SanctionKind.SUSPENDED,
        issued_by=ts_lead.pk,
    )
    answer = call("moderation.sanctions_export", {})

    assert "rows" in answer and "items" not in answer
    assert answer["total"] == 1
    row = answer["rows"][0]
    assert row["subject_user_id"] == str(author_user.pk)
    # Unix MILLISECONDS — the Event-timestamp clock, so a live fact arriving
    # mid-rebuild supersedes the snapshot row instead of racing it.
    assert row["seq"] > 1_700_000_000_000


def test_the_live_query_omits_unsanctioned_users(content_double, author_user, user, ts_lead):
    """The live_query contract: absent means unsanctioned, so a caller can
    tell it from 'unknown'."""
    from stapel_core.comm import call

    services.issue_sanction(
        case=_case(),
        subject_user_id=author_user.pk,
        kind=SanctionKind.SUSPENDED,
        issued_by=ts_lead.pk,
    )
    answer = call(
        "moderation.sanctions_by_users",
        {"keys": [str(author_user.pk), str(user.pk)]},
    )
    assert str(author_user.pk) in answer
    assert str(user.pk) not in answer


def test_both_projection_halves_answer_the_same_shape(content_double, author_user, ts_lead):
    """The stapel-shop defect, refused here: local mode returned {avg, count}
    and remote mode {rating_avg, rating_count}, so the two modes of one
    declaration disagreed and rebuild() raised TypeError."""
    from stapel_core.comm import call, projection_registry

    services.issue_sanction(
        case=_case(),
        subject_user_id=author_user.pk,
        kind=SanctionKind.SUSPENDED,
        issued_by=ts_lead.pk,
    )
    projection = projection_registry.get("moderation.user_sanctions")

    live = call("moderation.sanctions_by_users", {"keys": [str(author_user.pk)]})[
        str(author_user.pk)
    ]
    snapshot_row = call("moderation.sanctions_export", {})["rows"][0]
    from_snapshot = projection.from_snapshot(snapshot_row)

    assert set(live) == set(from_snapshot) == {"allowed", "sanctions"}


def test_the_permission_class_a_host_hangs_on_its_own_views(
    content_double, author_user, ts_lead, rf
):
    """Moderation answers; the host refuses. Gating publication inside
    stapel-listings would be a decision about the listings API."""
    from stapel_moderation.authz import NotSanctioned

    request = rf.post("/listings")
    request.user = author_user

    assert NotSanctioned().has_permission(request, None) is True

    services.issue_sanction(
        case=_case(),
        subject_user_id=author_user.pk,
        kind=SanctionKind.POSTING_RESTRICTED,
        issued_by=ts_lead.pk,
    )
    assert NotSanctioned().has_permission(request, None) is False
