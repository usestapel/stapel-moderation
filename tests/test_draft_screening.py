"""Draft screening: a refusal at the composer, and it has to be appealable.

Every other entry into this module is asynchronous — a case is opened, a
comm-Task screens it, the verdict lands later — which is the right shape for
content that is already live. It is the wrong shape for the moment a person
presses Publish: an obviously non-compliant photo goes out and is taken down
afterwards, and the author learns about the rules from a takedown letter.

``services.screen_draft`` is the synchronous half, and the whole weight of
this file is on the two properties that make it honest rather than a
convenience wrapper:

1. **A refusal is a real, appealable decision.** Not a boolean the composer
   invented — a persisted ``Case`` with a ``Verdict``, resolved, carrying the
   appeal address DSA Art. 17 requires, and accepted by the existing
   ``open_appeal`` unchanged.
2. **An approval persists nothing.** Every draft of every user would
   otherwise become a row in the human queue, and a queue nobody can work is
   a queue nobody works.

And the one that is not a property but a scar: a screener that could not
answer never returns ``allowed=True``.
"""
import pytest

from stapel_moderation import services
from stapel_moderation.models import (
    Case,
    CaseState,
    Verdict,
    VerdictDecision,
    VerdictSource,
)

pytestmark = pytest.mark.django_db


def _draft(**kwargs):
    return services.TargetContent(
        title=kwargs.pop("title", "A bicycle"),
        text=kwargs.pop("text", "Barely used, good condition."),
        language="en",
        media=tuple(kwargs.pop("media", ())),
        author_id=kwargs.pop("author_id", ""),
    )


# ── The approved half: allowed, and nothing is written down ──────────


def test_an_approved_draft_is_allowed_and_persists_no_case(
    content_double, llm_double, author_user
):
    result = services.screen_draft(
        target_type="listing",
        content=_draft(author_id=str(author_user.pk)),
        subject_user_id=author_user.pk,
    )

    assert result.allowed is True
    assert result.decision == VerdictDecision.APPROVED
    assert result.case_id is None
    assert result.appeal_url == ""
    assert Case.objects.count() == 0


# ── The refused half: a real case, and a real appeal ─────────────────


def test_a_rejected_draft_persists_a_resolved_case_with_a_verdict(
    content_double, llm_double, author_user
):
    llm_double["envelope"]["result"] = {
        "decision": "rejected",
        "reason_code": "illegal",
        "rationale": "Depicts a prohibited item.",
        "confidence": 0.96,
    }

    result = services.screen_draft(
        target_type="listing",
        content=_draft(author_id=str(author_user.pk)),
        subject_user_id=author_user.pk,
    )

    assert result.allowed is False
    assert result.decision == VerdictDecision.REJECTED
    assert result.reason_code == "illegal"
    assert result.rationale == "Depicts a prohibited item."
    assert result.case_id

    case = Case.objects.get(pk=result.case_id)
    assert case.state == CaseState.RESOLVED
    assert case.origin == "draft"
    assert str(case.subject_user_id) == str(author_user.pk)
    verdict = case.verdicts.get()
    assert verdict.decision == VerdictDecision.REJECTED
    assert verdict.source == VerdictSource.LLM


def test_a_refused_draft_can_be_appealed_through_the_existing_flow(
    content_double, llm_double, author_user
):
    """The hard requirement. An inline refusal that cannot be appealed is a
    silent moderation decision, which is the thing DSA Art. 20 forbids."""
    llm_double["envelope"]["result"] = {
        "decision": "rejected",
        "reason_code": "illegal",
        "rationale": "Depicts a prohibited item.",
        "confidence": 0.96,
    }

    result = services.screen_draft(
        target_type="listing",
        content=_draft(author_id=str(author_user.pk)),
        subject_user_id=author_user.pk,
    )
    case = Case.objects.get(pk=result.case_id)

    appeal = services.open_appeal(
        case, appellant_id=author_user.pk, body="The photo is of a toy."
    )

    assert appeal.case_id == case.id


def test_the_appeal_url_is_the_configured_template(
    content_double, llm_double, author_user, settings
):
    settings.STAPEL_MODERATION = {
        "APPEAL_URL_TEMPLATE": "https://example.test/appeals/{case_id}"
    }
    llm_double["envelope"]["result"] = {
        "decision": "rejected",
        "reason_code": "adult",
        "rationale": "Adult content.",
        "confidence": 0.91,
    }

    result = services.screen_draft(
        target_type="listing", content=_draft(), subject_user_id=author_user.pk
    )

    assert result.appeal_url == f"https://example.test/appeals/{result.case_id}"


