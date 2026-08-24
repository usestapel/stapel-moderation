"""i18n error keys of stapel-moderation.

Only ``error.<status>.moderation_<slug>`` keys leave this package — human
strings are translations, never literals in responses. The English registry
below is the source; ``translations/errors.<lang>.json`` ships the localized
catalogues in the same release (owning keys means shipping their catalogues).
"""
from stapel_core.django.api.errors import ErrorKeysView, register_service_errors

# ── Registries ───────────────────────────────────────────────────────
ERR_400_UNKNOWN_TARGET_TYPE = "error.400.moderation_unknown_target_type"
ERR_400_UNKNOWN_REASON = "error.400.moderation_unknown_reason"
ERR_400_REASON_NOT_APPLICABLE = "error.400.moderation_reason_not_applicable"
ERR_400_DESCRIPTION_REQUIRED = "error.400.moderation_description_required"
ERR_400_EVIDENCE_INVALID = "error.400.moderation_evidence_invalid"

# ── Intake ───────────────────────────────────────────────────────────
ERR_400_OWN_CONTENT = "error.400.moderation_own_content"
ERR_400_CONTACT_REQUIRED = "error.400.moderation_contact_required"
ERR_403_CANNOT_REPORT = "error.403.moderation_cannot_report"
ERR_404_TARGET_NOT_FOUND = "error.404.moderation_target_not_found"
ERR_409_ALREADY_REPORTED = "error.409.moderation_already_reported"

# ── Queue ────────────────────────────────────────────────────────────
ERR_400_INVALID_DECISION = "error.400.moderation_invalid_decision"
ERR_400_INVALID_TRANSITION = "error.400.moderation_invalid_transition"
ERR_403_FORBIDDEN = "error.403.moderation_forbidden"
ERR_404_CASE_NOT_FOUND = "error.404.moderation_case_not_found"
ERR_409_CASE_CLAIMED = "error.409.moderation_case_claimed"
ERR_409_CASE_RESOLVED = "error.409.moderation_case_resolved"
ERR_409_NOT_CLAIMANT = "error.409.moderation_not_claimant"
ERR_503_CONTENT_UNAVAILABLE = "error.503.moderation_content_unavailable"

# ── Sanctions ────────────────────────────────────────────────────────
ERR_400_INVALID_SANCTION_KIND = "error.400.moderation_invalid_sanction_kind"
ERR_404_SANCTION_NOT_FOUND = "error.404.moderation_sanction_not_found"
ERR_409_SANCTION_NOT_ACTIVE = "error.409.moderation_sanction_not_active"

# ── Appeals ──────────────────────────────────────────────────────────
ERR_400_INVALID_OUTCOME = "error.400.moderation_invalid_outcome"
ERR_403_NOT_APPELLANT = "error.403.moderation_not_appellant"
ERR_403_SAME_ACTOR = "error.403.moderation_same_actor"
ERR_404_APPEAL_NOT_FOUND = "error.404.moderation_appeal_not_found"
ERR_409_ALREADY_APPEALED = "error.409.moderation_already_appealed"
ERR_409_APPEAL_RESOLVED = "error.409.moderation_appeal_resolved"
ERR_409_CASE_NOT_RESOLVED = "error.409.moderation_case_not_resolved"

