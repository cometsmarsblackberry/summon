"""MOTD (Message of the Day) page for in-game display."""

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.instance import EnabledLocation
from app.models.reservation import Reservation, ReservationStatus
from app.services.trivia import get_trivia


router = APIRouter(tags=["motd"])
templates = Jinja2Templates(directory="templates")
logger = logging.getLogger(__name__)


@router.get("/motd/{motd_token}", response_class=HTMLResponse)
async def motd_page(
    request: Request,
    motd_token: str,
    db: AsyncSession = Depends(get_db),
):
    """MOTD page shown to players inside TF2."""
    # Look up by unguessable token instead of reservation number
    result = await db.execute(
        select(Reservation)
        .where(Reservation.motd_token == motd_token)
        .options(selectinload(Reservation.cloud_instance))
    )
    reservation = result.scalar_one_or_none()

    if not reservation or reservation.status != ReservationStatus.ACTIVE:
        return templates.TemplateResponse(
            request,
            "motd.html",
            {"reservation": None, "trivia": None, "connection": None, "location_display": None},
            status_code=404,
        )

    # Fetch location metadata for trivia
    loc_result = await db.execute(
        select(EnabledLocation).where(EnabledLocation.code == reservation.location)
    )
    location = loc_result.scalar_one_or_none()

    trivia = await get_trivia(
        db,
        city=location.city if location else None,
        subdivision=location.subdivision if location else None,
        country=location.country if location else None,
    )

    location_display = (
        location.city if location and location.city else reservation.location
    )

    # Build connection data for copy buttons
    connection = {
        "sdr_ip": reservation.sdr_ip,
        "sdr_port": reservation.sdr_port,
        "password": reservation.password,
        "tv_password": reservation.tv_password,
    }
    if reservation.enable_direct_connect and reservation.cloud_instance:
        ip = reservation.cloud_instance.ip_address
        if ip and ip != "0.0.0.0":
            connection["ip_address"] = ip

    response = templates.TemplateResponse(
        request,
        "motd.html",
        {
            "reservation": reservation,
            "location_display": location_display,
            "trivia": trivia,
            "connection": connection,
        },
    )

    # Override security headers for TF2 embedded browser compatibility
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://fonts.bunny.net; "
        "font-src 'self' https://fonts.bunny.net; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://d1f8nxls7qx69o.cloudfront.net; "
        "img-src 'self' data: https:; "
        "frame-ancestors 'self'"
    )

    return response
