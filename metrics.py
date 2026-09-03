"""Screening, as a number an operator can alarm on.

This module shipped with no instrumentation at all, and the cost of that
was measured rather than imagined. On a client fleet's stand, between
2026-08-21 and 2026-09-02, **215 of 276 screening tasks failed** — a 78%
failure rate, sustained for twelve days. The module behaved correctly
throughout: every one of those cases landed in the human queue as
``needs_review / screening_unavailable``, exactly as ``ON_SCREENING_FAILURE
= "hold"`` promises, and nothing was published unscreened. That is the
good news and it is also the trap.

Because the fallback worked, nothing looked broken. Containers were Up,
healthchecks green, the API answered, the queue filled with cases a human
would eventually work. The automatic screener had been down for nearly two
weeks and the only place that fact was written down was a
``failure_reason`` column nobody had a reason to query. A degradation that
degrades gracefully is invisible precisely because it degrades gracefully —
which is why the graceful path is the one that most needs a counter.

Three questions these metrics answer that no log line can:

``moderation_screen_total{outcome}``
    How often screening reaches a verdict at all, split by what happened.
    ``unavailable`` rising while ``approved``/``rejected`` fall is the
    signature of a provider outage; the human queue filling up is the
    same event seen an hour later, by a person.
``moderation_screen_seconds``
    What screening costs in wall time. A screen measured at ~3s against a
    45-60s timeout has enormous headroom, and nobody can say whether it
    still does without the distribution.
``moderation_draft_screen_total{outcome}``
    The synchronous draft entrance, whose caller is entitled to fail OPEN
    (a seller must not be blocked by our provider) and which therefore
    produces no case, no verdict and no queue row when it cannot run. It
    is the one path where an outage leaves nothing behind at all — so it
    is the path where the counter is not a convenience but the only
    record that exists.

Recording never raises. Every call site here is doing something more
important than being observed, and half of them are already on a failure
path.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: Asynchronous case screening (the `moderation.screen` task).
SCREEN_METRIC = "moderation_screen_total"
SCREEN_DURATION_METRIC = "moderation_screen_seconds"

#: The synchronous draft entrance (`moderation.screen_draft`).
DRAFT_SCREEN_METRIC = "moderation_draft_screen_total"

#: What a caller did with an unavailable answer. Separate from the screen
#: outcome on purpose: "we could not screen" and "and we let it through
#: anyway" are two facts, and a deployment that changes its mind about the
#: second must not lose the history of the first.
DRAFT_FAIL_OPEN_METRIC = "moderation_draft_screen_fail_open_total"

#: Outcome label values. A closed vocabulary — these become Prometheus
#: label values, and an unbounded set of them is a cardinality incident.
OUTCOME_APPROVED = "approved"
OUTCOME_REJECTED = "rejected"
OUTCOME_NEEDS_REVIEW = "needs_review"
OUTCOME_DISMISSED = "dismissed"
OUTCOME_UNAVAILABLE = "unavailable"
OUTCOMES = (
    OUTCOME_APPROVED,
    OUTCOME_REJECTED,
    OUTCOME_NEEDS_REVIEW,
    OUTCOME_DISMISSED,
    OUTCOME_UNAVAILABLE,
)

_DESCRIPTIONS = {
    SCREEN_METRIC: "Case screenings by outcome (unavailable = could not run)",
    SCREEN_DURATION_METRIC: "Wall time of one case screening, seconds",
    DRAFT_SCREEN_METRIC: "Draft screenings by outcome",
    DRAFT_FAIL_OPEN_METRIC: (
        "Drafts allowed through because screening could not answer"
    ),
}


def _safe(fn_name: str, name: str, *args, **kwargs) -> None:
    try:
        from stapel_core.observability import metrics

        kwargs.setdefault("description", _DESCRIPTIONS.get(name, ""))
        getattr(metrics, fn_name)(name, *args, **kwargs)
    except Exception:  # pragma: no cover - the facade already guards itself
        logger.debug("moderation: metric %s not recorded", name, exc_info=True)


def _outcome(value: str) -> str:
    """Clamp to the closed vocabulary — an unknown decision is counted, not
    dropped, but it does not get to invent a new series."""
    text = str(value or "").lower()
    return text if text in OUTCOMES else "other"


def declare_series(target_types=()) -> None:
    """Create every series at zero, before anything has happened.

    A counter that has never been incremented does not exist, and
    ``rate(moderation_screen_total{outcome="unavailable"}[15m]) > 0`` on a
    series with no subject does not fire — it silently reports nothing,
    which is the same shape as the outage it is meant to catch. Call this
    at app startup, where the target types are known.
    """
    types = tuple(target_types) or ("",)
    for target_type in types:
        for outcome in (*OUTCOMES, "other"):
            _safe(
                "counter", SCREEN_METRIC, 0,
                labels={"target_type": target_type, "outcome": outcome},
            )
            _safe(
                "counter", DRAFT_SCREEN_METRIC, 0,
                labels={"target_type": target_type, "outcome": outcome},
            )
        _safe(
            "counter", DRAFT_FAIL_OPEN_METRIC, 0,
            labels={"target_type": target_type},
        )


def record_screen(target_type: str, outcome: str, *, seconds: float | None = None) -> None:
    """Count one asynchronous case screening."""
    _safe(
        "counter", SCREEN_METRIC,
        labels={"target_type": str(target_type), "outcome": _outcome(outcome)},
    )
    if seconds is not None:
        _safe(
            "histogram", SCREEN_DURATION_METRIC, float(seconds),
            labels={"target_type": str(target_type)},
        )


def record_draft_screen(target_type: str, outcome: str) -> None:
    """Count one synchronous draft screening."""
    _safe(
        "counter", DRAFT_SCREEN_METRIC,
        labels={"target_type": str(target_type), "outcome": _outcome(outcome)},
    )


def record_draft_fail_open(target_type: str, reason_code: str = "") -> None:
    """Count a draft that a CALLER let through unscreened.

    Called by the composer, not by this module: the decision to continue
    without a verdict belongs to the product (a seller must not be blocked
    by our provider blinking), and this module's job is to make sure the
    decision leaves a trace. Without this counter the draft path is the one
    place an outage leaves no record at all — no case, no verdict, no queue
    row, just a log line in a container nobody is tailing.
    """
    _safe(
        "counter", DRAFT_FAIL_OPEN_METRIC,
        labels={"target_type": str(target_type)},
    )
    logger.warning(
        "moderation: draft allowed through UNSCREENED (target_type=%s "
        "reason=%s) — alert on %s",
        target_type, reason_code or "unknown", DRAFT_FAIL_OPEN_METRIC,
    )


__all__ = [
    "DRAFT_FAIL_OPEN_METRIC",
    "DRAFT_SCREEN_METRIC",
    "OUTCOMES",
    "OUTCOME_APPROVED",
    "OUTCOME_DISMISSED",
    "OUTCOME_NEEDS_REVIEW",
    "OUTCOME_REJECTED",
    "OUTCOME_UNAVAILABLE",
    "SCREEN_DURATION_METRIC",
    "SCREEN_METRIC",
    "declare_series",
    "record_draft_fail_open",
    "record_draft_screen",
    "record_screen",
]
