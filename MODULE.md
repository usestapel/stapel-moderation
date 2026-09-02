# stapel-moderation — MODULE.md

> Agent-facing map of this module: what it provides, where to extend it
> without forking, and what not to do. Kept in the same PR as any change to a
> seam. See also README.md, CONFIG.MD and CHANGELOG.md.

## What this module provides

- **One cross-target moderation queue.** `Case` is keyed by an opaque
  `(target_type, target_key)` pair the module never parses — the
  stapel-reviews shape. A listing, a review, a chat message and an avatar are
  the same kind of work item, served by one console and one REST surface.
- **One status vocabulary.** `Case.state` (`open → screening → queued →
  claimed → resolved`) with a single declared transition table and exactly one
  backward edge (`resolved → queued`, for a successful appeal). `Report` has
  no status; `Verdict` and `CaseEvent` are append-only facts; `Sanction` has
  its own orthogonal lifecycle.
- **Screening as a comm-Task.** `moderation.screen` — deterministic rules
  first, then `llm.complete` with a schema-constrained answer. Retries,
  atomic claim, deadline sweep and the FAILED park all come from the Task
  primitive, not from a hand-rolled beat.
- **Verdicts that ACT.** Resolving a case emits `moderation.completed`, which
  stapel-listings 0.4.0 and stapel-reviews 0.2.0 already consume and apply to
  themselves. This module never calls a host back to mutate it.
- **Sanctions with teeth.** `Sanction` carries kind, scope, reason, clock,
  appeal and audit, and enforcement is core's cross-service user blacklist —
  the hook every request path already checked and nobody had ever called.
- **Notice-and-action artefacts generated from the registries**: a public
  policy disclosure, a statement of reasons on every takedown, an
  acknowledgement to every complainant, an internal appeal heard by a
  different moderator.

## Extension points (fork-free)

### Target types — `STAPEL_MODERATION["TARGET_TYPES"]` (MERGE registry)

The flagship seam. Built-ins are **empty**: the module ships knowing no
targets. Three layers — built-ins → settings → runtime
`register_target_type(name, policy)` — last wins, `None` removes. A policy is
a plain dict, never an ABC.

| Key | Default | What it decides |
|---|---|---|
| `gate` | `GATE_DEFAULT` (`"post"`) | Whether the target is live while under review. |
| `intake_events` | `()` | Topics that open a case for this type. Subscribed at `ready()` **and** on runtime registration. |
| `id_field` | `"target_key"` | The payload key the target's `content_function` expects its id under (`listing_id`, `review_id`). |
| `content_function` | — **required unless `evidence`** | comm Function returning the target's live content. Missing = `E004`. |
| `evidence` | `False` | This target's content is served by NOBODY, so a report carries the reporter's own snapshot and that is what is screened and shown. Mutually exclusive with `content_function` (`E007`). |
| `verdict_event` | `"moderation.completed"` | Topic the verdict travels on. Explicit `None` = "this target consumes no verdict", a statement rather than an omission. |
| `notification_types` | `{}` | `"content_blocked"` → the notification type announcing a takedown. |
| `can_report` | `None` | comm Function; `None` = any authenticated user (fail-OPEN). |
| `can_view_content` | `None` | comm Function; `None` = a cleared moderator sees it (fail-OPEN, argued in `registry.py`). Since 0.3.0 it is called with the **asking moderator's** `actor_id`, so a per-person gate can actually answer. |
| `reasons` | `["*"]` | Which reason codes apply to this type. |
| `screen` / `media` | `True` / `True` | Run the automatic stage; include images in it. |
| `severity_floor` | `0` | Minimum queue severity for this type. |

#### Evidence-based types (0.2.0)

The whole module is built on "never store a copy of the content, ask the
owner at the moment you need it". An **ephemeral** target has no owner to
ask: a chat message, a story, a frame of a live stream. Screening the intake
event's payload is the copy-shaped defect; refusing the complaint is worse.

So a type declares `"evidence": True` and drops `content_function`. Then:

- a report may carry `evidence` — a bounded JSON object
  (`MAX_EVIDENCE_BYTES`, default 8192, **refused over the bound, never
  truncated**) whose `text` / `title` / `language` / `media` / `author_id` /
  `url` fill a `TargetContent` and whose other keys ride along in `extra`;
