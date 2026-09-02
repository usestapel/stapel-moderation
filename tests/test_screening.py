"""Screening: the retry ladder, and the switches that decide what happens
when it runs out.

Spec §14 assertions 4-6 live here, and assertion 4 is the single most
important test in the module. ``llm.complete`` does not raise on behalf of a
provider — it RETURNS ``{"status": "failure"}``. A handler that returned that
envelope would have its comm-Task marked DONE, the retry ladder would never
run, and the case would sit holding a decision nobody made. The fix is one
``raise``, and this file is what keeps it there.
"""
import pytest
from stapel_core.django.taskstore.models import TaskRecord

from stapel_moderation import services
from stapel_moderation.models import (
    Case,
    CaseState,
    Verdict,
    VerdictDecision,
    VerdictSource,
)
from stapel_moderation.screening import ScreeningUnavailable

pytestmark = pytest.mark.django_db


def _open_case(**kwargs):
    from stapel_core.comm import mutate_and_emit

    with mutate_and_emit() as emit_event:
        case, _created = services.open_case(
            "listing", "42", origin="submission", emit_event=emit_event, **kwargs
        )
    return case


# ── Assertion 4: a failure envelope must RETRY, not succeed ──────────


def test_llm_failure_envelope_raises_rather_than_returning(content_double, llm_double):
    """The three-level check, at the level that actually bites.

    ``llm.complete`` answers a failure with HTTP 200 and a dict. If the
    screener passed that back as a result, the task machinery would see a
    successful run.
    """
    from stapel_moderation.screening import run_llm

    llm_double["envelope"] = {"status": "failure", "reason": "provider timeout"}
    case = _open_case()
    content = services.fetch_content("listing", "42")

    with pytest.raises(ScreeningUnavailable, match="failure"):
        run_llm(case, content)


def test_llm_malformed_result_raises(content_double, llm_double):
    """Level 3: an ``ok`` envelope whose result is the wrong shape."""
    from stapel_moderation.screening import run_llm

    llm_double["envelope"] = {"status": "ok", "result": "approved, probably"}
    case = _open_case()
    content = services.fetch_content("listing", "42")

    with pytest.raises(ScreeningUnavailable, match="not an object"):
        run_llm(case, content)


def test_llm_unknown_decision_raises(content_double, llm_double):
    from stapel_moderation.screening import run_llm

    llm_double["envelope"] = {
        "status": "ok",
        "result": {"decision": "maybe", "reason_code": "", "rationale": "", "confidence": 1},
    }
    case = _open_case()
    content = services.fetch_content("listing", "42")

    with pytest.raises(ScreeningUnavailable, match="decision"):
        run_llm(case, content)


def test_task_retries_to_exhaustion_then_holds(content_double, llm_double, settings):
    """Assertion 4 end to end: retried, parked, and then HELD for a human.

    Three attempts (the default ladder), a FAILED task record, and a case in
    the human queue carrying a ``needs_review`` verdict that names
    ``screening_unavailable`` — not a published listing, which is what legacy
    produced roughly thirty minutes after its LLM went down.
    """
    llm_double["envelope"] = {"status": "failure", "reason": "provider down"}

    case = _open_case()
    from stapel_core.comm import mutate_and_emit

    with mutate_and_emit() as emit_event:
        services.start_screening(case, emit_event=emit_event)

    task = TaskRecord.objects.get(kind=services.SCREEN_TASK)
    assert task.state == TaskRecord.FAILED
    assert task.attempts == 3

    case.refresh_from_db()
    assert case.state == CaseState.QUEUED

    verdict = case.verdicts.get()
    assert verdict.decision == VerdictDecision.NEEDS_REVIEW
    assert verdict.source == VerdictSource.POLICY_DEFAULT
    assert verdict.reason_code == "screening_unavailable"
    assert case.events.filter(kind="screen_failed").exists()


# ── Assertion 5: the confession switch ───────────────────────────────


