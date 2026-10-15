"""User feedback: limits, reservation context snapshots and Discord notifications."""

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.i18n import translate
from app.models.feedback import Feedback
from app.models.instance import EnabledLocation
from app.models.instant import InstantAssignment, InstantSlot
from app.models.reservation import Reservation, RuntimeKind
from app.models.user import User


logger = logging.getLogger(__name__)
settings = get_settings()

FEEDBACK_CATEGORIES = (
    "lag",
    "connection",
    "config",
    "map",
    "provisioning",
    "website",
    "suggestion",
    "other",
)
FEEDBACK_STATUSES = ("new", "resolved")
MAX_MESSAGE_LENGTH = 2000
FEEDBACK_PER_HOUR = 5

# Discord embed limits
_DISCORD_TITLE_MAX = 256
_DISCORD_DESCRIPTION_MAX = 4096
_DISCORD_FIELD_MAX = 1024

_MARKDOWN_RE = re.compile(r"([\\*_~`|\[\]()<>#])")


async def count_recent_feedback(user: User, db: AsyncSession) -> int:
    """Count the user's feedback submissions in the last hour."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    result = await db.execute(
        select(func.count(Feedback.id))
        .where(Feedback.user_id == user.id)
        .where(Feedback.created_at >= cutoff)
    )
    return result.scalar_one()


async def get_reservation_for_feedback(
    reservation_id: int,
    db: AsyncSession,
) -> Reservation | None:
    """Load a reservation with the relationships build_reservation_context reads."""
    result = await db.execute(
        select(Reservation)
        .where(Reservation.id == reservation_id)
        .options(
            selectinload(Reservation.cloud_instance),
            selectinload(Reservation.instant_assignments)
            .selectinload(InstantAssignment.slot)
            .selectinload(InstantSlot.host),
        )
    )
    return result.scalar_one_or_none()


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


async def build_reservation_context(
    reservation: Reservation,
    db: AsyncSession,
) -> dict[str, Any]:
    """Snapshot the technical details admins need to act on a report.

    The reservation must be loaded with get_reservation_for_feedback.
    """
    location = await db.get(EnabledLocation, reservation.location)
    runtime_kind = getattr(reservation.runtime_kind, "value", reservation.runtime_kind)
    context: dict[str, Any] = {
        "reservation_number": reservation.reservation_number,
        "status": reservation.status.value,
        "runtime_kind": runtime_kind,
        "location": reservation.location,
        "location_city": location.city if location else None,
        "first_map": reservation.first_map,
        "current_map": reservation.current_map,
        "config_file": reservation.config_file,
        "enable_direct_connect": bool(reservation.enable_direct_connect),
        "started_at": _iso(reservation.started_at),
        "ended_at": _iso(reservation.ended_at),
        "peak_player_count": reservation.peak_player_count,
        "failure_reason": reservation.failure_reason,
        "instance_id": reservation.instance_id,
    }

    if runtime_kind == RuntimeKind.INSTANT.value:
        assignments = reservation.instant_assignments
        host = assignments[-1].slot.host if assignments else None
        context["instant_host"] = host.name if host else None
        return context

    # CloudInstance rows are deleted at teardown, so once a reservation has
    # ended the provider can only be inferred from the location's primary
    # mapping. That guess is wrong if provisioning fell back to another
    # provider; provider_source records which one was used.
    instance = reservation.cloud_instance
    if instance is not None:
        context["provider"] = instance.provider_code
        context["provider_region"] = instance.provider_region
        context["provider_source"] = "instance"
    elif location is not None and location.provider:
        context["provider"] = location.provider
        context["provider_region"] = location.provider_region
        context["provider_source"] = "location"
    return context


def _escape_markdown(text: str) -> str:
    return _MARKDOWN_RE.sub(r"\\\1", text)


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_webhook_payload(
    *,
    category: str,
    message: str,
    user: User,
    reservation_number: int | None,
    context: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build the Discord webhook body announcing a feedback submission."""
    category_label = translate(f"feedback.categories.{category}", "en")
    subject = f"Reservation #{reservation_number}" if reservation_number else "General"
    profile_url = f"https://steamcommunity.com/profiles/{user.steam_id}"

    fields = [{
        "name": "User",
        "value": _truncate(f"[{_escape_markdown(user.display_name)}]({profile_url})", _DISCORD_FIELD_MAX),
        "inline": True,
    }]
    if context:
        city = context.get("location_city")
        code = context.get("location")
        fields.append({
            "name": "Location",
            "value": f"{city} ({code})" if city else str(code),
            "inline": True,
        })
        if context.get("runtime_kind") == RuntimeKind.INSTANT.value:
            server = f"Instant · {_escape_markdown(context.get('instant_host') or 'unknown host')}"
        elif context.get("provider"):
            region = context.get("provider_region") or "?"
            server = _escape_markdown(f"{context['provider']} · {region}")
            if context.get("provider_source") == "location":
                server += " (location default)"
        else:
            server = "unknown"
        fields.append({"name": "Server", "value": server, "inline": True})
        map_name = context.get("current_map") or context.get("first_map") or "?"
        config = context.get("config_file") or "none"
        fields.append({
            "name": "Map / config",
            "value": _escape_markdown(f"{map_name} · {config}"),
            "inline": True,
        })

    embed = {
        "title": _truncate(f"{category_label} · {subject}", _DISCORD_TITLE_MAX),
        "description": _truncate(message, _DISCORD_DESCRIPTION_MAX),
        "url": f"{settings.base_url}/admin#feedback",
        "fields": fields,
    }
    # User text must never be able to ping @everyone, roles or users.
    return {"embeds": [embed], "allowed_mentions": {"parse": []}}


async def send_feedback_webhook(payload: dict[str, Any]) -> None:
    """POST a feedback notification to Discord. Never raises."""
    url = settings.discord_feedback_webhook_url
    if not url:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
    # httpx error messages include the request URL, which embeds the webhook
    # token, so only the status code or exception type is logged.
    except httpx.HTTPStatusError as exc:
        logger.warning("Feedback webhook returned HTTP %s", exc.response.status_code)
    except Exception as exc:
        logger.warning("Feedback webhook delivery failed: %s", type(exc).__name__)
