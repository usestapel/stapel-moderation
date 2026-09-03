"""A case that nothing can move is a case nobody decided.

Two shapes of stall, one mechanism.

**The parked queue.** ``needs_review`` is the machine abstaining, and it puts
the case in the human queue. On a deployment with no staffed queue — every
young marketplace — that is where the case stays: a live stand carried 51 of
them, the oldest two days old, each with ``screen_attempts=3`` and no path
onward. ``sweep_stale_cases`` does not help: it recycles expired CLAIMED
leases and stalled SCREENING rows back INTO the queue, so it is the thing
that fills the queue, not the thing that drains it. The one setting that
touches QUEUED, ``AUTO_RESOLVE_STALE_QUEUE``, blanket-approves — the exact
legacy sin the module docstring names.

**The stale verdict.** An owner edits a listing whose case is still QUEUED.
``open_case`` finds the open case, returns it, and ``handle_intake`` logs
RESUBMITTED and re-screens only when the case is OPEN — so an edit to a
queued listing changes the content under a verdict that was reached about
different content.

The recovery is a re-SCREEN, never a resolution: the sweep hands the case
back to the screening ladder and the ladder decides, exactly as it does on
first submission. What the sweep owns is *when*, with a backoff so a stuck
case is not a billing loop, and a cap so a permanently failing case is
surfaced rather than retried forever.
"""
import pytest
from django.utils import timezone

from stapel_moderation.models import Case, CaseEventKind, CaseState, VerdictDecision


pytestmark = pytest.mark.django_db


def _submit(listing_id="42"):
    """Deliver a ``listing.submitted`` the way the bus does."""
    from stapel_core.comm import emit

    emit("listing.submitted", {"listing_id": listing_id})


def _age(case, **delta):
    """Push a case's ``updated_at`` into the past.

    ``auto_now`` means the field cannot be set through ``save()``, and the
    whole mechanism is a function of how long a case has sat still.
    """
    past = timezone.now() - timezone.timedelta(**delta)
    fields = {"updated_at": past}
    if case.last_screened_at is not None:
        # The sweep measures from the last SCREENING, not from the last write
        # of any kind — so a test that only ages `updated_at` is not ageing
        # the thing the mechanism reads.
        fields["last_screened_at"] = past
    Case.objects.filter(pk=case.pk).update(**fields)
    case.refresh_from_db()
    return case


@pytest.fixture
def abstaining(llm_double):
    """A screener that cannot make up its mind — the queue's whole input."""
    llm_double["envelope"] = {
        "status": "ok",
        "result": {
            "decision": "needs_review",
            "reason_code": "spam",
            "rationale": "Could be a listing, could be a leaflet.",
            "confidence": 0.9,
        },
        "model": "medium",
        "usage": {"input_tokens": 100, "output_tokens": 20},
    }
    return llm_double


@pytest.fixture
def queued_case(content_double, abstaining, settings):
    """One case parked in the human queue, the way the stand's 51 got there."""
    settings.STAPEL_MODERATION = {
        **getattr(settings, "STAPEL_MODERATION", {}),
        "RESCREEN_STUCK_AFTER": 3600,
        "RESCREEN_MAX_ATTEMPTS": 3,
    }
    _submit()
    case = Case.objects.get(target_type="listing", target_key="42")
    assert case.state == CaseState.QUEUED, "precondition: the machine abstained"
    return case


# ── The sweep drains what the queue cannot ───────────────────────────


def test_a_case_stuck_past_the_threshold_is_screened_again(queued_case, abstaining):
    """The listing the owner reported: parked, and nothing moving it."""
    from stapel_moderation.tasks import rescreen_stuck_cases

    before = len(abstaining["calls"])
    _age(queued_case, hours=2)

    picked = rescreen_stuck_cases()

    assert picked == 1
    assert len(abstaining["calls"]) == before + 1, "the screener was asked again"
    queued_case.refresh_from_db()
    assert queued_case.rescreen_attempts == 1


def test_a_fresh_queued_case_is_left_alone(queued_case, abstaining):
    """A case a moderator could still reach is not the sweep's business."""
    from stapel_moderation.tasks import rescreen_stuck_cases

    before = len(abstaining["calls"])
    assert rescreen_stuck_cases() == 0
    assert len(abstaining["calls"]) == before


