from django.contrib import admin
from django.urls import include, path

from .probe import ProbeView

urlpatterns = [
    # Mounted so the module's read-only admin exists somewhere real and so
    # LOGIN_URL / LOGIN_REDIRECT_URL resolve.
    path("admin/", admin.site.urls),
    path("auth/api/", include("stapel_auth.urls")),
    # listings contributes only the `v1/` segment (api-versioning.md §6), so the
    # host supplies `listings/api/` — mounting it at a bare `listings/` trips
    # core's §37 mount gate with two dozen E004s.
    path("listings/api/", include("stapel_listings.urls")),
    path("moderation/", include("stapel_moderation.urls")),
    # Not part of any shipped surface: the run's window into the CONSUMER's
    # state. The claim under test is "the target changed", and only listings
    # can answer that.
    path("_e2e/probe/<int:listing_id>", ProbeView.as_view(), name="e2e-probe"),
]
