"""The three merge-registries, and the system checks that police them."""
import pytest

from stapel_moderation.registry import (
    UnknownReason,
    UnknownTargetType,
    get_reasons,
    get_target_types,
    reasons_for_target,
    register_reason,
    register_rule,
    register_target_type,
    resolve_policy,
    resolve_policy_lenient,
    resolve_reason,
    rules_for_target,
)


# ── Merge semantics ──────────────────────────────────────────────────


def test_the_module_ships_knowing_no_targets(db):
    """Target-generic by construction: BUILTIN_TARGET_TYPES is empty."""
    assert get_target_types() == {}
    with pytest.raises(UnknownTargetType):
        resolve_policy("listing")


def test_settings_then_runtime_wins(db, settings):
    settings.STAPEL_MODERATION = {
        "TARGET_TYPES": {
            "listing": {"content_function": "a.content", "severity_floor": 1}
        }
    }
    assert resolve_policy("listing")["content_function"] == "a.content"

    register_target_type("listing", {"content_function": "b.content"})
    assert resolve_policy("listing")["content_function"] == "b.content"


def test_none_is_the_removal_marker(db, settings):
    settings.STAPEL_MODERATION = {
        "TARGET_TYPES": {"listing": {"content_function": "a.content"}}
    }
    register_target_type("listing", None)
    with pytest.raises(UnknownTargetType):
        resolve_policy("listing")


def test_every_policy_key_is_defaulted_so_no_call_site_guesses(db):
    register_target_type("listing", {"content_function": "a.content"})
    policy = resolve_policy("listing")
    assert set(policy) == {
        "gate",
        "intake_events",
        "id_field",
        "content_function",
        "verdict_event",
        "notification_types",
        "can_report",
        "can_view_content",
        "reasons",
        "screen",
        "media",
        "severity_floor",
    }
    assert policy["gate"] == "post"
    assert policy["verdict_event"] == "moderation.completed"
    assert policy["screen"] is True


def test_an_explicit_none_verdict_event_is_not_the_default(db):
    """"This target consumes no verdict" and "I forgot to say" must differ."""
    register_target_type(
        "avatar", {"content_function": "p.content", "verdict_event": None}
    )
    assert resolve_policy("avatar")["verdict_event"] is None


def test_the_lenient_resolver_keeps_staff_unblocked(db):
    """Spec §22.7 from the other end: a host that de-registers a type must not
    be able to strand the open cases about it."""
    with pytest.raises(UnknownTargetType):
        resolve_policy("gone")
    policy = resolve_policy_lenient("gone")
    assert policy["content_function"] == ""
    assert policy["verdict_event"] is None
    assert policy["screen"] is False


# ── Reasons ──────────────────────────────────────────────────────────


def test_reasons_ship_built_in_unlike_target_types(db):
    """A complaint taxonomy is universal; a target type is not."""
    codes = set(get_reasons())
    assert {"spam", "fraud", "illegal", "other"} <= codes


def test_a_host_extends_and_removes_reasons(db, settings):
    settings.STAPEL_MODERATION = {
        "REASONS": {
            "counterfeit_pharma": {"severity": 4, "requires_description": True},
            "wrong_category": None,
        }
    }
    codes = set(get_reasons())
    assert "counterfeit_pharma" in codes
    assert "wrong_category" not in codes


def test_system_reasons_cannot_be_removed(db, settings):
    """A host that deleted screening_unavailable would leave the hold path
    unable to name why it held — not a configuration choice, a broken module."""
    settings.STAPEL_MODERATION = {"REASONS": {"screening_unavailable": None}}
    assert "screening_unavailable" in get_reasons()


def test_system_reasons_are_not_offered_to_reporters(db):
    register_target_type("listing", {"content_function": "a.content"})
    offered = reasons_for_target("listing")
    assert "spam" in offered
    assert "screening_unavailable" not in offered
    assert "low_confidence" not in offered


def test_a_policy_narrows_the_reasons_it_accepts(db):
    register_target_type(
        "listing", {"content_function": "a.content", "reasons": ["spam", "fraud"]}
    )
    assert set(reasons_for_target("listing")) == {"spam", "fraud"}


def test_a_reason_can_be_scoped_to_some_target_types(db):
    register_reason(
        "price_gouging", {"severity": 2, "applies_to": ["listing"]}
    )
    register_target_type("listing", {"content_function": "a.content"})
    register_target_type("review", {"content_function": "b.content"})
    assert "price_gouging" in reasons_for_target("listing")
    assert "price_gouging" not in reasons_for_target("review")


def test_an_unknown_reason_raises(db):
    with pytest.raises(UnknownReason):
        resolve_reason("nonsense")


def test_reason_translation_keys_are_derived_not_required(db):
    """Owning the keys means shipping their catalogues, so the keys are
    generated rather than left to a host to remember."""
    entry = resolve_reason("spam")
    assert entry["label_key"] == "moderation.reason.spam.label"
    assert entry["description_key"] == "moderation.reason.spam.description"


# ── Rules ────────────────────────────────────────────────────────────


def test_rules_ship_empty(db):
    """A shipped keyword list is somebody else's speech policy."""
    assert rules_for_target("listing") == []