def test_on_screening_failure_approve_resolves_and_warns(
    content_double, llm_double, settings
):
    """Assertion 5: ``approve`` publishes unscreened content AND says so."""
    from django.core.checks import run_checks

    llm_double["envelope"] = {"status": "failure", "reason": "provider down"}
    settings.STAPEL_MODERATION = {"ON_SCREENING_FAILURE": "approve"}

    case = _open_case()
    from stapel_core.comm import mutate_and_emit

    with mutate_and_emit() as emit_event:
        services.start_screening(case, emit_event=emit_event)

    case.refresh_from_db()
    assert case.state == CaseState.RESOLVED
    assert case.verdicts.get().decision == VerdictDecision.APPROVED

    ids = {getattr(m, "id", "") for m in run_checks()}
    assert "stapel_moderation.W001" in ids


def test_on_screening_failure_reject_is_also_a_confession(
    content_double, llm_double, settings
):
    from django.core.checks import run_checks

    llm_double["envelope"] = {"status": "failure", "reason": "provider down"}
    settings.STAPEL_MODERATION = {"ON_SCREENING_FAILURE": "reject"}

    case = _open_case()
    from stapel_core.comm import mutate_and_emit

    with mutate_and_emit() as emit_event:
        services.start_screening(case, emit_event=emit_event)

    case.refresh_from_db()
    assert case.verdicts.get().decision == VerdictDecision.REJECTED
    assert "stapel_moderation.W001" in {getattr(m, "id", "") for m in run_checks()}


def test_default_hold_prints_no_warning(content_double, settings):
    from django.core.checks import run_checks

    assert "stapel_moderation.W001" not in {getattr(m, "id", "") for m in run_checks()}


# ── Assertion 6: a queued case is never resolved by a clock ──────────


def test_stale_queue_is_never_auto_resolved_by_default(content_double, llm_double):
    """Assertion 6. Legacy swept `pending` AND `needs_review` into approval
    every five minutes, so human review existed on paper only."""
    from django.utils import timezone

    llm_double["envelope"]["result"]["decision"] = "needs_review"
    case = _open_case()
    from stapel_core.comm import mutate_and_emit

    with mutate_and_emit() as emit_event:
        services.start_screening(case, emit_event=emit_event)
    case.refresh_from_db()
    assert case.state == CaseState.QUEUED

    Case.objects.filter(pk=case.pk).update(
        updated_at=timezone.now() - timezone.timedelta(days=30)
    )

    from stapel_moderation.tasks import sweep_stale_cases

    result = sweep_stale_cases()
    assert result["auto_resolved"] == 0
    case.refresh_from_db()
    assert case.state == CaseState.QUEUED


def test_auto_resolve_when_the_host_opts_in_and_is_warned(
    content_double, llm_double, settings
):
    from django.core.checks import run_checks
    from django.utils import timezone

    llm_double["envelope"]["result"]["decision"] = "needs_review"
    case = _open_case()
    from stapel_core.comm import mutate_and_emit

    with mutate_and_emit() as emit_event:
        services.start_screening(case, emit_event=emit_event)

    settings.STAPEL_MODERATION = {"AUTO_RESOLVE_STALE_QUEUE": 60}
    Case.objects.filter(pk=case.pk).update(
        updated_at=timezone.now() - timezone.timedelta(hours=2)
    )

    from stapel_moderation.tasks import sweep_stale_cases

    assert sweep_stale_cases()["auto_resolved"] == 1
    assert "stapel_moderation.W002" in {getattr(m, "id", "") for m in run_checks()}


# ── The rest of the ladder ───────────────────────────────────────────


def test_rules_stage_short_circuits_the_llm(content_double, llm_double):
    """A deterministic hit is a verdict without paying for a completion."""
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
    content_double["text"] = "Selling a hunting rifle, barely used."

    case = _open_case()
    from stapel_core.comm import mutate_and_emit

    with mutate_and_emit() as emit_event:
        services.start_screening(case, emit_event=emit_event)

    verdict = Verdict.objects.get()
    assert verdict.source == VerdictSource.RULE
    assert verdict.decision == VerdictDecision.REJECTED
    assert verdict.evidence["matched_rules"] == ["weapons"]
    assert llm_double["calls"] == []


