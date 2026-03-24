"""Location-based trivia for MOTD pages."""

import random
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.trivia import TriviaFact


async def get_trivia(
    db: AsyncSession,
    *,
    city: Optional[str] = None,
    subdivision: Optional[str] = None,
    country: Optional[str] = None,
) -> Optional[str]:
    """Pick a random trivia fact, cascading from most to least specific.

    Lookup order: city -> subdivision -> country -> generic fallback.
    Returns None if no facts exist in the database.
    """
    if city:
        fact = await _random_fact(db, "city", city.lower().strip())
        if fact:
            return fact

    if subdivision:
        key = subdivision.lower().strip()
        fact = await _random_fact(db, "subdivision", key)
        if not fact:
            # Try resolving between ISO codes and names via pycountry
            try:
                import pycountry
                normalized = subdivision.strip().upper().replace("_", "-")
                iso_match = pycountry.subdivisions.get(code=normalized)
                if iso_match:
                    # ISO code → full name (e.g. "US-CA" → "california")
                    fact = await _random_fact(db, "subdivision", iso_match.name.lower())
                else:
                    # Plain name → ISO code (e.g. "California" → "us-ca")
                    for sub in pycountry.subdivisions:
                        if sub.name.lower() == key:
                            fact = await _random_fact(db, "subdivision", sub.code.lower())
                            break
            except Exception:
                pass
        if fact:
            return fact

    if country:
        fact = await _random_fact(db, "country", country.lower().strip())
        if fact:
            return fact

    return await _random_fact(db, "generic", "")


async def _random_fact(db: AsyncSession, scope: str, key: str) -> Optional[str]:
    """Return a random fact for the given scope/key, or None."""
    result = await db.execute(
        select(func.count()).where(
            TriviaFact.scope == scope,
            TriviaFact.key == key,
        )
    )
    count = result.scalar_one()
    if count == 0:
        return None

    offset = random.randint(0, count - 1)
    result = await db.execute(
        select(TriviaFact.fact).where(
            TriviaFact.scope == scope,
            TriviaFact.key == key,
        ).offset(offset).limit(1)
    )
    return result.scalar_one_or_none()