- the assembled content is stamped `source: "evidence"`, `verified: false`.
  A console renders it as *reported as*, never as the platform's own read;
- a later read (case card, re-screen, appeal) takes the **newest** report's
  attestation — `services.stored_evidence`, the one place this module reads
  content it stored, confined to the types that have nothing to fetch;
- a target with no attestation on file is `TargetNotFound` (404), not
  `ContentUnavailable` (503): nothing is down, there is nothing to look at;
- evidence sent for a type that DOES have a `content_function` is a 400
  (`error.400.moderation_evidence_invalid`) — a snapshot beside a live read
  is a second, staler answer to the same question.

**Stated limitation.** `author_id` in evidence is an *accusation*, not a
fact: nobody in the fleet can confirm who wrote a message no service serves.
It still becomes `Case.subject_user_id`, because otherwise the sanction
ladder has no subject at all — but every render of it says `verified: false`.

What a host CAN narrow is who may file at all. stapel-classified names chat
messages `<conversation_id>:<message_id>` precisely so its `can_report`
callback can answer, off its own conversation↔parties table, that the
reporter was in the thread — and its frontend contract requires the
attested `author_id` to be the server-derived counterparty from that same
table rather than whatever a client's message cache holds. Neither makes the
attestation a fact; both make it accountable.

### Reasons — `STAPEL_MODERATION["REASONS"]` (MERGE registry)

Same three layers. Unlike the target types the built-ins are **non-empty**: a
complaint taxonomy is universal, a target type is not. Each entry carries
`severity`, `requires_description`, `applies_to`, optional `policy_clause`,
and derived translation keys. System reasons (`screening_unavailable`,
`screening_held`, `low_confidence`) cannot be removed and are never offered to
reporters.

### Rules — `STAPEL_MODERATION["RULES"]` (MERGE registry)

Ships **empty** on purpose: a shipped keyword list is somebody else's speech
policy. `{code: {pattern, decision, severity, reason_code, applies_to}}`. A
hit is a verdict without paying for a completion, and the policy disclosure
enumerates every rule that runs.

### Screener — `STAPEL_MODERATION["SCREENER"]` (REPLACE, dotted path)

`(case, content, *, reports) -> ScreeningResult`. A host ML provider plugs in
here rather than forking. Default:
`stapel_moderation.screening.default_screener`.

### Presenters (REPLACE, `STAPEL_SWAP`)

`MODERATION_CASE_PRESENTER`, `MODERATION_CASE_DETAIL_PRESENTER`,
`MODERATION_REPORT_PRESENTER`, `MODERATION_VERDICT_PRESENTER`,
`MODERATION_EVENT_PRESENTER`, `MODERATION_SANCTION_PRESENTER`,
`MODERATION_APPEAL_PRESENTER`.

The case-detail presenter owns the card's `content` field (0.3.0). The read
itself stays in the view — it needs the actor and it can fail — but its
result is handed to `present_case_detail(case, content=...)` and declared on
the DTO, so `docs/schema.json` carries `ContentDTO` and a generated client
can type the one field the console is built around. A replacement presenter
must keep the field; dropping it blanks the card.

### Serializer seams (`views.py`)

`SerializerSeamMixin` — subclass a view, set `request_serializer_class` /
`response_serializer_class`, remount the URL. Since 0.2.0 it is **core's**
(`stapel_core.django.api.views`, hoisted in core 0.37.0), not a local copy;
the attribute and getter names are unchanged, so an existing subclass keeps
working.

### `NotSanctioned` (a permission class for the HOST)

```python
from stapel_moderation import NotSanctioned

class ListingCreateView(APIView):
    permission_classes = [IsNotAnonymousUser, NotSanctioned("listing")]
```

Moderation answers whether a user is sanctioned; the host's own view refuses.
Gating publication inside stapel-listings would be a decision about the
listings API, and that belongs to its owner.

## Rights

Staff roles from stapel-auth plus the `stapel_core.access` mandate. **No
parallel allow-list**, ever — the predecessor's `ReportModerator` email list
living beside a real role system is the defect class this replaces.

