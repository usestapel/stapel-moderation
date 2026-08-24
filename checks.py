"""Django system checks for stapel-moderation configuration.

Policy (docs/library-standard.md §3.7): E-level for configuration the service
cannot run with; W-level for entries that degrade lazily.

Several of the warnings here are **confessions rather than diagnostics**. A
deployment that auto-approves whatever the LLM could not screen, or that
auto-resolves cases no human reached, looks identical at runtime to one that
does neither — which is exactly why each gets a check instead of a paragraph
in a README. Legacy had both behaviours and neither was written down anywhere.
"""
from django.core import checks


@checks.register(checks.Tags.compatibility)
def check_target_types(app_configs, **kwargs):
    """E001-E006: the target-type registry must be answerable."""
    from .conf import moderation_settings
    from .registry import get_reasons, get_target_types

    errors = []
    try:
        configured = moderation_settings.TARGET_TYPES
    except Exception as exc:  # noqa: BLE001
        return [checks.Error(
            f"STAPEL_MODERATION['TARGET_TYPES'] cannot be read: {exc}",
            id="stapel_moderation.E001",
        )]
    if configured is not None and not isinstance(configured, dict):
        return [checks.Error(
            "STAPEL_MODERATION['TARGET_TYPES'] must be a dict of "
            "{target_type: policy dict or None}.",
            id="stapel_moderation.E001",
        )]

    for name, policy in (configured or {}).items():
        if policy is None:
            # A None entry is the documented removal marker, not a mistake.
            continue
        if not isinstance(policy, dict):
            errors.append(checks.Error(
                f"STAPEL_MODERATION['TARGET_TYPES'][{name!r}] must be a dict "
                f"or None, got {type(policy).__name__}.",
                id="stapel_moderation.E002",
            ))
            continue
        gate = policy.get("gate", "post")
        if gate not in ("pre", "post"):
            errors.append(checks.Error(
                f"Target type {name!r} declares gate={gate!r}; only 'pre' and "
                f"'post' exist.",
                id="stapel_moderation.E003",
            ))
        if not policy.get("content_function") and not policy.get("evidence"):
            errors.append(checks.Error(
                f"Target type {name!r} declares no 'content_function'. The "
                f"module never stores or receives target content — without "
                f"the callback, a screener has nothing to screen and a "
                f"moderator gets an empty card.",
                hint="Add \"content_function\": \"<module>.moderation_content\" "
                     "to the policy (listings.moderation_content, "
                     "reviews.moderation_content) — or declare "
                     "\"evidence\": True if this target's content is served "
                     "by nobody and a report must carry the reporter's own "
                     "snapshot of it.",
                id="stapel_moderation.E004",
            ))
        if policy.get("evidence") and policy.get("content_function"):
            errors.append(checks.Error(
                f"Target type {name!r} declares BOTH a 'content_function' and "
                f"'evidence': True. One target has one source of truth — an "
                f"owner that answers, or a reporter's attestation because no "
                f"owner exists. Two would let a case card show the live "
                f"content while the verdict was screened on the snapshot.",
                hint="Drop 'evidence' if the owner serves the content; drop "
                     "'content_function' if it does not.",
                id="stapel_moderation.E007",
            ))

    known_reasons = set(get_reasons())
    for name, policy in get_target_types().items():
        reasons = list((policy or {}).get("reasons") or ["*"])
        unknown = [code for code in reasons if code != "*" and code not in known_reasons]
        if unknown:
            errors.append(checks.Error(
                f"Target type {name!r} allows reason codes nobody registered: "
                f"{sorted(unknown)}. A reason the taxonomy does not have is a "
                f"complaint form option that always answers 400.",
                hint="Register them in STAPEL_MODERATION['REASONS'] or drop "
                     "them from the policy's 'reasons' list.",
                id="stapel_moderation.E006",
            ))
    return errors