def test_a_rules_hit_refuses_a_draft_without_paying_for_a_completion(
    content_double, llm_double, author_user
):
    from stapel_moderation.registry import register_rule

    register_rule(
        "weapons",
        {
            "pattern": r"\b(rifle|handgun)\b",
            "decision": "rejected",
            "severity": 4,
            "reason_code": "illegal",
        },
    )

    result = services.screen_draft(
        target_type="listing",
        content=_draft(text="Selling a hunting rifle, barely used."),
        subject_user_id=author_user.pk,
    )

    assert result.allowed is False
    assert llm_double["calls"] == []
    assert Case.objects.get(pk=result.case_id).verdicts.get().source == VerdictSource.RULE


# ── Unavailability never becomes permission ──────────────────────────


def test_an_unavailable_screener_never_allows_the_draft(
    content_double, llm_double, author_user
):
    """The failure that made this module exist, at the new entry point.

    ``llm.complete`` answers a provider outage with a 200 and a failure
    envelope. A draft entry that read that as "nothing objectionable found"
    would publish exactly what nobody screened, inline and instantly.
    """
    llm_double["envelope"] = {"status": "failure", "reason": "provider down"}

    result = services.screen_draft(
        target_type="listing", content=_draft(), subject_user_id=author_user.pk
    )

    assert result.allowed is False
    assert result.decision == VerdictDecision.NEEDS_REVIEW
    assert result.reason_code == "screening_unavailable"
    # And NOTHING is written down (0.7.0). The caller still hears "no", which
    # is the assertion this test exists for; what it must not do is mint a
    # case carrying a machine verdict nobody rendered, against a synthetic
    # key nothing can ever re-read. That combination produced 69 undecidable
    # queue rows on a client stand and 207 screening failures retrying them.
    assert not result.case_id
    assert Case.objects.count() == 0
    assert Verdict.objects.count() == 0


def test_screening_disabled_holds_the_draft_rather_than_clearing_it(
    content_double, settings, author_user
):
    """No automatic screener at all is ``ON_SCREENING_UNAVAILABLE``, and its
    shipped value holds."""
    settings.STAPEL_MODERATION = {"SCREEN_ENABLED": False}

    result = services.screen_draft(
        target_type="listing", content=_draft(), subject_user_id=author_user.pk
    )

    assert result.allowed is False
    assert result.reason_code == "screening_unavailable"


def test_the_confession_switch_is_honoured_for_drafts_too(
    content_double, llm_double, settings, author_user
):
    """``ON_SCREENING_FAILURE="approve"`` trades safety for availability, and
    it is entitled to do that here as well — loudly, through the same setting
    and the same W001 warning, never through a quiet default."""
    settings.STAPEL_MODERATION = {"ON_SCREENING_FAILURE": "approve"}
    llm_double["envelope"] = {"status": "failure", "reason": "provider down"}

    result = services.screen_draft(
        target_type="listing", content=_draft(), subject_user_id=author_user.pk
    )

    assert result.allowed is True
    assert Case.objects.count() == 0


# ── The comm Function ────────────────────────────────────────────────


def test_the_comm_function_screens_a_draft_that_has_no_stored_target(
    content_double, llm_double, author_user
):
    """There is no target to fetch content from — the payload IS the content."""
    from stapel_core.comm import call

    llm_double["envelope"]["result"] = {
        "decision": "rejected",
        "reason_code": "adult",
        "rationale": "Adult content.",
        "confidence": 0.93,
    }

    answer = call(
        "moderation.screen_draft",
        {
            "target_type": "listing",
            "title": "A bicycle",
            "text": "Barely used, good condition.",
            "language": "en",
            "media": [],
            "author_id": str(author_user.pk),
        },
    )

    assert answer["allowed"] is False
    assert answer["decision"] == VerdictDecision.REJECTED
    assert answer["reason_code"] == "adult"
    assert answer["case_id"]
    assert Case.objects.get(pk=answer["case_id"]).state == CaseState.RESOLVED


def test_the_comm_function_approves_without_touching_the_queue(
    content_double, llm_double, author_user
):
    from stapel_core.comm import call

    answer = call(
        "moderation.screen_draft",
        {
            "target_type": "listing",
            "title": "A bicycle",
            "text": "Barely used, good condition.",
            "author_id": str(author_user.pk),
        },
    )

    assert answer["allowed"] is True
    assert answer["case_id"] is None
    assert Case.objects.count() == 0