```python
STAPEL_ACCESS = {"ROLES": {
    "moderator": {"clearance": "low", "apps": {"moderation": "mid"}},
    "ts_lead":   {"clearance": "mid", "apps": {"moderation": "high"}},
}}
```

| Action | Needs | Because |
|---|---|---|
| read the queue / a case card | `Case` view = MID | the card carries complaint text and the complainant's identity |
| claim / release / verdict / rescan | `Case` change = HIGH | mutating a sensitive model |
| issue / lift a sanction | `Sanction` add/change = HIGH | the same declaration `StaffRoleAssignment` carries |
| read the audit trail | `CaseEvent` view = HIGH (`ops`) | and every mutation is FORBIDDEN |
| report / appeal | `IsNotAnonymousUser` | a member surface, not a staff one |

Step-up (`MAX_AGE=900`, `LEVELS=("high",)`) applies to the HIGH actions
automatically — issuing a ban demands fresh authentication without a line of
code here.

## Comm surface

**Emits** (`schemas/emits/`, ids-only): `moderation.case.opened`,
`moderation.case.queued`, `moderation.completed`,
`moderation.report.received`, `moderation.report.reviewed`,
`moderation.sanction.{issued,lifted,expired}`,
`moderation.appeal.{opened,resolved}`.

**Consumes** (`schemas/consumes/`): each registered type's `intake_events`,
`task.failed` (filtered to `moderation.screen`), `moderation.applied`
(optional ack — nobody emits one today), `user.deleted`, `user.merged`,
`staff.role.revoked`, plus this module's own facts driving the notification
subscribers.

