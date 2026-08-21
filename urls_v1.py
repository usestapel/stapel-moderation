"""v1 URL set — paths are relative to the ``api/v1/`` mount contributed by the
root ``urls.py`` (api-versioning.md §2).

The file is ordered by AUDIENCE, not by resource: the user-facing routes come
first and the moderator console second, so "which routes does a logged-in
member reach" is answered by reading the file rather than by auditing eleven
permission classes.
"""
from typing import NamedTuple

from django.urls import path

from .errors import ModerationErrorKeysView
from .views import (
    AppealListCreateView,
    AppealQueueView,
    AppealResolveView,
    CaseClaimView,
    CaseDetailView,
    CaseEventsView,
    CaseListView,
    CaseReleaseView,
    CaseRescanView,
    CaseVerdictView,
    PolicyDisclosureView,
    ReportListCreateView,
    SanctionLiftView,
    SanctionListCreateView,
    StatsView,
)

user_patterns = [
    # Complaint intake and the submitter's own history.
    path("reports/", ReportListCreateView.as_view(), name="moderation-reports"),
    # The DSA Art. 15 disclosure. The only anonymous route in the module.
    path("policy", PolicyDisclosureView.as_view(), name="moderation-policy"),
    # Appeals by the subject of a decision.
    path("appeals/", AppealListCreateView.as_view(), name="moderation-appeals"),
]

console_patterns = [
    path("cases", CaseListView.as_view(), name="moderation-cases"),
    path("cases/<uuid:case_id>", CaseDetailView.as_view(), name="moderation-case"),
    path("cases/<uuid:case_id>/claim", CaseClaimView.as_view(), name="moderation-case-claim"),
    path("cases/<uuid:case_id>/release", CaseReleaseView.as_view(), name="moderation-case-release"),
    path("cases/<uuid:case_id>/verdict", CaseVerdictView.as_view(), name="moderation-case-verdict"),
    path("cases/<uuid:case_id>/rescan", CaseRescanView.as_view(), name="moderation-case-rescan"),
    path("cases/<uuid:case_id>/events", CaseEventsView.as_view(), name="moderation-case-events"),
    path("stats", StatsView.as_view(), name="moderation-stats"),
    path("sanctions", SanctionListCreateView.as_view(), name="moderation-sanctions"),
    path(
        "sanctions/<uuid:sanction_id>/lift",
        SanctionLiftView.as_view(),
        name="moderation-sanction-lift",
    ),
    path("appeals", AppealQueueView.as_view(), name="moderation-appeal-queue"),
    path(
        "appeals/<uuid:appeal_id>/resolve",
        AppealResolveView.as_view(),
        name="moderation-appeal-resolve",
    ),
]

urlpatterns = [
    *user_patterns,
    *console_patterns,
    # The listing the stapel-translate error collector reads.
    path("error-keys/", ModerationErrorKeysView.as_view(), name="moderation-error-keys"),
]


class GateEntry(NamedTuple):
    """One gated URL block (capability-config.md §2 p.2). ``flags`` compose
    with OR; empty flags = always on."""

    name: str
    flags: tuple
    patterns: tuple


#: Two blocks rather than one, because they answer different questions. The
#: console is always mounted — turning it off by configuration would give a
#: deployment a way to have a moderation queue nobody can work. The intake
#: block is declared separately so the capabilities emitter can show that the
#: complaint surface and the console are distinct halves of the module.
GATE_REGISTRY: dict = {
    "moderation.intake": GateEntry("moderation.intake", (), tuple(user_patterns)),
    "moderation.console": GateEntry("moderation.console", (), tuple(console_patterns)),
}
