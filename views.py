"""DRF views for stapel-moderation.

Two surfaces with different postures live here, and the split is the point:

- the **user** surface (report, my reports, appeal, my appeals, policy) is
  ``IsNotAnonymousUser`` — except ``/policy``, which is deliberately public,
  because a disclosure only readable by people with accounts is not a
  disclosure. Its gates are throttling and the type's ``can_report`` policy;
- the **moderator** surface is the staff mandate, one declared action per
  view, resolved by the single choke point in ``authz.authorize``.

Presenter-canonical (§55): a view resolves its presenter through
``presenters.present_*`` and returns ``StapelResponse(Serializer(...).data)``
— it never instantiates a ``dto.py`` dataclass itself (SWAP002) and never
imports a concrete presenter class (SWAP001).
"""
from __future__ import annotations

import functools

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import permissions
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from stapel_core.django.api.errors import StapelErrorResponse, StapelResponse
from stapel_core.django.api.permissions import (
    ANONYMOUS_ALLOWED,
    ANONYMOUS_DENIED,
    IsNotAnonymousUser,
)
from stapel_core.django.api.views import SerializerSeamMixin

from . import presenters, services
from .authz import HasModerationMandate
from .conf import moderation_settings
from .errors import (
    ERR_400_CONTACT_REQUIRED,
    ERR_400_DESCRIPTION_REQUIRED,
    ERR_400_EVIDENCE_INVALID,
    ERR_400_INVALID_DECISION,
    ERR_400_INVALID_OUTCOME,
    ERR_400_INVALID_SANCTION_KIND,
    ERR_400_INVALID_TRANSITION,
    ERR_400_OWN_CONTENT,
    ERR_400_UNKNOWN_REASON,
    ERR_400_UNKNOWN_TARGET_TYPE,
    ERR_403_CANNOT_REPORT,
    ERR_403_NOT_APPELLANT,
    ERR_403_SAME_ACTOR,
    ERR_404_APPEAL_NOT_FOUND,
    ERR_404_CASE_NOT_FOUND,
    ERR_404_SANCTION_NOT_FOUND,
    ERR_404_TARGET_NOT_FOUND,
    ERR_409_ALREADY_APPEALED,
    ERR_409_ALREADY_REPORTED,
    ERR_409_CASE_CLAIMED,
    ERR_409_CASE_NOT_RESOLVED,
    ERR_409_CASE_RESOLVED,
    ERR_409_SANCTION_NOT_ACTIVE,
    ERR_503_CONTENT_UNAVAILABLE,
)
from .models import Appeal, Case, Sanction
from .registry import UnknownReason, UnknownTargetType
from .serializers import (
    AppealCreateSerializer,
    AppealQuerySerializer,
    AppealResolveSerializer,
    AppealSerializer,
    CaseDetailSerializer,
    CaseEventSerializer,
    CaseQuerySerializer,
    CaseSerializer,
    ContentResponseSerializer,
    KeysetQuerySerializer,
    PolicyDisclosureResponseSerializer,
    PolicyQuerySerializer,
    ReportCreateSerializer,
    ReportResultResponseSerializer,
    ReportSerializer,
    RescanResultResponseSerializer,
    SanctionCreateSerializer,
    SanctionLiftSerializer,
    SanctionQuerySerializer,
    SanctionSerializer,
    StatsResponseSerializer,
    VerdictRequestSerializer,
    VerdictSerializer,
)


class ReportThrottle(ScopedRateThrottle):
    """Report-endpoint throttle whose rate comes from THIS module's namespace.

    DRF resolves scoped rates from the global ``DEFAULT_THROTTLE_RATES``, which
    a library cannot own — so the rate is read from ``STAPEL_MODERATION``
    instead (the workspaces / geo / forms canon). ``None`` disables it: a
    conscious act, never the default.
    """

    scope = "moderation_report"

    def get_rate(self):
        return moderation_settings.REPORT_THROTTLE

    def allow_request(self, request, view):
        if not self.get_rate():
            return True
        return super().allow_request(request, view)


