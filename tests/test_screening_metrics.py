"""Screening becomes a number.

The regression these guard: on a client fleet's stand, screening failed 215
times out of 276 over twelve days and every dashboard stayed green, because
the module's fallback worked perfectly and nothing counted the fallback
being used. A graceful degradation with no counter is an outage that looks
like health.
"""
import pytest

from stapel_moderation import metrics as mod_metrics
from stapel_core.observability import metrics as metrics_mod
from stapel_core.observability.backends import NoopMetricsBackend


class RecordingBackend(NoopMetricsBackend):
    available = True

    def __init__(self):
        self.calls = []

    def counter(self, name, value=1.0, labels=None, *, description=""):
        self.calls.append(("counter", name, value, dict(labels or {})))

    def gauge(self, name, value, labels=None, *, description=""):
        self.calls.append(("gauge", name, value, dict(labels or {})))

    def histogram(self, name, value, labels=None, *, description="", buckets=None):
        self.calls.append(("histogram", name, value, dict(labels or {})))


@pytest.fixture
def recorded():
    backend = RecordingBackend()
    metrics_mod.set_backend(backend)
    yield backend
    metrics_mod.reset_backend()


def _for(backend, metric, kind="counter"):
    return [c for c in backend.calls if c[0] == kind and c[1].endswith(metric)]


def test_record_screen_counts_outcome_and_time(recorded):
    mod_metrics.record_screen("listing", "approved", seconds=2.5)
    hit = _for(recorded, mod_metrics.SCREEN_METRIC)
    assert hit and hit[0][3] == {"target_type": "listing", "outcome": "approved"}
    timed = _for(recorded, mod_metrics.SCREEN_DURATION_METRIC, "histogram")
    assert timed and timed[0][2] == 2.5


def test_unknown_decision_does_not_invent_a_series(recorded):
    """Label values become Prometheus series. An unbounded vocabulary is a
    cardinality incident, so an unexpected decision is counted as `other`
    rather than passed through."""
    mod_metrics.record_screen("listing", "wibble")
    assert _for(recorded, mod_metrics.SCREEN_METRIC)[0][3]["outcome"] == "other"


def test_declare_series_creates_every_outcome_at_zero(recorded):
    """`rate(...[15m]) > 0` on a series that has never existed does not fire.
    It reports nothing, which looks exactly like healthy."""
    mod_metrics.declare_series(("listing", "review"))

    zeroed = _for(recorded, mod_metrics.SCREEN_METRIC)
    assert all(c[2] == 0 for c in zeroed)
    seen = {(c[3]["target_type"], c[3]["outcome"]) for c in zeroed}
    for target in ("listing", "review"):
        for outcome in mod_metrics.OUTCOMES:
            assert (target, outcome) in seen
    # The fail-open series too — it is the one with no other record at all.
    assert _for(recorded, mod_metrics.DRAFT_FAIL_OPEN_METRIC)


def test_fail_open_is_counted_separately_from_the_screen_outcome(recorded):
    """"We could not screen" and "and we let it through anyway" are two
    facts. A deployment that changes its mind about the second must not lose
    the history of the first."""
    mod_metrics.record_draft_screen("listing", mod_metrics.OUTCOME_UNAVAILABLE)
    mod_metrics.record_draft_fail_open("listing", reason_code="screening_unavailable")

    assert _for(recorded, mod_metrics.DRAFT_SCREEN_METRIC)[0][3]["outcome"] == (
        "unavailable"
    )
    assert _for(recorded, mod_metrics.DRAFT_FAIL_OPEN_METRIC)[0][3] == {
        "target_type": "listing"
    }


def test_recording_never_raises_when_the_backend_is_down():
    """Every call site is doing something more important than being
    observed, and half are already on a failure path."""

    class Exploding(NoopMetricsBackend):
        available = True

        def counter(self, *a, **k):
            raise RuntimeError("metrics down")

        def histogram(self, *a, **k):
            raise RuntimeError("metrics down")

    metrics_mod.set_backend(Exploding())
    try:
        mod_metrics.record_screen("listing", "approved", seconds=1.0)
        mod_metrics.record_draft_screen("listing", "rejected")
        mod_metrics.record_draft_fail_open("listing")
        mod_metrics.declare_series(("listing",))
    finally:
        metrics_mod.reset_backend()


@pytest.mark.django_db
def test_a_failed_screen_counts_unavailable_and_still_raises(recorded, monkeypatch):
    """The failure must be COUNTED and still PROPAGATE: the raise is what
    buys the retry ladder, and swallowing it into a counter would mark the
    task DONE with no verdict."""
    from stapel_moderation import services, tasks
    from stapel_moderation.models import Case, CaseOrigin, CaseState
    from stapel_moderation.registry import register_target_type
    from stapel_moderation.screening import ScreeningUnavailable

    register_target_type(
        "listing",
        {
            "intake_events": ["listing.submitted"],
            "id_field": "listing_id",
            "content_function": "listings.moderation_content",
        },
    )

    case = Case.objects.create(
        target_type="listing",
        target_key="listing:1",
        origin=CaseOrigin.SUBMISSION,
        state=CaseState.OPEN,
    )

    def boom(*a, **k):
        raise ScreeningUnavailable("provider unreachable")

    monkeypatch.setattr(tasks, "get_screener", lambda: boom, raising=False)
    monkeypatch.setattr(
        services,
        "fetch_content",
        lambda *a, **k: services.TargetContent(title="t", text="x", language="ru"),
    )

    with pytest.raises(ScreeningUnavailable):
        tasks.screen_case({"case_id": str(case.id)})

    hit = _for(recorded, mod_metrics.SCREEN_METRIC)
    assert hit[-1][3] == {"target_type": "listing", "outcome": "unavailable"}
