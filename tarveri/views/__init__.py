"""
Discord UI Views and Modals for TARVeri.
"""

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
]
