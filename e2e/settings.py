"""E2E host settings — a real host mounting auth + listings + moderation.

Not shipped in the wheel (the setuptools packages list is explicit).

**stapel-listings is installed on purpose.** The single claim this module
makes that no unit test in this repository can prove is "resolving a case
CHANGES THE TARGET": moderation emits `moderation.completed` and never calls
a host back, so the proof needs a real consumer in the process. The e2e host
therefore boots the released listings module and the run asserts on
`Listing.status` / `Listing.moderation_status` afterwards. That is the
legacy asymmetry — resolving a complaint about a review deleted the review,
resolving one about an ad touched nothing — closed by measurement rather than
by agreement.
"""
import os
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

SECRET_KEY = "e2e-only-not-a-secret"
DEBUG = True
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "drf_spectacular",
    "stapel_core.django.apps.CommonDjangoConfig",
    "stapel_core.django.users",
    "stapel_core.django.outbox",
    "stapel_core.django.taskstore",
    "stapel_core.django.projections",
    "stapel_auth",
    # The real verdict consumer. Without it this run would prove that a fact
    # was emitted, which is not the same as proving anything happened.
    "stapel_listings",
    "stapel_moderation",
    # Answers llm.complete in-process; see e2e/apps.py.
    "e2e.apps.E2EConfig",
]

AUTH_USER_MODEL = "users.User"

MIDDLEWARE = [
    # First on purpose: without it this project's own E-gates would run under
    # manage.py and nowhere else (stapel_core.boot.W002).
    "stapel_core.django.boot.BootGateMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

# The mandate IS the moderator's authorization. Without MandateBackend in the
# chain, has_perm() falls back to DAC rows and the console would be open to
# any staff account.
AUTHENTICATION_BACKENDS = [
    "stapel_core.access.backend.MandateBackend",
    "stapel_core.access.backend.AuditedModelBackend",
]

ROOT_URLCONF = "e2e.urls"

from stapel_core.django.settings import get_common_templates  # noqa: E402

TEMPLATES = get_common_templates(BASE_DIR)

_STATE_DIR = Path(
    os.environ.get(
        "STAPEL_MODERATION_E2E_DIR", tempfile.gettempdir() + "/stapel-moderation-e2e"
    )
)
_STATE_DIR.mkdir(parents=True, exist_ok=True)

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": _STATE_DIR / "db.sqlite3",
    }
}

# A FILE-BASED cache, not LocMem: the user blacklist is what gives a sanction
# its teeth, and a per-process cache would make it invisible to every other
# process — the run's own `manage.py` steps included. Production uses Redis
# for the same reason (one ban, every service).
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.filebased.FileBasedCache",
        "LOCATION": str(_STATE_DIR / "cache"),
    }
}

LOGIN_URL = "admin:login"
LOGIN_REDIRECT_URL = "admin:index"
STATIC_URL = "/static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "stapel_core.django.jwt.authentication.JWTCookieAuthentication",
    ],
    "EXCEPTION_HANDLER": "stapel_core.django.api.errors.stapel_exception_handler",
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}
SPECTACULAR_SETTINGS = {"TITLE": "stapel-moderation E2E", "VERSION": "0.1.0"}

STAPEL_COMM = {
    "OUTBOX_ENABLED": True,
    "ACTION_TRANSPORT": "inprocess",
    # One process, no worker: screening runs synchronously. A real composite
    # uses "action" (or "bus") so task.requested actually leaves the web
    # process — that is spec §18.3's own experiment, and it belongs on a
    # stand, not in a single-process script.
    "TASK_DISPATCH": "inline",
    "TASK_EXECUTOR": "inline",
}

# This run speaks plain http on loopback, and a Secure cookie would never be
# sent back — the authenticated half of the script would look anonymous.
JWT_COOKIE_SECURE = False

STAPEL_AUTH = {
    "AUTH_PASSWORD_LOGIN": True,
    "AUTH_EMAIL_LOGIN": False,
    "AUTH_OAUTH_LOGIN": False,
    "AUTH_EMAIL_REGISTRATION": False,
    "AUTH_OAUTH_REGISTRATION": False,
}

# The moderator roles the module documents. Per-app clearance is the point:
# a moderator is staff in the moderation app and LOW everywhere else.
STAPEL_ACCESS = {
    "ROLES": {
        "moderator": {"clearance": "low", "apps": {"moderation": "mid"}},
        "ts_lead": {"clearance": "mid", "apps": {"moderation": "high"}},
    }
}

STAPEL_MODERATION = {
    "TARGET_TYPES": {
        "listing": {
            "intake_events": ["listing.submitted"],
            "id_field": "listing_id",
            "content_function": "listings.moderation_content",
            "verdict_event": "moderation.completed",
            "notification_types": {"content_blocked": "listing_blocked"},
        },
    },
    "APPEAL_URL_TEMPLATE": "http://127.0.0.1:8772/appeals/{case_id}",
    "REPORT_THROTTLE": None,
    # The run needs to see the letters, not wait out a window.
    "NOTIFY_COOLDOWN_SECONDS": 0,
}

STAPEL_LISTINGS = {
    "MODERATION_TARGET_TYPE": "listing",
    "LISTING_URL_TEMPLATE": "http://127.0.0.1:8772/listings/{listing_id}",
    # The gate this module exists to replace: listings must NOT approve its
    # own submissions, or there would be nothing for a verdict to decide.
    "AUTO_APPROVE_ON_PUBLISH": False,
    # This host has no CDN, and the seed listing carries no photos. Requiring
    # one would only make the seed lie about having uploaded something.
    "REQUIRE_IMAGE_ON_PUBLISH": False,
}

STAPEL_SERVICES = [{"name": "stapel-moderation E2E", "prefix": ""}]
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
