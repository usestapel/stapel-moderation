"""The beat schedule, importable before Django is ready.

``tasks.py`` cannot be imported from a settings module. It reaches
``.services`` and ``.screening``, both of which import ``.models`` at module
level (``@task_handler(SCREEN_TASK)`` needs the task name at decoration
time), and a settings module is executed *before* ``django.setup()`` — so
``from stapel_moderation.tasks import get_moderation_beat_schedule``, the
exact line ``moderation.W004``'s hint asks a host to write, raises
``AppRegistryNotReady`` and the service does not boot.

The workaround a host reaches for next is to merge the schedule later, from
a Celery ``on_after_finalize`` signal. The jobs then run — and ``manage.py
check`` goes on printing W004 about jobs that *are* scheduled, because the
check reads ``settings.CELERY_BEAT_SCHEDULE`` and the settings dict never
learned about them. A warning that fires when the thing is fine is precisely
how the real one came to be ignored for a live stand's entire life: W004 and
``stapel_search.W003`` printed at every boot for months while nobody read
them, and 51 moderation cases sat in a queue no job was draining.

So this module holds the names and the schedule and imports **settings and
nothing else** — the property ``stapel_search.tasks`` already has, and the
reason a host can spell its search entries in settings today and could not
spell its moderation ones. ``tasks.py`` re-exports every name here, so the
documented import path keeps working.
"""
from __future__ import annotations

#: Stable names a beat schedule references (never renamed by a refactor).
SWEEP_TASK_NAME = "stapel_moderation.tasks.sweep_stale_cases"
RESCREEN_TASK_NAME = "stapel_moderation.tasks.rescreen_stuck_cases"
REARM_TASK_NAME = "stapel_moderation.tasks.rearm_active_sanctions"
EXPIRE_TASK_NAME = "stapel_moderation.tasks.expire_sanctions"
PURGE_TASK_NAME = "stapel_moderation.tasks.purge_expired_cases"

BEAT_TASK_NAMES = (
    SWEEP_TASK_NAME,
    RESCREEN_TASK_NAME,
    REARM_TASK_NAME,
    EXPIRE_TASK_NAME,
    PURGE_TASK_NAME,
)


def get_moderation_beat_schedule() -> dict:
    """Beat entries for every scheduled job, on the configured cadences.

    Wire it into a host's beat schedule, in the settings module itself::

        from stapel_moderation.beat import get_moderation_beat_schedule

        CELERY_BEAT_SCHEDULE = {**get_moderation_beat_schedule(), ...}

    Written there rather than merged in later so that ``manage.py check`` —
    which reads ``settings.CELERY_BEAT_SCHEDULE`` — sees what beat will
    actually run, and W004 keeps meaning what it says.
    """
    from celery.schedules import crontab

    from .conf import moderation_settings

    return {
        "moderation-sweep-stale-cases": {
            "task": SWEEP_TASK_NAME,
            "schedule": crontab(**dict(moderation_settings.SWEEP_SCHEDULE or {})),
        },
        "moderation-rescreen-stuck-cases": {
            "task": RESCREEN_TASK_NAME,
            "schedule": crontab(**dict(moderation_settings.RESCREEN_SCHEDULE or {})),
        },
        "moderation-rearm-sanctions": {
            "task": REARM_TASK_NAME,
            "schedule": crontab(**dict(moderation_settings.REARM_SCHEDULE or {})),
        },
        "moderation-expire-sanctions": {
            "task": EXPIRE_TASK_NAME,
            "schedule": crontab(**dict(moderation_settings.REARM_SCHEDULE or {})),
        },
        "moderation-retention-purge": {
            "task": PURGE_TASK_NAME,
            "schedule": crontab(**dict(moderation_settings.PURGE_SCHEDULE or {})),
        },
    }


__all__ = [
    "BEAT_TASK_NAMES",
    "EXPIRE_TASK_NAME",
    "PURGE_TASK_NAME",
    "REARM_TASK_NAME",
    "RESCREEN_TASK_NAME",
    "SWEEP_TASK_NAME",
    "get_moderation_beat_schedule",
]
