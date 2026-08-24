# CONFIG.MD — stapel-moderation

Config registry for **stapel-moderation** (`static-scaffold-and-config.md`
§2). One row per configuration key the module reads, its **source** (`env` =
the process environment / the `STAPEL_MODERATION` settings dict), what it is
for, whether it is required, and its default.

All keys are read through `moderation_settings`
(`stapel_moderation.conf.AppSettings`, namespace `STAPEL_MODERATION`).
Resolution order per key: `settings.STAPEL_MODERATION` dict → a flat Django
setting of the same name → environment variable → the default below.

Two notes on the `env` column, which is the manifest's vocabulary for "the
process environment or the `STAPEL_MODERATION` settings dict" rather than a
claim that every key is settable from a shell:

- the **structured** keys (`TARGET_TYPES`, `REASONS`, `RULES`,
  `SANCTION_LADDER`, `BLACKLIST_KINDS`, and the three `*_SCHEDULE` dicts)
  carry shapes an environment string cannot express, so in practice they are
  set in the settings dict and `AppSettings` never reads them from the
  environment;
- `SCREENER` is the module's only `import_strings` key. It names a callable
  the process imports and runs, so it is **env-closed by policy** — a stray
  export in an entrypoint must not get to choose which classifier decides
  what gets published.

## stapel-moderation

### Registries (source: env)

| Key | Source | Purpose | Required | Default |
|-----|--------|---------|----------|---------|
| TARGET_TYPES | env | The flagship merge-registry: `{target_type: policy dict or None}`. Built-ins are EMPTY — the module ships knowing no targets, so a host declares what may be moderated. `None` removes a type. Checks `E001`–`E006` police it; `E004` refuses a policy with no `content_function`. | no | `{}` |
| REASONS | env | Complaint taxonomy merged OVER a non-empty built-in set (spam, offensive, harassment, counterfeit, fraud, illegal, adult, personal_data, off_platform_payment, wrong_category, other). `None` removes. System reasons cannot be removed. | no | `{}` |
| RULES | env | Deterministic pre-LLM screening rules, `{code: {pattern, decision, severity, reason_code, applies_to}}`. Ships EMPTY: a shipped keyword list is somebody else's speech policy. | no | `{}` |

### Target policy defaults (source: env)

| Key | Source | Purpose | Required | Default |
|-----|--------|---------|----------|---------|
| GATE_DEFAULT | env | `pre` (target waits for a verdict) or `post` (target is live and can be taken down), for types that do not say. | no | `post` |

### Screening (source: env)

| Key | Source | Purpose | Required | Default |
|-----|--------|---------|----------|---------|
| SCREENER | env | Dotted path to `(case, content, *, reports) -> ScreeningResult`. The seam a host ML provider plugs into instead of forking. Env-closed by `import_strings` policy. | no | `stapel_moderation.screening.default_screener` |
| SCREEN_ENABLED | env | Master switch for the automatic stage. Off = every case goes straight to a person — the honest configuration with no model available. | no | `True` |
| SCREEN_MAX_ATTEMPTS | env | Retries of the `moderation.screen` comm-Task before it parks FAILED. | no | `3` |
| SCREEN_DEADLINE_SECONDS | env | Task deadline; `sweep_tasks` drops overdue ones. | no | `900` |
| SCREEN_TIMEOUT_SECONDS | env | Timeout of the `llm.complete` call. Deliberately not core's 5s `FUNCTION_TIMEOUT` — a completion never finishes in five seconds. | no | `60` |
| CONTENT_TIMEOUT_SECONDS | env | Timeout of a target's `content_function`. | no | `10` |
| LLM_MODEL | env | Size passed to `llm.complete`: `small`/`medium`/`large`. | no | `medium` |
| LLM_PROVIDER | env | Provider name; empty lets `llm.complete` pick its configured default. | no | `""` |
| LLM_CONFIDENCE_FLOOR | env | Below it the decision is forced to `needs_review` whatever the model said. | no | `0.7` |
| ON_SCREENING_FAILURE | env | `hold` \| `approve` \| `reject` when the screener could not answer. **Closed default `hold`**: the human queue IS the fallback. Anything else prints `moderation.W001`. | no | `hold` |
| ON_SCREENING_UNAVAILABLE | env | Same vocabulary, for "no screener configured at all". | no | `hold` |
| AUTO_RESOLVE_STALE_QUEUE | env | Seconds after which an unreviewed QUEUED case auto-approves. `None` = never, and never is the point. A number prints `moderation.W002`. | no | `None` |

### Queue (source: env)

| Key | Source | Purpose | Required | Default |
|-----|--------|---------|----------|---------|
| CLAIM_LEASE_SECONDS | env | How long a moderator's claim holds before `sweep_stale_cases` returns the case to the queue. | no | `900` |
| MAX_PAGE_SIZE | env | Server ceiling on any keyset page. | no | `100` |
| APPLY_ACK_TIMEOUT_SECONDS | env | Seconds to wait for a target module's optional `moderation.applied` ack. `None` = do not wait; no released consumer emits one. | no | `None` |