STAPEL_MODERATION_ERRORS = {
    ERR_400_UNKNOWN_TARGET_TYPE: "This kind of target is not moderated here",
    ERR_400_UNKNOWN_REASON: "Unknown report reason",
    ERR_400_REASON_NOT_APPLICABLE: "That reason does not apply to this kind of target",
    ERR_400_DESCRIPTION_REQUIRED: "This reason requires an explanation",
    ERR_400_EVIDENCE_INVALID: "This report's attached evidence is not accepted here",
    ERR_400_OWN_CONTENT: "You cannot report your own content",
    ERR_400_CONTACT_REQUIRED: "An anonymous report must carry a contact address",
    ERR_403_CANNOT_REPORT: "You may not report this target",
    ERR_404_TARGET_NOT_FOUND: "The reported target does not exist",
    ERR_409_ALREADY_REPORTED: "You have already reported this target",
    ERR_400_INVALID_DECISION: "Unknown moderation decision",
    ERR_400_INVALID_TRANSITION: "The case cannot move to that state",
    ERR_403_FORBIDDEN: "You do not have the clearance for this action",
    ERR_404_CASE_NOT_FOUND: "Case not found",
    ERR_409_CASE_CLAIMED: "Another moderator is holding this case",
    ERR_409_CASE_RESOLVED: "This case has already been resolved",
    ERR_409_NOT_CLAIMANT: "This case is claimed by another moderator",
    ERR_503_CONTENT_UNAVAILABLE: "The target's content cannot be read right now",
    ERR_400_INVALID_SANCTION_KIND: "Unknown sanction kind",
    ERR_404_SANCTION_NOT_FOUND: "Sanction not found",
    ERR_409_SANCTION_NOT_ACTIVE: "This sanction is no longer active",
    ERR_400_INVALID_OUTCOME: "Unknown appeal outcome",
    ERR_403_NOT_APPELLANT: "You may not appeal this decision",
    ERR_403_SAME_ACTOR: "An appeal must be decided by a different moderator",
    ERR_404_APPEAL_NOT_FOUND: "Appeal not found",
    ERR_409_ALREADY_APPEALED: "You have already appealed this case",
    ERR_409_APPEAL_RESOLVED: "This appeal has already been decided",
    ERR_409_CASE_NOT_RESOLVED: "Only a resolved case can be appealed",
}

#: What a client can actually DO about each refusal (core's REMEDIATION_VOCAB).
STAPEL_MODERATION_REMEDIATION = {
    ERR_400_UNKNOWN_TARGET_TYPE: "fix_input",
    ERR_400_UNKNOWN_REASON: "fix_input",
    ERR_400_REASON_NOT_APPLICABLE: "fix_input",
    ERR_400_DESCRIPTION_REQUIRED: "fix_input",
    ERR_400_EVIDENCE_INVALID: "fix_input",
    ERR_400_OWN_CONTENT: "verify",
    ERR_400_CONTACT_REQUIRED: "fix_input",
    ERR_403_CANNOT_REPORT: "contact_support",
    ERR_404_TARGET_NOT_FOUND: "verify",
    ERR_409_ALREADY_REPORTED: "verify",
    ERR_400_INVALID_DECISION: "fix_input",
    ERR_400_INVALID_TRANSITION: "verify",
    ERR_403_FORBIDDEN: "contact_support",
    ERR_404_CASE_NOT_FOUND: "verify",
    ERR_409_CASE_CLAIMED: "wait_and_retry",
    ERR_409_CASE_RESOLVED: "verify",
    ERR_409_NOT_CLAIMANT: "verify",
    ERR_503_CONTENT_UNAVAILABLE: "wait_and_retry",
    ERR_400_INVALID_SANCTION_KIND: "fix_input",
    ERR_404_SANCTION_NOT_FOUND: "verify",
    ERR_409_SANCTION_NOT_ACTIVE: "verify",
    ERR_400_INVALID_OUTCOME: "fix_input",
    ERR_403_NOT_APPELLANT: "contact_support",
    ERR_403_SAME_ACTOR: "contact_support",
    ERR_404_APPEAL_NOT_FOUND: "verify",
    ERR_409_ALREADY_APPEALED: "verify",
    ERR_409_APPEAL_RESOLVED: "verify",
    ERR_409_CASE_NOT_RESOLVED: "verify",
}

register_service_errors(
    STAPEL_MODERATION_ERRORS, remediation=STAPEL_MODERATION_REMEDIATION
)


class ModerationErrorKeysView(ErrorKeysView):
    """The error-key listing the stapel-translate collector reads.

    Mounted at ``error-keys/`` (the cdn / workspaces / profiles / forms
    convention). Without it the collector reports this service as having no
    endpoint and its catalogues are never regenerated.
    """

    def get_service_errors(self):
        return STAPEL_MODERATION_ERRORS


__all__ = (
    [name for name in dir() if name.startswith("ERR_")]
    + [
        "STAPEL_MODERATION_ERRORS",
        "STAPEL_MODERATION_REMEDIATION",
        "ModerationErrorKeysView",
    ]
)
