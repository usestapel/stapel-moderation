"""The run's window into the verdict CONSUMER's state.

Not part of any shipped surface and mounted only by the e2e host. It exists
because the one claim this module cannot prove from inside itself is the
important one: moderation emits a fact and never calls a host back, so
"the listing was actually taken down" is a question only ``stapel-listings``
can answer, and answering it needs a reader on that side.
"""
from django.http import JsonResponse
from django.views import View


class ProbeView(View):
    def get(self, request, listing_id):
        from stapel_listings.models import Listing

        listing = Listing.all_objects.filter(pk=listing_id).first()
        if listing is None:
            return JsonResponse({"found": False}, status=404)
        return JsonResponse(
            {
                "found": True,
                "listing_id": listing.pk,
                "status": listing.status,
                "moderation_status": listing.moderation_status,
                "moderation_note": listing.moderation_note,
                "is_active": listing.is_active,
            }
        )
