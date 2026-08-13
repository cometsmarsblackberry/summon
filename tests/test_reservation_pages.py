import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.i18n import SUPPORTED_LOCALES, translate
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

    async def test_status_page_uses_configured_auto_end_minutes(self):
        request = SimpleNamespace()
        user = SimpleNamespace(id=7, is_admin=False)
        reservation = SimpleNamespace(
            user_id=user.id,
            location="hel",
            first_map="cp_badlands",
        )
        location_result = SimpleNamespace(
            scalar_one_or_none=lambda: SimpleNamespace(city="Helsinki"),
        )
        map_result = SimpleNamespace(
            scalar_one_or_none=lambda: SimpleNamespace(is_default=True),
        )
        db = SimpleNamespace(
            execute=AsyncMock(side_effect=[location_result, map_result]),
        )

        with (
            patch.object(pages, "get_current_user", AsyncMock(return_value=user)),
            patch.object(
                pages,
                "get_reservation_by_id",
                AsyncMock(return_value=reservation),
            ),
            patch(
                "app.services.settings.get_reservation_settings",
                AsyncMock(return_value={
                    "max_duration_hours": 6,
                    "auto_end_minutes": 10,
                }),
            ) as get_reservation_settings,
            patch.object(
                pages.templates,
                "TemplateResponse",
                return_value="reservation page",
            ) as render,
        ):
            response = await pages.reservation_status_page(
                request=request,
                reservation_id=42,
                db=db,
            )

        self.assertEqual("reservation page", response)
        get_reservation_settings.assert_awaited_once_with(db)
        context = render.call_args.args[2]
        self.assertEqual(10, context["reservation_settings"]["auto_end_minutes"])

    def test_status_template_uses_dynamic_auto_end_minutes(self):
        template = (
            Path(__file__).resolve().parents[1] / "templates" / "status.html"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "autoEndSeconds: {{ reservation_settings.auto_end_minutes }} * 60",
            template,
        )
        self.assertNotIn("autoEndSeconds: 30 * 60", template)
        self.assertEqual(
            2,
            template.count(
                "_('status.auto_end_default', "
                "minutes=reservation_settings.auto_end_minutes)"
            ),
        )

    def test_auto_end_translations_accept_configured_minutes(self):
        with patch(
            "app.config.get_settings",
            return_value=SimpleNamespace(site_name="Summon"),
        ):
            for locale in SUPPORTED_LOCALES:
                with self.subTest(locale=locale):
                    label = translate(
                        "status.auto_end_default",
                        locale,
                        minutes=10,
                    )
                    explanation = translate(
                        "status.auto_end_explanation",
                        locale,
                        minutes=10,
                    )
                    self.assertIn("10", label)
                    self.assertIn("10", explanation)
                    self.assertNotIn("{minutes}", label)
                    self.assertNotIn("{minutes}", explanation)

    def test_stats_label_identifies_logical_cpus(self):
        with patch(
            "app.config.get_settings",
            return_value=SimpleNamespace(site_name="Summon"),
        ):
            for locale in SUPPORTED_LOCALES:
                with self.subTest(locale=locale):
                    label = translate("status.logical_cpus", locale)
                    self.assertNotEqual("status.logical_cpus", label)
                    self.assertNotIn("vCPU", label)

            self.assertEqual(
                "Logical CPUs",
                translate("status.logical_cpus", "en"),
            )

        template = (
            Path(__file__).resolve().parents[1] / "templates" / "status.html"
        ).read_text(encoding="utf-8")
        self.assertIn("_('status.logical_cpus')", template)
        self.assertNotIn("_('status.cpus')", template)


if __name__ == "__main__":
    unittest.main()
