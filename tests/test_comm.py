"""The comm surface: every topic has a schema, every helper has a call site.

The two structural assertions here are the ones a human reviewer cannot make
reliably. A declared emit with no schema passes every functional test and
fails only in production with VALIDATE_SCHEMAS on; a schema with no emitter
is a contract nobody keeps. Both are countable, so they are counted.
"""
import json
from pathlib import Path

import pytest
from stapel_core.comm import mutate_and_emit

from stapel_moderation import events, services
from stapel_moderation.models import SanctionKind, VerdictDecision

pytestmark = pytest.mark.django_db

SCHEMAS = Path(__file__).resolve().parent.parent / "schemas"


# ── Contract closure ─────────────────────────────────────────────────


def test_every_emitted_topic_has_a_committed_schema():
    for topic in events.EMITTED_TOPICS:
        assert (SCHEMAS / "emits" / f"{topic}.json").is_file(), topic


def test_every_emit_schema_has_an_emitting_topic():
    """No orphan contracts: a schema nobody emits is a promise nobody keeps."""
    on_disk = {path.stem for path in (SCHEMAS / "emits").glob("*.json")}
    assert on_disk == set(events.EMITTED_TOPICS)


def test_every_consumed_topic_has_a_committed_schema():
    """And every committed consumes-schema has a live subscriber."""
    from stapel_core.comm import action_registry

    on_disk = {path.stem for path in (SCHEMAS / "consumes").glob("*.json")}
    for topic in on_disk:
        assert action_registry._subscribers.get(topic), topic


def test_every_function_schema_has_a_provider():
    from stapel_core.comm.registry import function_registry

    on_disk = {path.stem for path in (SCHEMAS / "functions").glob("*.json")}
    for name in on_disk:
        assert name in function_registry._providers, name


def test_every_provider_has_a_committed_schema():
    from stapel_core.comm.registry import function_registry

    on_disk = {path.stem for path in (SCHEMAS / "functions").glob("*.json")}
    ours = {
        name
        for name in function_registry._providers
        if name.startswith("moderation.")
    }
    assert ours == on_disk


def test_every_schema_is_valid_json_with_a_description():
    """A contract without prose is a shape without a meaning."""
    for path in SCHEMAS.rglob("*.json"):
        doc = json.loads(path.read_text())
        assert doc.get("title") == path.stem, path
        assert len(doc.get("description", "")) > 40, path


# ── What actually rides the bus ──────────────────────────────────────


def test_a_verdict_fact_carries_identifiers_and_nothing_else(
    content_double, llm_double, ts_lead, user, captured_events
):
    """Spec §5.2: no complaint text, no complainant, no model reasoning, no
    evidence. A consumer that needs the case reads it under a mandate."""
    llm_double["envelope"]["result"]["decision"] = "needs_review"
    services.submit_report(
        target_type="listing",
        target_key="42",
        reporter_id=user.pk,
        reason_code="harassment",
        description="SECRET COMPLAINT TEXT",
    )
    case = services.list_cases()[0]
    captured_events.clear()

    services.resolve_case(
        case,
        decision=VerdictDecision.REJECTED,
        reason_code="harassment",
        note="Targeted abuse.",
        actor_id=ts_lead.pk,
    )

    completed = [e for e in captured_events if e.event_type == "moderation.completed"][0]
    assert set(completed.payload) == {
        "case_id",
        "target_type",
        "target_key",
        "decision",
        "reason_code",
        "note",
        "source",
        "decided_at",
    }
    blob = json.dumps(completed.payload)
    assert "SECRET COMPLAINT TEXT" not in blob
    assert str(user.pk) not in blob

    reviewed = [e for e in captured_events if e.event_type == "moderation.report.reviewed"]
    assert reviewed and "reporter_id" not in reviewed[0].payload


def test_the_wire_note_is_truncated(content_double, llm_double, ts_lead, settings, captured_events):
    settings.STAPEL_MODERATION = {"VERDICT_NOTE_WIRE_CHARS": 10}
    with mutate_and_emit() as emit_event:
        case, _ = services.open_case(
            "listing", "42", origin="submission", emit_event=emit_event
        )
    captured_events.clear()

    services.resolve_case(
        case,
        decision=VerdictDecision.REJECTED,
        note="A very long statement of reasons that nobody wants on a bus.",
        actor_id=ts_lead.pk,
    )
    payload = [e for e in captured_events if e.event_type == "moderation.completed"][0].payload
    assert payload["note"] == "A very lon"


def test_the_partition_key_groups_a_target(content_double, llm_double, ts_lead, captured_events):
    """Everything about one target stays ordered relative to itself; sanctions
    key on the subject instead, for the same reason about a person."""
    with mutate_and_emit() as emit_event:
        case, _ = services.open_case(
            "listing", "42", origin="submission", emit_event=emit_event
        )
    captured_events.clear()
    services.resolve_case(case, decision=VerdictDecision.APPROVED, actor_id=ts_lead.pk)

    completed = [e for e in captured_events if e.event_type == "moderation.completed"][0]
    assert completed.key == "listing:42"


