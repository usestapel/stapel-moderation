"""stapel-moderation capabilities.json emitter — a shim over stapel_tools."""
from pathlib import Path

from stapel_tools.capabilities import axis_group_rules, run_capabilities_cli


def main(argv=None):
    from stapel_moderation._codegen import _configure

    _configure()
    from stapel_moderation.conf import DEFAULTS
    from stapel_moderation.urls_v1 import GATE_REGISTRY

    # The CTO-facing axes are the ones that change WHAT THE PRODUCT DOES to
    # its users, not how fast it runs: what may be moderated at all, whether
    # unscreened content gets published, whether a queue resolves itself,
    # whether strangers may file complaints, whether an appeal is heard by a
    # second person, and how long the record is kept. Timeouts, page sizes,
    # media caps and cooldown seconds are tuning — they bound cost and abuse,
    # they do not change the deal with anybody.
    axes = {
        "TARGET_TYPES",
        "SCREEN_ENABLED",
        "SCREENER",
        "ON_SCREENING_FAILURE",
        "ON_SCREENING_UNAVAILABLE",
        "AUTO_RESOLVE_STALE_QUEUE",
        "ALLOW_ANONYMOUS_REPORTS",
        "APPEAL_REQUIRES_DIFFERENT_ACTOR",
        "RETENTION_DAYS",
        "SANCTION_RETENTION_DAYS",
        "WORKSPACE_SCOPED",
    }
    return run_capabilities_cli(
        argv,
        repo=Path(__file__).resolve().parent,
        canonical_prefix="/moderation/api/v1",
        defaults=DEFAULTS,
        registry=GATE_REGISTRY,
        is_axis=lambda k: k in axes,
        axis_group=axis_group_rules(
            exact={
                "TARGET_TYPES": "moderation.targets",
                "SCREEN_ENABLED": "moderation.screening",
                "SCREENER": "moderation.screening",
                "ON_SCREENING_FAILURE": "moderation.screening",
                "ON_SCREENING_UNAVAILABLE": "moderation.screening",
                "AUTO_RESOLVE_STALE_QUEUE": "moderation.queue",
                "ALLOW_ANONYMOUS_REPORTS": "moderation.intake",
                "APPEAL_REQUIRES_DIFFERENT_ACTOR": "moderation.appeals",
                "RETENTION_DAYS": "moderation.retention",
                "SANCTION_RETENTION_DAYS": "moderation.retention",
                "WORKSPACE_SCOPED": "moderation.tenancy",
            }
        ),
        prog="stapel-moderation-capabilities",
    )


if __name__ == "__main__":
    raise SystemExit(main())
