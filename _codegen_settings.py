"""Single-module Django settings for stapel-moderation.

One ``settings.configure(...)`` block serves three callers, which is the
point: the test suite, the contract-emission harness and the capabilities
emitter cannot drift apart if there is nothing to drift.

  - ``conftest.py`` — the bare test mount (``stapel_moderation.tests.urls``);
  - ``_codegen.py`` / ``make contract`` — the CANONICAL mount
    (``stapel_moderation.codegen_urls`` → ``moderation/``; the module's own
    ``urls.py`` bakes the ``api/v1`` segment in, so the full public prefix
    is ``/moderation/api/v1``), plus drf-spectacular and the production
    ``REST_FRAMEWORK`` block so the emitted schema matches what a real
    deployment serves;
  - ``_capabilities.py``, which reuses ``_codegen._configure``.

``SPECTACULAR_SETTINGS`` is deliberately not set: drf-spectacular builds
its settings singleton at import time, before a ``configure()``-based
harness can populate it, so the emitter runs on drf defaults — the state
every other pair-backend's harness emits under. The one knob that must
still be forced, ``SCHEMA_PATH_PREFIX``, is patched on the singleton
directly by the harness.
"""
from __future__ import annotations


def settings_kwargs(
    *,
    root_urlconf: str = "stapel_moderation.tests.urls",
    contract: bool = False,
) -> dict:
    """The ``settings.configure(**kwargs)`` for a single-module moderation instance."""
    if contract:
        # Mirror stapel_core.django.settings.REST_FRAMEWORK exactly (the
        # config a real deployment emits under). Inlined, not imported, to
        # dodge the import-time settings read.
        rest_framework = {
            "DEFAULT_AUTHENTICATION_CLASSES": [
                "stapel_core.django.jwt.authentication.JWTCookieAuthentication",
            ],
            "DEFAULT_PERMISSION_CLASSES": [
                "stapel_core.django.api.permissions.IsServiceRequest",
                "stapel_core.django.api.permissions.IsSuperUser",
            ],
            "DEFAULT_RENDERER_CLASSES": [
                "rest_framework.renderers.JSONRenderer",
                "rest_framework.renderers.BrowsableAPIRenderer",
            ],
            "DEFAULT_SCHEMA_CLASS": "stapel_core.django.openapi.schemas.PermissionAwareAutoSchema",
            "EXCEPTION_HANDLER": "stapel_core.django.api.errors.stapel_exception_handler",
        }
    else:
        rest_framework = None

    kwargs = dict(
        SECRET_KEY="test-secret-key-not-for-production",
        INSTALLED_APPS=[
            "django.contrib.contenttypes",
            "django.contrib.auth",
            "django.contrib.sessions",
            "django.contrib.admin",
            "django.contrib.messages",
            "stapel_core.django.apps.CommonDjangoConfig",
            "stapel_core.django.users",
            # The comm-Task persistence app: screening runs as a Task, so
            # TaskRecord has to exist and task.requested has to be routed to
            # the local handler.
            "stapel_core.django.taskstore",
            # ProjectionModel's owning app (moderation.UserSanctionState is
            # the concrete table; this one supplies the runner + rebuild
            # management command).
            "stapel_core.django.projections",
            "rest_framework",
            "drf_spectacular",
            "stapel_moderation",
        ],
        AUTH_USER_MODEL="users.User",
        # The mandate IS the moderator's authorization (authz.py). Without
        # MandateBackend in the chain, user.has_perm() answers off plain DAC
        # rows and the whole console would be open to any staff account —
        # which is precisely the failure the mandate exists to prevent, so
        # the harness runs the production chain rather than Django's default.
        AUTHENTICATION_BACKENDS=[
            "stapel_core.access.backend.MandateBackend",
            "stapel_core.access.backend.AuditedModelBackend",
        ],
        DATABASES={
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": ":memory:",
            }
        },
        DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
        USE_TZ=True,
        ROOT_URLCONF=root_urlconf,
        CACHES={
            "default": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            }
        },
        # Synchronous in-process comm with schema validation ON, so the
        # committed contracts in schemas/ are enforced by the tests.
        STAPEL_BUS_BACKEND="stapel_core.bus.backends.memory.MemoryBus",
        STAPEL_COMM={
            "OUTBOX_ENABLED": False,
            "ACTION_TRANSPORT": "inprocess",
            "VALIDATE_SCHEMAS": True,
            # An emit outside a transaction is a bug here, not a warning:
            # the outbox canon is what makes "the row exists" and "the fact
            # was announced" one decision.
            "EMIT_OUTSIDE_ATOMIC": "error",
            # Tests and the emission harness run screening synchronously.
            # A real composite uses "action" (or "bus") so task.requested
            # actually leaves the web process — spec §18.3.
            "TASK_DISPATCH": "inline",
            "TASK_EXECUTOR": "inline",
        },
        MIGRATION_MODULES={
            "users": None,
        },
    )
    if rest_framework is not None:
        kwargs["REST_FRAMEWORK"] = rest_framework
    return kwargs


# The multi-module common path prefix drf-spectacular auto-detects when
# every pair-backend's schema is emitted inside an all-modules aggregate.
# Forced on the singleton by the harness so a single-module instance
# derives the same operationIds.
CODEGEN_SCHEMA_PATH_PREFIX = "/"
