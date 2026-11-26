"""The reservation dialog is open to everyone; only some visitors can submit it."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app import database
from app.main import app
from app.models.user import User
from app.routers import pages


STATUS = {
    "fra": {
        "name": "Frankfurt",
        "city": "Frankfurt",
        "enabled": True,
        "reservable": True,
        "instant_available": True,
    },
}


def setUpModule():
    async def create():
        await database.create_tables()
        # The TestClient runs requests on its own event loop.
        await database.engine.dispose()

    asyncio.run(create())


class HomeDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def render(self, user=None, beta_access=True, active_reservation=None):
        with (
            patch.object(pages, "get_current_user", AsyncMock(return_value=user)),
            patch.object(pages, "has_beta_access", return_value=beta_access),
            patch.object(pages, "get_user_active_reservation",
                         AsyncMock(return_value=active_reservation)),
            patch.object(pages, "get_user_recent_locations", AsyncMock(return_value=[])),
            patch.object(pages, "get_status_snapshot", AsyncMock(return_value=STATUS)),
        ):
            response = self.client.get("/")
        self.assertEqual(200, response.status_code)
        return response.text

    def user(self):
        return User(id=1, steam_id="76561198000000001", display_name="Player")

    def test_anonymous_visitors_can_open_the_dialog_but_must_sign_in(self):
        html = self.render()

        self.assertIn('@click="openModal(item.entry[0], item.entry[1])"', html)
        self.assertIn('x-show="modalOpen"', html)
        self.assertIn("canReserve: false", html)
        self.assertIn('href="/login" @click="saveDraft()"', html)
        self.assertIn("Sign in with Steam to reserve", html)
        self.assertIn("Click a location to see the options", html)
        self.assertNotIn('@click="submit()"', html)
        # The draft is only picked up once signed in.
        self.assertNotIn("restoreDraft();", html)

    def test_signed_in_visitors_reserve_and_get_their_draft_back(self):
        html = self.render(user=self.user())

        self.assertIn("canReserve: true", html)
        self.assertIn('@click="submit()"', html)
        self.assertIn("restoreDraft();", html)
        self.assertNotIn('@click="saveDraft()"', html)
        self.assertNotIn("Sign in with Steam to reserve", html)

    def test_beta_locked_visitors_see_why_they_cannot_reserve(self):
        html = self.render(user=self.user(), beta_access=False)

        self.assertIn('x-show="modalOpen"', html)
        self.assertIn("canReserve: false", html)
        self.assertIn("Currently invite-only.", html)
        self.assertNotIn('@click="submit()"', html)

    def test_visitors_with_an_active_reservation_are_sent_to_it(self):
        reservation = SimpleNamespace(id=42, reservation_number=7, location="fra")
        html = self.render(user=self.user(), active_reservation=reservation)

        self.assertIn('x-show="modalOpen"', html)
        self.assertIn("canReserve: false", html)
        self.assertEqual(2, html.count('href="/reservations/42"'))
        self.assertNotIn('@click="submit()"', html)


if __name__ == "__main__":
    unittest.main()
