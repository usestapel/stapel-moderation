"""Admin for stapel-moderation — read-only, and that is a design decision.

Everywhere else in the fleet a read-only admin is an operator peephole because
the real audience is elsewhere. Here the audience is the SAME people: a
moderator is Django staff. The reason it is still read-only is different and
sharper — **path integrity**.

In legacy, bulk actions on the report admin flipped statuses through
``queryset.update()``: no audit row, no ``reviewed_at``, and the reviewed
content was never actually hidden. A second resolution path existed, invisible
to the audit log, and the report admin left ``status`` and ``moderator_notes``
editable even with ``has_add_permission = False``. Read-only registration
makes that path **impossible by construction** rather than forbidden by
instruction.

``CaseEvent`` is declared ``@access.ops``, whose mutations are ``FORBIDDEN``
at the mandate level, so the audit log is uneditable here even for a
superuser. The read-only classes below are the second of two independent
doors, not the only one.
"""
from django.contrib import admin

from .models import Appeal, Case, CaseEvent, Report, Sanction, UserSanctionState, Verdict


class _ReadOnlyAdmin(admin.ModelAdmin):
    """No add, no change, no delete — and no editable fields to forget about."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Case)
class CaseAdmin(_ReadOnlyAdmin):
    list_display = (
        "id",
        "target_type",
        "target_key",
        "state",
        "severity",
        "report_count",
        "claimed_by",
        "created_at",
        "resolved_at",
    )
    list_filter = ("state", "target_type", "origin")
    search_fields = ("id", "target_key", "scope_key", "subject_user_id")
    date_hierarchy = "created_at"


@admin.register(Report)
class ReportAdmin(_ReadOnlyAdmin):
    list_display = ("id", "case", "target_type", "reason_code", "reporter_id", "created_at")
    list_filter = ("reason_code", "target_type", "good_faith")
    search_fields = ("id", "target_key", "reporter_id")


@admin.register(Verdict)
class VerdictAdmin(_ReadOnlyAdmin):
    list_display = ("id", "case", "decision", "source", "reason_code", "actor_id", "created_at")
    list_filter = ("decision", "source")
    search_fields = ("id", "case__id", "reason_code")


@admin.register(CaseEvent)
class CaseEventAdmin(_ReadOnlyAdmin):
    """The audit trail. Two locks: this class, and @access.ops on the model."""

    list_display = ("id", "case", "kind", "from_state", "to_state", "actor_id", "created_at")
    list_filter = ("kind",)
    search_fields = ("id", "case__id")


@admin.register(Sanction)
class SanctionAdmin(_ReadOnlyAdmin):
    list_display = (
        "id",
        "subject_user_id",
        "kind",
        "scope",
        "state",
        "starts_at",
        "expires_at",
        "issued_by",
    )
    list_filter = ("kind", "state", "scope")
    search_fields = ("id", "subject_user_id", "case__id")


@admin.register(Appeal)
class AppealAdmin(_ReadOnlyAdmin):
    list_display = ("id", "case", "appellant_id", "state", "resolved_by", "created_at")
    list_filter = ("state",)
    search_fields = ("id", "case__id", "appellant_id")


@admin.register(UserSanctionState)
class UserSanctionStateAdmin(_ReadOnlyAdmin):
    """The projection table. Empty in a co-located deployment, by design."""

    list_display = ("projection_key", "allowed", "projection_seq", "projection_updated_at")
    search_fields = ("projection_key",)
