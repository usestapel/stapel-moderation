from django.apps import AppConfig


class ModerationConfig(AppConfig):
    name = "stapel_moderation"
    label = "moderation"
    verbose_name = "Moderation: cross-target queue, reports, verdicts, sanctions and appeals"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        # Import-time side effects, one module each.
        from . import checks  # noqa: F401
        from . import errors  # noqa: F401
        from . import functions  # noqa: F401
        from . import projections  # noqa: F401

        # The screening comm-Task handler must be registered before any
        # task.requested for this kind can arrive.
        from . import tasks  # noqa: F401

        # Static action subscriptions (task.failed, user.deleted,
        # staff.role.revoked, moderation.applied, and this module's own facts
        # driving the notification subscribers).
        from . import actions

        # Dynamic intake subscriptions: one per topic named by a registered
        # target policy's `intake_events`. A composite that registers
        # `listing` gets `listing.submitted` wired; one that does not, does
        # not — the module ships knowing no targets.
        actions.subscribe_intake_events()

        # GDPR provider registration (monolith mode). Hosts must ALSO list
        # "moderation" in STAPEL_GDPR["DATA_OWNERS"] — registering without
        # declaring is gdpr.E002 there, and the erasure closure never
        # completes. checks.W005 says so on this side.
        from stapel_core.gdpr import gdpr_registry

        from .gdpr import ModerationGDPRProvider

        if ModerationGDPRProvider().section not in gdpr_registry.sections:
            gdpr_registry.register(ModerationGDPRProvider())
