"""The automatic screening stages: deterministic rules, then the LLM.

Three stages exist (spec §5.6): ``rules`` (synchronous, explainable, free),
``llm`` (schema-constrained, paid), ``human`` (the queue). Only the first two
are here — the third is the absence of a decision.

The whole file exists behind one seam. ``STAPEL_MODERATION["SCREENER"]`` is a
dotted path to ``(case, content, *, reports) -> ScreeningResult``; a host with
its own classifier plugs in there rather than forking the module, and
:func:`default_screener` below is simply the implementation that ships.

**The one rule that makes retries possible.** ``llm.complete`` never raises on
behalf of a provider — it returns ``{"status": "failure", "reason": ...}``. A
handler that returned such an envelope as a result would have its comm-Task
marked DONE, and the retry ladder would never run: the failure would look
exactly like a successful screening that happened to decide nothing. So every
non-``ok`` envelope, every ``CommError`` and every malformed ``result`` raises
:class:`ScreeningUnavailable` — the three-level check
``stapel-recordings/vector/qa.py`` established.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace
from typing import Optional

from .models import VerdictDecision, VerdictSource
from .registry import REASON_LOW_CONFIDENCE

logger = logging.getLogger(__name__)


class ScreeningUnavailable(Exception):
    """The automatic screener could not render a verdict.

    Raised, never returned. Raising is what makes the comm-Task retry; a
    returned value would be a success and the case would sit forever holding a
    decision nobody made.
    """


@dataclass
class ScreeningResult:
    """One automatic verdict, before it becomes a :class:`Verdict` row."""

    decision: str
    source: str = VerdictSource.LLM
    reason_code: str = ""
    rationale: str = ""
    confidence: Optional[float] = None
    media_flags: tuple = ()
    matched_rules: tuple = ()
    model: str = ""
    usage: dict = field(default_factory=dict)

    def evidence(self, content, *, excerpt_chars: int) -> dict:
        """The stored evidence bundle (never emitted; see spec §5.2)."""
        text = (content.title + "\n" + content.text).strip() if content.title else content.text
        return {
            "excerpt": (text or "")[:excerpt_chars],
            "media_refs": list(self.media_flags),
            "matched_rules": list(self.matched_rules),
        }


# ── Stage 1: deterministic rules ─────────────────────────────────────


def run_rules(case, content) -> Optional[ScreeningResult]:
    """First hit wins, in stable rule-code order. ``None`` = nothing matched.

    A rule hit is a verdict without an LLM call: cheap, deterministic and
    explainable, which is exactly why it runs first and why the disclosure
    endpoint can enumerate it.
    """
    from .registry import rules_for_target

    haystack = "\n".join(
        part for part in (content.title, content.text) if part
    )
    for rule in rules_for_target(case.target_type):
        pattern = rule["pattern"]
        if not pattern:
            continue
        try:
            hit = re.search(pattern, haystack, re.IGNORECASE | re.MULTILINE)
        except re.error:
            logger.error(
                "moderation: rule %r has an invalid pattern and was skipped",
                rule["code"],
            )
            continue
        if hit:
            return ScreeningResult(
                decision=rule["decision"],
                source=VerdictSource.RULE,
                reason_code=rule["reason_code"],
                rationale=f"matched rule {rule['code']}",
                matched_rules=(rule["code"],),
            )
    return None


# ── Stage 2: the LLM ─────────────────────────────────────────────────


def _sanitize(text: str) -> str:
    """Strip injection markers before the text re-enters a model's context.

    ``stapel-agent`` is imported LAZILY and is deliberately NOT a dependency
    of this package: the LLM is reached by the string name ``llm.complete``
    over comm, and a deployment with no agent at all must still boot (its
    ``ON_SCREENING_UNAVAILABLE`` policy sends everything to humans). The
    laziness is a consequence of the screener being a replaceable seam, not a
    workaround for a circular import.
    """
    if not text:
        return ""
    try:
        from stapel_agent.safety.markers import sanitize_for_rag
    except ImportError:
        # No stapel-agent in this process. The text still goes to a model
        # through comm, so say so once rather than pretending it was cleaned.
        logger.warning(
            "moderation: stapel-agent is not installed, screening content is "
            "sent to llm.complete without marker sanitization"
        )
        return text
    return sanitize_for_rag(text)


def _absolute_url(url: str) -> str:
    """Resolve one ``cdn.describe`` variant URL against ``MEDIA_BASE_URL``.

    ``cdn.describe`` answers RELATIVE variant URLs on purpose — it is
    host-agnostic and leaves the origin to whoever renders the page — so what
    it hands back is ``/media/cdn/<ref>/<tier>.webp``, a path with no scheme
    and no host. Forwarding that to a completion provider is not a degraded
    image, it is a 400 (``invalid_image_url``) on all three attempts of the
    retry ladder, and a case parked on a ``policy_default`` verdict.

    Returns ``""`` when the URL is relative and no origin is configured. The
    caller skips such an image rather than sending it, which is the same
    posture the unresolvable-ref branch already takes: one photo nobody could
    resolve is not a reason to abandon the text next to it.
    """
    from urllib.parse import urljoin, urlsplit

    from .conf import moderation_settings

    if urlsplit(url).scheme in ("http", "https"):
        return url
    base = str(moderation_settings.MEDIA_BASE_URL or "").strip()
    if not base:
        return ""
    return urljoin(base if base.endswith("/") else base + "/", url.lstrip("/"))


def _fetch_image(url: str) -> Optional[tuple]:
    """Fetch one variant's bytes for the ``data_b64`` transport.

    Returns ``(bytes, mime)`` or ``None``; ``None`` is always a skip, never a
    raise, for the same reason as everywhere else in this file — an image the
    fleet could not hand over is not a reason to lose the text screening, and
    it is emphatically not a reason to burn a retry attempt.

    Bounded twice, both bounds being settings rather than constants:
    ``MEDIA_FETCH_TIMEOUT_SECONDS`` (a hanging CDN must not hold the
    screening Task open until its deadline) and ``MEDIA_FETCH_MAX_BYTES``,
    measured on the bytes actually read — a ``Content-Length`` is a claim by
    the other end, and one byte over the cap is a skip rather than a
    truncation, because half a JPEG is not a picture.
    """
    import urllib.request
    from urllib.error import URLError

    from .conf import moderation_settings

    max_bytes = int(moderation_settings.MEDIA_FETCH_MAX_BYTES)
    timeout = float(moderation_settings.MEDIA_FETCH_TIMEOUT_SECONDS)
    request = urllib.request.Request(url, headers={"User-Agent": "stapel-moderation"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            # One byte over the cap is enough to know it is over the cap.
            body = response.read(max_bytes + 1)
            declared = ""
            headers = getattr(response, "headers", None)
            if headers is not None:
                declared = str(headers.get("Content-Type") or "").split(";")[0].strip()
    except (URLError, OSError, ValueError) as exc:  # noqa: BLE001 — a skip, not a failure
        logger.info("moderation: could not fetch media bytes from %s: %s", url, exc)
        return None
    if not body:
        logger.info("moderation: media fetch from %s returned no bytes", url)
        return None
    if len(body) > max_bytes:
        logger.info(
            "moderation: media at %s is over MEDIA_FETCH_MAX_BYTES (%s), skipped",
            url,
            max_bytes,
        )
        return None
    return body, declared


def _media_images(content) -> list:
    """Resolve the target's media refs into ``llm.complete`` image entries.

    Two transports, and ``MEDIA_TRANSPORT`` finally decides between them:

    ``"url"``
        the provider fetches the image itself, which needs an address the
        provider can actually reach — a relative CDN path absolutized against
        ``MEDIA_BASE_URL``, or nothing at all.

    ``"data_b64"``
        the bytes ride in the payload. This is the transport that works when
        the provider cannot reach the fleet inbound, which behind a proxy or
        on a private network is the ordinary case rather than the exotic one.

    Before either of them: ``content.extra["inline_images"]`` short-circuits
    the whole function. Those are bytes a caller HANDED us — a draft holds
    photos before any upload has settled into a ref, so there is nothing for
    ``cdn.describe`` to resolve and nothing to fetch. They arrive already
    validated and bounded (``services._normalize_inline_images``) and go to
    the model as they are.

    Every branch that cannot produce a usable image LOGS and continues. A
    silent drop is what let a live stand screen text only for its entire
    lifetime while every case said "screened".
    """
    import base64
    import mimetypes

    from stapel_core.comm import CommError, call

    from .conf import moderation_settings

    inline = list((content.extra or {}).get("inline_images") or ())
    if inline:
        return [
            {"data_b64": entry["data_b64"], "mime": entry["mime"]} for entry in inline
        ]

    refs = list(content.media or ())[: int(moderation_settings.MAX_MEDIA_PER_CASE)]
    if not refs:
        return []
    tier = int(moderation_settings.MEDIA_SCREEN_TIER)
    transport = str(moderation_settings.MEDIA_TRANSPORT or "url").lower()
    images = []
    for ref in refs:
        try:
            described = call("cdn.describe", {"ref": str(ref)}) or {}
        except (CommError, LookupError, KeyError):
            # A media reference we cannot resolve is not a reason to abandon
            # the screening of the text next to it.
            logger.info("moderation: could not describe media ref %s", ref)
            continue
        variants = described.get("variants") or []
        best = None
        for variant in variants:
            width = int(variant.get("width") or 0)
            url = variant.get("url")
            if not url or width > tier:
                continue
            if best is None or width > int(best.get("width") or 0):
                best = variant
        if best is None and variants:
            best = variants[0]
        if not best or not best.get("url"):
            continue

        url = _absolute_url(str(best["url"]))
        if not url:
            logger.warning(
                "moderation: media ref %s resolves to the relative URL %s and "
                "STAPEL_MODERATION['MEDIA_BASE_URL'] is empty — the image is "
                "SKIPPED; the text is still screened (see moderation.W007)",
                ref,
                best["url"],
            )
            continue

        if transport != "data_b64":
            images.append({"url": url})
            continue

        fetched = _fetch_image(url)
        if fetched is None:
            continue
        body, declared = fetched
        mime = (
            str(best.get("mime") or "")
            or str(described.get("mime") or "")
            or declared
            or (mimetypes.guess_type(url)[0] or "")
        )
        if not mime:
            logger.info(
                "moderation: media ref %s has no known content type, skipped", ref
            )
            continue
        images.append(
            {"data_b64": base64.b64encode(body).decode("ascii"), "mime": mime}
        )
    return images


def run_llm(case, content, *, reports=()) -> ScreeningResult:
    """Ask ``llm.complete`` for a schema-constrained verdict.

    Raises :class:`ScreeningUnavailable` on all three failure shapes. The
    prompt cache is disabled by the agent for schema and image requests, so
    every call here is paid in full — which is why ``MAX_MEDIA_PER_CASE`` and
    ``policy["media"]`` exist.
    """
    from stapel_core.comm import CommError, call

    from . import prompts
    from .conf import moderation_settings
    from .registry import reasons_for_target, resolve_policy

    policy = resolve_policy(case.target_type)
    sanitized = replace(
        content, text=_sanitize(content.text), title=_sanitize(content.title)
    )
    payload = {
        "prompt": prompts.build_user_prompt(
            target_type=case.target_type,
            content=sanitized,
            reason_codes=list(reasons_for_target(case.target_type)),
            reports=reports,
        ),
        "system_prompt": prompts.SYSTEM_PROMPT,
        "model": moderation_settings.LLM_MODEL,
        "schema": prompts.OUTPUT_SCHEMA,
    }
    provider = moderation_settings.LLM_PROVIDER
    if provider:
        payload["provider"] = provider
    if policy["media"]:
        images = _media_images(content)
        if images:
            payload["images"] = images

    # Level 1: the transport itself.
    try:
        envelope = call(
            "llm.complete",
            payload,
            timeout=float(moderation_settings.SCREEN_TIMEOUT_SECONDS),
        )
    except CommError as exc:
        raise ScreeningUnavailable(f"llm.complete unreachable: {exc}") from exc

    # Level 2: the envelope. THE line that makes retries happen at all.
    if not isinstance(envelope, dict) or envelope.get("status") != "ok":
        reason = (envelope or {}).get("reason") if isinstance(envelope, dict) else envelope
        raise ScreeningUnavailable(f"llm.complete returned failure: {reason!r}")

    # Level 3: the shape of the result.
    result = envelope.get("result")
    if not isinstance(result, dict):
        raise ScreeningUnavailable(f"llm.complete result is not an object: {result!r}")
    decision = result.get("decision")
    if decision not in ("approved", "rejected", "needs_review"):
        raise ScreeningUnavailable(f"llm.complete returned decision {decision!r}")
    try:
        confidence = float(result.get("confidence"))
    except (TypeError, ValueError) as exc:
        raise ScreeningUnavailable("llm.complete returned no usable confidence") from exc

    reason_code = str(result.get("reason_code") or "")
    floor = float(moderation_settings.LLM_CONFIDENCE_FLOOR or 0)
    if confidence < floor:
        # A confident-sounding guess is still a guess. Below the floor the
        # decision is a person's to make, whatever the model said.
        decision = VerdictDecision.NEEDS_REVIEW
        reason_code = REASON_LOW_CONFIDENCE

    return ScreeningResult(
        decision=decision,
        source=VerdictSource.LLM,
        reason_code=reason_code,
        rationale=str(result.get("rationale") or "")[:500],
        confidence=confidence,
        media_flags=tuple(result.get("media_flags") or ()),
        # The prompt version rides along with the provider/size so a
        # verdict stays attributable after the prompt is rewritten.
        model=f"{envelope.get('model') or moderation_settings.LLM_MODEL}"
        f"@prompt{prompts.PROMPT_VERSION}",
        usage=envelope.get("usage") or {},
    )


# ── The shipped screener ─────────────────────────────────────────────


def default_screener(case, content, *, reports=()) -> ScreeningResult:
    """Rules first, then the LLM. The value of ``SCREENER`` by default."""
    hit = run_rules(case, content)
    if hit is not None:
        return hit
    return run_llm(case, content, reports=reports)


def get_screener():
    """The configured screener callable (``import_strings`` resolves it)."""
    from .conf import moderation_settings

    return moderation_settings.SCREENER


__all__ = [
    "ScreeningResult",
    "ScreeningUnavailable",
    "default_screener",
    "get_screener",
    "run_llm",
    "run_rules",
]
