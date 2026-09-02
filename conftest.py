"""Test harness for stapel-moderation.

The settings block is NOT here: it lives in ``_codegen_settings.py`` so the
test suite, ``make contract`` and the capabilities emitter cannot drift apart
(contract-pipeline.md §3).

The autouse fixtures below are the ones a target-generic module with three
merge-registries needs: every registry is reset around every test, and so is
the comm Function registry, so a fake ``content_function`` or a stubbed
``llm.complete`` from one test can never leak into the next and quietly make
it pass.
"""


def pytest_configure(config):
    from django.conf import settings

    if not settings.configured:
        from stapel_moderation._codegen_settings import settings_kwargs

        settings.configure(**settings_kwargs())
        import django

        django.setup()

        from stapel_core.comm.schemas import autoload_schemas

        autoload_schemas()


import pytest  # noqa: E402


# ── Registry isolation ───────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_registries():
    """Target types, reasons and rules, clean before and after every test.

    The intake subscriptions are torn down with them: ``register_target_type``
    wires a policy's ``intake_events`` as a side effect, and a handler left
    subscribed from a previous test would open a case for an event this test
    never registered a type for.
    """
    from stapel_core.comm import action_registry

    from stapel_moderation.actions import handle_intake, reset_intake_subscriptions
    from stapel_moderation.registry import reset_registries

    def _unwire():
        reset_registries()
        reset_intake_subscriptions()
        for handlers in action_registry._subscribers.values():
            while handle_intake in handlers:
                handlers.remove(handle_intake)

    _unwire()
    yield
    _unwire()


@pytest.fixture(autouse=True)
def _reset_comm_functions():
    """Restore the Function registry, so test doubles never outlive a test.

    Snapshot-and-restore rather than clear: this module REGISTERS its own
    providers at app ready(), and clearing outright would unregister
    ``moderation.submit`` for every later test.
    """
    from stapel_core.comm.registry import function_registry

    providers = dict(function_registry._providers)
    schemas = dict(function_registry._schemas)
    yield
    function_registry._providers.clear()
    function_registry._providers.update(providers)
    function_registry._schemas.clear()
    function_registry._schemas.update(schemas)


@pytest.fixture(autouse=True)
def _clear_cache():
    """The notification cooldown and the user blacklist both live in the cache."""
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


# ── Users ────────────────────────────────────────────────────────────


@pytest.fixture
def api_client():
    from rest_framework.test import APIClient

    return APIClient()


@pytest.fixture
def client_for():
    """``client_for(user)`` — a FRESH client per actor.

    Not a convenience: several tests act as two or three people in one
    scenario (a reporter, the moderator who decided, the second moderator who
    hears the appeal), and re-authenticating one shared client silently
    changes who every earlier handle is. That produced a real false failure
    while this suite was being written.
    """
    from rest_framework.test import APIClient

    def _make(user=None):
        client = APIClient()
        if user is not None:
            client.force_authenticate(user=user)
        return client

    return _make


@pytest.fixture
def user(db):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(
        username="alice", email="alice@example.com", password="x"
    )


@pytest.fixture
def other_user(db):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(
        username="bob", email="bob@example.com", password="x"
    )


@pytest.fixture
def author_user(db):
    """The author of the moderated content — the subject of verdicts."""
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(
        username="carol", email="carol@example.com", password="x"
    )


@pytest.fixture
def moderator(db, settings):
    """A staff user holding MID clearance in the moderation app only.

    This is the shape the module documents for hosts: ``is_staff`` plus a role
    whose ``apps`` entry raises clearance for ``moderation`` and nothing else.
    """
    from django.contrib.auth import get_user_model

    settings.STAPEL_ACCESS = {
        "ROLES": {
            "moderator": {"clearance": "low", "apps": {"moderation": "mid"}},
            "ts_lead": {"clearance": "mid", "apps": {"moderation": "high"}},
        }
    }
    user = get_user_model().objects.create_user(
        username="mod", email="mod@example.com", password="x", is_staff=True
    )
    user.staff_roles = ["moderator"]
    return user


@pytest.fixture
def ts_lead(db, settings):
    """A staff user holding HIGH clearance in the moderation app.

    HIGH is what mutations of a sensitive model need: claim, verdict, sanction.
    The split from ``moderator`` (MID, read-only) is the module's own
    demonstration that per-app clearance actually grades the surface.
    """
    from django.contrib.auth import get_user_model

    settings.STAPEL_ACCESS = {
        "ROLES": {
            "moderator": {"clearance": "low", "apps": {"moderation": "mid"}},
            "ts_lead": {"clearance": "mid", "apps": {"moderation": "high"}},
        }
    }
    user = get_user_model().objects.create_user(
        username="lead", email="lead@example.com", password="x", is_staff=True
    )
    user.staff_roles = ["ts_lead"]
    return user


