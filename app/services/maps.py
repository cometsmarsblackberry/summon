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