@checks.register(checks.Tags.compatibility)
def check_notification_types(app_configs, **kwargs):
    """E005: a policy naming a notification type nobody registered.

    Only checkable when stapel-notifications is co-installed; in a split
    topology the registry lives in another process and the check stays silent
    rather than guessing.
    """
    from .registry import get_target_types

    try:
        from stapel_notifications.routing import registered_types
    except ImportError:
        return []
    try:
        known = set(registered_types())
    except Exception:  # noqa: BLE001 — a settings error there is theirs to report
        return []

    errors = []
    for name, policy in get_target_types().items():
        for slot, notification_type in ((policy or {}).get("notification_types") or {}).items():
            if notification_type and notification_type not in known:
                errors.append(checks.Error(
                    f"Target type {name!r} maps {slot!r} to notification type "
                    f"{notification_type!r}, which is not registered.",
                    hint="Register it in STAPEL_NOTIFICATIONS['TYPES'] or fix "
                         "the policy. A type nobody routes is a letter that is "
                         "never sent and never reported as missing.",
                    id="stapel_moderation.E005",
                ))
    return errors


@checks.register(checks.Tags.security)
def check_screening_failure_policy(app_configs, **kwargs):
    """W001: this deployment publishes what it could not screen.

    The confession switch. ``ON_SCREENING_FAILURE="approve"`` is a legitimate
    choice for a low-risk vertical, and the alternative to naming it here is
    forcing an owner to patch the module — but legacy's version of this
    behaviour was invisible, and invisible is what made it a defect.
    """
    from .conf import moderation_settings

    policy = str(moderation_settings.ON_SCREENING_FAILURE or "hold").lower()
    if policy not in ("hold", "approve", "reject"):
        return [checks.Error(
            f"STAPEL_MODERATION['ON_SCREENING_FAILURE'] is {policy!r}; only "
            f"'hold', 'approve' and 'reject' exist.",
            id="stapel_moderation.E007",
        )]
    if policy == "hold":
        return []
    if policy == "approve":
        return [checks.Warning(
            "STAPEL_MODERATION['ON_SCREENING_FAILURE'] = 'approve' — when the "
            "automatic screener is unavailable, content is published WITHOUT "
            "any moderation decision. This deployment has chosen availability "
            "over safety.",
            hint="'hold' sends those cases to the human queue instead. If the "
                 "choice is deliberate, re-screen the auto-approved cases when "
                 "the screener returns (POST .../rescan).",
            id="stapel_moderation.W001",
        )]
    return [checks.Warning(
        "STAPEL_MODERATION['ON_SCREENING_FAILURE'] = 'reject' — when the "
        "automatic screener is unavailable, content is REJECTED without a "
        "decision. This deployment has chosen safety over availability.",
        hint="'hold' sends those cases to the human queue instead.",
        id="stapel_moderation.W001",
    )]


@checks.register(checks.Tags.security)
def check_auto_resolve(app_configs, **kwargs):
    """W002: the queue resolves itself on a clock.

    The setting exists mainly so that "we do not do this" is readable. Legacy
    swept both ``pending`` and ``needs_review`` into auto-approval every five
    minutes, which meant human review was on the org chart and nowhere else.
    """
    from .conf import moderation_settings

    auto = moderation_settings.AUTO_RESOLVE_STALE_QUEUE
    if not auto:
        return []
    return [checks.Warning(
        f"STAPEL_MODERATION['AUTO_RESOLVE_STALE_QUEUE'] = {auto} — cases "
        f"nobody reviewed are auto-APPROVED after {auto} seconds. Human "
        f"review becomes optional in practice.",
        hint="Set it to None (the default) so a queued case is only ever "
             "resolved by a person.",
        id="stapel_moderation.W002",
    )]


@checks.register(checks.Tags.security)
def check_anonymous_reports(app_configs, **kwargs):
    """W003: anonymous intake is open without a captcha behind it."""
    from django.conf import settings

    from .conf import moderation_settings

    if not moderation_settings.ALLOW_ANONYMOUS_REPORTS:
        return []
    captcha = getattr(settings, "STAPEL_CAPTCHA", None) or {}
    if captcha.get("SECRET"):
        return []
    return [checks.Warning(
        "STAPEL_MODERATION['ALLOW_ANONYMOUS_REPORTS'] is on but no "
        "STAPEL_CAPTCHA['SECRET'] is configured — the report endpoint accepts "
        "every bot that finds it, and a flood of anonymous complaints is a "
        "denial-of-service against the moderation queue itself.",
        hint="Configure a captcha backend, or close the switch again.",
        id="stapel_moderation.W003",
    )]


