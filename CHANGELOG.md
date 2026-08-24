# Changelog

All notable changes to stapel-moderation are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Pre-1.0 semver: **minor = breaking**, patch = compatible.

## [0.3.0] — 2026-08-24

### Fixed

**The case card's `content` is a field of the contract, not a graft.** The
view built the card, serialized it, then wrote `body["content"] = …` on the
resulting dict — so `docs/schema.json` never carried it and every client
generating types from the contract had to hand-write the one field the whole
moderator console is built around. `content` is now a declared field of
`CaseDetailPresenter`, the schema carries a real `ContentDTO` component, and
the read still happens in the view (it needs the actor, and it can fail):
`present_case_detail(case, content=…)`. A card presented without a resolved
read renders `available=false, error="not_loaded"` — never a missing key.

**`can_view_content` was asked on behalf of nobody.** The view passed
`actor_id=None`, so a target type gating the read per moderator answered
about an anonymous caller. It now receives `request.user.pk`.

**A decided appeal answered `400 invalid_outcome`.** Re-resolving an appeal
that was already upheld or overturned is a STATE conflict, and answering with
a field error sent the console back to fix an `outcome` that was never wrong.
It is now `409 error.409.moderation_appeal_resolved` — the key was registered
in 0.1.0 and had never been reachable. An unknown outcome word is still 400.

**Two more registered refusals were unreachable.**

- `error.400.moderation_reason_not_applicable` — a real reason code that this
  target type does not accept was folded into `unknown_reason`. The remedies
  differ: an unknown code means the client sent nonsense, a non-applicable one
  means the form was built from a stale policy. New
  `services.ReasonNotApplicable`.
- `error.409.moderation_not_claimant` — releasing a case was unconditional, so
  a second console tab could hand back a lease somebody else was working
  under. A named moderator may now only release their own live lease; the
  system sweeper and the `staff.role.revoked` handler pass no actor and are
  never refused. New `services.NotClaimant`.

**`error.400.moderation_evidence_invalid` had no remediation verb** (added in
0.2.0 without one, so a client could not tell what to do about it):
`fix_input`. `tests/test_contract.py` now gates the whole catalogue.

### Changed

- `presenters.present_case_detail(case, *, content=None)` — the extra keyword
  is how the resolved content envelope reaches the DTO.
- `serializers.ContentResponseSerializer` is gone: `ContentDTO` is now
  serialized as a nested dataclass of the card, and a second hand-written
  serializer for the same shape is a second place to drift.

## [0.2.0] — 2026-08-24

### Added

**Evidence-based target types** — a complaint about content NOBODY serves.
Every target so far had an owner answering `*.moderation_content`; a chat
message has none, and by the time a moderator opens the case the thing may
exist only in the complainant's screenshot. A policy now declares
`"evidence": True` instead of a `content_function`, a report carries the
reporter's own snapshot in the new `Report.evidence` JSON field, and the
content assembled from it is stamped `source: "evidence"`, `verified: false`
so no console can render an attestation as something the platform read.

- `Report.evidence` (migration `0002_report_evidence`) — additive, nullable
  by default (`{}`), no backfill.
- `services.validate_evidence` / `evidence_content` / `stored_evidence`, and
  `submit_report(..., evidence=...)`; `fetch_content(..., evidence=...)`.
- `MAX_EVIDENCE_BYTES` (default 8192). Over the bound the report is refused,
  never truncated — a half-quoted message is a moderator deciding on text
  nobody sent.
- `error.400.moderation_evidence_invalid` (+ ru/es catalogues): evidence on a
  type that serves its own content, a non-object blob, or an oversized one.
- `stapel_moderation.E007`: declaring BOTH a `content_function` and
  `evidence` — one target, one source of truth. `E004` did not become
  optional; it grew exactly one alternative.
- An evidence type with no attestation on file reads as `TargetNotFound`
  (404), not `ContentUnavailable` (503): nothing is down, there is nothing to
  look at.

This is what lets stapel-classified register `chat_message` and `seller`
target types without stapel-chat or stapel-profiles shipping a content
function first — see that composite's MODULE.md.

### Changed

- The module's `SerializerSeamMixin` is now **core's** hoisted one
  (`stapel_core.django.api.views`, core 0.37.0) rather than a 24th local copy.
  Same two attributes, same two getters: a host subclass is unaffected.
