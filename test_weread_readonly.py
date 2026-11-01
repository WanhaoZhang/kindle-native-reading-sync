"""Offline only: no accounts, no service requests, all credentials synthetic."""
import contextlib
import hashlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

import weread_readonly as bridge


class OfflineConnectorTests(unittest.TestCase):
    def test_help_and_missing_online_never_create_transport(self):
        with patch.object(bridge, "Transport") as transport:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(bridge.main([]), 0)
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                bridge.main(["login"])
            transport.assert_not_called()

    def test_login_signature_and_units(self):
        body = bridge.login_body("synthetic-code", "synthetic-device", now_ms=1793534400456, random_value=7)
        self.assertEqual(body["signature"], hashlib.sha256(b"1793534400456synthetic-device7").hexdigest())
        self.assertEqual(body["timestamp"], 1793534400456)
        self.assertNotIn("rt", body)

    def test_mocked_qr_login_without_web_login_or_api_key(self):
        class Mock:
            def __init__(self): self.calls = []
            def request(self, method, url, **kwargs):
                self.calls.append((method, url, kwargs))
                if url.endswith("/wxticket"):
                    return {"signature": "synthetic-ticket", "timeStamp": 1793534400}
                if url.endswith("/sdk/qrconnect"):
                    return {"errcode": 0, "uuid": "synthetic-uuid"}
                if url.endswith("/l/qrconnect"):
                    return {"wx_errcode": 405, "wx_code": "synthetic-auth-code"}
                if url.endswith("/login"):
                    return {"vid": 123, "accessToken": "synthetic-access", "refreshToken": "synthetic-refresh"}
                raise AssertionError("Unexpected route")
        client, displayed = Mock(), []
        auth = bridge.login(client, displayed.append)
        self.assertEqual(auth["vid"], "123")
        self.assertEqual(len(displayed), 1)
        self.assertEqual(sum(method == "POST" for method, _, _ in client.calls), 1)
        self.assertTrue(all("credentials" not in kwargs for _, _, kwargs in client.calls))
        self.assertEqual(client.calls[-1][2]["body"]["code"], "synthetic-auth-code")

    def test_expired_qr_never_exchanges(self):
        from unittest.mock import Mock
        client = Mock()
        client.request.side_effect = [
            {"signature": "synthetic-ticket", "timeStamp": 1793534400},
            {"errcode": 0, "uuid": "synthetic-uuid"}, {"wx_errcode": 402},
        ]
        with self.assertRaises(bridge.BridgeError): bridge.login(client, lambda _: None)
        self.assertEqual(client.request.call_count, 3)

    def test_uncertain_login_exchange_never_retries(self):
        from unittest.mock import Mock
        client = Mock()
        client.request.side_effect = [
            {"signature": "synthetic-ticket", "timeStamp": 1793534400},
            {"errcode": 0, "uuid": "synthetic-uuid"},
            {"wx_errcode": 405, "wx_code": "synthetic-code"},
            bridge.BridgeError("timeout"),
        ]
        with self.assertRaises(bridge.BridgeError): bridge.login(client, lambda _: None)
        self.assertEqual(client.request.call_count, 4)

    def test_upload_routes_and_credential_leaks_rejected_before_io(self):
        client = bridge.Transport()
        with patch.object(client.opener, "open") as network:
            for method, url, opts in (
                ("POST", bridge.BASE + "/book/batchUploadProgress", {}),
                ("POST", "https://weread.qq.com/web/book/read", {}),
                ("GET", "https://long.open.weixin.qq.com/connect/l/qrconnect", {"credentials": {}}),
            ):
                if opts:
                    opts["credentials"] = {"vid": "synthetic", "accessToken": "synthetic"}
                with self.assertRaises(bridge.BridgeError): client.request(method, url, **opts)
            network.assert_not_called()

    def test_http_error_does_not_expose_auth_body_or_url(self):
        client = bridge.Transport()
        err = urllib.error.HTTPError("https://secret.invalid/synthetic-token", 401,
                                     "synthetic-token", {}, io.BytesIO(b"synthetic-token"))
        with patch.object(client.opener, "open", side_effect=err):
            with self.assertRaises(bridge.BridgeError) as caught:
                client.request("POST", bridge.BASE + "/login", body={"code": "synthetic-token"})
        self.assertNotIn("synthetic-token", str(caught.exception))
        self.assertIn("401", str(caught.exception))

    def test_atomic_private_credentials_no_clobber(self):
        fake = dict(vid="synthetic", accessToken="synthetic-access", refreshToken="synthetic-refresh", deviceId="synthetic-device")
        with tempfile.TemporaryDirectory() as temp:
            dest = Path(temp) / ".weread" / "auth.json"
            bridge.save_auth(dest, fake)
            self.assertEqual(dest.stat().st_mode & 0o777, 0o600)
            self.assertEqual(bridge.load_auth(dest), fake)
            with self.assertRaises(bridge.BridgeError): bridge.save_auth(dest, fake)
            os.chmod(dest, 0o644)
            with self.assertRaises(bridge.BridgeError): bridge.load_auth(dest)


if __name__ == "__main__":
    unittest.main()
