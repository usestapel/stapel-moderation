"""``user.merged``: a merged-away account's moderation history follows it.

The other half of the account life cycle ``user.deleted`` already answered,
and the opposite instruction. Erasure detaches the complainant from their
complaint; a merge re-parents everything onto the account that absorbed the
guest. The load-bearing half is the sanction: if a banned guest could shed
their ban by signing in with an authenticator an existing account holds,
"merge" would be a one-click ban-evasion route and the progressive ladder's
memory would reset with it.

Pinned here: every column moves, a redelivery moves nothing further, the two
uniqueness constraints resolve instead of raising an IntegrityError the bus
would redeliver forever, a malformed payload is ACKed, and an event naming
users this module has no rows for does nothing.
"""
import types
import uuid

import pytest
from stapel_core.comm import mutate_and_emit

from stapel_moderation import services
from stapel_moderation.actions import handle_user_merged
from stapel_moderation.models import (
    Appeal,
    AppealState,
    Case,
    CaseEvent,
    CaseEventKind,
    CaseState,
    Report,
    Sanction,
    SanctionKind,
    SanctionState,
    Verdict,
    VerdictDecision,
    VerdictSource,
)

pytestmark = pytest.mark.django_db

BAD_IDS = ["not-a-uuid", "", "  ", "['x']"]

GUEST = uuid.uuid4()
SURVIVOR = uuid.uuid4()
STRANGER = uuid.uuid4()


def _event(**payload):
    return types.SimpleNamespace(payload=payload, event_id=str(uuid.uuid4()))


def _case(key="42", **kwargs):
    return Case.objects.create(
        target_type="listing", target_key=key, state=CaseState.RESOLVED, **kwargs
    )


def _report(case, reporter_id, reason_code="harassment"):
    Case.objects.filter(pk=case.pk).update(report_count=case.report_count + 1)
    case.refresh_from_db()
    return Report.objects.create(
        case=case,
        target_type=case.target_type,
        target_key=case.target_key,
        reporter_id=reporter_id,
        reason_code=reason_code,
        description="They will not stop.",
    )


def _sanction(case, subject_user_id, issued_by=None, **kwargs):
    from django.utils import timezone

    return Sanction.objects.create(
        case=case,
        subject_user_id=subject_user_id,
        kind=kwargs.pop("kind", SanctionKind.SUSPENDED),
        reason_code="fraud",
        starts_at=timezone.now(),
        issued_by=issued_by,
        **kwargs,
    )


def _snapshot():
    return (
        sorted(Case.objects.values_list("id", "subject_user_id", "claimed_by")),
        sorted(Report.objects.values_list("id", "reporter_id")),
        sorted(Verdict.objects.values_list("id", "actor_id")),
        sorted(CaseEvent.objects.values_list("id", "actor_id")),
        sorted(
            Sanction.objects.values_list(
                "id", "subject_user_id", "issued_by", "lifted_by"
            )
        ),
        sorted(Appeal.objects.values_list("id", "appellant_id", "resolved_by")),
    )


@pytest.fixture
def guest_history():
    """One row of every user-keyed shape, all naming the guest."""
    case = _case(subject_user_id=GUEST, claimed_by=GUEST)
    report = _report(case, GUEST)
    verdict = Verdict.objects.create(
        case=case,
        decision=VerdictDecision.REJECTED,
        source=VerdictSource.HUMAN,
        actor_id=GUEST,
        reason_code="counterfeit",
    )
    log = CaseEvent.objects.create(
        case=case, kind=CaseEventKind.REPORTED, actor_id=GUEST
    )
    sanction = _sanction(case, GUEST, issued_by=GUEST)
    appeal = Appeal.objects.create(
        case=case, appellant_id=GUEST, body="It was not me.", resolved_by=GUEST
    )
    return case, report, verdict, log, sanction, appeal


class TestHappyPath:
    def test_every_user_keyed_column_is_re_parented(self, guest_history):
        case, report, verdict, log, sanction, appeal = guest_history

        handle_user_merged(_event(from_user_id=str(GUEST), into_user_id=str(SURVIVOR)))

        for row in (case, report, verdict, log, sanction, appeal):
            row.refresh_from_db()
        assert case.subject_user_id == SURVIVOR
        assert case.claimed_by == SURVIVOR
        assert report.reporter_id == SURVIVOR
        assert verdict.actor_id == SURVIVOR
        assert log.actor_id == SURVIVOR
        assert sanction.subject_user_id == SURVIVOR
        assert sanction.issued_by == SURVIVOR
        assert appeal.appellant_id == SURVIVOR
        assert appeal.resolved_by == SURVIVOR

    def test_a_lifted_by_column_moves_too(self):
        case = _case()
        sanction = _sanction(
            case, STRANGER, lifted_by=GUEST, state=SanctionState.LIFTED
        )

        handle_user_merged(_event(from_user_id=GUEST, into_user_id=SURVIVOR))

        sanction.refresh_from_db()
        assert sanction.lifted_by == SURVIVOR
        assert sanction.subject_user_id == STRANGER

    def test_a_third_partys_rows_are_untouched(self, guest_history):
        other_case = _case(key="99", subject_user_id=STRANGER)
        other_report = _report(other_case, STRANGER)

        handle_user_merged(_event(from_user_id=GUEST, into_user_id=SURVIVOR))

        other_case.refresh_from_db()
        other_report.refresh_from_db()
        assert other_case.subject_user_id == STRANGER
        assert other_report.reporter_id == STRANGER

    def test_the_audit_trail_is_kept_not_erased(self, guest_history):
        _case_, report, *_ = guest_history

        handle_user_merged(_event(from_user_id=GUEST, into_user_id=SURVIVOR))

        report.refresh_from_db()
        assert report.description == "They will not stop."
        assert Verdict.objects.count() == 1
        assert CaseEvent.objects.count() == 1