### Evidence and the wire (source: env)

| Key | Source | Purpose | Required | Default |
|-----|--------|---------|----------|---------|
| EVIDENCE_EXCERPT_CHARS | env | Length of the content excerpt STORED with a verdict (never emitted), so a statement of reasons stays checkable after the content is gone. | no | `512` |
| VERDICT_NOTE_WIRE_CHARS | env | Truncation of the statement of reasons that DOES ride `moderation.completed`. | no | `200` |

### Media screening (source: env)

| Key | Source | Purpose | Required | Default |
|-----|--------|---------|----------|---------|
| MEDIA_SCREEN_TIER | env | Largest CDN variant width handed to the model. | no | `1024` |
| MAX_MEDIA_PER_CASE | env | Image prompts disable the provider's prompt cache, so every media screen is paid in full — this is the price control. | no | `4` |
| MEDIA_TRANSPORT | env | `url` (the model vendor fetches it — needs publicly reachable CDN URLs) or `data_b64` (inlined, bounded by the broker's max payload). | no | `url` |

### Complaint intake (source: env)

| Key | Source | Purpose | Required | Default |
|-----|--------|---------|----------|---------|
| ALLOW_ANONYMOUS_REPORTS | env | **Closed.** Opening it makes `contact_email` mandatory and demands a captcha; without one, prints `moderation.W003`. | no | `False` |
| REPORT_THROTTLE | env | DRF scoped rate for the report endpoint. A library cannot own `DEFAULT_THROTTLE_RATES`, so the rate lives here. `None` disables — a conscious act. | no | `20/h` |
| MAX_EVIDENCE_BYTES | env | Ceiling on one report's reporter-supplied `evidence` blob (compact JSON, UTF-8 bytes). Only target types that declared `evidence: True` accept one at all; over the bound the report is **refused**, never truncated. | no | `8192` |

### Appeals (source: env)

| Key | Source | Purpose | Required | Default |
|-----|--------|---------|----------|---------|
| APPEAL_REQUIRES_DIFFERENT_ACTOR | env | The moderator who decided may not decide the appeal. A one-moderator team turns it off knowingly. | no | `True` |
| APPEAL_URL_TEMPLATE | env | Template with `{case_id}` for the `appeal_url` notification variable. Empty yields `""` rather than an invented address. | no | `""` |

### Sanctions (source: env)

| Key | Source | Purpose | Required | Default |
|-----|--------|---------|----------|---------|
| SANCTION_LADDER | env | `{kind: [seconds, ...]}`; the n-th sanction of a kind takes the n-th rung, the last rung repeats, `None` = permanent. | no | `{"posting_restricted": [86400, 604800, 2592000], "suspended": [604800, 2592000, None]}` |
| BLACKLIST_KINDS | env | Which sanction kinds kill live sessions through core's cross-service user blacklist. | no | `["suspended", "banned"]` |
| BLACKLIST_TTL_SECONDS | env | TTL of that cache key. The row stays the truth; `rearm_active_sanctions` keeps the key alive. | no | `7200` |

### Scheduled work (source: env)

| Key | Source | Purpose | Required | Default |
|-----|--------|---------|----------|---------|
| SWEEP_SCHEDULE | env | crontab kwargs for `sweep_stale_cases`. | no | `{"minute": "*/5"}` |
| REARM_SCHEDULE | env | crontab kwargs for `rearm_active_sanctions` and `expire_sanctions`. Must be well inside `BLACKLIST_TTL_SECONDS`. | no | `{"minute": "*/30"}` |
| PURGE_SCHEDULE | env | crontab kwargs for `purge_expired_cases`. | no | `{"hour": 4, "minute": 20}` |
| RETENTION_DAYS | env | How long a resolved case is kept — one annual reporting cycle. | no | `365` |
| SANCTION_RETENTION_DAYS | env | Longer than the case, because the progressive ladder IS memory. | no | `1095` |
| EXPORT_PAGE_SIZE | env | Rows per page of the projection rebuild snapshot. | no | `500` |

### Notifications (source: env)

| Key | Source | Purpose | Required | Default |
|-----|--------|---------|----------|---------|
| NOTIFY_COOLDOWN_SECONDS | env | One letter per (type, recipient) per window. Forty reports about one listing must not become forty letters to its author. | no | `600` |

### Tenancy (source: env)

| Key | Source | Purpose | Required | Default |
|-----|--------|---------|----------|---------|
| WORKSPACE_SCOPED | env | Declared and closed. The capability names ship on day one so role overlays never migrate; the branch is not built, so turning this on denies loudly rather than granting silently. | no | `False` |
