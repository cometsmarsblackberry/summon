"""User-visible explanations for why a reservation ended."""

from typing import Optional

from app.i18n import t
from app.models.reservation import EndReason, ReservationStatus


_MESSAGE_KEYS = {
    EndReason.OWNER: "status.end_reason_owner",
    EndReason.OWNER_IN_GAME: "status.end_reason_in_game",
    EndReason.ADMIN: "status.end_reason_admin",
    EndReason.EXPIRED: "status.end_reason_expired",
    EndReason.SERVER_REMOVED: "status.end_reason_server_removed",
}


def public_end_reason(reservation, auto_end_minutes: int) -> Optional[str]:
    """Return a localized sentence saying why the reservation ended.

    Returns None while the reservation is running, and for reservations that
    ended before reasons were recorded.
    """
    if getattr(reservation, "status", None) != ReservationStatus.ENDED:
        return None

    reason = getattr(reservation, "end_reason", None)
    if reason == EndReason.EMPTY:
        # The empty timer starts when the server is ready, so a server nobody
        # ever joined ends the same way as one everybody left.
        if getattr(reservation, "player_joined", False):
            return t("status.end_reason_empty", minutes=auto_end_minutes)
        return t("status.end_reason_never_joined", minutes=auto_end_minutes)

    key = _MESSAGE_KEYS.get(reason)
    return t(key) if key else None
