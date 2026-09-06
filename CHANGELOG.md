# Changelog

All notable changes to stapel-moderation are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Pre-1.0 semver: **minor = breaking**, patch = compatible.

## [0.7.0] — 2026-09-06

### A failure is not a verdict

**Breaking: a screening failure no longer produces a `needs_review` verdict,
and no longer lands in the human queue.** It lands in a new case state,
`dlq`, carrying the class of what broke.

The behaviour this replaces was defensible one line at a time. A screening
that could not run applied `ON_SCREENING_FAILURE = "hold"`, which recorded
`policy_default / needs_review / screening_unavailable` and queued the case
for a person: nothing published unscreened, every failure written down,
`W001` printed at every boot. What none of that says is that no machine had
looked at anything — so a machine verdict claiming a machine wanted a person
was generated into the audit trail, into DSA Art. 17 statements of reasons,
and into the moderator queue, where it was indistinguishable from a screener
that had genuinely abstained.

Measured on a client stand on 2026-09-06: **567 `policy_default needs_review`
verdicts, not one of which was a judgement**, and 122 queued cases no
moderator could act on. The queue looked busy. It was an outage.

So, three states where there was one:

* `queued` — a human is needed. The machine abstained, or the policy asks
  for a person. Unchanged.
* `dlq` — **new**. The screening seam failed and kept failing. Out of the
  human queue, carrying `last_error_class` and `dlq_at`, and holding **no
  verdict at all**, because "we could not check" is not a decision about
  content.
* `resolved` — still the only terminal state.

`ON_SCREENING_FAILURE` keeps its three values and `"approve"` / `"reject"`
are untouched: those are real decisions an owner chose in advance, they act
on the target, and a rejection must stay appealable. Only `"hold"` changed,
and it still holds.

The synchronous draft door changed the same way: an unavailable screener
still answers `allowed=False` — unavailability is never permission — but it
no longer persists a case and a verdict for it. A refusal gets a case when
somebody DECIDED to refuse.

### The key that named nothing

