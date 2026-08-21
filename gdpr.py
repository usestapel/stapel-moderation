"""GDPR data handler for stapel-moderation.

The asymmetry with stapel-forms is deliberate and is the point of this
docstring. A form answer is the respondent's own data, so erasure destroys it.
A moderation case is not: it is a record ABOUT a piece of content, produced by
the platform in the exercise of a legal obligation (DSA Art. 17 wants a
statement of reasons that stays checkable). Erasing a case because the person
who reported it closed their account would delete the platform's own
compliance record at a stranger's request.

So the erasure here is narrow and precise: the complainant's **identity** goes
(``reporter_id = None``), and their free-text description goes with it,
because a complaint someone wrote is theirs and can name them. The case, the
verdicts, the audit trail and the reason codes stay — the count of reports
stays truthful, and "40 reports, 3 of them from erased accounts" is a
different and better fact than a count that silently shrinks.

The subject of a SANCTION is a separate question and is not erased here: a
sanction is an enforcement record with its own retention clock
(``SANCTION_RETENTION_DAYS``), and dropping the subject id would both unban
the person and destroy the progressive ladder's memory. That is a limitation
stated rather than papered over — a host with a legal basis to erase enforcement
history lifts the sanction first, and the retention purge takes it from there.

Registered as a provider in ``apps.ready()`` and driven by the
``@on_action("user.deleted")`` consumer in ``actions.py``. A host must ALSO
list ``"moderation"`` in ``STAPEL_GDPR["DATA_OWNERS"]`` — registering without
declaring is ``gdpr.E002`` on that side, and the erasure closure never
completes.
"""
from __future__ import annotations

import logging

from stapel_core.gdpr import GDPRProvider

logger = logging.getLogger(__name__)


class ModerationGDPRProvider(GDPRProvider):
    section = "moderation"

    def export(self, user_id) -> dict:
        """Everything this module holds that is about the user as a person.

        Three roles a user can be in, and all three are theirs to see: the
        complaints they filed, the sanctions they carry, and the appeals they
        wrote. Cases about their content are represented by the sanction and
        appeal rows rather than dumped whole — a case card contains OTHER
        people's complaints, which are not this user's data to receive.
        """
        from .models import Appeal, Report, Sanction

        reports = Report.objects.filter(reporter_id=user_id).order_by("created_at")
        sanctions = Sanction.objects.filter(subject_user_id=user_id).order_by("created_at")
        appeals = Appeal.objects.filter(appellant_id=user_id).order_by("created_at")
        return {
            "reports": [
                {
                    "id": str(row.id),
                    "target_type": row.target_type,
                    "target_key": row.target_key,
                    "reason_code": row.reason_code,
                    "description": row.description,
                    "created_at": row.created_at.isoformat(),
                }
                for row in reports
            ],
            "sanctions": [
                {
                    "id": str(row.id),
                    "kind": row.kind,
                    "scope": row.scope,
                    "reason_code": row.reason_code,
                    "note": row.note,
                    "starts_at": row.starts_at.isoformat() if row.starts_at else "",
                    "expires_at": row.expires_at.isoformat() if row.expires_at else None,
                    "state": row.state,
                }
                for row in sanctions
            ],
            "appeals": [
                {
                    "id": str(row.id),
                    "case_id": str(row.case_id),
                    "body": row.body,
                    "state": row.state,
                    "resolution_note": row.resolution_note,
                    "created_at": row.created_at.isoformat(),
                }
                for row in appeals
            ],
        }

    def delete(self, user_id) -> None:
        self.anonymize(user_id)

    def anonymize(self, user_id) -> None:
        """Detach the person from their complaints and appeals.

        Idempotent — an already-erased row erases to itself, so at-least-once
        redelivery is harmless. Returning normally IS the receipt (the cdn
        lesson): the provider returns only after the update has landed.
        """
        from .services import erase_user_reports

        erased = erase_user_reports(user_id)
        if erased:
            logger.info(
                "moderation: erased the reporter identity on %s report(s) of user %s",
                erased,
                user_id,
            )


__all__ = ["ModerationGDPRProvider"]
