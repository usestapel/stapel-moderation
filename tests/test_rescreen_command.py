"""``manage.py moderation_rescreen`` — the operator's button.

A dead-letter state with no way to empty it is a state that turns into a
backlog nobody looks at. The beat sweep gets there eventually; this is the
call a release engineer makes when the proxy is back and "eventually" is not
good enough — and the one that moves cases carrying pre-0.7.0
screening-failure verdicts, which this release deliberately does not rewrite
in SQL.
"""
import pytest
from django.core.management import call_command

from stapel_moderation.models import Case, CaseOrigin, CaseState, VerdictDecision
from stapel_moderation.services import DRAFT_KEY_PREFIX

pytestmark = pytest.mark.django_db


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


def test_an_unknown_state_is_refused(content_double):
    from django.core.management.base import CommandError

    with pytest.raises(CommandError):
        call_command("moderation_rescreen", "--state", "nonsense")
