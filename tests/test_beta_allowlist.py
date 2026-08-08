import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app.config import Settings
from app.routers import auth


ALLOWLISTED_STEAM_ID = "76561198000000001"
OTHER_STEAM_ID = "76561198000000002"


def _user(steam_id: str, *, is_admin: bool = False):
    return SimpleNamespace(
        steam_id=steam_id,
        is_admin=is_admin,
        is_banned=False,
    )


class BetaAllowlistConfigTests(unittest.TestCase):
    def test_parses_comma_separated_steam_ids(self):
        settings = Settings(
            _env_file=None,
            beta_allowlist_steam_ids=(
                f" {ALLOWLISTED_STEAM_ID},, {OTHER_STEAM_ID} "
            ),
        )

        self.assertEqual(
            [ALLOWLISTED_STEAM_ID, OTHER_STEAM_ID],
            settings.beta_allowlist_steam_id_list,
        )


class BetaAllowlistAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_allowlisted_regular_user_can_use_beta(self):
        user = _user(ALLOWLISTED_STEAM_ID)

        with (
            patch.object(auth.settings, "beta_mode", True),
            patch.object(
                auth.settings,
                "beta_allowlist_steam_ids",
                ALLOWLISTED_STEAM_ID,
            ),
            patch.object(
                auth,
                "get_current_user",
                AsyncMock(return_value=user),
            ),
        ):
            result = await auth.require_user(object(), object())

        self.assertIs(user, result)

    async def test_unlisted_regular_user_is_rejected_during_beta(self):
        user = _user(OTHER_STEAM_ID)

        with (
            patch.object(auth.settings, "beta_mode", True),
            patch.object(
                auth.settings,
                "beta_allowlist_steam_ids",
                ALLOWLISTED_STEAM_ID,
            ),
            patch.object(
                auth,
                "get_current_user",
                AsyncMock(return_value=user),
            ),
        ):
            with self.assertRaises(HTTPException) as raised:
                await auth.require_user(object(), object())

        self.assertEqual(403, raised.exception.status_code)

    async def test_allowlisted_user_does_not_gain_admin_access(self):
        user = _user(ALLOWLISTED_STEAM_ID)

        with (
            patch.object(auth.settings, "beta_mode", True),
            patch.object(
                auth.settings,
                "beta_allowlist_steam_ids",
                ALLOWLISTED_STEAM_ID,
            ),
        ):
            self.assertTrue(auth.has_beta_access(user))
            with self.assertRaises(HTTPException) as raised:
                await auth.require_admin(user)

        self.assertEqual(403, raised.exception.status_code)


if __name__ == "__main__":
    unittest.main()