def _maps_errors(handler):
    """Translate the service layer's refusals into the error catalogue.

    One place, so a new endpoint cannot invent a second spelling of "already
    reported" — the legacy defect where the same condition answered 400 in one
    view and 409 in another.
    """

    @functools.wraps(handler)
    def wrapper(self, request, *args, **kwargs):
        try:
            return handler(self, request, *args, **kwargs)
        except UnknownTargetType:
            return StapelErrorResponse(400, ERR_400_UNKNOWN_TARGET_TYPE)
        except UnknownReason:
            return StapelErrorResponse(400, ERR_400_UNKNOWN_REASON)
        except services.OwnContent:
            return StapelErrorResponse(400, ERR_400_OWN_CONTENT)
        except services.CannotReport:
            return StapelErrorResponse(403, ERR_403_CANNOT_REPORT)
        except services.AlreadyReported:
            return StapelErrorResponse(409, ERR_409_ALREADY_REPORTED)
        except services.TargetNotFound:
            return StapelErrorResponse(404, ERR_404_TARGET_NOT_FOUND)
        except services.ContentUnavailable:
            return StapelErrorResponse(503, ERR_503_CONTENT_UNAVAILABLE)
        except services.CaseAlreadyResolved:
            return StapelErrorResponse(409, ERR_409_CASE_RESOLVED)
        except services.CaseClaimedByAnother:
            return StapelErrorResponse(409, ERR_409_CASE_CLAIMED)
        except services.InvalidDecision:
            return StapelErrorResponse(400, ERR_400_INVALID_DECISION)
        except services.InvalidSanctionKind:
            return StapelErrorResponse(400, ERR_400_INVALID_SANCTION_KIND)
        except services.SanctionNotActive:
            return StapelErrorResponse(409, ERR_409_SANCTION_NOT_ACTIVE)
        except services.SameActor:
            return StapelErrorResponse(403, ERR_403_SAME_ACTOR)
        except services.AppealNotAllowed as exc:
            reason = str(exc)
            if reason == "already_appealed":
                return StapelErrorResponse(409, ERR_409_ALREADY_APPEALED)
            if reason == "case_not_resolved":
                return StapelErrorResponse(409, ERR_409_CASE_NOT_RESOLVED)
            return StapelErrorResponse(400, ERR_400_INVALID_OUTCOME)
        except services.InvalidTransition:
            return StapelErrorResponse(400, ERR_400_INVALID_TRANSITION)
        except ValueError as exc:
            if str(exc) == "description_required":
                return StapelErrorResponse(400, ERR_400_DESCRIPTION_REQUIRED)
            if str(exc) == "evidence_invalid":
                return StapelErrorResponse(400, ERR_400_EVIDENCE_INVALID)
            raise

    return wrapper


def _page_cursor(rows):
    """Continuation anchor for a keyset page: the last row's ``created_at``."""
    return rows[-1].created_at if rows else None


# ─────────────────────────────────────────────────────────────────────
# User surface
# ─────────────────────────────────────────────────────────────────────


