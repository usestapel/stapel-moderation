"""Models for stapel-moderation.

The central entity is **not** the complaint — it is the :class:`Case`, one
unit of moderation work about one target. Forty reports about one listing are
forty :class:`Report` rows hanging off ONE case with ``report_count == 40``;
legacy kept forty queue rows and that was its defining scaling defect.

**One status vocabulary in the whole module** (spec §3). ``Case.state`` is it.
:class:`Report` has no status of its own and inherits the case's;
:class:`Verdict` has none because it is an append-only fact; :class:`Sanction`
has an orthogonal lifecycle that never mixes with a case state. This is the
cure, by construction, for legacy's three near-identical unrelated status
enums plus two more free copies in a serializer and a template.

House rules:

- the moderated target is **opaque**: ``target_type`` (a host-registered
  registry key) + ``target_key`` (an opaque host string — a UUID, a slug, a
  composite — never parsed). There is no FK to any host model.
- every actor is a bare ``UUIDField``, not an FK to ``AUTH_USER_MODEL``: a
  moderation case survives the deletion of the account it is about, and the
  audit trail must not cascade away with a user row.
- ``db_table`` is spelled out with a ``moderation_`` prefix rather than left
  to Django's implicit default (the docs/recordings/billing convention).
"""
import uuid

from django.db import models
from stapel_core.access import Level, access
from stapel_core.django.projections.models import ProjectionModel


class CaseState(models.TextChoices):
    """The single status vocabulary of the module.

    Members:
        OPEN: Created; awaiting screening or about to be queued.
        SCREENING: A ``moderation.screen`` comm-Task is in flight.
        QUEUED: A human is needed — the automation abstained, failed, or the
            policy demands a person.
        CLAIMED: A moderator holds a lease on the case.
        RESOLVED: A verdict was recorded and emitted. The only terminal state.
    """

    OPEN = "open", "Open"
    SCREENING = "screening", "Screening"
    QUEUED = "queued", "Queued"
    CLAIMED = "claimed", "Claimed"
    RESOLVED = "resolved", "Resolved"


#: States in which a case is still the live case for its target. The partial
#: unique constraint over these is the whole idempotency mechanism (spec §5.3):
#: a redelivered intake event finds the open case instead of opening a second.
OPEN_STATES = (
    CaseState.OPEN,
    CaseState.SCREENING,
    CaseState.QUEUED,
    CaseState.CLAIMED,
)

#: The one transition table, modelled on ``LISTING_TRANSITIONS``. Every edge a
#: service may take is here; anything else raises. ``resolved -> queued`` is
#: the single backward edge and it exists for exactly one reason: an appeal
#: that succeeds has to be able to reopen the case it is appealing.
CASE_TRANSITIONS = {
    CaseState.OPEN: (CaseState.SCREENING, CaseState.QUEUED, CaseState.RESOLVED),
    CaseState.SCREENING: (CaseState.QUEUED, CaseState.RESOLVED, CaseState.OPEN),
    CaseState.QUEUED: (CaseState.CLAIMED, CaseState.RESOLVED, CaseState.SCREENING),
    CaseState.CLAIMED: (CaseState.QUEUED, CaseState.RESOLVED),
    CaseState.RESOLVED: (CaseState.QUEUED,),
}


class VerdictDecision(models.TextChoices):
    """What was decided. Distinct from :class:`CaseState` on purpose.

    Members:
        APPROVED: No violation; the target stays as it is.
        REJECTED: Violation; the target is taken down or hidden.
        NEEDS_REVIEW: The automation abstained. Not terminal for the case.
        DISMISSED: The report needed no action — a statement about the
            COMPLAINT, where ``approved`` is a statement about the CONTENT.

    The first three are literally the words ``stapel-listings`` already
    consumes, so an automated verdict reaches a target with no translation
    dictionary in between; ``dismissed`` was added to the listings consumer in
    0.4.0 for the fourth (spec §21.2).
    """

    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"
    NEEDS_REVIEW = "needs_review", "Needs review"
    DISMISSED = "dismissed", "Dismissed"


#: Decisions that close a case. ``needs_review`` does not: it is the machine
#: saying "a person must look", which is the opposite of a resolution.
TERMINAL_DECISIONS = (
    VerdictDecision.APPROVED,
    VerdictDecision.REJECTED,
    VerdictDecision.DISMISSED,
)


class VerdictSource(models.TextChoices):
    """Who or what produced a verdict."""

    LLM = "llm", "LLM screener"
    RULE = "rule", "Deterministic rule"
    HUMAN = "human", "Human moderator"
    POLICY_DEFAULT = "policy_default", "Policy default"
    APPEAL = "appeal", "Appeal outcome"