The same stand carried 69 cases of origin `draft`, each with
`screen_attempts=9`, and 207 `screen_failed` events reading

    ContentUnavailable("function 'listings.moderation_content' failed
     remotely: LookupError('listing draft:71bde8564c… not found')")

Every component behaved as documented. `screen_draft` minted a synthetic
`draft:<uuid>` target key, which is what makes a draft case a draft case.
`tasks.rescreen_stuck_cases` handed every QUEUED case back to the ladder
without asking what its key meant. `fetch_content` called the owner, the
owner said "no such listing", and `_is_not_found` — whose docstring had
warned about exactly this since 0.4.0 — could not see it, because
`__cause__` does not survive a NATS hop. A 404 read as a 503 and the ladder
retried it, three attempts a time, three times a case.

Three fixes, at the three places:

* **`services.target_is_addressable(case)`** — asked before the call, not
  discovered as a remote error. A case whose key cannot be handed to a
  content function is CLOSED as `dismissed / subject_gone` (new system
  reason), with the verdict event suppressed when the key is synthetic:
  instructing a sibling module about a `draft:<uuid>` it has never owned is
  a payload it can only log forever or redeliver forever.
* **`_is_not_found` reads the flattened transport.** Core rebuilds a remote
  failure as `FunctionCallError("… failed remotely: LookupError('…')")` with
  no cause attached, so the exception NAME is the one structured thing that
  survives. It is matched only in the `failed remotely:` tail, so a provider
  that says "not found" in prose cannot dismiss somebody's case. This stays
  a string match until comm carries structured error codes — that is core's
  work, and the docstring says so.
* **The stuck sweep no longer asks impossible questions**, and now sweeps
  `dlq` as well as `queued`: a dead letter is a case waiting for a REPAIR,
  and retrying it on the existing backoff and cap is how the sweep finds out
  the repair landed.

### Screening, as a number that says WHICH seam

`moderation_screen_total{outcome="unavailable"}` could not tell two
unrelated faults apart, and the stand was running both at once: an
unreachable LLM proxy (199 events) and a content function asked for a key
that names nothing (207). An engineer alarming on that series would have
repaired the wrong one.

* `moderation_screen_failed_total{target_type,error_class}` — the series to
  alarm on. The label says who to wake. Recorded from **both** doors, and
  from `fetch_content`, whose failures reached no screening metric at all.
* `moderation_case_dlq_total{target_type,error_class}` — cases parked, once
  per case rather than once per attempt.
* Both declared at zero by `metrics.declare_series`, because a counter that
  has never been incremented does not fire an alert.

### `stapel_moderation.E009` / `W009` — one place a provider is named

Eight of the stand's failures read *"Anthropic API key not configured — set
`STAPEL_AGENT['ANTHROPIC_API_KEY']`"* while the fleet was configured for an
OpenAI-compatible endpoint. `llm.complete` is called by name over comm and
executes wherever the agent lives, so there are two places a provider can be
named — the agent's `DEFAULT_PROVIDER`, and `STAPEL_MODERATION['LLM_PROVIDER']`
here, which overrides it per call. When they disagree, screening routes to a
provider nobody credentialed and the failure looks exactly like the outage
running next to it.

`W009` fires whenever `LLM_PROVIDER` is set at all — the shipped `""` means
"go where the agent already goes", which is a single source of truth by
construction. `E009` fires on the case this process can PROVE: the agent runs
in the same service and the named provider's key is empty there.

### Added

* `CaseState.DLQ`, `HUMAN_QUEUE_STATES`, and the `dlq_at` /
  `last_error_class` / `last_error` columns (migration `0005`).
* `CaseEventKind.DEAD_LETTERED` / `REVIVED`.
* `services.dead_letter_case`, `services.close_subject_gone`,
  `services.target_is_addressable`, `services.error_class_of`,
  `services.ERROR_CLASSES`.
* Reason codes `subject_gone` and `screening_failed`.
* `GET cases?state=dlq&error_class=…` — the dead-letter tab, as filters on
  the one list endpoint rather than a second route.
* `GET stats` gains `queue_total`, `dlq_total` and `dlq_by_error_class`.
  Separate headline numbers on purpose: the first is work a moderator owes,
  the second is work an engineer owes, and adding them together is how a
  broken seam spent twelve days looking like a busy queue.
* The queue row (`CasePresenter`) gains `dlq_at`, `last_error_class`,
  `last_error` and `escalated_at`, so the DLQ tab groups and sorts without
  opening every card.
* `POST cases/<id>/rescan` revives a dead-lettered case.
* **`manage.py moderation_rescreen`** — `--state dlq|queued`,
  `--error-class`, `--origin`, `--target-type`, `--limit`, `--dry-run`. The
  operator's button for "the seam is repaired, empty the park", and the way
  pre-0.7.0 screening-failure cases are moved.

### Not done, deliberately

Migration `0005` rewrites no existing verdict. An append-only audit trail
that feeds statements of reasons is not something a schema migration gets to
edit, even to correct it. Move those cases with `moderation_rescreen` and
they reach the new states the way every other case reaches them, with the
audit rows to show it.

## [0.6.3] — 2026-09-03

### Screening becomes a number

**This is the module 0.6.2 was repairing.** The instrumentation below was
written into `tasks.py` while another change was being committed from the
same working tree, and `metrics.py` was still untracked — so 0.6.0 and
0.6.1 shipped a `screen_case` importing a module that was not in either
release, and every screening raised `ImportError` into the
`ON_SCREENING_FAILURE` park. 0.6.2 removed the calls to stop the bleeding;
this release lands the calls and the module they need, together, in one
commit. A file that is used by a commit and not in it is not a partial
feature, it is an outage.

This module shipped with no instrumentation at all, and the bill for that
was twelve days long. On a client fleet's stand, 2026-08-21 → 2026-09-02,
**215 of 276 screening tasks failed** — a 78% failure rate — and every
dashboard stayed green throughout.

The module was not wrong. It was, if anything, exactly right: every one of
those cases landed in the human queue as `needs_review /
screening_unavailable`, precisely as `ON_SCREENING_FAILURE = "hold"`
promises, and nothing was published unscreened. That is what made it
invisible. A degradation that degrades gracefully looks like health from
every angle except a `GROUP BY` nobody had a reason to run — which is why
the graceful path is the one that most needs a counter.

- **`moderation_screen_total{target_type,outcome}`** — case screenings by
  outcome, with `unavailable` for "could not run at all". `unavailable`
  rising while `approved`/`rejected` fall is the signature of a provider
  outage; the human queue filling up is the same event, seen an hour later,
  by a person.
- **`moderation_screen_seconds{target_type}`** — a measured screen is
  documented at ~3s against a 45–60s timeout. Nobody could say whether it
  still was.
- **`moderation_draft_screen_total{target_type,outcome}`** — the
  synchronous draft entrance.
- **`moderation_draft_screen_fail_open_total{target_type}`** — recorded by
  the CALLER, via `metrics.record_draft_fail_open()`. The decision to let a
  draft through unscreened belongs to the product (a seller must not be
  blocked because our provider blinked), but the draft path is the one
  place where an outage leaves NOTHING behind — no case, no verdict, no
  queue row, just a log line in a container nobody is tailing. "We could
  not screen" and "and we let it through anyway" are counted separately, so
  a deployment that changes its mind about the second does not lose the
  history of the first.
- **`metrics.declare_series()`**, called from `ready()` with the registered
  target types. A counter that has never been incremented does not exist,
  and `rate(...[15m]) > 0` on a series with no subject does not fire — it
  reports nothing, which is the same shape as the outage it is meant to
  catch.

Recording never raises: every call site is doing something more important
than being observed, and half of them are already on a failure path.

Compatible: additive only. No model, no setting, no signature changes.

## [0.6.2] — 2026-09-03

### Fixed — 0.6.0 and 0.6.1 cannot screen anything

`tasks.screen_case` carried three calls into a `stapel_moderation.metrics`
module that is not in either release, so the first thing every screening did
was raise `ImportError`. Every case went to the `ON_SCREENING_FAILURE` park
instead of to a verdict — which, on the default `"hold"`, means every listing
lands in the human queue and none is ever screened.

The lines were another change in flight in this repo on the same day, in the
same file. They reached a release because the commit staged `tasks.py` whole:
an explicit pathspec keeps another change's *files* out of a commit and does
nothing about another change's *lines* in a file you are also editing. The
local suite passed because the untracked `metrics.py` was sitting in the
working tree; CI, which has only what is committed, went red immediately.

Anyone on 0.6.0 or 0.6.1 should move to 0.6.2. No API change, no migration —
0.6.0's four columns and `beat.py` are unchanged and still apply.

## [0.6.1] — 2026-09-03

> **BROKEN — do not use. Upgrade to 0.6.3.**
> Both wheels ship a `tasks.screen_case` that does `from . import metrics`,
> and `metrics.py` is in neither release: it was still untracked when the
> commit was cut, so the module the code imports was never published. Every
> screening raises `ImportError` before it reaches a provider and lands in
> the `ON_SCREENING_FAILURE` park — on the shipped `"hold"` default that
> means **every listing goes to the human queue and nothing is screened
> automatically**. A deployment pinned here has a screener that cannot
> start, and its dashboard will not say so. 0.6.2 removed the calls; 0.6.3
> restores them together with the module they need.

### Fixed — the beat schedule could not be wired the way W004 asks

`moderation.W004`'s hint says to write
`CELERY_BEAT_SCHEDULE = {**get_moderation_beat_schedule(), ...}` in settings.
A host that did got `AppRegistryNotReady`: `tasks.py` reaches `.services` and
`.screening`, both of which import `.models` at module level
(`@task_handler(SCREEN_TASK)` needs the name at decoration time), and a
settings module runs before `django.setup()`.

The workaround is to merge the schedule from a Celery `on_after_finalize`
signal instead. The jobs then run — and `manage.py check` goes on printing
W004 about jobs that *are* scheduled, because the check reads
`settings.CELERY_BEAT_SCHEDULE` and the settings dict never learned about
them. A warning that fires when the thing is fine is exactly how the real one
came to be ignored: W004 and `stapel_search.W003` printed at every boot of a
live stand for months while 51 cases sat in a queue no job was draining.

`stapel_moderation.beat` now holds the task names and the schedule and
imports settings and nothing else — the property `stapel_search.tasks`
already had, and the reason a host could spell its search entries in settings
but not its moderation ones.

- `stapel_moderation.beat`: `get_moderation_beat_schedule()`, `BEAT_TASK_NAMES`
  and the five `*_TASK_NAME` constants.
- `tasks.py` re-exports all of them, so the previously documented import path
  keeps working.
- W004's hint names `stapel_moderation.beat` and says why it is not `.tasks`.

## [0.6.0] — 2026-09-03

> **BROKEN — do not use. Upgrade to 0.6.3.**
> Both wheels ship a `tasks.screen_case` that does `from . import metrics`,
> and `metrics.py` is in neither release: it was still untracked when the
> commit was cut, so the module the code imports was never published. Every
> screening raises `ImportError` before it reaches a provider and lands in
> the `ON_SCREENING_FAILURE` park — on the shipped `"hold"` default that
> means **every listing goes to the human queue and nothing is screened
> automatically**. A deployment pinned here has a screener that cannot
> start, and its dashboard will not say so. 0.6.2 removed the calls; 0.6.3
> restores them together with the module they need.

### Added — a case that nothing could move now recovers

`tasks.rescreen_stuck_cases`, a fifth beat job, and the thing this module was
missing rather than a tuning of something it had. `sweep_stale_cases` returns
expired CLAIMED leases and stalled SCREENING rows *to* the queue — it is the
job that FILLS the human queue, and nothing drained it. On a deployment whose
queue is not staffed, which is every deployment on day one, `needs_review`
was a terminal state in practice: a live stand held 51 cases parked there,
the oldest two days old, every one with `screen_attempts=3` and no path
onward. The only setting that touched QUEUED, `AUTO_RESOLVE_STALE_QUEUE`,
blanket-approves — the exact legacy sin this module's own docstring names —
and it is still off, still the wrong answer, and now no longer the only one.

The recovery is a re-SCREEN and never a resolution: the case goes back to the
ladder and the ladder decides, exactly as on first submission. Three guards
keep that from being a billing loop — exponential backoff
(`RESCREEN_STUCK_AFTER * 2**attempts`), coalescing (a timestamp, not a
counter), and a cap (`RESCREEN_MAX_ATTEMPTS`, default 3) past which the case
is ESCALATED: marked once, logged once, left for a human. A permanently
failing case has to be *visible*, and a job that retries it forever looks
exactly like a job that is working.

- `Case.last_screened_at`, `Case.resubmitted_at`, `Case.rescreen_attempts`,
  `Case.escalated_at` (migration `0004`, additive and nullable).
- `CaseEventKind.RESCREENED` and `CaseEventKind.ESCALATED`.
- `RESCREEN_STUCK_AFTER` (3600), `RESCREEN_MAX_ATTEMPTS` (3),
  `RESCREEN_SCHEDULE` (`*/15`).
- `RESCREEN_TASK_NAME` joins `BEAT_TASK_NAMES`, so `moderation.W004` names it
  when a host has not scheduled it.

### Fixed — an edit to a queued listing was never looked at

`open_case` dedups on `OPEN_STATES`, so an owner editing a listing whose case
was still QUEUED found the existing case; `handle_intake` re-screened only
from OPEN. The edit therefore changed the content underneath a verdict that
had been reached about *different* content, and the sole trace was a
`RESUBMITTED` audit row.

`handle_intake` now stamps `resubmitted_at` and `rescreen_stuck_cases` acts
on it, skipping the stuck window — an edit is new information, not a retry of
the same question. Deliberately not an inline re-screen: the payload cannot
tell a redelivery from an edit, and stamping a timestamp means five
redeliveries of one event collapse into one screening bounded by the beat
cadence rather than by the bus. A CLAIMED case is never touched; a moderator
holding the lease outranks the clock.

### Fixed — a screening that saw no photo no longer reports as a screening

`moderation.W007` catches the deployment that could never resolve a variant
URL. This is the row-level half, and it was still live on a stand where W007
was satisfied and `MEDIA_BASE_URL` was set: the listing carried media refs,
`cdn.describe` answered `LookupError` for every one of them, `_media_images`
returned `[]`, the `if images:` guard quietly omitted the key, and the model
approved the listing on the strength of its title. Measured, not theorised —
six of twelve sampled live listings resolved, six did not, and all twelve got
a verdict that read as fully screened.

`run_llm` now abstains when a target declares media and **none** of it can be
resolved: `needs_review` / `media_unavailable`, which routes to the human
queue that is already this module's answer to "cannot screen". Dropping one
ref of several is still right and still happens — the text and the other
photos are real. Dropping all of them is a different sentence.

Not a `ScreeningUnavailable`: unresolvable refs will not resolve on the next
attempt either, so the retry ladder would spend three attempts to arrive in
the same place.

- `registry.REASON_MEDIA_UNAVAILABLE`.

## [0.5.1] — 2026-09-03

### Added

- `stapel_moderation.W008`: the comm timeout is shorter than the screen it
  has to wait for. `moderation.screen_draft` is the one Function here a
  caller sits and waits on, and screening a photo against a real provider
  takes seconds; core's default `FUNCTION_TIMEOUT` is 5.0. What makes that
  worth a check rather than a paragraph is the shape of the failure: every
  caller's answer to a timeout is its fail-open branch — a screener that
  cannot answer must never block a seller, which is the right policy — so
  the timeout silently converts "screen this draft" into "do not screen this
  draft" while the endpoint returns 200 and the deploy gate stays green. A
  gate defeated by its own guard rail, visible only here, at boot, where the
  two numbers can be compared. The hint names the one-line fix
  (`STAPEL_COMM["FUNCTION_TIMEOUTS"]`, stapel-core 0.58.0) and the check
  accepts a raised global timeout on older core.
- The `moderation.screen_draft` schema now states the timeout a caller must
  give it, and why. The number was previously discoverable only by reading
  this module's settings from inside another service.

## [0.5.0] — 2026-09-03

### Fixed — image screening had never once looked at an image

`screening._media_images` resolved a media ref through `cdn.describe`, picked
a variant and forwarded `{"url": best["url"]}` verbatim. `cdn.describe`
answers **relative** variant URLs by design — it is host-agnostic and leaves
the origin to whoever renders the page — so what reached `llm.complete` was
`{"url": "/media/cdn/<ref>/1080w.webp"}`: a path with no scheme and no host.
The provider answered `400 invalid_image_url`, `run_llm` correctly raised
`ScreeningUnavailable`, the ladder retried 3/3, and the case parked on a
`policy_default / screening_unavailable` verdict.

On a live stand every case carrying a real photo failed exactly that way,
three attempts out of three. The cases that "succeeded" were the ones whose
media ref was dangling — `cdn.describe` could not resolve it, `_media_images`
returned `[]`, and only the TEXT was screened. Net effect: **the screener had
never once been shown a photo**, and nothing at runtime said so. A queue full
of screened-looking verdicts is exactly what a queue looks like when the
images are silently dropped.

`MEDIA_TRANSPORT` was supposed to be the control for this and was **never
read**: a declared setting with no implementation, which is a switch that
proves nothing. Both of its branches are now implemented.

- **`MEDIA_BASE_URL`** (new, default `""`) — the absolute origin a relative
  variant URL is resolved against. Under `MEDIA_TRANSPORT="url"` a relative
  URL is absolutized against it; with the setting empty that image is
  **SKIPPED with a log line** rather than handed to a provider that cannot
  fetch it. Skipping is the posture the surrounding code already took for an
  unresolvable ref — one photo nobody could resolve is not a reason to
  abandon the text next to it.
- **`MEDIA_TRANSPORT="data_b64"`** now fetches the chosen variant's bytes and
  hands `llm.complete` `{"data_b64": ..., "mime": ...}` and no URL at all.
  This is the transport that works when the provider cannot reach the fleet
  inbound — behind a proxy or on a private network, the ordinary case rather
  than the exotic one. Bounded by two new settings rather than by constants:
  **`MEDIA_FETCH_MAX_BYTES`** (default `5000000`, measured on the bytes
  actually read, because a `Content-Length` is a claim by the other end; over
  it the image is skipped, never truncated) and
  **`MEDIA_FETCH_TIMEOUT_SECONDS`** (default `10` — a hanging CDN must not
  hold a screening Task open to its deadline).
- **`moderation.W007`** fires at boot on the exact misconfiguration above:
  `MEDIA_TRANSPORT="url"`, no `MEDIA_BASE_URL`, and a target type that screens
  media. `moderation.E008` refuses a transport word that does not exist.

### Added — `screen_draft`: a refusal is possible before publication

Until now the only entrance was asynchronous — `moderation.submit` →
`start_screening` → a comm-Task whose verdict lands later. That is right for
content that is already live and wrong for the moment somebody presses
Publish: an obviously non-compliant photo went out and was moderated
afterwards, and its author learned the rules from a takedown letter.

- **`services.screen_draft(*, target_type, content, subject_user_id,
  reports=()) -> DraftScreeningResult`** runs the configured screener inline
  on a `TargetContent` the caller supplies. No persisted target is needed,
  because the draft does not exist yet.
- **A refusal is appealable, which is the hard requirement.** When the verdict
  is not `approved`, a real `Case` (new origin `draft`) is persisted and the
  verdict recorded through the existing `resolve_case`, so `open_appeal`
  accepts it unchanged and the answer carries `case_id` plus the `appeal_url`
  built from `APPEAL_URL_TEMPLATE`. DSA Art. 17 has no "it was only a draft"
  exemption, and an inline refusal nobody can contest is the silent moderation
  this module exists to abolish. The verdict's stored evidence excerpt is the
  record of what was judged — a draft case is the one kind whose content
  cannot be re-read from its owner later.
- **An approval persists NOTHING**, and the asymmetry is the design: every
  cleared draft becoming a queue row is a queue nobody can work, which is the
  same as having no queue, arrived at politely.
- **Unavailability is never permission.** A screener that raised, and a
  deployment with the automatic stage off, both come out through
  `ON_SCREENING_FAILURE` / `ON_SCREENING_UNAVAILABLE`; under the shipped
  `"hold"` the answer is `allowed=False` with `screening_unavailable`, and it
  is the caller's decision what to do about it. Nothing returns
  `allowed=True` because a model failed to answer.
- **`moderation.screen_draft`** wraps it on the bus, with its schema in
  `schemas/functions/`. Its payload carries the content fields directly
  (`title`, `text`, `language`, `media`, `images`, `author_id`) since there is
  no stored target to fetch; the response is `{allowed, decision, reason_code,
  rationale, confidence, source, case_id, appeal_url}`.
- **Photos reach a draft screening by either of two doors**, because a draft
  and a published listing hold different things. `media` is a list of CDN refs
  resolved through `cdn.describe` — what a *published* listing has. `images`
  is a list of `{"data_b64", "mime"}` entries handed to the model as they are,
  with no `cdn.describe`, no `MEDIA_BASE_URL` and no fetch — what a *draft*
  has, because at draft time the composer holds raw bytes and no upload has
  settled into a ref. Waiting for a ref would mean the composer cannot be
  answered; screening the text alone would mean answering "publishable" about
  a photo nobody looked at. Bounded by `MAX_MEDIA_PER_CASE` (count) and the
  new **`MAX_INLINE_IMAGE_BYTES`** (total decoded, default `8000000`), and
  over either bound the call is REFUSED with `services.InvalidDraftImage`
  rather than trimmed — as it is for a mime that is not `image/*` or bytes
  that are not base64. Refusing is the point: this module skips an
  unresolvable CDN ref because the text beside it is still worth judging, but
  bytes the caller HANDED us and is waiting on cannot be dropped into an
  `allowed=True`.
- **`resolve_case(..., emit_verdict_event=False)`** is new and has exactly one
  caller: a draft case's `target_key` names no listing, no review and no row
  anywhere, and the verdict topic is an INSTRUCTION to a target module — an
  announcement about a key nobody owns is a permanent "unknown target" in a
  sibling service at best. Every other caller leaves it alone.
- Migration `0003_case_origin_draft` adds the `draft` choice. Choices only —
  no column change, no data rewrite.

**Minor, not patch**: image screening starts actually working (a deployment
that "screened" photos never did, and now will — including the bill for it),
a new public entry point and a new comm Function appear, and a new case origin
enters the audit vocabulary.

## [0.4.0] — 2026-08-30

### Added — `user.merged`: a sanction follows the person

This module knew one thing about an account's end: erase the complainant.
When a visitor signs in with an authenticator an existing account already
holds, stapel-auth folds the guest into the survivor and emits `user.merged`
— the opposite instruction, and nothing here answered it. Every user-keyed
column kept naming an id that can no longer sign in: the reports, the audit
trail, the appeals, **and the sanctions**. A banned guest could shed the ban
by signing in, and the progressive ladder's memory would reset with it. That
is not a data-loss bug, it is a one-click ban-evasion route, and it has no
symptom at the seam — nothing raises, nothing retries, nothing is logged.

- **`user.merged` is subscribed in `stapel_moderation.actions`** and
  re-parents every column this module keys by a user, in one transaction:
  `Case.subject_user_id` and `claimed_by`, `Report.reporter_id`,
  `Verdict.actor_id`, `CaseEvent.actor_id`, `Sanction.subject_user_id`,
  `issued_by` and `lifted_by`, `Appeal.appellant_id` and `resolved_by`.
- **Each carried active sanction is re-announced** with
  `moderation.sanction.issued` inside the same transaction. The rows moved
  with a bulk `UPDATE`, which announces nothing, and the
  `moderation.user_sanctions` read-model keys on `subject_user_id` — so
  without the announcement a split topology would keep answering `allowed`
  for a user it now holds a ban on. `updated_at` is advanced in the same
  update so the projection's `seq` ordering token moves forward and the
  announcement is not discarded as stale. `UserSanctionState` is never
  rewritten by hand: it is the projection's row, owned by the projection
  runner, and hand-editing a read-model is how the two halves drift.
- **Both uniqueness constraints resolve rather than raise.** A blind
  re-point would be an `IntegrityError`, and on this bus an escaping
  exception is a payload redelivered forever. `uniq_report_per_user` and
  `uniq_appeal_per_case_user` both mean "one person, one row", and after a
  merge the two accounts ARE one person: the guest's duplicate report is
  dropped with `Case.report_count` decremented so the count stays truthful,
  and the guest's duplicate appeal is dropped. Both are logged, loudly.
- **No `MergeTargetNotReady` here, and there cannot be one.** Every actor in
  this module is a bare `UUIDField`, never an FK to `AUTH_USER_MODEL`
  (models.py house rules), precisely so a moderation record survives the
  account it is about — so nothing has to exist locally before the
  survivor's id can be written, and the transfer lands on the first
  delivery. The FK-carrying modules in this fleet need the retry signal;
  this one does not, and saying so is the point.
- A malformed or missing id is logged and ACKed, `ValidationError` included
  (see 0.3.1). `schemas/consumes/user.merged.json` carries the contract and
  `tests/test_user_merged.py` pins every column moving, the sanction
  following the person, the announcement and its ordering token, both
  collisions, a redelivery changing and announcing nothing, every malformed
  shape ACKing, and `stapel_core.lifecycle.E001` returning `[]`.

**Minor, not patch**: a new consumed action is public surface. Requires no
new stapel-core API; the E001 check that names the gap ships in stapel-core
0.52.1.

## [0.3.1] — 2026-08-30

### Fixed — a malformed id in an action payload was a poison pill

`ValidationError` is not a `ValueError`. Django answers a key it cannot coerce
to a column's type — a malformed UUID above all — with
`django.core.exceptions.ValidationError`, which does **not** subclass
`ValueError` or `TypeError`. The `user.deleted` / `user.merged` guards here
caught only `(ValueError, TypeError)`, so a bad id walked straight through
them, the handler raised, `consume_actions` re-raised to the bus, and the
event came back forever: a redelivery loop over a payload no retry can repair,
burning the consumer's retry budget while looking exactly like a downstream
outage.

The consumed contracts do not save anyone from this. They type an id as
`{"type": "string"}` — and where they do say `format: uuid`, `jsonschema`
does not enforce `format` unless a format checker is passed, which the comm
registry does not do. A malformed id is a well-formed payload.

Three handlers addressed rows by an id straight out of a payload with no guard
at all, and each of them raised `ValidationError` on a malformed one:
`handle_user_deleted` (the reporter erasure), `handle_staff_role_revoked` (the
lease release) and `handle_own_verdict` (the case lookup behind the takedown
notification). All three now log the unusable id and ack.

This was not theoretical: it surfaced as a red test in **stapel-chat**, whose
harness installs this module, when a malformed `user.deleted` reached this
module's subscriber rather than chat's.


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