def test_a_sanction_fact_keys_on_the_subject(
    content_double, ts_lead, author_user, captured_events
):
    with mutate_and_emit() as emit_event:
        case, _ = services.open_case(
            "listing", "42", origin="submission", emit_event=emit_event
        )
    captured_events.clear()
    services.issue_sanction(
        case=case,
        subject_user_id=author_user.pk,
        kind=SanctionKind.SUSPENDED,
        note="A private note nobody else needs.",
        issued_by=ts_lead.pk,
    )

    issued = [e for e in captured_events if e.event_type == "moderation.sanction.issued"][0]
    assert issued.key == str(author_user.pk)
    assert "note" not in issued.payload


def test_a_policy_may_declare_that_no_verdict_travels(
    content_double, llm_double, ts_lead, captured_events
):
    """``verdict_event: None`` is a statement — "this target consumes no
    verdict" — and the module honours it rather than emitting anyway."""
    from stapel_moderation.registry import register_target_type

    register_target_type(
        "listing",
        {
            "id_field": "listing_id",
            "content_function": "listings.moderation_content",
            "verdict_event": None,
        },
    )
    with mutate_and_emit() as emit_event:
        case, _ = services.open_case(
            "listing", "42", origin="submission", emit_event=emit_event
        )
    captured_events.clear()
    services.resolve_case(case, decision=VerdictDecision.REJECTED, actor_id=ts_lead.pk)

    assert not [e for e in captured_events if e.event_type == "moderation.completed"]


def test_a_policy_may_route_the_verdict_to_its_own_topic(
    content_double, llm_double, ts_lead
):
    from stapel_core.comm import action_registry, subscribe_action

    from stapel_moderation.registry import register_target_type

    seen = []

    def _collect(event):
        seen.append(event)

    subscribe_action("moderation.completed", _collect)
    register_target_type(
        "listing",
        {
            "id_field": "listing_id",
            "content_function": "listings.moderation_content",
            "verdict_event": "moderation.completed",
        },
    )
    try:
        with mutate_and_emit() as emit_event:
            case, _ = services.open_case(
                "listing", "42", origin="submission", emit_event=emit_event
            )
        services.resolve_case(
            case, decision=VerdictDecision.APPROVED, actor_id=ts_lead.pk
        )
        assert len(seen) == 1
    finally:
        handlers = action_registry._subscribers.get("moderation.completed", [])
        if _collect in handlers:
            handlers.remove(_collect)


# ── The Function surface ─────────────────────────────────────────────


def test_submit_is_idempotent_by_state(content_double, llm_double):
    from stapel_core.comm import call

    llm_double["envelope"]["result"]["decision"] = "needs_review"
    first = call("moderation.submit", {"target_type": "listing", "target_key": "42"})
    second = call("moderation.submit", {"target_type": "listing", "target_key": "42"})

    assert first["created"] is True
    assert second["created"] is False
    assert first["case_id"] == second["case_id"]


def test_case_status_mirrors_listings_status(content_double, llm_double, ts_lead):
    from stapel_core.comm import call

    assert call(
        "moderation.case_status", {"target_type": "listing", "target_key": "42"}
    ) == {
        "case_id": None,
        "state": "none",
        "decision": "",
        "reason_code": "",
        "decided_at": "",
    }

    with mutate_and_emit() as emit_event:
        case, _ = services.open_case(
            "listing", "42", origin="submission", emit_event=emit_event
        )
    services.resolve_case(
        case,
        decision=VerdictDecision.REJECTED,
        reason_code="illegal",
        actor_id=ts_lead.pk,
    )

    answer = call(
        "moderation.case_status", {"target_type": "listing", "target_key": "42"}
    )
    assert answer["state"] == "resolved"
    assert answer["decision"] == "rejected"
    assert answer["reason_code"] == "illegal"


def test_the_disclosure_names_the_rules_it_runs(content_double):
    from stapel_core.comm import call

    from stapel_moderation.registry import register_rule

    register_rule(
        "weapons", {"pattern": r"\brifle\b", "decision": "rejected", "severity": 4}
    )
    answer = call("moderation.policy_disclosure", {})
    assert [rule["code"] for rule in answer["rules"]] == ["weapons"]
    assert "rules" in answer["automated_means"]["stages"]


def test_the_module_imports_no_sibling_module():
    """The loose-coupling rule, asserted rather than reviewed: everything is
    reached by string name over comm."""
    import re

    package = Path(__file__).resolve().parent.parent
    forbidden = re.compile(
        r"^\s*(from|import)\s+(stapel_listings|stapel_reviews|stapel_chat|"
        r"stapel_profiles|stapel_cdn|stapel_notifications)\b",
        re.MULTILINE,
    )
    offenders = []
    for path in package.glob("*.py"):
        if path.name.startswith(("test_", "conftest")):
            continue
        text = path.read_text()
        for match in forbidden.finditer(text):
            line_start = text.rfind("\n", 0, match.start()) + 1
            # checks.py imports the notifications ROUTING registry inside a
            # try/ImportError to validate policy entries when it is present —
            # a read of a registry, not a dependency.
            if path.name == "checks.py":
                continue
            offenders.append(f"{path.name}: {text[line_start:match.end()].strip()}")
    assert not offenders, offenders