`user.deleted` and `user.merged` are the two halves of one account life cycle
and say opposite things. Erasure detaches the complainant from their complaint;
a merge re-parents every user-keyed column onto the account that absorbed the
guest — `Case.subject_user_id`/`claimed_by`, `Report.reporter_id`,
`Verdict.actor_id`, `CaseEvent.actor_id`,
`Sanction.subject_user_id`/`issued_by`/`lifted_by`,
`Appeal.appellant_id`/`resolved_by`. **A sanction follows the person**, or a
merge would be a one-click ban-evasion route; each carried active sanction is
re-announced with `moderation.sanction.issued` (and its `updated_at` advanced,
so the projection's `seq` token moves) because the `moderation.user_sanctions`
read-model keys on `subject_user_id` and a bulk `UPDATE` announces nothing.
`UserSanctionState` itself is never rewritten by hand — it is the projection's
row, owned by the projection runner. The two uniqueness constraints resolve to
"one person, one row": a duplicate report is dropped (with `Case.report_count`
decremented) and so is a duplicate appeal. No `MergeTargetNotReady` exists here
and none can: every actor is a bare `UUIDField`, never an FK, so nothing has to
exist locally before the survivor's id can be written. Idempotent.

**Provides** (`schemas/functions/`): `moderation.submit`,
`moderation.screen_draft`, `moderation.check_sanctions`,
`moderation.sanctions_by_users`, `moderation.sanctions_export`,
`moderation.case_status`, `moderation.policy_disclosure`.

`moderation.screen_draft` is the one Function whose payload carries CONTENT
rather than an id, and it has to: a draft is not a target, nothing stores it,
and there is nothing for a `content_function` to answer about. It is the
synchronous entrance (0.5.0) — a refusal before publication, persisted as a
`Case` of origin `draft` so it is appealable; an approval persists nothing. Its
photos arrive by either door: `media` (CDN refs, resolved through
`cdn.describe`) or `images` (`{data_b64, mime}` bytes handed straight to the
model, for a draft whose upload has not settled into a ref yet). The inline
door refuses rather than trims — over `MAX_MEDIA_PER_CASE` or
`MAX_INLINE_IMAGE_BYTES`, on a non-`image/*` mime, or on bytes that are not
base64, the call raises `InvalidDraftImage` instead of quietly screening some
of the photos and answering about all of them.

**Calls**: each policy's `content_function`, `llm.complete`, `cdn.describe`,
`request_notification`, `blacklist_user`/`unblacklist_user`. There is not one
`import stapel_listings` in the package, and a test asserts it.

## Operational warnings

- **Schedule the beat jobs.** Without `rearm_active_sanctions`, every
  suspension silently stops being enforced once the blacklist cache key
  expires (`BLACKLIST_TTL_SECONDS`, 2h) while the `Sanction` row still reads
  `active`. `W004` says so at `manage.py check`.
- **Core's blacklist fails CLOSED.** When the cache backing it is
  unreachable, `stapel-core` rejects every authenticated request, not only
  sanctioned ones. That is a property of core's blacklist, and a Redis outage
  is therefore a full login outage. It belongs in the runbook.
- **`TASK_DISPATCH` must not be `"inline"` in production.** Inline runs
  screening inside the web request. A composite wants `"action"` or `"bus"`,
  and the stand should prove `task.requested` actually reaches a worker
  outside the web process before a queue is built on it.
- **Media screening is billed per case.** Image prompts disable the
  provider's prompt cache, so cost is linear in submissions. `policy["media"]`
  and `MAX_MEDIA_PER_CASE` are the controls; screening media only on
  complaint (`origin=report`) is expressible as policy.
- **A relative CDN URL is not an image a provider can fetch.** `cdn.describe`
  answers host-agnostic paths, so `MEDIA_BASE_URL` must name the origin they
  hang off. Under `MEDIA_TRANSPORT="url"` the vendor fetches that address and
  it must be reachable from outside the fleet; under `"data_b64"` this process
  fetches the bytes itself (bounded by `MEDIA_FETCH_MAX_BYTES` and
  `MEDIA_FETCH_TIMEOUT_SECONDS`) and the origin may be internal — which is the
  configuration for an auth-gated or non-public CDN. With no origin set, a
  relative image is skipped and logged, and `W007` names it at boot: a
  deployment that drops every photo from every screening is indistinguishable
  at runtime from one that screens them, which is how one stand ran its whole
  life without a screener ever seeing a photo.

## Known limitations (stated, not hidden)

- **A remote comm transport flattens "target gone" into "owner down".**
  `FunctionCallError.__cause__` only survives the in-process transport, so
  over NATS/HTTP a missing target reads as an outage and the screening task
  retries instead of dismissing. That is the safe direction of the error, and
  closing it properly means structured error codes on comm — core's work.
- **The two released upstreams disagree on the not-found exception.**
  `listings.moderation_content` raises `LookupError`;
  `reviews.moderation_content` raises a bare `ReviewNotFound`. Both are
  recognised (`services._is_not_found`); the honest fix is
  `ReviewNotFound(LookupError)` in a reviews minor.
- **A redelivered intake event is indistinguishable from a resubmission.** No
  intake topic in the fleet carries a revision token, so re-screening on every
  delivery would turn an at-least-once bus into an unbounded LLM bill. Only a
  case still in `open` is screened on redelivery; `rescan` and
  `moderation.submit` are the explicit "look again" paths.
- **A sanction's subject is not erased by the GDPR provider.** Dropping the
  subject id would both unban the person and destroy the progressive ladder's
  memory. A host with a legal basis lifts the sanction first and lets
  retention take it.
- **No cdn-side media producer.** Media is moderated as part of its owner's
  case; a `media` target type for orphan files is reserved, not built.
- **The workspace capability door is declared and closed.** `CAPABILITIES` is
  published on day one so role overlays never migrate, but `authorize` does
  not consult it until `WORKSPACE_SCOPED` is `True` — and that branch is not
  built, so flipping the switch denies loudly rather than granting silently.

## What not to do

- Do not write `Case`, `Verdict`, `CaseEvent` or `Sanction` rows directly —
  every mutation goes through `services.py`, which is where the transaction,
  the audit row and the fact are one decision.
- Do not emit `moderation.*` from host code. The service layer emits inside
  its own mutating transactions; a host emit double-publishes a public
  verdict.
- Do not call a target module to apply a verdict. Emit the fact; the target
  owns its consumer.
- Do not add a second status vocabulary. There is one, and its absence of
  duplicates is the module's founding decision.
- Do not make the Django admin writable. `CaseEvent` is `@access.ops` and the
  admin classes are read-only, together making a second resolution path
  impossible rather than discouraged.
