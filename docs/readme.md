## What this is

One **moderation queue** for everything a product publishes. A listing, a
review, a chat message and an avatar are the same kind of work item here, and
one moderator console works all of them.

Three decisions carry the whole design.

**The unit of work is a Case, not a complaint.** Forty people reporting one
listing produce forty `Report` rows hanging off **one** `Case` with
`report_count = 40`. The system this replaced kept forty queue rows, which is
why its moderators saw the same listing forty times and why its queue page
read two whole tables into memory before it could show anybody anything.

**There is one status vocabulary in the module: `Case.state`.** A `Report` has
no status of its own and inherits its case's; a `Verdict` has none because it
is an append-only fact; a `Sanction` has an orthogonal lifecycle that never
mixes with a case state. The predecessor had three near-identical unrelated
status enums plus two more free copies in a serializer and an HTML template,
and they could and did disagree.

**Moderation never calls a host back to mutate it.** Resolving a case emits
`moderation.completed`, and the target module applies the verdict to itself —
[stapel-listings](https://github.com/usestapel/stapel-listings) 0.4.0 and
[stapel-reviews](https://github.com/usestapel/stapel-reviews) 0.2.0 already
consume it. The action IS the fact, so a new kind of moderated thing is a
registry entry plus a consumer in its own repository, never a branch in here.

## Quick start

```bash
pip install stapel-moderation
```

```python
INSTALLED_APPS = [
    # ...
    "stapel_moderation",
]

# urls.py
path("moderation/", include("stapel_moderation.urls"))   # -> /moderation/api/v1/...

# Declare what may be moderated. The module ships knowing NO target types.
STAPEL_MODERATION = {
    "TARGET_TYPES": {
        "listing": {
            "intake_events": ["listing.submitted"],
            "id_field": "listing_id",
            "content_function": "listings.moderation_content",
            "notification_types": {"content_blocked": "listing_blocked"},
        },
        "review": {
            "id_field": "review_id",
            "content_function": "reviews.moderation_content",
        },
    },
}

# Moderator rights are staff roles + the core mandate. No allow-list.
STAPEL_ACCESS = {"ROLES": {
    "moderator": {"clearance": "low", "apps": {"moderation": "mid"}},   # read the queue
    "ts_lead":   {"clearance": "mid", "apps": {"moderation": "high"}},  # decide and sanction
}}

# The scheduled half. Without it, long suspensions stop being enforced.
from stapel_moderation.tasks import get_moderation_beat_schedule
CELERY_BEAT_SCHEDULE = {**get_moderation_beat_schedule()}
```

## How a case moves

```
listing.submitted ──▶ open_case ──▶ comm-Task "moderation.screen"
                                          │
                     ┌────────────────────┼────────────────────┐
                     ▼                    ▼                    ▼
                 rules hit            llm.complete         unavailable
              (no LLM billed)      (schema-constrained)   (retry ×3, then
                     │                    │                ON_SCREENING_FAILURE)
                     └────────┬───────────┘                    │
                              ▼                                ▼
                    approved / rejected              needs_review ──▶ human queue
                              │                                          │
                              ▼                                          ▼
                    emit moderation.completed  ◀──────────────  moderator verdict
                              │                                  (+ optional sanction)
                              ▼
                 the target module blocks itself
```

## The switches that ship closed

Every setting that trades safety for availability is off by default, and the
three that matter print a startup warning when a host turns them on — because
each one is *invisible at runtime*, and the predecessor system had two of them
silently enabled for years.

| setting | default | what opening it costs |
|---|---|---|
| `ON_SCREENING_FAILURE` | `"hold"` | `"approve"` publishes content nobody screened; `"reject"` removes content nobody screened. Either prints `W001`. |
| `AUTO_RESOLVE_STALE_QUEUE` | `None` | A number makes unreviewed cases approve themselves on a clock. Prints `W002`. |
| `ALLOW_ANONYMOUS_REPORTS` | `False` | Requires a contact address and a captcha; without one, a complaint flood is a denial-of-service against the queue. Prints `W003`. |
| `APPEAL_REQUIRES_DIFFERENT_ACTOR` | `True` | Off, the moderator who decided also hears the appeal. |

## What a ban actually does

`Sanction` is a row with a kind, a scope, a reason, a clock, an appeal and an
audit trail — not a boolean. Its teeth are `stapel-core`'s cross-service user
blacklist, which DRF authentication, the middleware (twice), channels and the
auth refresh endpoint all already check on every request, and which **had no
producer anywhere in the fleet** until this module. Deactivating the account
instead would touch no live session at all: `is_active` is only consulted when
a new token is issued.

Two operational consequences, stated rather than discovered:

- the blacklist is a cache key with a TTL, so `rearm_active_sanctions` must be
  scheduled — otherwise a thirty-day suspension quietly stops being enforced
  after two hours while the row still reads `active`. `W004` says so;
- core fails **closed** when that cache is unreachable, so a Redis outage
  locks everybody out, not just the sanctioned. That is a property of core's
  blacklist, and it belongs in the runbook.

## Notice-and-action artefacts

The compliance surface is generated from the registries, not maintained as
prose beside them:

- `GET /moderation/api/v1/policy` — public, and assembled from the reason
  registry, the rule registry and the actual screening settings, so it cannot
  describe a system other than the one running;
- every takedown carries a statement of reasons and an appeal link;
- every complainant gets an acknowledgement and, later, the outcome;
- an appeal reopens and re-decides its case rather than filing a letter — the
  one backward edge in the state machine exists for exactly that.

## The Django admin is read-only, on purpose

Moderators *are* Django staff here, so the usual "different audience" argument
does not apply. The reason is path integrity: in the predecessor, admin bulk
actions flipped report statuses through `queryset.update()` — no audit row, no
timestamp, and the reviewed content was never actually hidden. A second
resolution path existed, invisible to the audit log. Read-only registration
makes that path impossible by construction, and `CaseEvent` is declared
`@access.ops`, whose mutations are `FORBIDDEN` even for a superuser.