class CaseOrigin(models.TextChoices):
    """How the case came into being."""

    SUBMISSION = "submission", "Target submitted for review"
    REPORT = "report", "User report"
    MANUAL = "manual", "Opened by a moderator"
    RESCAN = "rescan", "Re-screen of an earlier decision"
    APPEAL = "appeal", "Reopened by an appeal"


class CaseEventKind(models.TextChoices):
    """Audit vocabulary. Append-only; see :class:`CaseEvent`."""

    CREATED = "created", "Created"
    REPORTED = "reported", "Reported"
    RESUBMITTED = "resubmitted", "Resubmitted"
    SCREEN_STARTED = "screen_started", "Screening started"
    SCREEN_FAILED = "screen_failed", "Screening failed"
    VERDICT = "verdict", "Verdict recorded"
    STATE_CHANGED = "state_changed", "State changed"
    APPLIED = "applied", "Verdict applied by the target"
    APPLY_FAILED = "apply_failed", "Verdict application unconfirmed"
    CLAIMED = "claimed", "Claimed"
    RELEASED = "released", "Released"
    SANCTIONED = "sanctioned", "Sanction issued"
    APPEALED = "appealed", "Appealed"
    REOPENED = "reopened", "Reopened"
    NOTIFIED = "notified", "Notification requested"


class SanctionKind(models.TextChoices):
    """Escalating account-level consequences."""

    WARNING = "warning", "Warning"
    CONTENT_REMOVED = "content_removed", "Content removed"
    POSTING_RESTRICTED = "posting_restricted", "Posting restricted"
    SUSPENDED = "suspended", "Suspended"
    BANNED = "banned", "Banned"


class SanctionState(models.TextChoices):
    """A sanction's own lifecycle — never mixed with a case state.

    Members:
        ACTIVE: In force.
        EXPIRED: Ran out on its own clock.
        LIFTED: A moderator revoked it early.
        OVERTURNED: An appeal undid it (distinct from ``lifted``: one is
            discretion, the other is the platform being wrong).
    """

    ACTIVE = "active", "Active"
    EXPIRED = "expired", "Expired"
    LIFTED = "lifted", "Lifted"
    OVERTURNED = "overturned", "Overturned"


class AppealState(models.TextChoices):
    """An appeal's lifecycle (DSA Art. 20)."""

    OPEN = "open", "Open"
    UPHELD = "upheld", "Upheld (the original decision stands)"
    OVERTURNED = "overturned", "Overturned"
    WITHDRAWN = "withdrawn", "Withdrawn"


