import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.routers import pages


class ReservationStatusPageTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_reservation_renders_404_page_for_anonymous_user(self):
        request = SimpleNamespace()

        with (
            patch.object(pages, "get_current_user", AsyncMock(return_value=None)),
            patch.object(
                pages,
                "get_reservation_by_id",
                AsyncMock(return_value=None),
            ),
            patch.object(
                pages.templates,
                "TemplateResponse",
                return_value="not found",
            ) as render,
        ):
            response = await pages.reservation_status_page(
                request=request,
                reservation_id=1000,
                db=object(),
            )

        self.assertEqual("not found", response)
        render.assert_called_once_with(
            request,
            "404.html",
            {"user": None},
            status_code=404,
        )


if __name__ == "__main__":
    unittest.main()
