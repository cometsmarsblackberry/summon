"""Steam trust checks: account age, TF2 hours, VAC bans, profile visibility."""

import asyncio
import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.i18n import t
from app.models.steam_trust_snapshot import SteamTrustSnapshot
from app.services.steam_http import create_steam_async_client
from app.services.settings import get_steam_trust_settings

logger = logging.getLogger(__name__)

STEAM_API_URL = "https://api.steampowered.com"


class SteamTrustBlocked(Exception):
    """Raised when a user fails a Steam trust check."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


async def fetch_player_bans(steam_id: str) -> dict | None:
    """Call ISteamUser/GetPlayerBans/v1 and return first player entry."""
    settings = get_settings()
    if not settings.steam_configured:
        return None
    try:
        async with create_steam_async_client(timeout=10.0) as client:
            resp = await client.get(
                f"{STEAM_API_URL}/ISteamUser/GetPlayerBans/v1/",
                params={"key": settings.steam_api_key, "steamids": steam_id},
            )
            resp.raise_for_status()
            players = resp.json().get("players", [])
            return players[0] if players else None
    except Exception:
        logger.warning("Failed to fetch player bans for %s", steam_id, exc_info=True)
        return None


async def fetch_tf2_ownership(steam_id: str) -> tuple[dict | None, bool | None]:
    """Call IPlayerService/GetOwnedGames/v1 filtered to TF2 (appid 440).

    Returns:
        (game_dict, accessible)
        - game_dict: first matching game entry (appid 440) when available, else None
        - accessible: True when the API response looks like real game data, False when it
          looks like game details are not accessible (often due to privacy settings),
          None when Steam isn't configured or the request fails.
    """
    settings = get_settings()
    if not settings.steam_configured:
        return None, None
    try:
        async with create_steam_async_client(timeout=10.0) as client:
            resp = await client.get(
                f"{STEAM_API_URL}/IPlayerService/GetOwnedGames/v1/",
                params={
                    "key": settings.steam_api_key,
                    "steamid": steam_id,
                    "appids_filter[0]": "440",
                    "include_played_free_games": "1",
                },
            )
            resp.raise_for_status()
            response = resp.json().get("response", None)
            if not isinstance(response, dict):
                return None, None

            # Heuristic: when game details are inaccessible, Steam often returns an empty
            # response without "game_count". Use "game_count" as the indicator that we
            # can safely treat the result as authoritative for overwriting cached values.
            accessible = "game_count" in response
            games = response.get("games", [])
            if not isinstance(games, list):
                games = []
            return (games[0] if games else None), accessible
    except Exception:
        logger.warning("Failed to fetch TF2 ownership for %s", steam_id, exc_info=True)
        return None, None


def steam_trust_needs_refresh(user, trust: dict) -> bool:
    """Return True if any enabled trust check lacks cached data."""
    if trust["min_account_age_days"] > 0 and user.steam_account_created_at is None:
        return True
    if trust["min_tf2_hours"] > 0 and user.tf2_playtime_hours is None:
        return True
    if trust["require_tf2_ownership"] and user.owns_tf2 is None:
        return True
    if trust["block_vac_banned"] and user.has_vac_ban is None:
        return True
    if trust["require_public_profile"] and user.profile_public is None:
        return True
    return False


async def update_user_steam_trust(user, player_summary: dict, db: AsyncSession) -> None:
    """Fetch bans + TF2 data concurrently and update cached trust fields.

    Important behavior: when TF2 playtime/ownership isn't accessible (often due to
    Steam privacy settings), we keep the last known cached values instead of
    overwriting them with 0/False.
    """
    bans_data, tf2_result = await asyncio.gather(
        fetch_player_bans(user.steam_id),
        fetch_tf2_ownership(user.steam_id),
    )
    tf2_data, tf2_accessible = tf2_result

    def _as_utc(dt: datetime | None) -> datetime | None:
        if dt is None:
            return None
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt

    old_values = {
        "steam_account_created_at": _as_utc(user.steam_account_created_at),
        "profile_public": user.profile_public,
        "has_vac_ban": user.has_vac_ban,
        "owns_tf2": user.owns_tf2,
        "tf2_playtime_hours": user.tf2_playtime_hours,
        "steam_data_updated_at": _as_utc(user.steam_data_updated_at),
    }

    # timecreated from already-fetched GetPlayerSummaries
    timecreated = player_summary.get("timecreated")
    if timecreated:
        user.steam_account_created_at = datetime.fromtimestamp(timecreated, tz=timezone.utc)

    # communityvisibilitystate: 3 = public
    visibility = player_summary.get("communityvisibilitystate")
    if visibility is not None:
        user.profile_public = visibility == 3

    # VAC ban
    if bans_data is not None:
        user.has_vac_ban = bans_data.get("VACBanned", False)

    # TF2 ownership & hours
    if tf2_accessible is True:
        if tf2_data is not None:
            user.owns_tf2 = True
            playtime_minutes = tf2_data.get("playtime_forever", 0)
            user.tf2_playtime_hours = int(playtime_minutes) // 60
        else:
            # Accessible, but TF2 isn't present in owned games.
            user.owns_tf2 = False
            user.tf2_playtime_hours = 0
    elif tf2_accessible is False:
        # Inaccessible (often private game details): keep last-known cached values.
        pass
    else:
        # Unknown (Steam not configured or request error): keep cached values.
        pass

    now = datetime.now(timezone.utc)
    did_observe_anything = (
        timecreated is not None
        or visibility is not None
        or bans_data is not None
        or tf2_accessible is True
    )
    if did_observe_anything:
        user.steam_data_updated_at = now

    new_values = {
        "steam_account_created_at": _as_utc(user.steam_account_created_at),
        "profile_public": user.profile_public,
        "has_vac_ban": user.has_vac_ban,
        "owns_tf2": user.owns_tf2,
        "tf2_playtime_hours": user.tf2_playtime_hours,
    }

    changed = any(new_values[k] != old_values[k] for k in new_values.keys())

    # Bootstrap: if this is the first time we touch this user with snapshots enabled,
    # store their current cached values (if any) so we don't lose historical state.
    result = await db.execute(
        select(func.count(SteamTrustSnapshot.id)).where(SteamTrustSnapshot.user_id == user.id)
    )
    has_any_snapshot = (result.scalar_one() or 0) > 0

    def _has_any_trust_data(values: dict) -> bool:
        return any(
            values[k] is not None
            for k in (
                "steam_account_created_at",
                "profile_public",
                "has_vac_ban",
                "owns_tf2",
                "tf2_playtime_hours",
            )
        )

    if not has_any_snapshot and _has_any_trust_data(old_values):
        db.add(
            SteamTrustSnapshot(
                user_id=user.id,
                fetched_at=old_values["steam_data_updated_at"] or now,
                source="cache",
                steam_account_created_at=old_values["steam_account_created_at"],
                profile_public=old_values["profile_public"],
                has_vac_ban=old_values["has_vac_ban"],
                owns_tf2=old_values["owns_tf2"],
                tf2_playtime_hours=old_values["tf2_playtime_hours"],
            )
        )

    if changed and _has_any_trust_data(new_values):
        db.add(
            SteamTrustSnapshot(
                user_id=user.id,
                fetched_at=now,
                source="steam_api",
                steam_account_created_at=new_values["steam_account_created_at"],
                profile_public=new_values["profile_public"],
                has_vac_ban=new_values["has_vac_ban"],
                owns_tf2=new_values["owns_tf2"],
                tf2_playtime_hours=new_values["tf2_playtime_hours"],
            )
        )

    await db.commit()


async def check_steam_trust(user, db: AsyncSession) -> None:
    """Evaluate enabled trust checks; raise SteamTrustBlocked on failure.

    If cached data is missing for an enabled check, re-fetches from Steam
    before failing so users don't need a logout/login cycle.
    """
    trust = await get_steam_trust_settings(db)

    # Re-fetch from Steam if any enabled check lacks cached data
    if steam_trust_needs_refresh(user, trust):
        try:
            from app.routers.auth import get_steam_player_info
            player_info = await get_steam_player_info(user.steam_id)
            await update_user_steam_trust(user, player_info, db)
        except Exception:
            logger.warning("Failed to refresh Steam trust data for %s", user.steam_id, exc_info=True)

    # Account age
    min_age = trust["min_account_age_days"]
    if min_age > 0 and user.steam_account_created_at is not None:
        created = user.steam_account_created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - created).days
        if age_days < min_age:
            raise SteamTrustBlocked(
                t("errors.steam_account_age", age=age_days, min=min_age)
            )

    # TF2 hours
    min_hours = trust["min_tf2_hours"]
    if min_hours > 0:
        if user.tf2_playtime_hours is None:
            raise SteamTrustBlocked(
                t("errors.tf2_hours_private")
            )
        if user.tf2_playtime_hours < min_hours:
            raise SteamTrustBlocked(
                t("errors.tf2_hours_insufficient", hours=user.tf2_playtime_hours, min=min_hours)
            )

    # TF2 ownership
    if trust["require_tf2_ownership"]:
        if user.owns_tf2 is None:
            raise SteamTrustBlocked(
                t("errors.tf2_ownership_private")
            )
        if not user.owns_tf2:
            raise SteamTrustBlocked(
                t("errors.tf2_ownership_required")
            )

    # VAC ban
    if trust["block_vac_banned"] and user.has_vac_ban is not None:
        if user.has_vac_ban:
            raise SteamTrustBlocked(
                t("errors.vac_banned")
            )

    # Public profile
    if trust["require_public_profile"] and user.profile_public is not None:
        if not user.profile_public:
            raise SteamTrustBlocked(
                t("errors.profile_not_public")
            )