def test_a_draft_verdict_does_not_announce_a_target_that_does_not_exist(
    content_double, llm_double, author_user, captured_events
):
    """No ``moderation.completed`` for a draft.

    The topic is an INSTRUCTION to the target module — stapel-listings applies
    it to the listing named by target_key — and a draft has no listing. An
    announcement about a key nobody owns is, on this bus, either a permanent
    "unknown listing" log line or a payload redelivered forever.
    """
    llm_double["envelope"]["result"] = {
        "decision": "rejected",
        "reason_code": "illegal",
        "rationale": "Prohibited.",
        "confidence": 0.95,
    }

    services.screen_draft(
        target_type="listing", content=_draft(), subject_user_id=author_user.pk
    )

    assert not [e for e in captured_events if e.event_type == "moderation.completed"]


# ── Inline image bytes: a draft has bytes and no ref ─────────────────


def _blob(size: int = 32) -> str:
    import base64

    return base64.b64encode(b"\xff\xd8\xff" + b"x" * size).decode("ascii")


@pytest.fixture
def hostile_cdn():
    """A ``cdn.describe`` that fails the test if anything calls it.

    Inline bytes have no ref to describe. Reaching for the CDN here would not
    merely be wasteful — it would fail, and a failed image resolution is a
    dropped image, which is how a screener answers "nothing objectionable"
    about a photo it never saw.
    """
    from stapel_core.comm import function

    @function("cdn.describe")
    def _describe(payload):
        raise AssertionError(
            f"cdn.describe was called for an inline draft image: {payload!r}"
        )

    return _describe


def test_inline_image_bytes_reach_the_model_without_the_cdn(
    content_double, llm_double, hostile_cdn, author_user
):
    """A draft holds raw photo bytes before any CDN upload has settled: there
    is no ref for ``cdn.describe`` to resolve, and waiting for one would mean
    the composer cannot be answered."""
    blob = _blob()

    result = services.screen_draft(
        target_type="listing",
        content=_draft(),
        subject_user_id=author_user.pk,
        images=[{"data_b64": blob, "mime": "image/jpeg"}],
    )

    assert result.allowed is True
    assert llm_double["calls"][0]["images"] == [
        {"data_b64": blob, "mime": "image/jpeg"}
    ]


def test_a_non_image_mime_is_refused_loudly(content_double, llm_double, author_user):
    """Refused, not skipped. A dropped image would come back as allowed=True
    about content nobody screened — the very failure this release fixes."""
    with pytest.raises(services.InvalidDraftImage):
        services.screen_draft(
            target_type="listing",
            content=_draft(),
            images=[{"data_b64": _blob(), "mime": "application/pdf"}],
        )
    assert llm_double["calls"] == []


def test_undecodable_bytes_are_refused_loudly(content_double, llm_double, author_user):
    with pytest.raises(services.InvalidDraftImage):
        services.screen_draft(
            target_type="listing",
            content=_draft(),
            images=[{"data_b64": "not base64 at all!!", "mime": "image/png"}],
        )


def test_more_inline_images_than_the_cap_is_refused(
    content_double, llm_double, settings, author_user
):
    """Truncating to the cap would screen four photos out of ten and answer
    about all ten."""
    settings.STAPEL_MODERATION = {"MAX_MEDIA_PER_CASE": 1}

    with pytest.raises(services.InvalidDraftImage):
        services.screen_draft(
            target_type="listing",
            content=_draft(),
            images=[
                {"data_b64": _blob(), "mime": "image/jpeg"},
                {"data_b64": _blob(), "mime": "image/jpeg"},
            ],
        )


def test_inline_images_over_the_total_byte_cap_are_refused(
    content_double, llm_double, settings, author_user
):
    settings.STAPEL_MODERATION = {"MAX_INLINE_IMAGE_BYTES": 16}

    with pytest.raises(services.InvalidDraftImage):
        services.screen_draft(
            target_type="listing",
            content=_draft(),
            images=[{"data_b64": _blob(4096), "mime": "image/jpeg"}],
        )