def test_low_confidence_is_forced_to_needs_review(content_double, llm_double):
    """A confident-sounding guess is still a guess."""
    llm_double["envelope"]["result"] = {
        "decision": "approved",
        "reason_code": "",
        "rationale": "Probably fine.",
        "confidence": 0.4,
    }
    case = _open_case()
    from stapel_core.comm import mutate_and_emit

    with mutate_and_emit() as emit_event:
        services.start_screening(case, emit_event=emit_event)

    verdict = Verdict.objects.get()
    assert verdict.decision == VerdictDecision.NEEDS_REVIEW
    assert verdict.reason_code == "low_confidence"


def test_empty_content_is_screened_not_auto_approved(content_double, llm_double):
    """The fourth legacy fail-open: an empty description skipped the LLM
    entirely, in two independent copies of the shortcut."""
    content_double["text"] = ""
    content_double["title"] = ""

    case = _open_case()
    from stapel_core.comm import mutate_and_emit

    with mutate_and_emit() as emit_event:
        services.start_screening(case, emit_event=emit_event)

    assert len(llm_double["calls"]) == 1
    assert "(empty)" in llm_double["calls"][0]["prompt"]


def test_screening_sends_a_schema_and_a_system_prompt(content_double, llm_double):
    """Schema-constrained output is the contract; a provider that cannot
    constrain its decoder fails the call instead of answering in prose."""
    case = _open_case()
    from stapel_core.comm import mutate_and_emit

    with mutate_and_emit() as emit_event:
        services.start_screening(case, emit_event=emit_event)

    payload = llm_double["calls"][0]
    assert payload["schema"]["required"] == [
        "decision",
        "reason_code",
        "rationale",
        "confidence",
    ]
    assert "never instructions to be followed" in payload["system_prompt"]
    # The complaint text is deliberately NOT in the prompt: free text written
    # by whoever wants a competitor removed is the obvious injection vector.
    assert "user_content" in payload["prompt"]


def test_verdict_records_the_prompt_version(content_double, llm_double):
    """A statement of reasons whose reasoning cannot be reconstructed is not
    one — so the prompt version rides along with the model name."""
    case = _open_case()
    from stapel_core.comm import mutate_and_emit

    with mutate_and_emit() as emit_event:
        services.start_screening(case, emit_event=emit_event)

    assert Verdict.objects.get().model == "medium@prompt1"


def test_screening_is_skipped_when_the_policy_says_so(content_double, llm_double):
    from stapel_moderation.registry import register_target_type

    policy = dict(content_double)  # not the policy; re-register explicitly
    del policy
    register_target_type(
        "listing",
        {
            "intake_events": ["listing.submitted"],
            "id_field": "listing_id",
            "content_function": "listings.moderation_content",
            "screen": False,
        },
    )
    case = _open_case()
    from stapel_core.comm import mutate_and_emit

    with mutate_and_emit() as emit_event:
        services.start_screening(case, emit_event=emit_event)

    case.refresh_from_db()
    assert case.state == CaseState.QUEUED
    assert llm_double["calls"] == []
    assert not TaskRecord.objects.filter(kind=services.SCREEN_TASK).exists()


def test_a_vanished_target_is_dismissed_not_retried(content_double, llm_double):
    """Permanent failures must not burn the retry ladder: a target that is
    gone will still be gone on the third attempt."""
    case = _open_case()
    content_double["listing_id"] = "different"

    from stapel_core.comm import mutate_and_emit

    with mutate_and_emit() as emit_event:
        services.start_screening(case, emit_event=emit_event)

    task = TaskRecord.objects.get(kind=services.SCREEN_TASK)
    assert task.state == TaskRecord.DONE
    assert task.attempts == 1
    case.refresh_from_db()
    assert case.state == CaseState.RESOLVED
    assert case.verdicts.get().decision == VerdictDecision.DISMISSED


