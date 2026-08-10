import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.instance import GameMap
from app.models.reservation import Reservation, ReservationStatus
from app.models.upload_link import UploadLink  # noqa: F401 - registers relationship
from app.routers.admin import (
    BulkDeleteMapsRequest,
    _get_maps_data,
    bulk_delete_maps,
)


class AdminMapTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.session_maker = async_sessionmaker(
            self.engine,
            expire_on_commit=False,
        )

    async def asyncTearDown(self):
        await self.engine.dispose()

    @staticmethod
    def _reservation(
        number: int,
        map_name: str,
        created_at: datetime,
        *,
        started_at: datetime | None,
    ) -> Reservation:
        return Reservation(
            reservation_number=number,
            location="test",
            starts_at=created_at,
            ends_at=created_at + timedelta(hours=1),
            password="password",
            rcon_password="rcon-password",
            tv_password="tv-password",
            first_map=map_name,
            motd_token=f"motd-{number}",
            logsecret=f"log-{number}",
            plugin_api_key=f"plugin-{number}",
            status=(
                ReservationStatus.ENDED
                if started_at
                else ReservationStatus.CANCELLED
            ),
            created_at=created_at,
            started_at=started_at,
        )

    async def test_maps_are_alphabetical_with_selection_and_play_stats(self):
        async with self.session_maker() as db:
            db.add_all(
                [
                    GameMap(name="Zulu", display_name="Zulu", display_order=1),
                    GameMap(name="alpha", display_name="alpha", display_order=3),
                    GameMap(name="Bravo", display_name="Bravo", display_order=2),
                ]
            )
            first_use = datetime(2026, 7, 1, 10, 30)
            last_selection = datetime(2026, 7, 4, 12, 0)
            db.add_all(
                [
                    self._reservation(
                        1,
                        "alpha",
                        first_use,
                        started_at=first_use + timedelta(minutes=2),
                    ),
                    self._reservation(
                        2,
                        "alpha",
                        last_selection,
                        started_at=None,
                    ),
                    self._reservation(
                        3,
                        "Bravo",
                        last_selection,
                        started_at=last_selection + timedelta(minutes=1),
                    ),
                ]
            )
            await db.commit()

            maps = await _get_maps_data(db)

        self.assertEqual(["alpha", "Bravo", "Zulu"], [item["name"] for item in maps])
        self.assertEqual(2, maps[0]["selection_count"])
        self.assertEqual(1, maps[0]["play_count"])
        self.assertEqual("2026-07-01T10:32:00", maps[0]["last_used_at"])
        self.assertEqual(0, maps[2]["selection_count"])
        self.assertEqual(0, maps[2]["play_count"])
        self.assertIsNone(maps[2]["last_used_at"])

    async def test_bulk_delete_removes_all_selected_maps(self):
        async with self.session_maker() as db:
            maps = [
                GameMap(name="cp_alpha", display_name="cp_alpha"),
                GameMap(name="cp_bravo", display_name="cp_bravo"),
                GameMap(name="cp_charlie", display_name="cp_charlie"),
            ]
            db.add_all(maps)
            await db.commit()

            response = await bulk_delete_maps(
                BulkDeleteMapsRequest(map_ids=[maps[2].id, maps[0].id, maps[2].id]),
                user=SimpleNamespace(is_admin=True),
                db=db,
            )
            remaining = list(
                (await db.execute(select(GameMap).order_by(GameMap.name)))
                .scalars()
                .all()
            )

        self.assertEqual([maps[2].id, maps[0].id], response["ids"])
        self.assertEqual(["cp_bravo"], [game_map.name for game_map in remaining])

    async def test_bulk_delete_is_atomic_when_a_map_is_missing(self):
        async with self.session_maker() as db:
            game_map = GameMap(name="cp_alpha", display_name="cp_alpha")
            db.add(game_map)
            await db.commit()

            with self.assertRaises(HTTPException) as raised:
                await bulk_delete_maps(
                    BulkDeleteMapsRequest(map_ids=[game_map.id, 999]),
                    user=SimpleNamespace(is_admin=True),
                    db=db,
                )
            remaining = list((await db.execute(select(GameMap))).scalars().all())

        self.assertEqual(404, raised.exception.status_code)
        self.assertEqual([game_map.id], [item.id for item in remaining])


if __name__ == "__main__":
    unittest.main()
