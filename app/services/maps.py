"""Map lists for the reservation map pickers."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instance import GameMap


async def get_selectable_maps(db: AsyncSession) -> list[dict]:
    """Return enabled maps, league rotation first, then alphabetically."""
    result = await db.execute(
        select(GameMap)
        .where(GameMap.enabled == True)
        .order_by(
            GameMap.in_league_rotation.desc(),
            func.lower(GameMap.name),
            GameMap.name,
        )
    )
    return [
        {
            "name": m.name,
            "display": m.display_name,
            "in_league_rotation": bool(m.in_league_rotation),
        }
        for m in result.scalars().all()
    ]


async def get_map_download_url(map_name: str, db: AsyncSession) -> str | None:
    """Return the FastDL link for a custom map, or None for stock and unknown maps.

    Maps changed to in game can be stock maps missing from the map list,
    which FastDL does not have.
    """
    result = await db.execute(select(GameMap).where(GameMap.name == map_name))
    game_map = result.scalar_one_or_none()
    if not game_map or game_map.is_default:
        return None

    from app.services.settings import get_fastdl_url
    fastdl = await get_fastdl_url(db)
    if not fastdl.endswith("/"):
        fastdl += "/"
    if fastdl.endswith("maps/"):
        fastdl = fastdl[:-5]  # strip trailing maps/ to avoid duplication
    return f"{fastdl}maps/{map_name}.bsp"