@access.sensitive
class Case(models.Model):
    """One unit of moderation work about one opaque target.

    ``@access.sensitive`` (view MID, mutations HIGH): a case card carries the
    complaint text and the complainant's identity.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # The opaque target. Neither field is an FK — the module is domain-blind.
    target_type = models.CharField(max_length=64, db_index=True)
    target_key = models.CharField(max_length=255, db_index=True)
    #: Opaque tenant/area string (the stapel-chat ``scope_key`` seam). It
    #: partitions the queue without a workspace capability door (spec §8).
    scope_key = models.CharField(max_length=255, db_index=True, blank=True, default="")

    origin = models.CharField(
        max_length=16, choices=CaseOrigin.choices, default=CaseOrigin.REPORT
    )
    state = models.CharField(
        max_length=16, choices=CaseState.choices, default=CaseState.OPEN, db_index=True
    )
    #: Highest severity seen from the reason registry — the queue's sort key.
    severity = models.SmallIntegerField(default=0)
    #: The content's author, learned from ``content_function``. Nullable
    #: because a target may resolve later (or never).
    subject_user_id = models.UUIDField(null=True, blank=True, db_index=True)
    #: Denormalized count of reports. Owned by this module, so it cannot
    #: drift against a table somebody else writes.
    report_count = models.PositiveIntegerField(default=0)

    first_reported_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    #: Lease, not lock: ``claimed_until`` expires and the case returns to the
    #: queue. Legacy had neither assignment nor lease, so two moderators
    #: silently worked the same report.
    claimed_by = models.UUIDField(null=True, blank=True)
    claimed_until = models.DateTimeField(null=True, blank=True)

    resolved_at = models.DateTimeField(null=True, blank=True)
    last_verdict = models.ForeignKey(
        "moderation.Verdict",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    #: The comm TaskRecord id of the in-flight screening, for operators.
    screen_task_id = models.UUIDField(null=True, blank=True)
    screen_attempts = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "moderation_case"
        ordering = ["-created_at"]
        constraints = [
            # Idempotency BY STATE, not by event id: a redelivered
            # listing.submitted finds this row instead of opening a twin.
            # The outbox keeps no processed-event table and a JSONB lookup
            # on data__event_id (the notifications approach) does not scale;
            # state is the one carrier of truth that already exists.
            models.UniqueConstraint(
                fields=["target_type", "target_key"],
                condition=models.Q(state__in=OPEN_STATES),
                name="uniq_open_case_per_target",
            ),
        ]
        indexes = [
            models.Index(fields=["state", "severity", "-created_at"], name="mod_case_queue"),
            models.Index(fields=["target_type", "state"], name="mod_case_target"),
            models.Index(fields=["subject_user_id", "-created_at"], name="mod_case_subject"),
        ]

    def __str__(self):
        return f"{self.target_type}:{self.target_key} ({self.state})"

    @property
    def is_open(self) -> bool:
        return self.state in OPEN_STATES


@access.sensitive
class Report(models.Model):
    """One user's complaint about one target.

    **No status field** (spec §3). A report's fate is its case's fate; giving
    it a second vocabulary is how legacy ended up with two byte-identical
    enums that could disagree.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    case = models.ForeignKey(Case, on_delete=models.CASCADE, related_name="reports")

    #: Denormalized from the case so the one-report-per-user constraint keeps
    #: holding after the case is resolved and a new one opens for the target.
    target_type = models.CharField(max_length=64)
    target_key = models.CharField(max_length=255)

    #: Null after GDPR erasure — the report survives, the reporter does not.
    reporter_id = models.UUIDField(null=True, blank=True, db_index=True)
    reason_code = models.CharField(max_length=64)
    description = models.TextField(blank=True, default="")
    #: Reporter-supplied snapshot of the target, for target types whose
    #: content NOBODY serves — an ephemeral chat message, a story, a live
    #: stream frame. Accepted only where the policy says ``evidence: True``,
    #: bounded by ``MAX_EVIDENCE_BYTES``, and ALWAYS unverified: it is what
    #: the complainant says they saw, never what the platform read. The
    #: content assembled from it is marked ``source: "evidence"`` so no
    #: console can render it as if a content_function had answered.
    evidence = models.JSONField(default=dict, blank=True)
    #: DSA Art. 16(2)(d) — the good-faith declaration.
    good_faith = models.BooleanField(default=False)
    #: DSA Art. 16(2)(c) — a contact for the anonymous intake mode.
    contact_email = models.EmailField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "moderation_report"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["target_type", "target_key", "reporter_id"],
                name="uniq_report_per_user",
            ),
        ]
        indexes = [
            models.Index(fields=["case", "-created_at"], name="mod_report_case"),
            models.Index(fields=["reason_code"], name="mod_report_reason"),
        ]

    def __str__(self):
        return f"report {self.id} ({self.reason_code})"


@access.sensitive
class Verdict(models.Model):
    """An append-only decision about a case. Never updated, never deleted
    outside the retention purge."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    case = models.ForeignKey(Case, on_delete=models.CASCADE, related_name="verdicts")

    decision = models.CharField(max_length=16, choices=VerdictDecision.choices)
    source = models.CharField(max_length=16, choices=VerdictSource.choices)
    #: Null for machine verdicts.
    actor_id = models.UUIDField(null=True, blank=True)
    reason_code = models.CharField(max_length=64, blank=True, default="")
    #: The statement of reasons (DSA Art. 17).
    note = models.TextField(blank=True, default="")
    #: LLM only.
    confidence = models.FloatField(null=True, blank=True)
    #: ``{"excerpt": str, "media_refs": [...], "matched_rules": [...]}``. The
    #: excerpt is the single deliberate exception to "keep no copy of the
    #: content": a statement of reasons has to stay checkable after the
    #: content is deleted. It never rides the bus, and it dies with the case
    #: on the retention clock.
    evidence = models.JSONField(default=dict, blank=True)
    #: Provider/size and the prompt version that produced an LLM verdict.
    model = models.CharField(max_length=128, blank=True, default="")
    usage = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "moderation_verdict"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["case", "-created_at"], name="mod_verdict_case"),
        ]

    def __str__(self):
        return f"{self.decision} by {self.source}"


@access.ops
class CaseEvent(models.Model):
    """The append-only audit trail of one case.

    ``@access.ops`` — view HIGH and every mutation ``FORBIDDEN`` by
    declaration, so the log is physically uneditable through the admin even by
    a superuser. That is the point: legacy's second resolution path (admin
    bulk actions writing straight through ``queryset.update()``) was invisible
    to its audit table, and no instruction can prevent what a declaration can.

    ``from_state``/``to_state`` carry ``choices``, unlike legacy's
    ``ReportStatusLog``, whose two bare CharFields were the reason a fourth
    and fifth spelling of the status vocabulary could exist.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    case = models.ForeignKey(Case, on_delete=models.CASCADE, related_name="events")

    kind = models.CharField(max_length=24, choices=CaseEventKind.choices)
    from_state = models.CharField(
        max_length=16, choices=CaseState.choices, blank=True, default=""
    )
    to_state = models.CharField(
        max_length=16, choices=CaseState.choices, blank=True, default=""
    )
    #: Null means the system acted.
    actor_id = models.UUIDField(null=True, blank=True)
    payload = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "moderation_case_event"
        ordering = ["created_at", "id"]
        indexes = [
            models.Index(fields=["case", "created_at"], name="mod_event_case"),
        ]

    def __str__(self):
        return f"{self.kind} @ {self.created_at:%Y-%m-%d %H:%M:%S}"


