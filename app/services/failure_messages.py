"""Helpers for safe user-visible reservation failure messages."""

from typing import Optional

from app.config import get_settings
from app.i18n import t
from app.models.reservation import ReservationStatus


def public_failure_reason(
    status: ReservationStatus,
    provision_attempts: int,
    failure_reason: Optional[str] = None,
) -> Optional[str]:
    """Return a sanitized failure message safe to show in the UI/API."""
    if status != ReservationStatus.FAILED:
        return failure_reason

    if provision_attempts >= get_settings().max_provision_attempts:
        return t("errors.provision_failed", attempts=provision_attempts)

    return t("status.provision_error")
