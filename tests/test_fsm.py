"""The case state machine, walked over every (state, action) pair.

``CASE_ACTIONS`` is what the API tells a console it may do; this file holds
the service layer to it in both directions: every listed pair succeeds, every
other pair is refused. A console that renders its buttons from ``actions``
therefore never offers a button the backend answers with 400.
"""
import itertools
from datetime import timedelta

import pytest
from django.utils import timezone
from stapel_core.comm import mutate_and_emit

from stapel_moderation import services
from stapel_moderation.models import (
    CASE_ACTIONS,
    CASE_TRANSITIONS,
    Case,
    CaseAction,
    CaseState,
    VerdictDecision,
)

pytestmark = pytest.mark.django_db

BASE = "/moderation/api/v1"


def _case_in(state, *, holder=None, key="42"):
    with mutate_and_emit() as emit_event:
        case, _ = services.open_case("listing", key, origin="submission", emit_event=emit_event)
    fields = {"state": state}
    if state == CaseState.CLAIMED:
        fields.update(claimed_by=holder.pk, claimed_until=timezone.now() + timedelta(minutes=5))
    if state == CaseState.RESOLVED:
        fields.update(resolved_at=timezone.now())
    if state == CaseState.DLQ:
        fields.update(
            dlq_at=timezone.now(),
            last_error_class="MediaUnavailable",
            last_error="2 media ref(s) unresolved: product/a, product/b",
        )
    Case.objects.filter(pk=case.pk).update(**fields)
    case.refresh_from_db()
    return case


def _do(action, case, actor):
    if action == CaseAction.CLAIM:
        services.claim_case(case, actor_id=actor.pk)
    elif action == CaseAction.RELEASE:
        services.release_case(case, actor_id=actor.pk)
    elif action == CaseAction.RESOLVE:
        services.resolve_case(case, decision=VerdictDecision.APPROVED, actor_id=actor.pk)
    elif action == CaseAction.RESCAN:
        services.rescan_case(case, actor_id=actor.pk)
    else:  # pragma: no cover - the product below is over CaseAction
        raise AssertionError(action)


def test_every_state_has_a_declared_action_row():
    assert set(CASE_ACTIONS) == set(CaseState.values)
    assert set(CASE_TRANSITIONS) == set(CaseState.values)


@pytest.mark.parametrize(
    "state,action", list(itertools.product(CaseState.values, CaseAction.values))
)
def test_the_service_layer_honours_the_action_table(
    state, action, content_double, llm_double, moderator
):
    case = _case_in(state, holder=moderator)
    if action in CASE_ACTIONS[state]:
        _do(action, case, moderator)
    else:
        with pytest.raises(services.ModerationError):
            _do(action, case, moderator)
        case.refresh_from_db()
        assert case.state == state, "a refused action leaves the case where it was"


def test_a_dead_lettered_case_offers_a_claim():
    assert CaseAction.CLAIM in CASE_ACTIONS[CaseState.DLQ]
    assert CaseAction.RESOLVE in CASE_ACTIONS[CaseState.DLQ]
    assert CaseAction.RESCAN in CASE_ACTIONS[CaseState.DLQ]


def test_a_moderator_takes_and_decides_a_dead_lettered_case_over_http(
    content_double, llm_double, ts_lead, client_for
):
    """The console path: "Take this case", then "Record the decision".

    The dead-letter reason stays on the case while a person works it, and a
    claim handed back lands in the human queue rather than in the park.
    """
    client = client_for(ts_lead)
    case = _case_in(CaseState.DLQ)

    card = client.get(f"{BASE}/cases/{case.id}")
    assert card.status_code == 200
    assert card.data["actions"] == ["claim", "resolve", "rescan"]

    claimed = client.post(f"{BASE}/cases/{case.id}/claim")
    assert claimed.status_code == 200, claimed.data
    assert claimed.data["state"] == CaseState.CLAIMED
    assert claimed.data["actions"] == ["claim", "release", "resolve", "rescan"]
    assert claimed.data["last_error_class"] == "MediaUnavailable"
    assert claimed.data["dlq_at"] is not None

    released = client.post(f"{BASE}/cases/{case.id}/release")
    assert released.status_code == 200
    assert released.data["state"] == CaseState.QUEUED, "back to the human queue"
    assert released.data["last_error"].startswith("2 media ref(s)")

    assert client.post(f"{BASE}/cases/{case.id}/claim").status_code == 200
    verdict = client.post(
        f"{BASE}/cases/{case.id}/verdict", {"decision": "approved"}, format="json"
    )
    assert verdict.status_code == 201, verdict.data
    case.refresh_from_db()
    assert case.state == CaseState.RESOLVED
    assert case.last_error_class == "MediaUnavailable", "the reason survives the decision"


def test_releasing_a_case_nobody_holds_is_refused_for_a_person(
    content_double, llm_double, ts_lead, client_for
):
    case = _case_in(CaseState.QUEUED)
    response = client_for(ts_lead).post(f"{BASE}/cases/{case.id}/release")
    assert response.status_code == 400
    # The system sweepers still release idempotently.
    services.release_case(case)


def test_the_queue_row_carries_the_actions_too(content_double, llm_double, client_for, ts_lead):
    _case_in(CaseState.DLQ)
    rows = client_for(ts_lead).get(f"{BASE}/cases?state=dlq").data
    assert rows[0]["actions"] == ["claim", "resolve", "rescan"]
