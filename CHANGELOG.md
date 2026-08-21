# Changelog

All notable changes to stapel-moderation are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Pre-1.0 semver: **minor = breaking**, patch = compatible.

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
