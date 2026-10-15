import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.routers import auth


STEAM_ID = "76561198000000001"
OTHER_STEAM_ID = "76561198000000002"
STATE = "signed-login-state"


def _nonce(issued_at: datetime, suffix: str = "a1b2c3") -> str:
    return issued_at.strftime("%Y-%m-%dT%H:%M:%SZ") + suffix


def _assertion(**overrides) -> dict:
    """A positive assertion shaped like the ones Steam sends back."""
    claimed_id = f"https://steamcommunity.com/openid/id/{STEAM_ID}"
    params = {
        "state": STATE,
        "openid.ns": "http://specs.openid.net/auth/2.0",
        "openid.mode": "id_res",
        "openid.op_endpoint": "https://steamcommunity.com/openid/login",
        "openid.claimed_id": claimed_id,
        "openid.identity": claimed_id,
        "openid.return_to": auth._login_return_url(STATE),
        "openid.response_nonce": _nonce(datetime.now(timezone.utc)),
        "openid.assoc_handle": "1234567890",
        "openid.signed": "signed,op_endpoint,claimed_id,identity,return_to,response_nonce,assoc_handle",
        "openid.sig": "c2lnbmF0dXJl",
    }
    params.update(overrides)
    return params


class SteamAssertionTests(unittest.TestCase):
    def test_accepts_assertion_issued_for_this_login(self):
        self.assertEqual(STEAM_ID, auth._asserted_steam_id(_assertion(), STATE))

    def test_rejects_assertion_issued_to_another_site(self):
        params = _assertion(**{
            "openid.return_to": f"https://other.example/auth/callback?state={STATE}",
        })
        self.assertIsNone(auth._asserted_steam_id(params, STATE))

    def test_rejects_assertion_for_a_different_login_state(self):
        params = _assertion(**{"openid.return_to": auth._login_return_url("other-state")})
        self.assertIsNone(auth._asserted_steam_id(params, STATE))

    def test_rejects_return_to_with_extra_parameters(self):
        params = _assertion(**{
            "openid.return_to": auth._login_return_url(STATE) + "&next=/admin",
        })
        self.assertIsNone(auth._asserted_steam_id(params, STATE))

    def test_rejects_other_providers_and_modes(self):
        self.assertIsNone(auth._asserted_steam_id(
            _assertion(**{"openid.op_endpoint": "https://openid.example/login"}), STATE,
        ))
        self.assertIsNone(auth._asserted_steam_id(
            _assertion(**{"openid.mode": "cancel"}), STATE,
        ))

    def test_rejects_unsigned_return_to(self):
        params = _assertion(**{
            "openid.signed": "signed,op_endpoint,claimed_id,identity,response_nonce,assoc_handle",
        })
        self.assertIsNone(auth._asserted_steam_id(params, STATE))

    def test_rejects_claimed_id_that_is_not_a_steam_profile(self):
        claimed_id = f"https://evil.example/steamcommunity.com/openid/id/{STEAM_ID}"
        params = _assertion(**{
            "openid.claimed_id": claimed_id,
            "openid.identity": claimed_id,
        })
        self.assertIsNone(auth._asserted_steam_id(params, STATE))

    def test_rejects_claimed_id_that_differs_from_identity(self):
        params = _assertion(**{
            "openid.identity": f"https://steamcommunity.com/openid/id/{OTHER_STEAM_ID}",
        })
        self.assertIsNone(auth._asserted_steam_id(params, STATE))


class VerifySteamLoginTests(unittest.IsolatedAsyncioTestCase):
    def _steam_client(self, body: str):
        client = MagicMock()
        client.post = AsyncMock(return_value=SimpleNamespace(status_code=200, text=body))
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=client)
        context.__aexit__ = AsyncMock(return_value=False)
        return client, context

    async def test_foreign_assertion_is_rejected_without_asking_steam(self):
        client, context = self._steam_client("ns:http://specs.openid.net/auth/2.0\nis_valid:true\n")
        params = _assertion(**{
            "openid.return_to": f"https://other.example/login?state={STATE}",
        })
        with patch.object(auth, "create_steam_async_client", return_value=context):
            self.assertIsNone(await auth.verify_steam_login(params, STATE))
        client.post.assert_not_called()

    async def test_valid_assertion_is_confirmed_with_steam(self):
        client, context = self._steam_client("ns:http://specs.openid.net/auth/2.0\nis_valid:true\n")
        with patch.object(auth, "create_steam_async_client", return_value=context):
            self.assertEqual(STEAM_ID, await auth.verify_steam_login(_assertion(), STATE))
        sent = client.post.await_args.kwargs["data"]
        self.assertEqual("check_authentication", sent["openid.mode"])

    async def test_assertion_steam_rejects_is_not_accepted(self):
        _, context = self._steam_client("ns:http://specs.openid.net/auth/2.0\nis_valid:false\n")
        with patch.object(auth, "create_steam_async_client", return_value=context):
            self.assertIsNone(await auth.verify_steam_login(_assertion(), STATE))


class OpenIdNonceTests(unittest.TestCase):
    def setUp(self):
        auth._used_openid_nonces.clear()
        self.addCleanup(auth._used_openid_nonces.clear)

    def test_fresh_nonce_is_accepted_once(self):
        params = {"openid.response_nonce": _nonce(datetime.now(timezone.utc))}
        self.assertTrue(auth._consume_openid_nonce(params))
        self.assertFalse(auth._consume_openid_nonce(params))

    def test_nonce_older_than_a_login_is_rejected(self):
        issued_at = datetime.now(timezone.utc) - timedelta(minutes=30)
        self.assertFalse(auth._consume_openid_nonce({"openid.response_nonce": _nonce(issued_at)}))

    def test_nonce_without_a_timestamp_is_rejected(self):
        self.assertFalse(auth._consume_openid_nonce({"openid.response_nonce": "not-a-nonce"}))

    def test_expired_nonces_are_forgotten(self):
        now = datetime.now(timezone.utc)
        auth._used_openid_nonces[_nonce(now - timedelta(hours=1), "old")] = now - timedelta(hours=1)
        self.assertTrue(auth._consume_openid_nonce({"openid.response_nonce": _nonce(now)}))
        self.assertEqual([_nonce(now)], list(auth._used_openid_nonces))


if __name__ == "__main__":
    unittest.main()