class TestSanctionsFollowThePerson:
    """The ban-evasion half. A merge must not be a way to shed a sanction."""

    def test_an_active_ban_moves_onto_the_surviving_account(self):
        case = _case()
        _sanction(case, GUEST, kind=SanctionKind.BANNED)

        handle_user_merged(_event(from_user_id=GUEST, into_user_id=SURVIVOR))

        assert not services.sanction_snapshot(GUEST)["sanctions"]
        after = services.sanction_snapshot(SURVIVOR)
        assert after["allowed"] is False
        assert [row["kind"] for row in after["sanctions"]] == [SanctionKind.BANNED]

    def test_each_carried_sanction_is_re_announced_for_the_read_model(
        self, captured_events
    ):
        """The rows moved with a bulk UPDATE, which announces nothing. The
        ``moderation.user_sanctions`` projection keys on ``subject_user_id``
        and recomputes a user's whole row from one event, so a split topology
        only learns about the carried ban if it is announced."""
        case = _case()
        _sanction(case, GUEST, kind=SanctionKind.BANNED)
        captured_events.clear()

        handle_user_merged(_event(from_user_id=GUEST, into_user_id=SURVIVOR))

        issued = [
            e for e in captured_events if e.event_type == "moderation.sanction.issued"
        ]
        assert len(issued) == 1
        assert issued[0].payload["subject_user_id"] == str(SURVIVOR)

    def test_the_announcement_carries_a_fresh_ordering_token(self, captured_events):
        """``seq`` is the sanction's ``updated_at`` in unix ms. A bulk UPDATE
        does not touch an ``auto_now`` column, so the handler advances it —
        otherwise the projection discards the announcement as out of order."""
        case = _case()
        sanction = _sanction(case, GUEST, kind=SanctionKind.BANNED)
        before_seq = int(sanction.updated_at.timestamp() * 1000)
        captured_events.clear()

        handle_user_merged(_event(from_user_id=GUEST, into_user_id=SURVIVOR))

        issued = [
            e for e in captured_events if e.event_type == "moderation.sanction.issued"
        ]
        assert issued[0].payload["seq"] >= before_seq

    def test_an_expired_sanction_is_carried_but_not_re_announced(self, captured_events):
        case = _case()
        _sanction(case, GUEST, state=SanctionState.EXPIRED)
        captured_events.clear()

        handle_user_merged(_event(from_user_id=GUEST, into_user_id=SURVIVOR))

        assert Sanction.objects.get().subject_user_id == SURVIVOR
        assert not [
            e for e in captured_events if e.event_type == "moderation.sanction.issued"
        ]


class TestUniquenessCollisions:
    """Both constraints resolve to "they are one person now, so one row is
    correct". A blind re-point would be an IntegrityError, and on this bus an
    escaping exception is a payload redelivered forever."""

    def test_a_duplicate_report_is_dropped_and_the_count_stays_truthful(self):
        case = _case()
        _report(case, SURVIVOR)
        _report(case, GUEST)
        case.refresh_from_db()
        assert case.report_count == 2

        handle_user_merged(_event(from_user_id=GUEST, into_user_id=SURVIVOR))

        case.refresh_from_db()
        assert Report.objects.count() == 1
        assert Report.objects.get().reporter_id == SURVIVOR
        assert case.report_count == 1

    def test_a_report_on_a_target_the_survivor_never_reported_still_moves(self):
        case = _case()
        other = _case(key="99")
        _report(case, SURVIVOR)
        guest_report = _report(other, GUEST)

        handle_user_merged(_event(from_user_id=GUEST, into_user_id=SURVIVOR))

        guest_report.refresh_from_db()
        assert guest_report.reporter_id == SURVIVOR
        assert Report.objects.count() == 2

    def test_a_duplicate_appeal_is_dropped(self):
        case = _case()
        Appeal.objects.create(case=case, appellant_id=SURVIVOR, body="Mine.")
        Appeal.objects.create(case=case, appellant_id=GUEST, body="Also mine.")

        handle_user_merged(_event(from_user_id=GUEST, into_user_id=SURVIVOR))

        assert Appeal.objects.count() == 1
        assert Appeal.objects.get().appellant_id == SURVIVOR

    def test_an_appeal_on_another_case_still_moves(self):
        case = _case()
        other = _case(key="99")
        Appeal.objects.create(case=case, appellant_id=SURVIVOR, body="Mine.")
        guest_appeal = Appeal.objects.create(
            case=other, appellant_id=GUEST, body="Different case.", state=AppealState.OPEN
        )

        handle_user_merged(_event(from_user_id=GUEST, into_user_id=SURVIVOR))

        guest_appeal.refresh_from_db()
        assert guest_appeal.appellant_id == SURVIVOR
        assert Appeal.objects.count() == 2


