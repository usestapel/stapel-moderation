"""The single authorization choke point of the moderator surface.

Every staff decision routes through :func:`authorize`; there is no second
read path. The user-facing endpoints (report, appeal, policy) are deliberately
outside it — they are ``IsNotAnonymousUser`` surfaces gated by throttling and
policy callbacks, not by clearance.

**Rights are staff roles plus the core mandate, and nothing else.** No parallel
allow-list. ``stapel-auth`` already owns role assignment (``assign_staff_role``
/ ``revoke_staff_role``, both atomic with outbox facts), the roles ride the JWT
as the ``staff_roles`` claim, and ``stapel_core.access`` computes ``has_perm``
from (model declaration × role clearance) at call time. Legacy's
``ReportModerator`` email allow-list — duplicated in two files, living beside a
real role system — is the exact defect class that gets fixed in the core rather
than patched per module.

The per-app clearance in ``RoleDefinition.apps`` is what makes this workable:

    STAPEL_ACCESS = {"ROLES": {
        "moderator": {"clearance": "low", "apps": {"moderation": "mid"}},
        "ts_lead":   {"clearance": "mid", "apps": {"moderation": "high"}},
    }}

A moderator is ``is_staff`` and holds MID **in the moderation app only** — LOW
everywhere else, so the queue does not come with the billing tables attached.

Step-up (``stapel_core.access.stepup``, MAX_AGE 900, LEVELS ``("high",)``)
applies to the HIGH actions automatically: issuing a ban demands fresh
authentication without a line of code here.

**The workspace door is declared and closed.** ``CAPABILITIES`` names the full
set on day one — the docs rule, so a host's role overlay never has to migrate —
but :func:`authorize` does not ask them until
``STAPEL_MODERATION["WORKSPACE_SCOPED"]`` is True. The reason is measured, not
stylistic: neither ``stapel-listings`` nor ``stapel-reviews`` is workspace-keyed
(zero occurrences), and ``Case.scope_key`` already partitions the queue by an
opaque tenant string, so tenancy works without a capability door.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional
from uuid import UUID

logger = logging.getLogger(__name__)

ALLOW = "allow"
DENY = "deny"
UNAVAILABLE = "unavailable"

#: Action -> (model name, mandate verb). The clearance each one needs is the
#: model's ``@access`` declaration, so the table below is a routing map and
#: never a second copy of the levels.
ACTION_MANDATES = {
    # Reading the queue and a case card: Case view = MID (sensitive) —
    # the card carries complaint text and the complainant's identity.
    "queue.view": ("case", "view"),
    "case.view": ("case", "view"),
    # Claim, release, verdict, rescan: mutations of a sensitive model = HIGH.
    "case.claim": ("case", "change"),
    "case.resolve": ("case", "change"),
    "case.rescan": ("case", "change"),
    # Sanctions mirror StaffRoleAssignment: add/change = HIGH.
    "sanction.view": ("sanction", "view"),
    "sanction.issue": ("sanction", "add"),
    "sanction.lift": ("sanction", "change"),
    # The audit log is @access.ops: view HIGH, every mutation FORBIDDEN.
    "audit.view": ("caseevent", "view"),
    # Deciding an appeal changes the case it reopens.
    "appeal.view": ("appeal", "view"),
    "appeal.resolve": ("case", "change"),
}

#: Declared in full on day 1 so host role overlays never have to migrate,
#: even though ``authorize`` does not consult them while WORKSPACE_SCOPED is
#: False. Declaring the closed door is what keeps it a door rather than a
#: future rewrite.
CAPABILITIES = (
    "moderation.review",
    "moderation.sanction",
    "moderation.appeal.resolve",
    "moderation.policy.manage",
)

#: Action -> the workspace capability that would answer it if the door opened.
ACTION_CAPABILITIES = {
    "queue.view": "moderation.review",
    "case.view": "moderation.review",
    "case.claim": "moderation.review",
    "case.resolve": "moderation.review",
    "case.rescan": "moderation.review",
    "sanction.view": "moderation.sanction",
    "sanction.issue": "moderation.sanction",
    "sanction.lift": "moderation.sanction",
    "audit.view": "moderation.review",
    "appeal.view": "moderation.appeal.resolve",
    "appeal.resolve": "moderation.appeal.resolve",
}


@dataclass(frozen=True)
class Principal:
    """Who is asking. Built by the view layer, consumed only here.

    Fixed on day 1 so that an anonymous-report grant later is an additive
    branch rather than a rewrite: ``user_id=None`` means no session at all,
    ``is_anonymous`` marks an anonymous ACCOUNT of the auth axis (which does
    have a user_id).
    """

    user_id: Optional[UUID]
    is_staff: bool = False
    is_superuser: bool = False
    is_anonymous: bool = False
    user: object = None

    @classmethod
    def from_request(cls, request) -> "Principal":
        user = getattr(request, "user", None)
        authenticated = bool(getattr(user, "is_authenticated", False))
        return cls(
            user_id=getattr(user, "pk", None) if authenticated else None,
            is_staff=bool(getattr(user, "is_staff", False)),
            is_superuser=bool(getattr(user, "is_superuser", False)),
            is_anonymous=bool(getattr(user, "is_anonymous_account", False)),
            user=user if authenticated else None,
        )


def authorize(*, principal: Principal, action: str, case=None) -> str:
    """Decide *action* for *principal*. Returns ``allow`` | ``deny``.

    ``unavailable`` is in the vocabulary because the workspace branch will
    need it (a capability service that renders no verdict must answer 503, not
    403 — "a routing 404 is not a verdict"). The mandate branch never returns
    it: the roles are already on the request as a JWT claim, so there is no
    remote party that can be down.
    """
    if action not in ACTION_MANDATES:
        raise ValueError(f"unknown moderation action: {action!r}")
    if principal.user_id is None:
        return DENY

    from .conf import moderation_settings

    if moderation_settings.WORKSPACE_SCOPED:
        # The declared, closed door. Left as an explicit refusal rather than
        # a silent fall-through so that flipping the switch without building
        # the branch fails loudly instead of granting everything.
        logger.error(
            "STAPEL_MODERATION['WORKSPACE_SCOPED'] is on but the workspace "
            "branch is not built in this version — denying %s",
            action,
        )
        return DENY

    model_name, verb = ACTION_MANDATES[action]
    user = principal.user
    if user is None:
        return DENY
    if user.has_perm(f"moderation.{verb}_{model_name}"):
        return ALLOW
    return DENY


class HasModerationMandate:
    """DRF permission asking :func:`authorize` for one declared action.

    Used as ``permission_classes = [HasModerationMandate.for_action("case.view")]``
    so the action a view needs is visible in ``urls_v1.py``-adjacent code
    rather than buried in a method body.
    """

    action = "queue.view"

    def __init__(self, action: Optional[str] = None):
        if action:
            self.action = action

    @classmethod
    def for_action(cls, action: str):
        if action not in ACTION_MANDATES:
            raise ValueError(f"unknown moderation action: {action!r}")
        return type(
            f"HasModerationMandate_{action.replace('.', '_')}",
            (cls,),
            {"action": action},
        )

    def has_permission(self, request, view) -> bool:
        action = getattr(view, "mandate_action", None) or self.action
        return authorize(principal=Principal.from_request(request), action=action) == ALLOW

    def has_object_permission(self, request, view, obj) -> bool:
        return self.has_permission(request, view)


class NotSanctioned:
    """DRF permission a HOST hangs on its own write views.

    This is the enforcement half moderation deliberately does NOT do itself
    (spec §9.4): the module answers "is this user sanctioned", the host's own
    endpoint refuses the action. Gating publication inside ``stapel-listings``
    would be a decision about the listings API, and that belongs to its owner.

    Fails OPEN when the moderation module cannot answer, and the choice is
    argued rather than assumed: the sanction that matters most (a suspension)
    has already killed the user's session through the blacklist, so a
    moderation outage that also blocked every unsanctioned user's writes would
    trade a small enforcement gap for a total outage of the host's product.

    Usage::

        class ListingCreateView(APIView):
            permission_classes = [IsNotAnonymousUser, NotSanctioned("listing")]
    """

    def __init__(self, scope: str = ""):
        self.scope = scope

    def has_permission(self, request, view) -> bool:
        from stapel_core.comm import CommError, call

        user = getattr(request, "user", None)
        if not getattr(user, "is_authenticated", False):
            return True
        payload = {"user_id": str(user.pk)}
        if self.scope:
            payload["scope"] = self.scope
        try:
            answer = call("moderation.check_sanctions", payload) or {}
        except (CommError, LookupError):
            logger.warning("moderation.check_sanctions unreachable — allowing the write")
            return True
        return bool(answer.get("allowed", True))

    def has_object_permission(self, request, view, obj) -> bool:
        return self.has_permission(request, view)


__all__ = [
    "ACTION_CAPABILITIES",
    "ACTION_MANDATES",
    "ALLOW",
    "CAPABILITIES",
    "DENY",
    "UNAVAILABLE",
    "HasModerationMandate",
    "NotSanctioned",
    "Principal",
    "authorize",
]