def test_the_sweep_resolves_nothing_by_itself(queued_case):
    """The legacy sin, held down by a test.

    ``retry_stuck_moderation`` swept ``needs_review`` into auto-approval and
    published unmoderated listings. This sweep hands the case back to the
    ladder; only the ladder — or a human — resolves.
    """
    from stapel_moderation.tasks import rescreen_stuck_cases

    _age(queued_case, hours=2)
    rescreen_stuck_cases()

    queued_case.refresh_from_db()
    assert queued_case.state != CaseState.RESOLVED
    assert queued_case.resolved_at is None
    assert queued_case.last_verdict.decision == VerdictDecision.NEEDS_REVIEW


def test_a_claimed_case_is_never_swept(queued_case, moderator, abstaining):
    """A moderator holding the case outranks the clock."""
    from stapel_moderation import services
    from stapel_moderation.tasks import rescreen_stuck_cases

    services.claim_case(queued_case, actor_id=moderator.pk)
    _age(queued_case, days=3)

    before = len(abstaining["calls"])
    assert rescreen_stuck_cases() == 0
    assert len(abstaining["calls"]) == before


# ── Backoff and the cap ──────────────────────────────────────────────


def test_backoff_widens_between_attempts(queued_case, abstaining):
    """A re-screen that just ran does not run again on the next tick.

    Without this the sweep is a billing loop: every tick, every parked case,
    one LLM call each.
    """
    from stapel_moderation.tasks import rescreen_stuck_cases

    # Attempt 1 waits one window (RESCREEN_STUCK_AFTER = 1h).
    _age(queued_case, hours=2)
    assert rescreen_stuck_cases() == 1

    # Ninety minutes would have been plenty for attempt 1 and is not enough
    # for attempt 2, which waits two windows. That is the widening.
    _age(queued_case, minutes=90)
    assert rescreen_stuck_cases() == 0, "the second attempt waits longer"

    _age(queued_case, hours=5)
    assert rescreen_stuck_cases() == 1


def test_a_permanently_stuck_case_is_surfaced_not_retried_forever(
    queued_case, abstaining
):
    """The cap. Past it the case is ESCALATED once and left for a human.

    Surfaced, not resolved and not abandoned: ``escalated_at`` is the field a
    queue filter sorts on, and the CaseEvent is the audit trail that says the
    machine gave up rather than that nobody looked.
    """
    from stapel_moderation.tasks import rescreen_stuck_cases

    for attempt in range(3):
        _age(queued_case, hours=2 * 2 ** (attempt + 1))
        assert rescreen_stuck_cases() == 1, f"attempt {attempt + 1} should run"

    queued_case.refresh_from_db()
    assert queued_case.rescreen_attempts == 3
    assert queued_case.escalated_at is None

    _age(queued_case, days=7)
    calls_before = len(abstaining["calls"])
    assert rescreen_stuck_cases() == 0, "the cap holds"
    assert len(abstaining["calls"]) == calls_before, "no further screening is paid for"

    queued_case.refresh_from_db()
    assert queued_case.escalated_at is not None, "and it is SURFACED"
    assert queued_case.events.filter(kind=CaseEventKind.ESCALATED).count() == 1

    # Escalation is announced once, not on every tick.
    _age(queued_case, days=14)
    assert rescreen_stuck_cases() == 0
    queued_case.refresh_from_db()
    assert queued_case.events.filter(kind=CaseEventKind.ESCALATED).count() == 1


# ── An edit re-triggers moderation ───────────────────────────────────


def test_an_edit_to_a_queued_listing_is_screened_again(
    queued_case, content_double, abstaining
):
    """The second defect: today the edit rides the old case and the old verdict.

    ``open_case`` dedups on OPEN_STATES, so the resubmission finds the queued
    case; ``handle_intake`` re-screens only from OPEN. The edited content is
    therefore never looked at. Marking the case resubmitted lets the sweep
    pick it up on its own cadence — which is also what keeps a redelivered
    event from being a screening each: many resubmits coalesce into one.
    """
    from stapel_moderation.tasks import rescreen_stuck_cases

    content_double["title"] = "A bicycle, now with the price it actually costs"
    _submit()

    queued_case.refresh_from_db()
    assert queued_case.resubmitted_at is not None
    assert Case.objects.filter(target_type="listing", target_key="42").count() == 1

    before = len(abstaining["calls"])
    assert rescreen_stuck_cases() == 1, "an edit does not wait out the stuck window"
    assert len(abstaining["calls"]) == before + 1
    assert "actually costs" in abstaining["calls"][-1]["prompt"], (
        "and the screener saw the NEW content, not the content it already judged"
    )