@extend_schema(tags=["Moderation / reports"])
class ReportListCreateView(SerializerSeamMixin, APIView):
    """File a complaint, or list the ones you filed."""

    permission_classes = [IsNotAnonymousUser]
    throttle_classes = [ReportThrottle]
    stapel_anonymous_access = ANONYMOUS_DENIED
    request_serializer_class = ReportCreateSerializer
    response_serializer_class = ReportResultResponseSerializer

    @extend_schema(
        request=ReportCreateSerializer,
        responses={201: ReportResultResponseSerializer},
    )
    @_maps_errors
    def post(self, request):
        payload = self.get_request_serializer_class()(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data

        if moderation_settings.ALLOW_ANONYMOUS_REPORTS and not data.get("contact_email"):
            # DSA Art. 16(2)(c) wants a contact for the submitter. With the
            # anonymous switch open there is no account to fall back on, so
            # the address becomes mandatory rather than optional.
            return StapelErrorResponse(400, ERR_400_CONTACT_REQUIRED)

        report, _case = services.submit_report(
            target_type=data["target_type"],
            target_key=data["target_key"],
            reporter_id=request.user.pk,
            reason_code=data["reason_code"],
            description=data.get("description") or "",
            good_faith=bool(data.get("good_faith")),
            contact_email=data.get("contact_email") or "",
            scope_key=data.get("scope_key") or "",
            evidence=data.get("evidence") or {},
        )
        return StapelResponse(
            self.get_response_serializer_class()(
                presenters.present_report_result(report)
            ).data,
            status=201,
        )

    @extend_schema(responses={200: ReportSerializer(many=True)})
    def get(self, request):
        query = KeysetQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        rows = services.list_reports(
            reporter_id=request.user.pk,
            before=query.validated_data.get("before"),
            limit=query.validated_data.get("limit"),
        )
        return StapelResponse(
            ReportSerializer(
                [presenters.present_report(row) for row in rows], many=True
            ).data
        )


@extend_schema(tags=["Moderation / policy"])
class PolicyDisclosureView(SerializerSeamMixin, APIView):
    """DSA Art. 15(1)(e) — how this deployment moderates, generated from code.

    Public on purpose: a transparency disclosure that requires an account is
    not one. Nothing here is per-user, and everything in it is derivable from
    the registries and settings anyway.
    """

    permission_classes = [permissions.AllowAny]
    authentication_classes = []
    stapel_anonymous_access = ANONYMOUS_ALLOWED
    response_serializer_class = PolicyDisclosureResponseSerializer

    @extend_schema(
        parameters=[
            OpenApiParameter("lang", str, description="BCP-47 tag (annotation only)."),
            OpenApiParameter("target_type", str, description="Narrow to one target type."),
        ],
        responses={200: PolicyDisclosureResponseSerializer},
    )
    def get(self, request):
        query = PolicyQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        disclosure = services.policy_disclosure(
            lang=query.validated_data.get("lang") or "",
            target_type=query.validated_data.get("target_type") or "",
        )
        return StapelResponse(
            self.get_response_serializer_class()(
                presenters.present_policy_disclosure(disclosure)
            ).data
        )


@extend_schema(tags=["Moderation / appeals"])
class AppealListCreateView(SerializerSeamMixin, APIView):
    """Appeal a decision about your content, or list your appeals."""

    permission_classes = [IsNotAnonymousUser]
    stapel_anonymous_access = ANONYMOUS_DENIED
    request_serializer_class = AppealCreateSerializer
    response_serializer_class = AppealSerializer

    @extend_schema(request=AppealCreateSerializer, responses={201: AppealSerializer})
    @_maps_errors
    def post(self, request):
        payload = self.get_request_serializer_class()(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data

        case = Case.objects.filter(pk=data["case_id"]).first()
        if case is None:
            return StapelErrorResponse(404, ERR_404_CASE_NOT_FOUND)
        # Only the subject of the decision may appeal it. Anyone else asking
        # gets 403 rather than 404: the case id came from a notification we
        # sent, so pretending it does not exist would be theatre.
        if str(case.subject_user_id or "") != str(request.user.pk):
            return StapelErrorResponse(403, ERR_403_NOT_APPELLANT)

        sanction = None
        if data.get("sanction_id"):
            sanction = Sanction.objects.filter(
                pk=data["sanction_id"], subject_user_id=request.user.pk
            ).first()
            if sanction is None:
                return StapelErrorResponse(404, ERR_404_SANCTION_NOT_FOUND)

        appeal = services.open_appeal(
            case, appellant_id=request.user.pk, body=data["body"], sanction=sanction
        )
        return StapelResponse(
            self.get_response_serializer_class()(presenters.present_appeal(appeal)).data,
            status=201,
        )

    @extend_schema(responses={200: AppealSerializer(many=True)})
    def get(self, request):
        query = KeysetQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        rows = services.list_appeals(
            appellant_id=request.user.pk,
            before=query.validated_data.get("before"),
            limit=query.validated_data.get("limit"),
        )
        return StapelResponse(
            AppealSerializer(
                [presenters.present_appeal(row) for row in rows], many=True
            ).data
        )


# ─────────────────────────────────────────────────────────────────────
# Moderator surface
# ─────────────────────────────────────────────────────────────────────


@extend_schema(tags=["Moderation / queue"])
class CaseListView(SerializerSeamMixin, APIView):
    """One keyset page of the cross-target queue."""

    mandate_action = "queue.view"
    permission_classes = [HasModerationMandate.for_action("queue.view")]
    stapel_anonymous_access = ANONYMOUS_DENIED
    response_serializer_class = CaseSerializer

    @extend_schema(responses={200: CaseSerializer(many=True)})
    def get(self, request):
        query = CaseQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        data = query.validated_data
        rows = services.list_cases(
            state=data.get("state") or "",
            target_type=data.get("target_type") or "",
            reason_code=data.get("reason_code") or "",
            severity_min=data.get("severity_min"),
            scope_key=data.get("scope_key") or "",
            subject_user_id=data.get("subject_user_id"),
            before=data.get("before"),
            limit=data.get("limit"),
        )
        response = StapelResponse(
            self.get_response_serializer_class()(
                [presenters.present_case(row) for row in rows], many=True
            ).data
        )
        cursor = _page_cursor(rows)
        if cursor:
            response["X-Moderation-Next-Before"] = cursor.isoformat()
        return response


@extend_schema(tags=["Moderation / queue"])
class CaseDetailView(SerializerSeamMixin, APIView):
    """One case card, with the target's content read live."""

    mandate_action = "case.view"
    permission_classes = [HasModerationMandate.for_action("case.view")]
    stapel_anonymous_access = ANONYMOUS_DENIED
    response_serializer_class = CaseDetailSerializer

    @extend_schema(responses={200: CaseDetailSerializer})
    def get(self, request, case_id):
        case = (
            Case.objects.filter(pk=case_id)
            .prefetch_related("reports", "verdicts", "sanctions", "appeals")
            .first()
        )
        if case is None:
            return StapelErrorResponse(404, ERR_404_CASE_NOT_FOUND)

        body = self.get_response_serializer_class()(
            presenters.present_case_detail(case)
        ).data
        # The content read can fail, and a failed read is a RENDERED state,
        # not a failed request: the console shows the `failed` branch with the
        # reason. A moderator must never be handed an empty card that looks
        # like empty content.
        body["content"] = ContentResponseSerializer(_case_content(case)).data
        return StapelResponse(body)


def _case_content(case):
    from .registry import check_can_view_content, resolve_policy_lenient

    policy = resolve_policy_lenient(case.target_type)
    if not policy["content_function"] and not policy["evidence"]:
        return presenters.present_content(
            None, available=False, error="no_content_function"
        )
    try:
        if not check_can_view_content(
            policy,
            actor_id=None,
            target_type=case.target_type,
            target_key=case.target_key,
        ):
            return presenters.present_content(None, available=False, error="forbidden")
        content = services.fetch_content(case.target_type, case.target_key, policy=policy)
    except services.TargetNotFound:
        return presenters.present_content(None, available=False, error="target_not_found")
    except services.ContentUnavailable as exc:
        return presenters.present_content(None, available=False, error=str(exc)[:200])
    return presenters.present_content(content)


@extend_schema(tags=["Moderation / queue"])
class CaseClaimView(SerializerSeamMixin, APIView):
    """Take a lease on a case."""

    mandate_action = "case.claim"
    permission_classes = [HasModerationMandate.for_action("case.claim")]
    stapel_anonymous_access = ANONYMOUS_DENIED
    response_serializer_class = CaseSerializer

    @extend_schema(request=None, responses={200: CaseSerializer})
    @_maps_errors
    def post(self, request, case_id):
        case = Case.objects.filter(pk=case_id).first()
        if case is None:
            return StapelErrorResponse(404, ERR_404_CASE_NOT_FOUND)
        case = services.claim_case(case, actor_id=request.user.pk)
        return StapelResponse(
            self.get_response_serializer_class()(presenters.present_case(case)).data
        )


@extend_schema(tags=["Moderation / queue"])
class CaseReleaseView(SerializerSeamMixin, APIView):
    """Hand a claimed case back to the queue."""

    mandate_action = "case.claim"
    permission_classes = [HasModerationMandate.for_action("case.claim")]
    stapel_anonymous_access = ANONYMOUS_DENIED
    response_serializer_class = CaseSerializer

    @extend_schema(request=None, responses={200: CaseSerializer})
    @_maps_errors
    def post(self, request, case_id):
        case = Case.objects.filter(pk=case_id).first()
        if case is None:
            return StapelErrorResponse(404, ERR_404_CASE_NOT_FOUND)
        case = services.release_case(case, actor_id=request.user.pk)
        return StapelResponse(
            self.get_response_serializer_class()(presenters.present_case(case)).data
        )


@extend_schema(tags=["Moderation / queue"])
class CaseVerdictView(SerializerSeamMixin, APIView):
    """Decide a case — and, in the same act, sanction its author.

    "Take it down, suspend the seller, tell them why" is one request and one
    transaction. In legacy it was three screens, two of which wrote nothing to
    the audit log.
    """

    mandate_action = "case.resolve"
    permission_classes = [HasModerationMandate.for_action("case.resolve")]
    stapel_anonymous_access = ANONYMOUS_DENIED
    request_serializer_class = VerdictRequestSerializer
    response_serializer_class = VerdictSerializer

    @extend_schema(request=VerdictRequestSerializer, responses={201: VerdictSerializer})
    @_maps_errors
    def post(self, request, case_id):
        payload = self.get_request_serializer_class()(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data

        case = Case.objects.filter(pk=case_id).first()
        if case is None:
            return StapelErrorResponse(404, ERR_404_CASE_NOT_FOUND)

        verdict = services.resolve_case(
            case,
            decision=data["decision"],
            reason_code=data.get("reason_code") or "",
            note=data.get("note") or "",
            actor_id=request.user.pk,
            sanction=data.get("sanction") or None,
        )
        return StapelResponse(
            self.get_response_serializer_class()(
                presenters.present_verdict(verdict)
            ).data,
            status=201,
        )


@extend_schema(tags=["Moderation / queue"])
class CaseRescanView(SerializerSeamMixin, APIView):
    """Send a case back through the automatic screener."""

    mandate_action = "case.rescan"
    permission_classes = [HasModerationMandate.for_action("case.rescan")]
    stapel_anonymous_access = ANONYMOUS_DENIED
    response_serializer_class = RescanResultResponseSerializer

    @extend_schema(request=None, responses={202: RescanResultResponseSerializer})
    @_maps_errors
    def post(self, request, case_id):
        case = Case.objects.filter(pk=case_id).first()
        if case is None:
            return StapelErrorResponse(404, ERR_404_CASE_NOT_FOUND)
        task_id = services.rescan_case(case, actor_id=request.user.pk)
        case.refresh_from_db()
        return StapelResponse(
            self.get_response_serializer_class()(
                presenters.present_rescan_result(case, task_id)
            ).data,
            status=202,
        )


@extend_schema(tags=["Moderation / queue"])
class CaseEventsView(SerializerSeamMixin, APIView):
    """The append-only audit trail of one case. Read-only, by declaration."""

    mandate_action = "audit.view"
    permission_classes = [HasModerationMandate.for_action("audit.view")]
    stapel_anonymous_access = ANONYMOUS_DENIED
    response_serializer_class = CaseEventSerializer

    @extend_schema(responses={200: CaseEventSerializer(many=True)})
    def get(self, request, case_id):
        case = Case.objects.filter(pk=case_id).first()
        if case is None:
            return StapelErrorResponse(404, ERR_404_CASE_NOT_FOUND)
        rows = case.events.order_by("created_at")
        return StapelResponse(
            self.get_response_serializer_class()(
                [presenters.present_event(row) for row in rows], many=True
            ).data
        )


@extend_schema(tags=["Moderation / queue"])
class StatsView(SerializerSeamMixin, APIView):
    """Queue counters — the console header and DSA Art. 24(1) reporting."""

    mandate_action = "queue.view"
    permission_classes = [HasModerationMandate.for_action("queue.view")]
    stapel_anonymous_access = ANONYMOUS_DENIED
    response_serializer_class = StatsResponseSerializer

    @extend_schema(responses={200: StatsResponseSerializer})
    def get(self, request):
        return StapelResponse(
            self.get_response_serializer_class()(
                presenters.present_stats(services.queue_stats())
            ).data
        )


@extend_schema(tags=["Moderation / sanctions"])
class SanctionListCreateView(SerializerSeamMixin, APIView):
    """List sanctions, or issue one outside a verdict."""

    mandate_action = "sanction.view"
    permission_classes = [HasModerationMandate.for_action("sanction.view")]
    stapel_anonymous_access = ANONYMOUS_DENIED
    request_serializer_class = SanctionCreateSerializer
    response_serializer_class = SanctionSerializer

    @extend_schema(responses={200: SanctionSerializer(many=True)})
    def get(self, request):
        query = SanctionQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        data = query.validated_data
        rows = services.list_sanctions(
            subject_user_id=data.get("subject_user_id"),
            state=data.get("state") or "",
            before=data.get("before"),
            limit=data.get("limit"),
        )
        return StapelResponse(
            self.get_response_serializer_class()(
                [presenters.present_sanction(row) for row in rows], many=True
            ).data
        )

    @extend_schema(request=SanctionCreateSerializer, responses={201: SanctionSerializer})
    @_maps_errors
    def post(self, request):
        from .authz import ALLOW, Principal, authorize

        # Issuing is a different clearance from listing (add=HIGH vs
        # view=MID), so the second question is asked here rather than by
        # widening the view's permission class to the stricter of the two.
        if authorize(principal=Principal.from_request(request), action="sanction.issue") != ALLOW:
            return StapelErrorResponse(403, "error.403.moderation_forbidden")

        payload = self.get_request_serializer_class()(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data

        case = None
        if data.get("case_id"):
            case = Case.objects.filter(pk=data["case_id"]).first()
            if case is None:
                return StapelErrorResponse(404, ERR_404_CASE_NOT_FOUND)

        sanction = services.issue_standalone_sanction(
            subject_user_id=data["subject_user_id"],
            kind=data["kind"],
            reason_code=data.get("reason_code") or "",
            note=data.get("note") or "",
            duration_seconds=data.get("duration_seconds"),
            scope=data.get("scope") or "*",
            issued_by=request.user.pk,
            case=case,
            target_type=data.get("target_type") or "",
            target_key=data.get("target_key") or "",
        )
        return StapelResponse(
            self.get_response_serializer_class()(
                presenters.present_sanction(sanction)
            ).data,
            status=201,
        )


@extend_schema(tags=["Moderation / sanctions"])
class SanctionLiftView(SerializerSeamMixin, APIView):
    """Revoke an active sanction."""

    mandate_action = "sanction.lift"
    permission_classes = [HasModerationMandate.for_action("sanction.lift")]
    stapel_anonymous_access = ANONYMOUS_DENIED
    request_serializer_class = SanctionLiftSerializer
    response_serializer_class = SanctionSerializer

    @extend_schema(request=SanctionLiftSerializer, responses={200: SanctionSerializer})
    @_maps_errors
    def post(self, request, sanction_id):
        payload = self.get_request_serializer_class()(data=request.data or {})
        payload.is_valid(raise_exception=True)
        sanction = Sanction.objects.filter(pk=sanction_id).first()
        if sanction is None:
            return StapelErrorResponse(404, ERR_404_SANCTION_NOT_FOUND)
        sanction = services.lift_sanction(
            sanction,
            actor_id=request.user.pk,
            note=payload.validated_data.get("note") or "",
        )
        return StapelResponse(
            self.get_response_serializer_class()(
                presenters.present_sanction(sanction)
            ).data
        )


@extend_schema(tags=["Moderation / appeals"])
class AppealQueueView(SerializerSeamMixin, APIView):
    """The appeals a moderator has to decide."""

    mandate_action = "appeal.view"
    permission_classes = [HasModerationMandate.for_action("appeal.view")]
    stapel_anonymous_access = ANONYMOUS_DENIED
    response_serializer_class = AppealSerializer

    @extend_schema(responses={200: AppealSerializer(many=True)})
    def get(self, request):
        query = AppealQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        data = query.validated_data
        rows = services.list_appeals(
            state=data.get("state") or "",
            before=data.get("before"),
            limit=data.get("limit"),
        )
        return StapelResponse(
            self.get_response_serializer_class()(
                [presenters.present_appeal(row) for row in rows], many=True
            ).data
        )


@extend_schema(tags=["Moderation / appeals"])
class AppealResolveView(SerializerSeamMixin, APIView):
    """Decide an appeal. An overturn actually reopens and re-decides the case."""

    mandate_action = "appeal.resolve"
    permission_classes = [HasModerationMandate.for_action("appeal.resolve")]
    stapel_anonymous_access = ANONYMOUS_DENIED
    request_serializer_class = AppealResolveSerializer
    response_serializer_class = AppealSerializer

    @extend_schema(request=AppealResolveSerializer, responses={200: AppealSerializer})
    @_maps_errors
    def post(self, request, appeal_id):
        payload = self.get_request_serializer_class()(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data

        appeal = Appeal.objects.select_related("case", "sanction").filter(pk=appeal_id).first()
        if appeal is None:
            return StapelErrorResponse(404, ERR_404_APPEAL_NOT_FOUND)
        appeal = services.resolve_appeal(
            appeal,
            outcome=data["outcome"],
            actor_id=request.user.pk,
            note=data.get("note") or "",
            reason_code=data.get("reason_code") or "",
        )
        return StapelResponse(
            self.get_response_serializer_class()(presenters.present_appeal(appeal)).data
        )


__all__ = [
    "AppealListCreateView",
    "AppealQueueView",
    "AppealResolveView",
    "CaseClaimView",
    "CaseDetailView",
    "CaseEventsView",
    "CaseListView",
    "CaseReleaseView",
    "CaseRescanView",
    "CaseVerdictView",
    "PolicyDisclosureView",
    "ReportListCreateView",
    "ReportThrottle",
    "SanctionListCreateView",
    "SanctionLiftView",
    "SerializerSeamMixin",
    "StatsView",
]
