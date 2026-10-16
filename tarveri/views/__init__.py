"""
Discord UI Views and Modals for TARVeri.
"""

from tarveri.views.admin_views import MassRevocationApprovalView
from tarveri.views.guest_views import (
    CloseTicketModal,
    GuestApplicationModal,
    GuestReviewThreadView,
    ReferralEntryModal,
    RejectReasonModal,
    StudentVerificationModal,
    VerificationGatewayView,
    VouchModal,
    build_gateway_panel_embed,
    build_review_embed,
    get_admin_role_mention,
    get_admin_role_or_fallback,
    is_admin_or_has_role,
)
from tarveri.views.verification_views import (
    AlumniClaimModal,
    AlumniEmailConfirmationView,
    ExpiryAnomalyConfirmView,
    ExtendExpiryAnomalyConfirmView,
    ExtendExpiryModal,
    FurtherStudyTransitionModal,
    OtpVerificationPromptView,
    ReEnterExpiryModal,
    StudentDropoutConfirmModal,
    StudentLifecycleResolutionView,
    StudentOtpModal,
    VerificationModal,
    build_alumni_email_confirm_embed,
    build_expiry_anomaly_embed,
)

__all__ = [
    # Admin
    "MassRevocationApprovalView",
    # Verification
    "AlumniClaimModal",
    "AlumniEmailConfirmationView",
    "ExpiryAnomalyConfirmView",
    "ExtendExpiryAnomalyConfirmView",
    "ExtendExpiryModal",
    "FurtherStudyTransitionModal",
    "OtpVerificationPromptView",
    "ReEnterExpiryModal",
    "StudentDropoutConfirmModal",
    "StudentLifecycleResolutionView",
    "StudentOtpModal",
    "VerificationModal",
    "build_alumni_email_confirm_embed",
    "build_expiry_anomaly_embed",
    # Guest
    "CloseTicketModal",
    "GuestApplicationModal",
    "GuestReviewThreadView",
    "ReferralEntryModal",
    "RejectReasonModal",
    "StudentVerificationModal",
    "VerificationGatewayView",
    "VouchModal",
    "build_gateway_panel_embed",
    "build_review_embed",
    "get_admin_role_mention",
    "get_admin_role_or_fallback",
    "is_admin_or_has_role",
]