@checks.register(checks.Tags.compatibility)
def check_beat_schedule(app_configs, **kwargs):
    """W004: a beat schedule that runs none of this module's jobs.

    Retention that nobody schedules is a promise, not a mechanism (the DOCS-02
    lesson). Worse here: without ``rearm_active_sanctions`` every suspension
    silently stops being enforced after ``BLACKLIST_TTL_SECONDS``, while the
    row still says "active".
    """
    from django.conf import settings

    from .tasks import BEAT_TASK_NAMES, REARM_TASK_NAME

    schedule = getattr(settings, "CELERY_BEAT_SCHEDULE", None)
    if not schedule:
        return []
    scheduled = {
        entry.get("task")
        for entry in schedule.values()
        if isinstance(entry, dict)
    }
    missing = [name for name in BEAT_TASK_NAMES if name not in scheduled]
    if not missing:
        return []
    warnings = [checks.Warning(
        "CELERY_BEAT_SCHEDULE has no entry for: " + ", ".join(missing),
        hint="CELERY_BEAT_SCHEDULE = {**get_moderation_beat_schedule(), ...} "
             "(stapel_moderation.tasks).",
        id="stapel_moderation.W004",
    )]
    if REARM_TASK_NAME in missing:
        warnings.append(checks.Warning(
            "Without stapel_moderation.tasks.rearm_active_sanctions, every "
            "suspension and ban stops being enforced once the blacklist cache "
            "key expires (STAPEL_MODERATION['BLACKLIST_TTL_SECONDS']), while "
            "the Sanction row still reads 'active'.",
            hint="Schedule it well inside the TTL — the shipped cadence is "
                 "every 30 minutes against a 2-hour TTL.",
            id="stapel_moderation.W004",
        ))
    return warnings


@checks.register(checks.Tags.compatibility)
def check_gdpr_declaration(app_configs, **kwargs):
    """W005: the module holds PII the host has not declared to stapel-gdpr."""
    from django.conf import settings

    gdpr = getattr(settings, "STAPEL_GDPR", None)
    if gdpr is None:
        return []
    owners = gdpr.get("DATA_OWNERS") or []
    names = {o if isinstance(o, str) else (o or {}).get("name") for o in owners}
    if "moderation" in names:
        return []
    return [checks.Warning(
        "stapel-moderation stores complaint text and complainant identities "
        'but "moderation" is not in STAPEL_GDPR["DATA_OWNERS"] — the erasure '
        "closure will never complete for this data (gdpr.E002 raises this to "
        "an error on that side).",
        hint='Add "moderation" to STAPEL_GDPR["DATA_OWNERS"] and bump '
             "DATA_OWNERS_VERSION.",
        id="stapel_moderation.W005",
    )]


@checks.register(checks.Tags.compatibility)
def check_verdict_consumers(app_configs, **kwargs):
    """W006: a target type declared and not wired to anything.

    A policy whose ``verdict_event`` is a topic and whose ``content_function``
    is a name nobody provides is "declared but not connected" — the class of
    defect the fleet gate exists to catch. Only checkable in a co-located
    process; in a split topology the provider is in another service and the
    check stays quiet rather than crying wolf.
    """
    from stapel_core.comm import function_unreachable_reason

    from .registry import get_target_types

    warnings = []
    for name, policy in get_target_types().items():
        content_function = (policy or {}).get("content_function")
        # No content function: either E004 already covers it, or the type is
        # evidence-based — and an evidence-based type HAS its content source
        # (the report), so there is nothing unreachable to probe.
        reason = function_unreachable_reason(content_function) if content_function else ""
        if reason:
            warnings.append(checks.Warning(
                f"Target type {name!r} names content_function "
                f"{content_function!r}, which cannot be called here: {reason}. "
                f"Every case of this type will open with an unreadable card.",
                hint="Install the owning module in this process, or configure "
                     "the function route for the split topology.",
                id="stapel_moderation.W006",
            ))
        if (policy or {}).get("verdict_event") is None:
            warnings.append(checks.Warning(
                f"Target type {name!r} declares verdict_event=None — a verdict "
                f"about it notifies and sanctions but never reaches the "
                f"target. This is a valid statement; the check exists so it is "
                f"a deliberate one.",
                hint="Set verdict_event to the topic the target module "
                     "consumes (default \"moderation.completed\"), or leave "
                     "None knowingly.",
                id="stapel_moderation.W006",
            ))
    return warnings


__all__ = [
    "check_anonymous_reports",
    "check_auto_resolve",
    "check_beat_schedule",
    "check_gdpr_declaration",
    "check_notification_types",
    "check_screening_failure_policy",
    "check_target_types",
    "check_verdict_consumers",
]