def test_cdn_refs_still_work_for_a_draft_that_has_them(
    content_double, cdn_double, llm_double, settings, author_user
):
    """Both doors stay open: a draft has bytes and no ref, a published listing
    has a ref and no bytes, and both must be screened."""
    settings.STAPEL_MODERATION = {"MEDIA_BASE_URL": "https://cdn.example.test"}

    services.screen_draft(
        target_type="listing",
        content=_draft(media=["product/802d669"]),
        subject_user_id=author_user.pk,
    )

    assert llm_double["calls"][0]["images"] == [
        {"url": "https://cdn.example.test/media/cdn/product/802d669/1080w.webp"}
    ]


def test_the_comm_function_carries_inline_images(
    content_double, llm_double, hostile_cdn, author_user
):
    from stapel_core.comm import call

    blob = _blob()
    llm_double["envelope"]["result"] = {
        "decision": "rejected",
        "reason_code": "adult",
        "rationale": "Adult content.",
        "confidence": 0.94,
    }

    answer = call(
        "moderation.screen_draft",
        {
            "target_type": "listing",
            "title": "A bicycle",
            "text": "Barely used.",
            "language": "ru",
            "author_id": str(author_user.pk),
            "images": [{"data_b64": blob, "mime": "image/jpeg"}],
        },
    )

    assert set(answer) >= {
        "allowed",
        "decision",
        "reason_code",
        "rationale",
        "confidence",
        "case_id",
        "appeal_url",
    }
    assert answer["allowed"] is False
    assert llm_double["calls"][0]["images"] == [{"data_b64": blob, "mime": "image/jpeg"}]


# --- W008: the comm timeout a synchronous screen needs ---------------------


def test_a_comm_timeout_below_the_screen_timeout_is_named(settings):
    """The defect this closes is a timeout that DEFEATS a gate.

    `moderation.screen_draft` is the one Function in this module a caller
    waits on, and a live screening measured ~3s. Under core's 5s default
    FUNCTION_TIMEOUT it is one slow model away from a TimeoutError — and
    every caller's answer to a timeout is its fail-open branch, because a
    screener that cannot answer must never block a seller. So the timeout
    silently converts "screen this draft" into "do not screen this draft",
    while both the endpoint and the gate keep reporting success.

    A warning rather than an error: a deployment that never calls the
    Function synchronously is entitled to core's default.
    """
    from stapel_moderation.checks import check_screen_draft_timeout

    settings.STAPEL_COMM = {"FUNCTION_TIMEOUT": 5.0}
    settings.STAPEL_MODERATION = {"SCREEN_TIMEOUT_SECONDS": 60}
    findings = check_screen_draft_timeout(None)
    assert [f.id for f in findings] == ["stapel_moderation.W008"]
    assert "5" in str(findings[0]) and "60" in str(findings[0])


def test_a_named_timeout_silences_it(settings):
    """The fix the check points at, in the form core 0.58.0 gives it."""
    from stapel_moderation.checks import check_screen_draft_timeout

    settings.STAPEL_COMM = {
        "FUNCTION_TIMEOUT": 5.0,
        "FUNCTION_TIMEOUTS": {"moderation.screen_draft": 60.0},
    }
    settings.STAPEL_MODERATION = {"SCREEN_TIMEOUT_SECONDS": 60}
    assert check_screen_draft_timeout(None) == []


def test_a_prefix_entry_silences_it_too(settings):
    from stapel_moderation.checks import check_screen_draft_timeout

    settings.STAPEL_COMM = {
        "FUNCTION_TIMEOUT": 5.0,
        "FUNCTION_TIMEOUTS": {"moderation.": 90.0},
    }
    settings.STAPEL_MODERATION = {"SCREEN_TIMEOUT_SECONDS": 60}
    assert check_screen_draft_timeout(None) == []


def test_a_generous_global_timeout_silences_it(settings):
    """A deployment that raised the global number has already answered."""
    from stapel_moderation.checks import check_screen_draft_timeout

    settings.STAPEL_COMM = {"FUNCTION_TIMEOUT": 90.0}
    settings.STAPEL_MODERATION = {"SCREEN_TIMEOUT_SECONDS": 60}
    assert check_screen_draft_timeout(None) == []


def test_screening_switched_off_is_silent(settings):
    """Nothing waits on the Function, so nothing can be cut short by it."""
    from stapel_moderation.checks import check_screen_draft_timeout

    settings.STAPEL_COMM = {"FUNCTION_TIMEOUT": 5.0}
    settings.STAPEL_MODERATION = {"SCREEN_ENABLED": False, "SCREEN_TIMEOUT_SECONDS": 60}
    assert check_screen_draft_timeout(None) == []

