"""A malformed id in an action payload must not become a poison pill.

``user.deleted``, ``staff.role.revoked`` and this module's own verdict echo
all address rows by a uuid that arrived as a plain string in a payload.
Django answers a key it cannot coerce with
``django.core.exceptions.ValidationError`` — NOT a subclass of ``ValueError``
— so an unguarded (or ``(ValueError, TypeError)``-only) handler lets it
escape, ``consume_actions`` re-raises it to the bus, and the event is
redelivered forever over a payload no retry can repair.

Pinned here: each handler ACKs the malformed payload and touches no rows.
"""
import types
import uuid

import pytest
from stapel_core.bus import Event
from stapel_core.comm import deliver

from stapel_moderation import services
from stapel_moderation.actions import (
    handle_own_verdict,
    handle_staff_role_revoked,
    handle_user_deleted,
)
from stapel_moderation.models import Case, CaseState, Report

pytestmark = pytest.mark.django_db

BAD_IDS = ["not-a-uuid", "", "  ", "['x']"]


def _event(**payload):
    return types.SimpleNamespace(payload=payload, event_id=str(uuid.uuid4()))


@pytest.fixture
def a_case(content_double, llm_double, user):
    llm_double["envelope"]["result"]["decision"] = "needs_review"
    services.submit_report(
        target_type="listing",
        target_key="42",
        reporter_id=user.pk,
        reason_code="harassment",
        description="They will not stop.",
    )
    return Case.objects.get()


def _snapshot():
    return (
        sorted(Case.objects.values_list("id", "state", "claimed_by")),
        sorted(Report.objects.values_list("id", "reporter_id", "description")),
    )


def test_user_deleted_with_a_malformed_id_acks_and_erases_nothing(a_case):
    before = _snapshot()
    for bad in BAD_IDS:
        deliver(
            Event(event_type="user.deleted", service="auth", payload={"user_id": bad})
        )
    assert _snapshot() == before


def test_user_deleted_without_a_user_id_acks(a_case):
    before = _snapshot()
    handle_user_deleted(_event())
    assert _snapshot() == before


def test_staff_role_revoked_with_a_malformed_id_acks(a_case, moderator):
    services.claim_case(a_case, actor_id=moderator.pk)
    a_case.refresh_from_db()
    assert a_case.state == CaseState.CLAIMED
    before = _snapshot()

    for bad in BAD_IDS:
        handle_staff_role_revoked(_event(user_id=bad))

    assert _snapshot() == before


def test_own_verdict_with_a_malformed_case_id_acks(a_case):
    before = _snapshot()
    for bad in BAD_IDS:
        handle_own_verdict(_event(case_id=bad))
    assert _snapshot() == before


def test_a_real_erasure_still_runs(a_case, user):
    """The guard is narrow: a valid id still erases the reporter."""
    deliver(
        Event(
            event_type="user.deleted",
            service="auth",
            payload={"user_id": str(user.pk)},
        )
    )
    assert Report.objects.filter(reporter_id=None).exists()
    assert Case.objects.count() == 1