class TestIdempotency:
    def test_a_redelivery_changes_nothing_further(self, guest_history):
        event = _event(from_user_id=GUEST, into_user_id=SURVIVOR)

        handle_user_merged(event)
        after_first = _snapshot()

        handle_user_merged(event)
        handle_user_merged(event)

        assert _snapshot() == after_first

    def test_a_redelivery_re_announces_nothing(self, captured_events):
        case = _case()
        _sanction(case, GUEST, kind=SanctionKind.BANNED)
        event = _event(from_user_id=GUEST, into_user_id=SURVIVOR)
        handle_user_merged(event)
        captured_events.clear()

        handle_user_merged(event)

        assert captured_events == []


class TestPoisonPayloads:
    """A raise here is a poison pill: the bus redelivers a payload no retry
    can repair. ``not-a-uuid`` is the one that bites — Django answers an
    uncoercible UUID with ``ValidationError``, which is NOT a ``ValueError``.
    """

    def test_a_malformed_from_id_acks_and_moves_nothing(self, guest_history):
        before = _snapshot()
        for bad in BAD_IDS:
            handle_user_merged(_event(from_user_id=bad, into_user_id=str(SURVIVOR)))
        assert _snapshot() == before

    def test_a_malformed_into_id_acks_and_moves_nothing(self, guest_history):
        before = _snapshot()
        for bad in BAD_IDS:
            handle_user_merged(_event(from_user_id=str(GUEST), into_user_id=bad))
        assert _snapshot() == before

    def test_a_missing_id_acks_and_moves_nothing(self, guest_history):
        before = _snapshot()
        handle_user_merged(_event())
        handle_user_merged(_event(from_user_id=str(GUEST)))
        handle_user_merged(_event(into_user_id=str(SURVIVOR)))
        assert _snapshot() == before

    def test_a_payload_that_is_not_a_mapping_acks(self, guest_history):
        handle_user_merged(types.SimpleNamespace(payload=None, event_id="evt-empty"))

    def test_a_self_merge_is_a_no_op(self, guest_history):
        case, *_ = guest_history
        handle_user_merged(_event(from_user_id=GUEST, into_user_id=GUEST))
        case.refresh_from_db()
        assert case.subject_user_id == GUEST


class TestUnknownUsers:
    def test_an_event_about_users_with_no_rows_here_does_nothing(self):
        case = _case(subject_user_id=STRANGER)
        before = _snapshot()

        handle_user_merged(_event(from_user_id=GUEST, into_user_id=SURVIVOR))

        case.refresh_from_db()
        assert case.subject_user_id == STRANGER
        assert _snapshot() == before

    def test_a_survivor_unknown_here_needs_no_local_row(self):
        """Unlike the FK-carrying modules, every actor here is a bare
        UUIDField — deliberately, so a moderation record survives the account
        it is about. There is nothing to wait for, so the transfer lands on
        the first delivery."""
        case = _case(subject_user_id=GUEST)
        unseen = uuid.uuid4()

        handle_user_merged(_event(from_user_id=GUEST, into_user_id=unseen))

        case.refresh_from_db()
        assert case.subject_user_id == unseen


class TestLifecycleCheck:
    """stapel_core.lifecycle.E001 — an app that answers ``user.deleted`` and
    not ``user.merged`` is a system-check ERROR. Registered here so the pair
    cannot be broken by a later refactor without a red test."""

    def test_the_lifecycle_pair_check_is_green(self):
        from stapel_core.comm.lifecycle_checks import check_lifecycle_pairs

        assert check_lifecycle_pairs() == []


def test_the_committed_consumes_schema_has_this_subscriber():
    """`tests/test_comm.py` asserts the reverse for every schema on disk; this
    is the pair for the one added with this handler."""
    from stapel_core.comm import action_registry

    assert action_registry._subscribers.get("user.merged")


def test_the_erasure_path_still_detaches_rather_than_moves(guest_history):
    """The two halves must not converge: `user.deleted` still nulls the
    complainant, `user.merged` still re-parents them."""
    _case_, report, *_ = guest_history
    with mutate_and_emit():
        services.erase_user_reports(GUEST)
    report.refresh_from_db()
    assert report.reporter_id is None
    assert report.description == ""
