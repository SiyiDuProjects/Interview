from __future__ import annotations

import os
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.main import app
from app.config import get_settings
from app.services.openai_realtime import InterviewRuntime, get_interview_registry


class InterviewApiTests(unittest.TestCase):
    def setUp(self) -> None:
        get_interview_registry()._current = None
        self.environment = patch.dict(os.environ, {"INTERVIEW_ACCESS_TOKEN": "test-access"})
        self.environment.start()
        self.client = TestClient(app, base_url="https://interview.test")

    def tearDown(self) -> None:
        self.client.close()
        get_interview_registry()._current = None
        self.environment.stop()

    def create_interview(self) -> dict:
        response = self.client.post(
            "/api/interviews",
            headers={"Authorization": "Bearer test-access"},
        )
        self.assertEqual(response.status_code, 201)
        return response.json()

    def test_health_reports_current_models_and_protocol(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OPENAI_REALTIME_MODEL": "gpt-realtime-2.1",
                "OPENAI_REALTIME_TRANSCRIPTION_MODEL": "gpt-realtime-whisper",
            },
        ):
            response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["realtime_model"], "gpt-realtime-2.1")
        self.assertEqual(data["realtime_transcription_model"], "gpt-realtime-whisper")
        self.assertEqual(data["code_model"], "gpt-5.6-sol")
        self.assertEqual(data["realtime_protocol"], "realtime-interview-v4")

    def test_remote_openai_base_url_requires_https(self) -> None:
        with patch.dict(os.environ, {"OPENAI_BASE_URL": "http://api.example.com/v1"}):
            with self.assertRaises(ValueError):
                get_settings()
        with patch.dict(os.environ, {"OPENAI_BASE_URL": "http://127.0.0.1:9000/v1"}):
            self.assertEqual(get_settings().openai_base_url, "http://127.0.0.1:9000/v1")

    def test_empty_post_creates_random_token_and_authenticated_delete_cleans_up(self) -> None:
        response = self.client.post(
            "/api/interviews", headers={"Authorization": "Bearer test-access"}
        )
        self.assertEqual(response.status_code, 201)
        session = response.json()
        self.assertTrue(session["interview_id"])
        self.assertGreaterEqual(len(session["session_token"]), 32)
        self.assertGreaterEqual(len(session["capture_token"]), 32)
        self.assertNotEqual(session["session_token"], session["capture_token"])
        self.assertTrue(session["expires_at"].endswith("Z"))
        self.assertEqual(response.headers.get("cache-control"), "no-store")

        rejected = self.client.delete(
            f"/api/interviews/{session['interview_id']}",
            headers={"Authorization": "Bearer wrong"},
        )
        self.assertEqual(rejected.status_code, 401)

        deleted = self.client.delete(
            f"/api/interviews/{session['interview_id']}",
            headers={"Authorization": f"Bearer {session['session_token']}"},
        )
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(self.client.delete(f"/api/interviews/{session['interview_id']}").status_code, 404)

    def test_optional_creation_access_token(self) -> None:
        with patch.dict(os.environ, {"INTERVIEW_ACCESS_TOKEN": "create-secret"}):
            self.assertEqual(self.client.post("/api/interviews").status_code, 401)
            self.assertEqual(
                self.client.post(
                    "/api/interviews",
                    headers={"Authorization": "Bearer create-secret"},
                ).status_code,
                201,
            )

    def test_post_is_idempotent_while_current_session_is_alive(self) -> None:
        first = self.create_interview()
        second = self.create_interview()
        self.assertEqual(first, second)

    def test_websocket_first_frame_authenticates_before_upstream_and_token_is_not_in_url(self) -> None:
        session = self.create_interview()
        path = f"/ws/interviews/{session['interview_id']}/interviewer"
        ensure_main = AsyncMock(return_value=object())
        with patch.object(InterviewRuntime, "ensure_main", new=ensure_main):
            with self.client.websocket_connect(f"{path}?token={session['session_token']}") as websocket:
                websocket.send_json({"type": "authenticate", "token": "wrong"})
                with self.assertRaises(WebSocketDisconnect):
                    websocket.receive_json()
            ensure_main.assert_not_awaited()

            with self.client.websocket_connect(path) as websocket:
                websocket.send_json({"type": "authenticate", "token": session["capture_token"]})
                event = websocket.receive_json()
        self.assertEqual(event["type"], "session_ready")
        self.assertEqual(event["interview_id"], session["interview_id"])
        self.assertNotIn("token", event)
        ensure_main.assert_not_awaited()

    def test_capture_and_ui_tokens_cannot_impersonate_each_other(self) -> None:
        session = self.create_interview()
        capture_path = f"/ws/interviews/{session['interview_id']}/interviewer"
        client_path = f"/ws/interviews/{session['interview_id']}/client"
        with self.client.websocket_connect(capture_path) as websocket:
            websocket.send_json({"type": "authenticate", "token": session["session_token"]})
            with self.assertRaises(WebSocketDisconnect):
                websocket.receive_json()
        with self.client.websocket_connect(client_path) as websocket:
            websocket.send_json({"type": "authenticate", "token": session["capture_token"]})
            with self.assertRaises(WebSocketDisconnect):
                websocket.receive_json()

    def test_electron_file_origin_can_connect_after_token_authentication(self) -> None:
        session = self.create_interview()
        path = f"/ws/interviews/{session['interview_id']}/client"
        with self.client.websocket_connect(path, headers={"Origin": "file://"}) as websocket:
            websocket.send_json({"type": "authenticate", "token": session["session_token"]})
            event = websocket.receive_json()
        self.assertEqual(event["type"], "session_ready")
        self.assertEqual(event["speaker"], "client")

    def test_browser_login_cookie_and_current_bootstrap(self) -> None:
        self.assertEqual(self.client.get("/api/interviews/current").status_code, 401)
        self.assertEqual(
            self.client.post("/api/browser/login", json={"access_token": "wrong"}).status_code,
            401,
        )
        login = self.client.post("/api/browser/login", json={"access_token": "test-access"})
        self.assertEqual(login.status_code, 200)
        cookie = login.headers["set-cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=strict", cookie)
        self.assertIn("Secure", cookie)
        self.assertNotIn("test-access", cookie)
        self.assertEqual(login.headers.get("cache-control"), "no-store")
        self.assertEqual(self.client.get("/api/interviews/current").status_code, 204)

        session = self.create_interview()
        current = self.client.get("/api/interviews/current")
        self.assertEqual(current.status_code, 200)
        data = current.json()
        self.assertEqual(data["interview_id"], session["interview_id"])
        self.assertEqual(data["session_token"], session["session_token"])
        self.assertNotIn("capture_token", data)
        self.assertEqual(data["device_status"]["status"], "offline")
        self.assertFalse(data["interview_state"]["active"])
        self.assertEqual(current.headers.get("cache-control"), "no-store")

    def test_empty_access_token_is_local_only(self) -> None:
        with patch.dict(os.environ, {"INTERVIEW_ACCESS_TOKEN": ""}):
            self.assertEqual(self.client.post("/api/interviews").status_code, 503)
            self.assertEqual(
                self.client.post("/api/browser/login", json={"access_token": "local"}).status_code,
                503,
            )
            self.assertEqual(self.client.get("/api/interviews/current").status_code, 503)

            local = TestClient(
                app,
                base_url="http://127.0.0.1",
                client=("127.0.0.1", 50000),
            )
            try:
                self.assertEqual(local.get("/api/interviews/current").status_code, 204)
                self.assertEqual(local.post("/api/interviews").status_code, 201)
            finally:
                local.close()

    def test_removed_preview_and_legacy_websocket_are_not_routes(self) -> None:
        self.assertIn(self.client.post("/api/context/preview", json={}).status_code, {404, 405})
        self.assertIn(self.client.post("/api/coach/respond", json={}).status_code, {404, 405})

    def test_cors_allows_local_vite_not_arbitrary_origin(self) -> None:
        allowed = self.client.options(
            "/api/interviews",
            headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST"},
        )
        denied = self.client.options(
            "/api/interviews",
            headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
        )
        self.assertEqual(allowed.headers.get("access-control-allow-origin"), "http://localhost:5173")
        self.assertNotEqual(denied.headers.get("access-control-allow-origin"), "https://evil.example")


if __name__ == "__main__":
    unittest.main()
