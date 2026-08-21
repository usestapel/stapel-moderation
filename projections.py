"""Read-model declarations of stapel-moderation.

One projection, and it ships with **both** of its halves built. That is the
whole point of the file: ``stapel-shop`` declares a rating projection against
``reviews.aggregates_by_keys`` / ``reviews.aggregates_export``, Functions that
did not exist when it was written, and the declaration sat there looking
correct. A projection is not a declaration, it is a declaration plus the two
Functions it names.

**Two modes, one declaration** (core's projections primitive). In a composite
where moderation and its consumer share a process, ``resolve_mode`` sees the
``moderation`` app label and reads through ``live_query`` in-process — no
table, no bus subscription. In a split topology the same declaration becomes a
materialised table fed from the bus with idempotency, ordering and
``rebuild``. Business code never branches on which: it calls
``read("moderation.user_sanctions", keys=[...])``.
"""
from __future__ import annotations

from stapel_core.comm import Projection

from .events import SANCTION_EXPIRED, SANCTION_ISSUED, SANCTION_LIFTED


class UserSanctions(Projection):
    """Who is under an active sanction, as a read-model.

    ``sequence_field = "seq"`` rather than the event timestamp: all three
    facts carry the sanction row's ``updated_at`` in unix milliseconds, and
    the export snapshot carries the same number, so a rebuild running while
    facts arrive orders them against each other correctly. Falling back to the
    publish timestamp would compare two different clocks.
    """

    name = "moderation.user_sanctions"
    consumes = (SANCTION_ISSUED, SANCTION_LIFTED, SANCTION_EXPIRED)
    model = "moderation.UserSanctionState"
    source_key = "subject_user_id"
    sequence_field = "seq"
    live_query = "moderation.sanctions_by_users"
    source_of_truth = "moderation.sanctions_export"

    def apply(self, event) -> dict:
        """Recompute the row from the owner's own tables.

        The events carry one sanction, but the read-model answers a question
        about a USER, who may be under several. Deriving the row from the
        payload alone would make "lifted one of two suspensions" read as
        "unsanctioned". Re-reading is cheap and is the only correct mapping —
        the event is the trigger, not the whole truth.
        """
        from . import services

        return services.sanction_snapshot(event.payload[self.source_key])

    def from_snapshot(self, row: dict) -> dict:
        """Map an export row to the same fields ``apply`` produces.

        Overridden explicitly, even though the default would nearly work: the
        shop projection's bug was that its ``apply`` and its snapshot rows
        used different field names, so ``rebuild()`` raised ``TypeError`` in
        remote mode while local mode looked fine. Both halves here answer
        ``{allowed, sanctions}``, and this method is what proves it.
        """
        return {"allowed": row.get("allowed", True), "sanctions": row.get("sanctions") or []}


__all__ = ["UserSanctions"]
