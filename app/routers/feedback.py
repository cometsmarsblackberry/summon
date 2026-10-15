"""User feedback API."""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.i18n import t
from app.models.feedback import Feedback
from app.models.user import User
from app.routers.auth import require_user
from app.services.feedback import (
    FEEDBACK_CATEGORIES,
    FEEDBACK_PER_HOUR,
    MAX_MESSAGE_LENGTH,
    build_reservation_context,
    build_webhook_payload,
    count_recent_feedback,
    get_reservation_for_feedback,
    send_feedback_webhook,
)


router = APIRouter(tags=["feedback"])
settings = get_settings()


class FeedbackRequest(BaseModel):
    """Request body for submitting feedback.

    Fields default to empty so missing values produce the translated
    validation errors below instead of a raw 422 response.
    """
    # Bounded to SQLite's INTEGER range; the form only sends real IDs.
    reservation_id: int | None = Field(None, ge=1, lt=2**63)
    category: str = ""
    message: str = ""


@router.post("/api/feedback")
async def submit_feedback(
    body: FeedbackRequest,
    background_tasks: BackgroundTasks,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """Store feedback, optionally about one of the user's reservations."""
    if body.category not in FEEDBACK_CATEGORIES:
        raise HTTPException(status_code=400, detail=t("errors.feedback_invalid_category"))
    message = body.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail=t("errors.feedback_message_required"))
    if len(message) > MAX_MESSAGE_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=t("errors.feedback_message_too_long", max=MAX_MESSAGE_LENGTH),
        )
    if await count_recent_feedback(user, db) >= FEEDBACK_PER_HOUR:
        raise HTTPException(status_code=429, detail=t("errors.feedback_rate_limited"))

    reservation = None
    context = None
    if body.reservation_id is not None:
        reservation = await get_reservation_for_feedback(body.reservation_id, db)
        # Owners only, admins included: feedback speaks for the reservation owner.
        if reservation is None or reservation.user_id != user.id:
            raise HTTPException(status_code=404, detail=t("errors.reservation_not_found"))
        context = await build_reservation_context(reservation, db)

    feedback = Feedback(
        user_id=user.id,
        reservation_id=reservation.id if reservation else None,
        category=body.category,
        message=message,
        context=context,
        status="new",
    )
    db.add(feedback)
    await db.commit()

    if settings.discord_feedback_webhook_configured:
        background_tasks.add_task(
            send_feedback_webhook,
            build_webhook_payload(
                category=body.category,
                message=message,
                user=user,
                reservation_number=reservation.reservation_number if reservation else None,
                context=context,
            ),
        )

    return {"id": feedback.id}