def test_redelivered_screen_task_is_a_no_op(content_double, llm_double):
    """The composite guard: only a case still in {open, screening} is screened."""
    from stapel_moderation.tasks import screen_case

    case = _open_case()
    from stapel_core.comm import mutate_and_emit

    with mutate_and_emit() as emit_event:
        services.start_screening(case, emit_event=emit_event)
    assert len(llm_double["calls"]) == 1

    assert screen_case({"case_id": str(case.id)}) == {"skipped": CaseState.RESOLVED}
    assert len(llm_double["calls"]) == 1


# ── Media transport: the screener has to hand over a fetchable image ──


class _FakeResponse:
    """The bounded-read shape :func:`urllib.request.urlopen` answers with."""

    def __init__(self, body: bytes, content_type: str = "image/webp"):
        self._body = body
        self.headers = {"Content-Type": content_type}

    def read(self, size=-1):
        if size is None or size < 0:
            body, self._body = self._body, b""
            return body
        body, self._body = self._body[:size], self._body[size:]
        return body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def http_double(monkeypatch):
    """Stand in for the outbound fetch of one CDN variant's bytes."""
    import urllib.request

    state = {
        "calls": [],
        "body": b"RIFF-webp-bytes",
        "content_type": "image/webp",
        "error": None,
    }

    def _urlopen(request, timeout=None):
        state["calls"].append(
            {"url": getattr(request, "full_url", request), "timeout": timeout}
        )
        if state["error"] is not None:
            raise state["error"]
        return _FakeResponse(state["body"], state["content_type"])

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    return state


def _media_content():
    return services.TargetContent(
        text="Barely used, good condition.", media=("product/802d669",)
    )


def test_a_relative_variant_url_is_absolutized_against_the_base(
    content_double, cdn_double, settings
):
    """The live defect: a bare CDN path is not an image a vendor can fetch.

    ``cdn.describe`` answers ``/media/cdn/...`` by design; a provider handed
    that path replies 400 invalid_image_url, the screening is retried to
    exhaustion and the case parks on a policy_default verdict. Every listing
    with a real photo on one live stand failed exactly this way, and the
    listings that "passed" were the ones whose media ref did not resolve at
    all — so the screener had never once seen a photo.
    """
    from stapel_moderation.screening import _media_images

    settings.STAPEL_MODERATION = {"MEDIA_BASE_URL": "https://cdn.example.test"}

    images = _media_images(_media_content())

    assert images == [
        {"url": "https://cdn.example.test/media/cdn/product/802d669/1080w.webp"}
    ]


def test_a_relative_variant_url_without_a_base_is_skipped(
    content_double, cdn_double, caplog
):
    """No base URL configured: skip the image and SAY so.

    Skipping matches the posture already stated one line above in the same
    function — an unresolvable media ref is not a reason to abandon the text
    next to it — and it is the only honest option, because the alternative is
    handing a provider a path it will answer 400 to on all three attempts.
    """
    import logging

    from stapel_moderation.screening import _media_images

    with caplog.at_level(logging.WARNING, logger="stapel_moderation.screening"):
        images = _media_images(_media_content())

    assert images == []
    assert "MEDIA_BASE_URL" in caplog.text


def test_an_absolute_variant_url_is_passed_through(content_double, cdn_double, settings):
    """A CDN that already answers absolute URLs needs no base at all."""
    from stapel_moderation.screening import _media_images

    cdn_double["snapshots"]["product/802d669"]["variants"] = [
        {
            "url": "https://cdn.example.test/media/cdn/product/802d669/1080w.webp",
            "tier": 1080,
            "width": 447,
            "mime": "image/webp",
        }
    ]

    images = _media_images(_media_content())

    assert images == [
        {"url": "https://cdn.example.test/media/cdn/product/802d669/1080w.webp"}
    ]


