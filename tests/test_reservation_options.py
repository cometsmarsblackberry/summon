import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException

from app.models.reservation import ReservationStatus
from app.models.upload_link import UploadLink  # noqa: F401 - registers relationship
from app.routers.reservations import (
    CreateReservationRequest,
    UploadSettingsRequest,
    update_upload_settings,
)
from app.services.orchestrator import build_reservation_config
from app.services.reservation import create_reservation


class CreateReservationOptionsTests(unittest.IsolatedAsyncioTestCase):
    def test_request_defaults_enable_external_uploads(self):
        request = CreateReservationRequest(location="test", first_map="cp_badlands")

        self.assertIsNone(request.config_file)
        self.assertTrue(request.enable_logs_tf_upload)
        self.assertTrue(request.enable_demos_tf_upload)

    async def test_service_persists_config_and_upload_preferences(self):
        user = SimpleNamespace(id=7, steam_id="76561198000000000", reservation_count=0)
        db = SimpleNamespace(add=Mock(), commit=AsyncMock(), refresh=AsyncMock())

        with (
            patch(
                "app.services.settings.get_reservation_settings",
                new=AsyncMock(return_value={"max_duration_hours": 4}),
            ),
            patch(
                "app.services.reservation.get_next_reservation_number",
                new=AsyncMock(return_value=42),
            ),
        ):
            reservation = await create_reservation(
                user=user,
                location="test",
                duration_hours=4,
                first_map="cp_badlands",
                config_file="rgl_6s_5cp_match_pro",
                enable_logs_tf_upload=False,
                enable_demos_tf_upload=True,
                db=db,
            )

        self.assertEqual("rgl_6s_5cp_match_pro", reservation.config_file)
        self.assertFalse(reservation.enable_logs_tf_upload)
        self.assertTrue(reservation.enable_demos_tf_upload)
        db.add.assert_called_once_with(reservation)
        db.commit.assert_awaited_once()

    def test_agent_config_includes_initial_reservation_options(self):
        now = datetime.now(timezone.utc)
        reservation = SimpleNamespace(
            id=42,
            reservation_number=42,
            location="test",
            password="server-password",
            rcon_password="rcon-password",
            tv_password="tv-password",
            first_map="cp_process_f12",
            config_file="rgl_6s_5cp_match_pro",
            logsecret="log-secret",
            ends_at=now + timedelta(hours=4),
            plugin_api_key="plugin-key",
            enable_direct_connect=False,
            enable_logs_tf_upload=False,
            enable_demos_tf_upload=True,
            motd_token="motd-token",
        )

        config = build_reservation_config(reservation)

        self.assertEqual("rgl_6s_5cp_match_pro", config["config_file"])
        self.assertFalse(config["enable_logs_tf_upload"])
        self.assertTrue(config["enable_demos_tf_upload"])


class ActiveUploadSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_updates_running_agent_and_persists_preferences(self):
        reservation = SimpleNamespace(
            id=9,
            user_id=3,
            status=ReservationStatus.ACTIVE,
            instance_id="cloud-record-id",
            enable_logs_tf_upload=True,
            enable_demos_tf_upload=True,
        )
        cloud_instance = SimpleNamespace(instance_id="agent-id")
        result = SimpleNamespace(scalar_one_or_none=lambda: cloud_instance)
        db = SimpleNamespace(execute=AsyncMock(return_value=result), commit=AsyncMock())
        user = SimpleNamespace(id=3, is_admin=False)

        with (
            patch(
                "app.routers.reservations.get_reservation_by_id",
                new=AsyncMock(return_value=reservation),
            ),
            patch(
                "app.routers.internal.send_upload_settings",
                new=AsyncMock(return_value=True),
            ) as send_settings,
        ):
            response = await update_upload_settings(
                reservation_id=9,
                body=UploadSettingsRequest(
                    enable_logs_tf_upload=False,
                    enable_demos_tf_upload=True,
                ),
                user=user,
                db=db,
            )

        send_settings.assert_awaited_once_with(
            "agent-id",
            logs_tf=False,
            demos_tf=True,
        )
        self.assertFalse(reservation.enable_logs_tf_upload)
        self.assertTrue(reservation.enable_demos_tf_upload)
        self.assertFalse(response["enable_logs_tf_upload"])
        db.commit.assert_awaited_once()

    async def test_rejects_changes_when_server_is_not_active(self):
        reservation = SimpleNamespace(
            id=9,
            user_id=3,
            status=ReservationStatus.PROVISIONING,
            instance_id="cloud-record-id",
        )
        user = SimpleNamespace(id=3, is_admin=False)

        with (
            patch(
                "app.routers.reservations.get_reservation_by_id",
                new=AsyncMock(return_value=reservation),
            ),
            patch("app.routers.reservations.t", return_value="Server is not active"),
        ):
            with self.assertRaises(HTTPException) as exc:
                await update_upload_settings(
                    reservation_id=9,
                    body=UploadSettingsRequest(
                        enable_logs_tf_upload=False,
                        enable_demos_tf_upload=False,
                    ),
                    user=user,
                    db=SimpleNamespace(),
                )

        self.assertEqual(400, exc.exception.status_code)


if __name__ == "__main__":
    unittest.main()
