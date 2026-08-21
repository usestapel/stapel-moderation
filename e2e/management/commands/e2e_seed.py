"""Seed the e2e host: three people and one listing on its way to publication.

Only the setup is done here. Every step the run actually asserts on happens
over real HTTP in ``run_e2e.py`` — a seed that resolved a case would be a
seed that tested nothing.
"""
import json

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Create the e2e users and one PENDING listing; print their ids as JSON."

    def handle(self, *args, **options):
        from django.contrib.auth import get_user_model
        from stapel_listings.models import Listing

        User = get_user_model()
        password = options.get("password") or "e2e-pass-Str0ng!"

        author = _user(User, "author", password)
        reporter = _user(User, "reporter", password)
        # Two moderators, because the different-actor rule on appeals is one
        # of the things this run proves, and it cannot be proved with one.
        lead = _user(User, "lead", password, staff=True, roles=["ts_lead"])
        reviewer = _user(User, "reviewer", password, staff=True, roles=["ts_lead"])

        listing = Listing.objects.create(
            owner=author,
            category_id="bicycles",
            title_draft="Vintage racing bicycle",
            description_draft="Steel frame, recently serviced, ready to ride.",
            price_draft="450.00",
            currency="USD",
            language="en",
        )
        # publish_listing promotes the draft, moves the listing to PENDING and
        # emits listing.submitted — which is the intake event moderation is
        # subscribed to. The case opens as a side effect of that fact, exactly
        # as it would in production.
        from stapel_listings.services.publish import publish_listing

        publish_listing(listing)
        listing.refresh_from_db()

        self.stdout.write(
            json.dumps(
                {
                    "author_id": str(author.pk),
                    "reporter_id": str(reporter.pk),
                    "lead_id": str(lead.pk),
                    "reviewer_id": str(reviewer.pk),
                    "listing_id": listing.pk,
                    "listing_status": listing.status,
                    "listing_moderation_status": listing.moderation_status,
                }
            )
        )

    def add_arguments(self, parser):
        parser.add_argument("--password", default="e2e-pass-Str0ng!")


def _user(User, username, password, *, staff=False, roles=()):
    """Create a user, and grant staff roles THROUGH stapel-auth.

    Writing ``user.staff_roles`` directly looks like it works and does not:
    that field is the materialized cache of the ``StaffRoleAssignment`` table,
    and the JWT claim is rebuilt from the assignments on every login — so a
    hand-set field is silently replaced by an empty list the moment the user
    signs in, and the console answers 403 with no explanation. ``stapel-auth``
    is the single writer, exactly as the mandate's A2 invariant says.
    """
    user, _created = User.objects.get_or_create(
        username=username, defaults={"email": f"{username}@example.test"}
    )
    user.set_password(password)
    user.is_staff = staff
    user.is_active = True
    user.save()

    if roles:
        from stapel_auth.staff_roles import assign_staff_role

        for role in roles:
            assign_staff_role(user, role)
    return user