def test_redelivery_of_one_event_costs_one_rescreen(
    queued_case, content_double, abstaining
):
    """At-least-once delivery must not be at-least-once billing."""
    from stapel_moderation.tasks import rescreen_stuck_cases

    for _ in range(5):
        _submit()

    before = len(abstaining["calls"])
    assert rescreen_stuck_cases() == 1
    assert len(abstaining["calls"]) == before + 1

    assert rescreen_stuck_cases() == 0, "the resubmission is spent"


def test_an_edit_after_a_verdict_still_opens_a_fresh_case(
    content_double, llm_double, settings
):
    """The already-working half, held down so the fix cannot break it."""
    _submit()
    first = Case.objects.get(target_type="listing", target_key="42")
    assert first.state == CaseState.RESOLVED

    content_double["title"] = "A bicycle, repriced"
    _submit()

    assert Case.objects.filter(target_type="listing", target_key="42").count() == 2


# ── The beat schedule is the mechanism, not the intention ────────────


def test_the_rescreen_job_is_in_the_shipped_beat_schedule():
    """A sweep a host cannot schedule in one line is a sweep nobody runs.

    The stand ran for its whole life with a CELERY_BEAT_SCHEDULE holding one
    entry and none of this module's — while `manage.py check` printed W004 at
    every boot. The job has to be in the shipped dict, and W004 has to name
    it, or the next deployment repeats the same silence.
    """
    from stapel_moderation.tasks import (
        BEAT_TASK_NAMES,
        RESCREEN_TASK_NAME,
        get_moderation_beat_schedule,
    )

    assert RESCREEN_TASK_NAME in BEAT_TASK_NAMES, "and W004 therefore names it"
    pytest.importorskip("celery")
    scheduled = {entry["task"] for entry in get_moderation_beat_schedule().values()}
    assert RESCREEN_TASK_NAME in scheduled


def test_the_beat_schedule_is_importable_before_django_is_ready():
    """A host must be able to write the one line W004's hint asks for.

    `stapel_moderation.tasks` cannot be imported from a settings module: it
    reaches `.services` -> `.models`, and a settings module is executed
    before `django.setup()`, so the import raises AppRegistryNotReady. A host
    following the hint therefore could not boot, and the workaround — merging
    the schedule later, after the app is finalized — leaves `manage.py check`
    printing W004 about jobs that ARE scheduled. A warning that fires when
    the thing is fine is how the real one got ignored for the stand's whole
    life.

    `stapel_search.tasks` already has this property (every import inside a
    function). `stapel_moderation.beat` gives moderation the same one: it
    imports settings and nothing else, so `CELERY_BEAT_SCHEDULE` can be
    spelled in settings where the check can read it.
    """
    import subprocess
    import sys

    # A subprocess with no Django set up at all — the settings-module moment.
    code = (
        "from stapel_moderation.beat import ("
        "  BEAT_TASK_NAMES, RESCREEN_TASK_NAME, get_moderation_beat_schedule)\n"
        "assert RESCREEN_TASK_NAME in BEAT_TASK_NAMES\n"
        "print('ok')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert proc.returncode == 0, (
        "stapel_moderation.beat must import with no Django app registry:\n"
        + proc.stderr
    )


def test_tasks_still_re_exports_the_schedule():
    """The old import path keeps working — a released host wrote it."""
    from stapel_moderation import beat, tasks

    assert tasks.get_moderation_beat_schedule is beat.get_moderation_beat_schedule
    assert tasks.BEAT_TASK_NAMES == beat.BEAT_TASK_NAMES
    assert tasks.RESCREEN_TASK_NAME == beat.RESCREEN_TASK_NAME
