"""Notice game servers that stop answering while their reservation is active.

The SourceMod plugin reports players every ten seconds and on every map
start, join and leave. Those reports are the only sign that the game server
itself is running: the agent's connection stays up when only the game server
is stuck, for example in a map load that never finishes. Without them the
empty-server timer never starts, and a stuck server would run until its
reservation's end time.

When an active reservation's reports stop for REPORT_SILENCE_LIMIT, the
backend asks the game server, through its agent, to answer a harmless RCON
command. A server that misses FAILED_CHECKS_TO_MARK answers in a row is
marked unresponsive and the empty-server timer starts from its last report,
so it ends like an empty server unless it recovers or its owner restarts it.
A later report or RCON answer clears the mark.

The RCON check keeps a reporting problem from ending a working server: if
the plugin cannot reach the backend but the game server answers, nothing
happens. Nothing happens either while the agent cannot be reached, as the
server cannot be checked then.

Everything here lives in memory. A web process that has just started has
received no reports, so it measures silence from when it first sees each
reservation, never from reports made before it started.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.database import async_session_maker
from app.models.reservation import EndReason, Reservation, ReservationStatus

logger = logging.getLogger(__name__)

# Map loads, including a large custom map on a slow disk, finish well within
# this, and the plugin reports as soon as a map starts.
REPORT_SILENCE_LIMIT = timedelta(minutes=3)
# Checks run once a minute, so a server is marked after about a minute of
# missed answers rather than on one slow reply.
FAILED_CHECKS_TO_MARK = 2
# A server that answered RCON but still does not report is asked again after
# this, rather than every minute.
ANSWERED_RECHECK_DELAY = timedelta(minutes=5)
# A marked server is asked again after this, in case it was only the reports
# that stopped. Each unanswered check holds up the agent's RCON for its ten
# seconds, so not every minute.
MARKED_RECHECK_DELAY = timedelta(minutes=2)
# Agents give RCON ten seconds and then report an error, so this is only
# reached when the agent itself does not reply.
CHECK_TIMEOUT_SECONDS = 15.0
CHECK_COMMAND = "echo summon_liveness_check"


@dataclass
class _ServerState:
    # The last plugin report while the reservation was active, or when this
    # process first saw the reservation active.
    last_report_at: datetime
    failed_checks: int = 0
    next_check_at: datetime | None = None
    # When set, the server is marked unresponsive. It is the time of its last
    # report, which is when it stopped answering.
    unresponsive_since: datetime | None = None
    # The mark started the empty-server timer: the server had players.
    started_empty_timer: bool = False
    unreachable_logged: bool = False
    answered_logged: bool = False


# reservation_number -> state, for active reservations only
_servers: dict[int, _ServerState] = {}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    # SQLite returns naive datetimes; they are stored as UTC.
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def forget(reservation_number: int) -> None:
    """Drop what is known about a reservation's server.

    Called when a reservation ends or its server restarts, so the next server
    is judged only by its own reports.
    """
    _servers.pop(reservation_number, None)


def unresponsive_since(reservation_number: int) -> datetime | None:
    """When a server marked unresponsive last reported, or None."""
    state = _servers.get(reservation_number)
    return state.unresponsive_since if state else None


def is_unresponsive(reservation_number: int) -> bool:
    return unresponsive_since(reservation_number) is not None


def auto_end_reason(reservation_number: int) -> str:
    """The EndReason for a reservation whose empty-server timer ran out."""
    if is_unresponsive(reservation_number):
        return EndReason.UNRESPONSIVE
    return EndReason.EMPTY


def record_report(
    reservation: Reservation, player_count: int, now: datetime
) -> bool:
    """Record a plugin report for an active reservation.

    Clears an unresponsive mark. Returns whether the reservation changed and
    needs saving: a server that recovers empty gets a fresh empty timer, as
    its players left while it was stuck.
    """
    number = reservation.reservation_number
    previous = _servers.get(number)
    _servers[number] = _ServerState(last_report_at=now)
    if previous is None or previous.unresponsive_since is None:
        return False

    logger.info(
        "Reservation #%s game server is reporting again after %ss without reports",
        number,
        int((now - previous.unresponsive_since).total_seconds()),
    )
    if (
        previous.started_empty_timer
        and player_count == 0
        and reservation.empty_since is not None
    ):
        reservation.empty_since = now
        return True
    return False


async def _check_server(reservation_id: int) -> str:
    """Ask a reservation's game server to answer RCON.

    Returns "answered", "unanswered" (the agent got no answer from the game
    server, or did not reply in time), "unreachable" (no connected agent to
    ask) or "inactive".
    """
    from app.routers.internal import RconRequestTimeout, RconRequestUnavailable
    from app.services.runtime import awaited_rcon_reservation_runtime

    async with async_session_maker() as db:
        reservation = await db.get(Reservation, reservation_id)
        if reservation is None or reservation.status != ReservationStatus.ACTIVE:
            return "inactive"
        try:
            result = await awaited_rcon_reservation_runtime(
                reservation, db, CHECK_COMMAND, timeout=CHECK_TIMEOUT_SECONDS
            )
        except RconRequestTimeout:
            return "unanswered"
        except RconRequestUnavailable:
            return "unreachable"
    return "unanswered" if result.get("error") else "answered"


async def _apply_check(
    reservation_id: int, number: int, state: _ServerState, now: datetime
) -> None:
    try:
        outcome = await _check_server(reservation_id)
    except Exception:
        logger.exception("Liveness check failed for reservation #%s", number)
        return

    # A report, restart or end while waiting replaced or dropped the state,
    # and is newer than this answer.
    if _servers.get(number) is not state:
        return

    silent_for = int((now - state.last_report_at).total_seconds())
    if outcome == "inactive":
        forget(number)
        return

    if outcome == "unreachable":
        state.next_check_at = None
        if not state.unreachable_logged:
            state.unreachable_logged = True
            logger.warning(
                "Reservation #%s game server has not reported for %ss and "
                "its agent is not connected; not checking it",
                number,
                silent_for,
            )
        return

    if outcome == "answered":
        state.failed_checks = 0
        state.next_check_at = now + ANSWERED_RECHECK_DELAY
        if state.unresponsive_since is None:
            if not state.answered_logged:
                state.answered_logged = True
                logger.warning(
                    "Reservation #%s game server answers RCON but has not "
                    "reported for %ss",
                    number,
                    silent_for,
                )
            return
        marked_since = state.unresponsive_since
        undo_timer = state.started_empty_timer
        state.unresponsive_since = None
        state.started_empty_timer = False
        logger.info(
            "Reservation #%s game server answers RCON again; no longer "
            "marked unresponsive",
            number,
        )
        if undo_timer:
            # The server may have players: put back the state from before
            # the mark. The next report sets the timer again if it is empty.
            async with async_session_maker() as db:
                reservation = await db.get(Reservation, reservation_id)
                if (
                    reservation is not None
                    and reservation.status == ReservationStatus.ACTIVE
                    and _aware(reservation.empty_since) == marked_since
                ):
                    reservation.empty_since = None
                    await db.commit()
        return

    state.failed_checks += 1
    if state.unresponsive_since is not None:
        state.next_check_at = now + MARKED_RECHECK_DELAY
        return
    state.next_check_at = None
    if state.failed_checks < FAILED_CHECKS_TO_MARK:
        return

    state.unresponsive_since = state.last_report_at
    state.next_check_at = now + MARKED_RECHECK_DELAY
    async with async_session_maker() as db:
        reservation = await db.get(Reservation, reservation_id)
        if _servers.get(number) is not state:
            return
        if reservation is None or reservation.status != ReservationStatus.ACTIVE:
            forget(number)
            return
        if reservation.empty_since is None:
            reservation.empty_since = state.last_report_at
            state.started_empty_timer = True
            await db.commit()
    logger.warning(
        "Reservation #%s game server stopped responding: no report for %ss "
        "and %s RCON checks unanswered; empty-server timer %s",
        number,
        silent_for,
        state.failed_checks,
        "started from its last report"
        if state.started_empty_timer
        else "was already running",
    )


async def check_active_servers(now: datetime | None = None) -> None:
    """Check active reservations whose game server has stopped reporting."""
    now = now or _utcnow()
    async with async_session_maker() as db:
        rows = await db.execute(
            select(Reservation.id, Reservation.reservation_number).where(
                Reservation.status == ReservationStatus.ACTIVE
            )
        )
        active = {number: reservation_id for reservation_id, number in rows.all()}

    for number in list(_servers):
        if number not in active:
            forget(number)

    due: list[tuple[int, int, _ServerState]] = []
    for number, reservation_id in active.items():
        state = _servers.get(number)
        if state is None:
            # Not seen active before in this process: silence counts from now.
            _servers[number] = _ServerState(last_report_at=now)
            continue
        if now - state.last_report_at < REPORT_SILENCE_LIMIT:
            continue
        if state.next_check_at is not None and now < state.next_check_at:
            continue
        due.append((reservation_id, number, state))

    if due:
        await asyncio.gather(*(
            _apply_check(reservation_id, number, state, now)
            for reservation_id, number, state in due
        ))
