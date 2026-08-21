"""Settings namespace for stapel-moderation.

All configuration is read through ``moderation_settings`` (lazily, at call
time) — never via module-level ``os.getenv`` (values would freeze at import).
Resolution order per key: ``settings.STAPEL_MODERATION`` dict -> flat Django
setting of the same name -> environment variable -> the default below.

Three merge-registries are the module's extension seams (``registry.py``):
``TARGET_TYPES`` (what may be moderated and how), ``REASONS`` (the complaint
taxonomy) and ``RULES`` (the deterministic pre-LLM filter). Only the first
ships empty — the module is target-generic and knows no targets, while
reasons and rules are universal enough to ship built-ins.

**Every switch that trades safety for availability ships CLOSED** (spec §5.5,
§6.1, §20). ``ON_SCREENING_FAILURE`` holds rather than approves,
``AUTO_RESOLVE_STALE_QUEUE`` is ``None`` (a queued case is never resolved by
a clock), anonymous reports are off, and appeals require a different actor.
Opening any of them is an explicit host decision, and the three that matter
print a system check saying so.
"""
from stapel_core.conf import AppSettings

#: AppSettings-shaped literal dict (capability-config.md §2): a top-level
#: DEFAULTS lets the capabilities.json emitter introspect axis keys/kinds
#: without re-parsing the AppSettings() call.
DEFAULTS = {
    # ── Registries (registry.py) ─────────────────────────────────────
    # {type_name: policy dict | None}. Merged OVER the (empty) built-ins;
    # None removes a type. See registry.resolve_policy for the key set.
    "TARGET_TYPES": {},
    # {reason_code: {severity, requires_description, applies_to, ...} | None}
    # merged over BUILTIN_REASONS. Unlike TARGET_TYPES the built-ins are
    # non-empty: a complaint taxonomy is universal, a target type is not.
    "REASONS": {},
    # {rule_code: {pattern, decision, severity, applies_to, ...} | None}
    # merged over the (empty) built-ins. The deterministic first stage: a
    # hit is a verdict without paying for an LLM call.
    "RULES": {},

    # ── Target policy defaults ───────────────────────────────────────
    # "post": the target is live and moderation can take it down;
    # "pre": the target waits for a verdict. A policy may override.
    "GATE_DEFAULT": "post",

    # ── Screening (spec §5.4-5.6) ────────────────────────────────────
    # The replaceable screener. A host ML provider plugs in HERE, not by
    # forking the module.
    "SCREENER": "stapel_moderation.screening.default_screener",
    # Master switch for the automatic stage. Off = every case goes
    # straight to the human queue (the honest deployment without an LLM).
    "SCREEN_ENABLED": True,
    # Retry ladder is the comm-Task primitive's, not a hand-rolled beat.
    "SCREEN_MAX_ATTEMPTS": 3,
    "SCREEN_DEADLINE_SECONDS": 900,
    # NOT core's 5s FUNCTION_TIMEOUT — a completion never finishes in five
    # seconds (the stapel-recordings QA_TIMEOUT_SECONDS precedent).
    "SCREEN_TIMEOUT_SECONDS": 60,
    "CONTENT_TIMEOUT_SECONDS": 10,
    "LLM_MODEL": "medium",
    # "" lets llm.complete pick its configured default provider.
    "LLM_PROVIDER": "",
    # Below the floor the decision is forced to needs_review whatever the
    # model said: a confident-sounding guess is still a guess.
    "LLM_CONFIDENCE_FLOOR": 0.7,
    # hold | approve | reject. CLOSED default: the human queue IS the
    # fallback. "approve" prints moderation.W001 — availability over
    # safety is a confession, not a default (legacy published unmoderated
    # listings ~30 minutes after the LLM failed).
    "ON_SCREENING_FAILURE": "hold",
    # Same vocabulary, for "no screener is configured at all".
    "ON_SCREENING_UNAVAILABLE": "hold",
    # Seconds after which a QUEUED case would be auto-resolved. None means
    # never, and never is the point: legacy's stale-sweeper auto-approved
    # everything a human had not reached, needs_review included, so human
    # review existed only on paper. Setting a number prints moderation.W002.
    "AUTO_RESOLVE_STALE_QUEUE": None,

    # ── Queue (spec §7) ──────────────────────────────────────────────
    # A claim is a lease, not a lock: it expires and the case returns to
    # the queue rather than being held by a moderator who went home.
    "CLAIM_LEASE_SECONDS": 900,
    # Keyset paging cap (no pagination framework — the docs/forms canon).
    "MAX_PAGE_SIZE": 100,
    # Seconds to wait for a target module's optional moderation.applied
    # ack before flagging the case. None = do not wait; neither released
    # consumer (listings 0.4, reviews 0.2) emits one, so None is the only
    # honest default: the mechanism exists, producers do not.
    "APPLY_ACK_TIMEOUT_SECONDS": None,

    # ── Evidence and the wire (spec §5.2) ────────────────────────────
    # Stored, never emitted: DSA Art. 17 wants a statement of reasons that
    # stays checkable after the content is gone.
    "EVIDENCE_EXCERPT_CHARS": 512,
    # The note that DOES ride the bus, truncated — Listing.apply_moderation
    # already expects one.
    "VERDICT_NOTE_WIRE_CHARS": 200,

    # ── Media screening (spec §10) ───────────────────────────────────
    # Largest CDN variant width to hand the model.
    "MEDIA_SCREEN_TIER": 1024,
    # Image prompts disable the provider's prompt cache, so every media
    # screen is paid for in full. The cap is the price control.
    "MAX_MEDIA_PER_CASE": 4,
    # "url" (the vendor fetches it — requires publicly reachable CDN URLs)
    # or "data_b64" (we inline it, bounded by the broker's max_payload).
    "MEDIA_TRANSPORT": "url",

    # ── Complaint intake (spec §6) ───────────────────────────────────
    # CLOSED by default (§20.5). Opening it turns contact_email into a
    # required field and captcha into a requirement, and prints W003.
    "ALLOW_ANONYMOUS_REPORTS": False,
    # DRF resolves scoped rates from the global DEFAULT_THROTTLE_RATES
    # setting, which a library cannot own — the rate is read from this
    # namespace instead (the workspaces/geo/forms canon). None disables
    # the throttle: a conscious act, never the default.
    "REPORT_THROTTLE": "20/h",

    # ── Appeals (spec §12, DSA Art. 20) ──────────────────────────────
    # The moderator who decided may not decide the appeal. A one-moderator
    # deployment turns this off knowingly.
    "APPEAL_REQUIRES_DIFFERENT_ACTOR": True,
    # Template with {case_id}; "" yields "" rather than a made-up address
    # (the listings LISTING_URL_TEMPLATE precedent). The notification
    # variable appeal_url is built from it.
    "APPEAL_URL_TEMPLATE": "",

    # ── Sanctions (spec §9) ──────────────────────────────────────────
    # Progressive ladder: the n-th sanction of a kind takes the n-th
    # entry (the last entry repeats). None = permanent. Shaped after
    # stapel-auth's LockoutService.THRESHOLDS — configured, not hardwired.
    "SANCTION_LADDER": {
        "posting_restricted": [86400, 604800, 2592000],
        "suspended": [604800, 2592000, None],
    },
    # Sanction kinds that kill the subject's live sessions through core's
    # cross-service user blacklist.
    "BLACKLIST_KINDS": ["suspended", "banned"],
    # Core's blacklist is a cache key with a TTL, so a long sanction is
    # kept alive by rearm_active_sanctions rather than by a longer TTL.
    "BLACKLIST_TTL_SECONDS": 7200,

    # ── Scheduled work (tasks.py) ────────────────────────────────────
    "REARM_SCHEDULE": {"minute": "*/30"},
    "SWEEP_SCHEDULE": {"minute": "*/5"},
    "PURGE_SCHEDULE": {"hour": 4, "minute": 20},
    # Resolved cases live a full annual reporting cycle (§20.3).
    "RETENTION_DAYS": 365,
    # Sanctions outlive their case: the progressive ladder is memory, and
    # a ladder that forgets is a first offence forever (§20.3).
    "SANCTION_RETENTION_DAYS": 1095,
    # Rows per export page for the projection rebuild snapshot.
    "EXPORT_PAGE_SIZE": 500,

    # ── Notifications (spec §6.3) ────────────────────────────────────
    # Forty reports about one listing must not become forty letters to its
    # author (the invitation lesson): one per (type, recipient) per window.
    "NOTIFY_COOLDOWN_SECONDS": 600,

    # ── Workspaces door: declared, closed (spec §8) ──────────────────
    # Neither listings nor reviews are workspace-keyed, and Case.scope_key
    # already partitions the queue by an opaque tenant string. The
    # capability names are declared on day one (authz.CAPABILITIES) so a
    # host's role overlay never has to migrate when this flips.
    "WORKSPACE_SCOPED": False,
}

moderation_settings = AppSettings(
    "STAPEL_MODERATION",
    defaults=DEFAULTS,
    # The screener is the one swappable behavior: a host ML provider
    # replaces the default LLM ladder without forking the module.
    import_strings=("SCREENER",),
)

__all__ = ["moderation_settings", "DEFAULTS"]
