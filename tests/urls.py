from django.urls import include, path

urlpatterns = [
    path("moderation/", include("stapel_moderation.urls")),
]