def test_a_rule_can_be_scoped_and_ordered(db):
    register_rule("b_rule", {"pattern": "b", "applies_to": ["listing"]})
    register_rule("a_rule", {"pattern": "a"})
    codes = [rule["code"] for rule in rules_for_target("listing")]
    assert codes == ["a_rule", "b_rule"]
    assert [r["code"] for r in rules_for_target("review")] == ["a_rule"]


def test_an_invalid_pattern_is_skipped_not_fatal(db, content_double, llm_double):
    """One malformed rule must not take the whole screener down with it."""
    from stapel_moderation.screening import run_rules
    from stapel_moderation.services import TargetContent

    register_rule("broken", {"pattern": "(unclosed"})
    register_rule("good", {"pattern": "rifle", "decision": "rejected"})

    class _Case:
        target_type = "listing"

    hit = run_rules(_Case(), TargetContent(text="a rifle", title=""))
    assert hit is not None and hit.matched_rules == ("good",)


# ── System checks ────────────────────────────────────────────────────


def test_e004_a_policy_without_a_content_function(db, settings):
    from stapel_moderation.checks import check_target_types

    settings.STAPEL_MODERATION = {"TARGET_TYPES": {"listing": {"gate": "post"}}}
    ids = {m.id for m in check_target_types(None)}
    assert "stapel_moderation.E004" in ids


def test_e003_an_unknown_gate(db, settings):
    from stapel_moderation.checks import check_target_types

    settings.STAPEL_MODERATION = {
        "TARGET_TYPES": {"listing": {"content_function": "a.c", "gate": "sideways"}}
    }
    ids = {m.id for m in check_target_types(None)}
    assert "stapel_moderation.E003" in ids


def test_e006_a_policy_allowing_a_reason_nobody_registered(db, settings):
    from stapel_moderation.checks import check_target_types

    settings.STAPEL_MODERATION = {
        "TARGET_TYPES": {
            "listing": {"content_function": "a.c", "reasons": ["spam", "invented"]}
        }
    }
    ids = {m.id for m in check_target_types(None)}
    assert "stapel_moderation.E006" in ids


def test_a_removal_marker_is_not_an_error(db, settings):
    from stapel_moderation.checks import check_target_types

    settings.STAPEL_MODERATION = {"TARGET_TYPES": {"listing": None}}
    assert check_target_types(None) == []


def test_w003_anonymous_intake_without_a_captcha(db, settings):
    from stapel_moderation.checks import check_anonymous_reports

    settings.STAPEL_MODERATION = {"ALLOW_ANONYMOUS_REPORTS": True}
    ids = {m.id for m in check_anonymous_reports(None)}
    assert "stapel_moderation.W003" in ids

    settings.STAPEL_CAPTCHA = {"SECRET": "x"}
    assert check_anonymous_reports(None) == []


def test_w004_a_beat_schedule_that_never_rearms_a_ban(db, settings):
    """Without the rearm job every suspension quietly stops being enforced
    when the cache key expires, while the row still reads 'active'."""
    from stapel_moderation.checks import check_beat_schedule

    settings.CELERY_BEAT_SCHEDULE = {
        "moderation-retention-purge": {
            "task": "stapel_moderation.tasks.purge_expired_cases",
            "schedule": 1,
        }
    }
    messages = check_beat_schedule(None)
    assert {m.id for m in messages} == {"stapel_moderation.W004"}
    assert any("stops being enforced" in m.msg for m in messages)


def test_the_whole_beat_schedule_silences_w004(db, settings):
    from stapel_moderation.tasks import BEAT_TASK_NAMES
    from stapel_moderation.checks import check_beat_schedule

    settings.CELERY_BEAT_SCHEDULE = {
        name: {"task": name, "schedule": 1} for name in BEAT_TASK_NAMES
    }
    assert check_beat_schedule(None) == []


def test_w005_undeclared_to_gdpr(db, settings):
    from stapel_moderation.checks import check_gdpr_declaration

    settings.STAPEL_GDPR = {"DATA_OWNERS": ["auth"]}
    assert "stapel_moderation.W005" in {m.id for m in check_gdpr_declaration(None)}

    settings.STAPEL_GDPR = {"DATA_OWNERS": ["auth", "moderation"]}
    assert check_gdpr_declaration(None) == []


def test_w006_a_content_function_nobody_provides(db, settings):
    """Declared and not connected — the defect class this module is about."""
    from stapel_moderation.checks import check_verdict_consumers

    settings.STAPEL_MODERATION = {
        "TARGET_TYPES": {"listing": {"content_function": "nobody.provides_this"}}
    }
    messages = check_verdict_consumers(None)
    assert "stapel_moderation.W006" in {m.id for m in messages}
    assert any("unreadable card" in m.msg for m in messages)


def test_the_beat_schedule_builder_names_every_job(db):
    """Celery is OPTIONAL, so this asserts on the entries only where it exists.

    The jobs themselves are plain callables a cron or a systemd timer can run;
    only the ready-made beat dict needs ``celery.schedules.crontab``. The
    check that matters without celery — "this host schedules none of them" —
    compares task NAMES and is covered above.
    """
    pytest.importorskip("celery")
    from stapel_moderation.tasks import BEAT_TASK_NAMES, get_moderation_beat_schedule

    schedule = get_moderation_beat_schedule()
    assert {entry["task"] for entry in schedule.values()} == set(BEAT_TASK_NAMES)