- Floor raised to `stapel-core>=0.43`.

### Fixed

- The sanction/appeal tests asserted on the raw cache key
  `user_blacklisted:<id>`. core 0.43.0 moved the user blacklist into the
  shared revocation namespace (`stapel_core.core.revocation_store`) so one ban
  is visible to every service verifying the same tokens; the tests now ask
  `is_user_blacklisted()`, which is the contract. Six red tests that were
  asserting a key layout, not a behaviour.

## [0.1.0] — 2026-08-21

First release. The fleet's single producer of moderation verdicts.

### Added

**The queue.** One `Case` per opaque `(target_type, target_key)` — the
stapel-reviews target-generic shape — so listings, reviews, chat messages and
profiles share one queue, one console and one REST surface. Forty complaints
about one listing are forty `Report` rows on **one** case with
`report_count = 40`.

**One status vocabulary.** `Case.state` and its single declared transition
table, with exactly one backward edge (`resolved → queued`, for a successful
appeal). `Report` carries no status; `Verdict` and `CaseEvent` are
append-only; `Sanction` has its own orthogonal lifecycle.

**Three merge-registries** in the reviews idiom (built-ins → settings →
runtime, `None` removes): `TARGET_TYPES` (empty — the module knows no
targets), `REASONS` (non-empty — a complaint taxonomy is universal) and
`RULES` (empty — a shipped keyword list is somebody else's speech policy).
Registering a target type at runtime also wires its intake subscriptions.

**Screening as a comm-Task.** `moderation.screen` runs deterministic rules,
then `llm.complete` with a schema-constrained answer. `llm.complete` returns
`{"status": "failure"}` rather than raising, so the handler converts every
non-`ok` envelope, every `CommError` and every malformed result into a raised
`ScreeningUnavailable` — without which the task would be marked DONE and the
retry ladder would never run.

**Closed defaults, each with a check that says what opening it costs**:
`ON_SCREENING_FAILURE="hold"` (`W001`), `AUTO_RESOLVE_STALE_QUEUE=None`
(`W002`), `ALLOW_ANONYMOUS_REPORTS=False` (`W003`),
`APPEAL_REQUIRES_DIFFERENT_ACTOR=True`. Empty content is screened, not
auto-approved.

**Verdicts that act.** Resolution emits `moderation.completed`, which
stapel-listings 0.4.0 and stapel-reviews 0.2.0 already consume and apply to
themselves. The module never calls a host back to mutate it, and a policy must
declare its `verdict_event` or an explicit `None`.

**Sanctions with teeth.** Kind, scope, reason, clock, appeal and audit, a
configurable progressive ladder, and enforcement through
`stapel_core`'s cross-service user blacklist — the hook DRF authentication,
the middleware, channels and the auth refresh endpoint all already checked and
which had **no producer anywhere in the fleet** until now. `rearm_active_sanctions`
keeps the key alive past its TTL; `W004` warns when it is unscheduled.

**`moderation.user_sanctions` Projection with both halves built** —
`moderation.sanctions_by_users` (`live_query`) and
`moderation.sanctions_export` (`source_of_truth`), the latter answering
`{rows, cursor, total}` with `seq` in unix milliseconds. An `{items}` shape
would rebuild the table to empty and report success.

**Rights are staff roles plus the core mandate**, with per-app clearance and
no parallel allow-list. A MID moderator reads the queue; HIGH is needed to
decide or to sanction, and step-up applies to HIGH automatically.

**Notice-and-action artefacts generated from the registries**: a public
`GET /policy` disclosure, a statement of reasons on every takedown, an
acknowledgement and an outcome to every complainant, a sanction letter with an
appeal link, and an appeal that reopens and re-decides its case rather than
filing a letter. Five notification types through `request_notification`, with
variable names chosen to survive the notifications merge.

**Read-only Django admin**, so the second resolution path that existed in the
predecessor — admin bulk actions writing statuses through `queryset.update()`,
invisible to the audit log — is impossible by construction. `CaseEvent` is
`@access.ops`: its mutations are FORBIDDEN even for a superuser.

**GDPR provider** that erases the complainant and keeps the platform's own
compliance record, plus retention on two clocks (cases 365 days, sanctions
1095) and a purge that never removes a case a live sanction depends on.