@access(category="business", view=Level.MID, add=Level.HIGH, change=Level.HIGH, delete=Level.HIGH)
class Sanction(models.Model):
    """An account-level consequence with a reason, a scope, a clock and an
    audit trail — not a boolean gate.

    The access declaration is deliberately the same one ``StaffRoleAssignment``
    carries in stapel-auth: handing out a ban is an operation of the same class
    as handing out a role.

    ``case`` is PROTECTed and never null, including for ``origin=manual``
    sanctions: one audit trail, no side door.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    case = models.ForeignKey(Case, on_delete=models.PROTECT, related_name="sanctions")

    subject_user_id = models.UUIDField(db_index=True)
    kind = models.CharField(max_length=24, choices=SanctionKind.choices)
    #: ``"*"`` (everything), a ``target_type``, or a ``scope_key``.
    scope = models.CharField(max_length=255, default="*")
    reason_code = models.CharField(max_length=64, blank=True, default="")
    note = models.TextField(blank=True, default="")

    starts_at = models.DateTimeField(db_index=True)
    #: Null = indefinite.
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    state = models.CharField(
        max_length=16, choices=SanctionState.choices, default=SanctionState.ACTIVE
    )

    issued_by = models.UUIDField(null=True, blank=True)
    lifted_by = models.UUIDField(null=True, blank=True)
    lifted_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "moderation_sanction"
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["subject_user_id", "state", "expires_at"],
                name="mod_sanction_lookup",
            ),
        ]

    def __str__(self):
        return f"{self.kind} on {self.subject_user_id} ({self.state})"


@access.sensitive
class Appeal(models.Model):
    """The internal complaint-handling system of DSA Art. 20."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    case = models.ForeignKey(Case, on_delete=models.CASCADE, related_name="appeals")
    sanction = models.ForeignKey(
        Sanction, null=True, blank=True, on_delete=models.SET_NULL, related_name="appeals"
    )

    appellant_id = models.UUIDField(db_index=True)
    body = models.TextField()
    state = models.CharField(
        max_length=16, choices=AppealState.choices, default=AppealState.OPEN, db_index=True
    )
    resolved_by = models.UUIDField(null=True, blank=True)
    resolution_note = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "moderation_appeal"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["case", "appellant_id"], name="uniq_appeal_per_case_user"
            ),
        ]

    def __str__(self):
        return f"appeal {self.id} ({self.state})"


class UserSanctionState(ProjectionModel):
    """Read-model row of the ``moderation.user_sanctions`` projection.

    Written ONLY by the projection runner in remote mode; in a co-located
    deployment the table stays empty and reads go through the owner's
    ``live_query`` Function instead. Business code never touches it directly —
    it calls ``projections.read("moderation.user_sanctions", keys=[...])``,
    which answers identically in both modes.
    """

    #: Mirrors the live_query answer so both modes hand back the same shape.
    allowed = models.BooleanField(default=True)
    sanctions = models.JSONField(default=list, blank=True)

    class Meta:
        db_table = "moderation_user_sanction_state"

    def __str__(self):
        return f"sanctions of {self.projection_key}"


__all__ = [
    "CASE_TRANSITIONS",
    "OPEN_STATES",
    "TERMINAL_DECISIONS",
    "Appeal",
    "AppealState",
    "Case",
    "CaseEvent",
    "CaseEventKind",
    "CaseOrigin",
    "CaseState",
    "Report",
    "Sanction",
    "SanctionKind",
    "SanctionState",
    "UserSanctionState",
    "Verdict",
    "VerdictDecision",
    "VerdictSource",
]
