import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.instance import GameMap
from app.models.upload_link import UploadLink  # noqa: F401 - registers relationship
from app.routers import internal

PLUGIN = (
    Path(__file__).resolve().parents[1]
    / "tf2-reservation-plugin" / "scripting" / "summon.sp"
)


class MapCatalogEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.sessions() as db:
            db.add_all([
                GameMap(name="koth_product_final", display_name="Product",
                        enabled=True, in_league_rotation=True),
                GameMap(name="cp_process_f12", display_name="Process",
                        enabled=True, in_league_rotation=True),
                GameMap(name="koth_proot_b6c-alt2", display_name="Proot",
                        enabled=True, in_league_rotation=False),
                GameMap(name="cp_retired", display_name="Retired",
                        enabled=False, in_league_rotation=False),
            ])
            await db.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def fetch(self, valid_key=True):
        with (
            patch.object(internal, "async_session_maker", self.sessions),
            patch.object(
                internal, "validate_reservation_api_key",
                AsyncMock(return_value=valid_key),
            ),
        ):
            return await internal.get_map_catalog(393, x_api_key="key")

    async def test_lists_the_enabled_maps_in_picker_order(self):
        response = await self.fetch()

        self.assertEqual(
            ["cp_process_f12", "koth_product_final", "koth_proot_b6c-alt2"],
            response.maps,
        )

    async def test_requires_the_reservation_key(self):
        with self.assertRaises(HTTPException) as raised:
            await self.fetch(valid_key=False)

        self.assertEqual(403, raised.exception.status_code)

    def test_plugin_requests_the_catalog_path(self):
        source = PLUGIN.read_text(encoding="utf-8")

        self.assertIn('"%s/internal/reservations/%d/maps"', source)


if __name__ == "__main__":
    unittest.main()