def test_data_b64_transport_inlines_the_bytes(
    content_double, cdn_double, http_double, settings
):
    """``MEDIA_TRANSPORT="data_b64"`` was a declared setting nobody read.

    It is the transport that works when the provider cannot reach the fleet
    inbound at all — the general case behind a proxy — so it inlines the
    variant's bytes and hands over no URL whatsoever.
    """
    import base64

    from stapel_moderation.screening import _media_images

    settings.STAPEL_MODERATION = {
        "MEDIA_TRANSPORT": "data_b64",
        "MEDIA_BASE_URL": "https://cdn.internal.test",
    }

    images = _media_images(_media_content())

    assert images == [
        {
            "data_b64": base64.b64encode(http_double["body"]).decode("ascii"),
            "mime": "image/webp",
        }
    ]
    assert "url" not in images[0]
    assert http_double["calls"][0]["url"] == (
        "https://cdn.internal.test/media/cdn/product/802d669/1080w.webp"
    )


def test_the_inline_fetch_honours_the_size_cap(
    content_double, cdn_double, http_double, settings
):
    """An image over the cap is skipped, never truncated: half a JPEG is not
    a picture, and a broker refusing an oversized payload is worse."""
    from stapel_moderation.screening import _media_images

    settings.STAPEL_MODERATION = {
        "MEDIA_TRANSPORT": "data_b64",
        "MEDIA_BASE_URL": "https://cdn.internal.test",
        "MEDIA_FETCH_MAX_BYTES": 8,
    }
    http_double["body"] = b"x" * 64

    assert _media_images(_media_content()) == []


def test_the_inline_fetch_honours_the_timeout(
    content_double, cdn_double, http_double, settings
):
    """The fetch is bounded in time as well as in size: a CDN that hangs must
    not hold a screening Task open until its deadline."""
    from stapel_moderation.screening import _media_images

    settings.STAPEL_MODERATION = {
        "MEDIA_TRANSPORT": "data_b64",
        "MEDIA_BASE_URL": "https://cdn.internal.test",
        "MEDIA_FETCH_TIMEOUT_SECONDS": 3,
    }

    _media_images(_media_content())

    assert http_double["calls"][0]["timeout"] == 3.0


def test_a_fetch_failure_skips_the_image_and_keeps_the_text(
    content_double, cdn_double, http_double, settings
):
    from urllib.error import URLError

    from stapel_moderation.screening import _media_images

    settings.STAPEL_MODERATION = {
        "MEDIA_TRANSPORT": "data_b64",
        "MEDIA_BASE_URL": "https://cdn.internal.test",
    }
    http_double["error"] = URLError("connection refused")

    assert _media_images(_media_content()) == []


def test_the_screening_call_carries_the_absolutized_image(
    content_double, cdn_double, llm_double, settings
):
    """End to end through ``run_llm``: what reaches the provider is fetchable."""
    from stapel_core.comm import mutate_and_emit

    settings.STAPEL_MODERATION = {"MEDIA_BASE_URL": "https://cdn.example.test"}
    content_double["media"] = ["product/802d669"]

    case = _open_case()
    with mutate_and_emit() as emit_event:
        services.start_screening(case, emit_event=emit_event)

    payload = llm_double["calls"][0]
    assert payload["images"] == [
        {"url": "https://cdn.example.test/media/cdn/product/802d669/1080w.webp"}
    ]


def test_media_transport_url_without_a_base_is_a_startup_warning(
    content_double, settings
):
    """W007: the exact live misconfiguration, made visible at boot.

    A target type screens media, the transport is "url", and there is no
    origin to resolve a relative CDN path against — so every photo is
    silently dropped from every screening and nothing anywhere says so.
    """
    from django.core.checks import run_checks

    assert "stapel_moderation.W007" in {getattr(m, "id", "") for m in run_checks()}

    settings.STAPEL_MODERATION = {"MEDIA_BASE_URL": "https://cdn.example.test"}
    assert "stapel_moderation.W007" not in {getattr(m, "id", "") for m in run_checks()}


def test_no_media_warning_when_the_type_does_not_screen_media(settings):
    """A text-only target type is not misconfigured for lacking a CDN origin."""
    from django.core.checks import run_checks

    from stapel_moderation.registry import register_target_type

    register_target_type(
        "review",
        {
            "id_field": "review_id",
            "content_function": "reviews.moderation_content",
            "media": False,
        },
    )

    assert "stapel_moderation.W007" not in {getattr(m, "id", "") for m in run_checks()}