@pytest.fixture
def lead_client(api_client, ts_lead):
    api_client.force_authenticate(user=ts_lead)
    return api_client


@pytest.fixture
def auth_client(api_client, user):
    api_client.force_authenticate(user=user)
    return api_client


# ── Target doubles ───────────────────────────────────────────────────


@pytest.fixture
def content_double(author_user):
    """Register a ``listing`` target type backed by an in-process content
    Function, and hand back the mutable dict it answers with.

    The double is the whole seam under test: the module never imports a target
    module, it calls a name. A test changes the returned dict to change what
    the "listing" says.
    """
    from stapel_core.comm import function

    from stapel_moderation.registry import register_target_type

    state = {
        "listing_id": "42",
        "title": "A bicycle",
        "text": "Barely used, good condition.",
        "language": "en",
        "media": [],
        "author_id": str(author_user.pk),
        "url": "https://example.test/listings/42",
        "status": "published",
        "moderation_status": "pending",
    }

    @function("listings.moderation_content")
    def _content(payload):
        if payload.get("listing_id") != state["listing_id"]:
            raise LookupError(f"listing {payload.get('listing_id')} not found")
        return dict(state)

    register_target_type(
        "listing",
        {
            "intake_events": ["listing.submitted"],
            "id_field": "listing_id",
            "content_function": "listings.moderation_content",
            "verdict_event": "moderation.completed",
            "notification_types": {"content_blocked": "listing_blocked"},
        },
    )
    return state


@pytest.fixture
def cdn_double():
    """Register a ``cdn.describe`` double and hand back the dict it answers with.

    The variant URLs are RELATIVE on purpose. ``cdn.describe`` is
    host-agnostic by design — it answers ``/media/cdn/<ref>/<tier>.webp`` and
    leaves the origin to whoever renders it — so a screener that forwards
    that path verbatim hands a model vendor something it cannot fetch. The
    double answers what the real Function answers, which is the only way a
    test can hold that seam.
    """
    from stapel_core.comm import function

    state = {
        "calls": [],
        "snapshots": {
            "product/802d669": {
                "ref": "product/802d669",
                "kind": "image",
                "mime": "image/webp",
                "variants": [
                    {
                        "url": "/media/cdn/product/802d669/540w.webp",
                        "tier": 540,
                        "width": 224,
                        "mime": "image/webp",
                    },
                    {
                        "url": "/media/cdn/product/802d669/1080w.webp",
                        "tier": 1080,
                        "width": 447,
                        "mime": "image/webp",
                    },
                ],
            }
        },
    }

    @function("cdn.describe")
    def _describe(payload):
        state["calls"].append(payload)
        ref = str(payload.get("ref") or "")
        if ref not in state["snapshots"]:
            raise LookupError(f"cdn.describe: unknown media ref {ref!r}")
        return dict(state["snapshots"][ref])

    return state


@pytest.fixture
def llm_double():
    """Register an ``llm.complete`` double whose answer a test controls.

    Defaults to the shape a real provider returns on success. Set
    ``double["envelope"]`` to ``{"status": "failure", ...}`` to exercise the
    single most important behaviour in the module: that a failure envelope
    RAISES instead of being returned as a successful screening.
    """
    from stapel_core.comm import function

    double = {
        "calls": [],
        "envelope": {
            "status": "ok",
            "result": {
                "decision": "approved",
                "reason_code": "",
                "rationale": "Nothing objectionable.",
                "confidence": 0.95,
            },
            "model": "medium",
            "usage": {"input_tokens": 100, "output_tokens": 20},
        },
    }

    @function("llm.complete")
    def _complete(payload):
        double["calls"].append(payload)
        return double["envelope"]

    return double


@pytest.fixture
def captured_events():
    """Collect this module's own emits (in-process, synchronous)."""
    from stapel_core.comm import action_registry, subscribe_action

    from stapel_moderation.events import EMITTED_TOPICS

    collected = []

    def _handler(event):
        collected.append(event)

    for name in EMITTED_TOPICS:
        subscribe_action(name, _handler)
    try:
        yield collected
    finally:
        for name in EMITTED_TOPICS:
            handlers = action_registry._subscribers.get(name, [])
            if _handler in handlers:
                handlers.remove(_handler)


@pytest.fixture
def captured_notifications(monkeypatch):
    """Capture ``request_notification`` calls without a notifications service."""
    sent = []

    def _fake(notification_type, **kwargs):
        sent.append({"type": notification_type, **kwargs})
        return True

    monkeypatch.setattr(
        "stapel_core.notifications.request_notification", _fake, raising=False
    )
    monkeypatch.setattr(
        "stapel_core.notifications.publish.request_notification", _fake, raising=False
    )
    return sent
