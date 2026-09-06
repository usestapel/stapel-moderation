"""``manage.py moderation_rescreen`` — the operator's button.

A dead-letter state with no way to empty it is a state that turns into a
backlog nobody looks at. The beat sweep gets there eventually; this is the
call a release engineer makes when the proxy is back and "eventually" is not
good enough — and the one that moves cases carrying pre-0.7.0
screening-failure verdicts, which this release deliberately does not rewrite
in SQL.
"""
import io

import pytest
from django.core.management import call_command
from django.test import override_settings

from stapel_moderation.models import Case, CaseOrigin, CaseState, VerdictDecision
from stapel_moderation.services import DRAFT_KEY_PREFIX

pytestmark = pytest.mark.django_db

#: Task ids the "worker" was handed. A dotted path in ``TASK_EXECUTOR`` is
#: core's own seam for "somebody else runs this", so this double stands in for
#: the worker process without needing one.
DISPATCHED: list[str] = []


def fake_worker(task_id: str) -> None:
    DISPATCHED.append(str(task_id))


#: Where core will import the double from. Read back through the same path it
#: is configured with, because pytest's importlib mode gives this file a module
#: name of its own and `import_string` would otherwise load a SECOND copy —
#: with a second, always-empty list.
WORKER = "stapel_moderation.tests.test_rescreen_command"


@pytest.fixture
def worker():
    """A deployment whose tasks leave this process, like every real one."""
    from django.conf import settings
    from django.utils.module_loading import import_string

    dispatched = import_string(f"{WORKER}.DISPATCHED")
    dispatched.clear()
    comm = dict(settings.STAPEL_COMM)
    comm["TASK_DISPATCH"] = "action"
    comm["TASK_EXECUTOR"] = f"{WORKER}.fake_worker"
    with override_settings(STAPEL_COMM=comm):
        yield dispatched
    dispatched.clear()


def _dlq_case(target_key="42", error_class="ScreeningUnavailable", **kwargs):
    from django.utils import timezone

    return Case.objects.create(
        target_type="listing",
        target_key=target_key,
        state=CaseState.DLQ,
        dlq_at=timezone.now(),
        last_error_class=error_class,
        last_error="llm.complete unreachable",
        **kwargs,
    )


def test_it_re_screens_the_park_when_the_seam_is_repaired(content_double, llm_double):
    case = _dlq_case()

    call_command("moderation_rescreen", "--state", "dlq")

    case.refresh_from_db()
    assert case.state == CaseState.RESOLVED
    assert case.last_verdict.decision == VerdictDecision.APPROVED


def test_it_closes_a_case_whose_subject_cannot_be_addressed(content_double):
    """The 69 draft cases. Not re-screened — closed."""
    case = _dlq_case(
        target_key=f"{DRAFT_KEY_PREFIX}71bde856",
        origin=CaseOrigin.DRAFT,
        error_class="ContentUnavailable",
    )

    call_command("moderation_rescreen", "--state", "dlq")

    case.refresh_from_db()
    assert case.state == CaseState.RESOLVED
    assert case.last_verdict.reason_code == "subject_gone"


def test_a_dry_run_changes_nothing(content_double, llm_double):
    case = _dlq_case()

    call_command("moderation_rescreen", "--state", "dlq", "--dry-run")

    case.refresh_from_db()
    assert case.state == CaseState.DLQ


def test_error_class_repairs_one_seam_at_a_time(content_double, llm_double):
    """Re-billing every case in the park to fix one provider is not a repair."""
    llm_broke = _dlq_case(target_key="42", error_class="ScreeningUnavailable")
    content_broke = _dlq_case(target_key="43", error_class="ContentUnavailable")

    call_command(
        "moderation_rescreen", "--state", "dlq", "--error-class", "ScreeningUnavailable"
    )

    llm_broke.refresh_from_db()
    content_broke.refresh_from_db()
    assert llm_broke.state == CaseState.RESOLVED
    assert content_broke.state == CaseState.DLQ


def test_it_dispatches_to_the_worker_and_says_how_many(
    content_double, llm_double, worker
):
    """The screening belongs to the worker, where a scrape can see it.

    Run inside ``docker compose run``, an inline screening is invisible: the
    container is not scraped and exits when the command returns, so a manual
    rescreen's failures never reach the counters the DLQ alert watches.
    """
    first = _dlq_case(target_key="42")
    second = _dlq_case(target_key="43")
    out = io.StringIO()

    call_command("moderation_rescreen", "--state", "dlq", stdout=out)

    assert len(worker) == 2
    assert "dispatched 2" in out.getvalue()
    for case in (first, second):
        case.refresh_from_db()
        # Handed over, not decided here: the verdict is the worker's to write.
        assert case.state == CaseState.SCREENING
        assert case.last_verdict_id is None


def test_sync_still_screens_in_this_process(content_double, llm_double, worker):
    """The debug flag keeps working — and says that nothing watched it."""
    case = _dlq_case()
    out = io.StringIO()

    call_command("moderation_rescreen", "--state", "dlq", "--sync", stdout=out)

    case.refresh_from_db()
    assert case.state == CaseState.RESOLVED
    assert case.last_verdict.decision == VerdictDecision.APPROVED
    assert "screened here 1" in out.getvalue()
    assert "invisible to the DLQ alert" in out.getvalue()


def test_an_inline_deployment_is_told_its_dispatch_was_not_one(
    content_double, llm_double
):
    """No claiming a handover the configuration cancels.

    The harness settings run tasks inline, which is what a one-off container
    on a monolith does — and a count that says "dispatched" about work this
    process just did is the same silence in a new sentence.
    """
    _dlq_case()
    out = io.StringIO()

    call_command("moderation_rescreen", "--state", "dlq", stdout=out)

    assert "runs tasks inline in this process" in out.getvalue()


def test_an_unknown_state_is_refused(content_double):
    from django.core.management.base import CommandError

    with pytest.raises(CommandError):
        call_command("moderation_rescreen", "--state", "nonsense")
