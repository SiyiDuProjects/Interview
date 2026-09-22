from __future__ import annotations

import io
import asyncio
import base64
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from app.main import app, health, InterviewStaticFiles
from app.services.openai_realtime import InterviewRegistry, InterviewRuntime, OpenAIRealtimeError
from deploy.probe import EXPECTED_HEALTH, request_json, snapshot_health, validate_drain, validate_health
from deploy.release import compose_context_signature, context_mount_signature, main as release_main, release_config, update_environment


class CaptureAndDeploymentApiTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {"INTERVIEW_ACCESS_TOKEN": "synthetic-access", "INTERVIEW_SCREENSHOT_MAX_BYTES": "1024"})
        self.environment.start()
        self.runtime = SimpleNamespace(
            active=True, closed=False,
            capture_token_matches=lambda token: token == "synthetic-capture",
            accept_screen_snapshot=AsyncMock(return_value=True),
        )
        self.registry = SimpleNamespace(
            get=AsyncMock(return_value=self.runtime),
            deployment_state=AsyncMock(return_value={"active": False, "draining": True}),
            begin_deployment=AsyncMock(return_value=True),
            cancel_deployment=AsyncMock(),
            create=AsyncMock(side_effect=OpenAIRealtimeError("Deployment is in progress.")),
        )
        self.registry_patch = patch("app.main.get_interview_registry", return_value=self.registry)
        self.registry_patch.start()
        self.client = TestClient(app, base_url="https://interview.test")
        self.url = "/api/interviews/synthetic/screenshots"
        self.headers = {"Authorization": "Bearer synthetic-capture"}
        self.payload = {"request_id": "synthetic:screen-1", "image_data": "data:image/png;base64,synthetic",
                        "source_id": "screen:1", "captured_at": "2026-09-07T00:00:00Z"}

    def tearDown(self):
        self.client.close()
        self.registry_patch.stop()
        self.environment.stop()

    def test_screenshot_requires_capture_token_before_body_handling(self):
        for headers in ({}, {"Authorization": "Bearer synthetic-access"}, {"Authorization": "Bearer synthetic-session"}):
            response = self.client.post(self.url, headers=headers, content=b"x" * 20000)
            self.assertEqual(response.status_code, 401)
        self.runtime.accept_screen_snapshot.assert_not_awaited()

    def test_screenshot_success_forwards_only_image_and_source_metadata(self):
        response = self.client.post(self.url, headers=self.headers, json={**self.payload, "error": "untrusted", "token": "ignored"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.runtime.accept_screen_snapshot.assert_awaited_once_with(self.payload)
        self.assertNotIn("token", response.text)

    def test_screenshot_rejects_inactive_missing_and_stale_requests(self):
        self.runtime.active = False
        self.assertEqual(self.client.post(self.url, headers=self.headers, json=self.payload).status_code, 409)
        self.runtime.accept_screen_snapshot.assert_not_awaited()
        self.runtime.active = True
        self.runtime.accept_screen_snapshot.return_value = False
        self.assertEqual(self.client.post(self.url, headers=self.headers, json=self.payload).status_code, 409)
        self.registry.get.return_value = None
        self.assertEqual(self.client.post(self.url, headers=self.headers, json=self.payload).status_code, 404)

    def test_screenshot_bounds_chunked_body_and_invalid_metadata(self):
        response = self.client.post(self.url, headers=self.headers, content=iter([b"x" * 6000, b"y" * 6000]))
        self.assertEqual(response.status_code, 413)
        for payload in ([], {"request_id": "x"}, {**self.payload, "source_id": []}, {**self.payload, "captured_at": "x" * 81}):
            self.assertEqual(self.client.post(self.url, headers=self.headers, json=payload).status_code, 400)
        self.assertEqual(self.client.post(self.url, headers=self.headers, content=b"not-json").status_code, 400)
        self.runtime.accept_screen_snapshot.assert_not_awaited()

    def test_screenshot_maps_runtime_validation_errors(self):
        self.runtime.accept_screen_snapshot.side_effect = OpenAIRealtimeError("Screenshot bytes do not match the declared MIME type.")
        self.assertEqual(self.client.post(self.url, headers=self.headers, json=self.payload).status_code, 400)
        self.runtime.accept_screen_snapshot.side_effect = OpenAIRealtimeError("Screenshot exceeds the configured size limit.")
        self.assertEqual(self.client.post(self.url, headers=self.headers, json=self.payload).status_code, 413)

    def test_deployment_requires_service_bearer_not_browser_cookie(self):
        self.client.post("/api/browser/login", json={"access_token": "synthetic-access"})
        for method in (self.client.get, self.client.post, self.client.delete):
            self.assertEqual(method("/api/deployment").status_code, 401)
            self.assertEqual(method("/api/deployment", headers=self.headers).status_code, 401)
        self.registry.begin_deployment.assert_not_awaited()

    def test_deployment_conflict_and_drain_state(self):
        headers = {"Authorization": "Bearer synthetic-access"}
        self.registry.begin_deployment.return_value = False
        self.assertEqual(self.client.post("/api/deployment", headers=headers).status_code, 409)
        self.registry.begin_deployment.return_value = True
        result = self.client.post("/api/deployment", headers=headers)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json(), {"active": False, "draining": True})
        self.assertEqual(result.headers["cache-control"], "no-store")
        self.registry.deployment_state.return_value = {"active": False, "draining": False}
        self.assertEqual(self.client.delete("/api/deployment", headers=headers).json(), {"active": False, "draining": False})
        self.registry.cancel_deployment.assert_awaited_once()
        self.assertEqual(self.client.post("/api/interviews", headers=headers).status_code, 503)


class DeploymentFixtureTests(unittest.TestCase):
    def test_web_entrypoints_revalidate_across_releases_including_conditional_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "index.html").write_text("<!doctype html><title>Interview</title>", encoding="utf-8")
            (root / "pcm-worklet.js").write_text("// synthetic worklet fixture", encoding="utf-8")
            web = FastAPI()
            web.mount("/", InterviewStaticFiles(directory=root, html=True))
            with TestClient(web) as client:
                for path in ("/", "/index.html", "/pcm-worklet.js"):
                    with self.subTest(path=path):
                        response = client.get(path)
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(response.headers["cache-control"], "no-cache")
                        unchanged = client.get(path, headers={"If-None-Match": response.headers["etag"]})
                        self.assertEqual(unchanged.status_code, 304)
                        self.assertEqual(unchanged.headers["cache-control"], "no-cache")
                self.assertEqual(client.get("/missing.js").status_code, 404)

    def test_real_runtime_accepts_pending_http_image_only_once(self):
        async def run():
            runtime = InterviewRuntime(
                interview_id="synthetic", session_token="synthetic-session", capture_token="synthetic-capture",
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                context_store=SimpleNamespace(documents=lambda: []),
            )
            runtime.active = True
            future = asyncio.get_running_loop().create_future()
            runtime.pending_screen_requests["synthetic:screen"] = future
            image = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\nsynthetic").decode("ascii")
            payload = {"request_id": "synthetic:screen", "image_data": image, "source_id": "screen:2", "captured_at": "2026-09-07T00:00:00Z"}
            registry = SimpleNamespace(get=AsyncMock(return_value=runtime))
            with patch("app.main.get_interview_registry", return_value=registry):
                async with AsyncClient(transport=ASGITransport(app=app), base_url="https://interview.test") as client:
                    path = "/api/interviews/synthetic/screenshots"
                    unauthorized = await client.post(path, json=payload, headers={"Authorization": "Bearer synthetic-session"})
                    self.assertEqual(unauthorized.status_code, 401)
                    self.assertFalse(future.done())
                    headers = {"Authorization": "Bearer synthetic-capture"}
                    valid = await client.post(path, json=payload, headers=headers)
                    self.assertEqual(valid.status_code, 200)
                    self.assertEqual(await future, image)
                    self.assertEqual(runtime._screen_metadata["synthetic:screen"]["source_id"], "screen:2")
                    repeated = await client.post(path, json=payload, headers=headers)
                    self.assertEqual(repeated.status_code, 409)
        asyncio.run(run())

    def test_real_registry_http_gate_blocks_create_and_existing_idle_start(self):
        class Socket:
            def __init__(self): self.messages = []
            async def send_json(self, payload): self.messages.append(payload)
            async def close(self, **_kwargs): pass

        async def run():
            registry = InterviewRegistry()
            runtime = await registry.create()
            ui, interviewer, candidate = Socket(), Socket(), Socket()
            runtime._ui_clients["ui"] = ui
            runtime._ready_ui_clients.add("ui")
            runtime._capture_clients.update({"interviewer": interviewer, "candidate": candidate})
            runtime._capture_ready.update({"interviewer", "candidate"})
            headers = {"Authorization": "Bearer synthetic-access"}
            with patch("app.main.get_interview_registry", return_value=registry):
                async with AsyncClient(transport=ASGITransport(app=app), base_url="https://interview.test") as client:
                    began = await client.post("/api/deployment", headers=headers)
                    self.assertEqual(began.json(), {"active": False, "draining": True})
                    self.assertEqual((await client.post("/api/interviews", headers=headers)).status_code, 503)
                    await runtime.start_interview(ui)
                    self.assertFalse(runtime.active)
                    self.assertFalse(interviewer.messages)
                    self.assertEqual(ui.messages[-1]["type"], "error")
                    ended = await client.delete("/api/deployment", headers=headers)
                    self.assertEqual(ended.json(), {"active": False, "draining": False})
                    await runtime.start_interview(ui)
                    self.assertTrue(runtime.active)
                    self.assertEqual((await client.post("/api/deployment", headers=headers)).status_code, 409)
                    self.assertEqual((await client.get("/api/deployment", headers=headers)).json(), {"active": True, "draining": False})
            await registry.clear()

        with patch.dict(os.environ, {"INTERVIEW_ACCESS_TOKEN": "synthetic-access", "INTERVIEW_START_DRAINED": "0"}), \
             patch("app.services.openai_realtime.ContextStore", return_value=SimpleNamespace(documents=lambda: [])), \
             patch("app.services.openai_realtime._connect_openai_realtime", new=AsyncMock(side_effect=AssertionError("No upstream allowed"))):
            asyncio.run(run())

    def test_real_registry_drain_and_start_are_atomic_during_capture_send(self):
        class Socket:
            async def send_json(self, _payload): pass
            async def close(self, **_kwargs): pass

        async def run():
            entered, release = asyncio.Event(), asyncio.Event()
            class BlockedSocket(Socket):
                async def send_json(self, payload):
                    if payload.get("type") == "capture_start":
                        entered.set()
                        await release.wait()
            registry = InterviewRegistry()
            runtime = await registry.create()
            ui = Socket()
            runtime._ui_clients["ui"] = ui
            runtime._ready_ui_clients.add("ui")
            runtime._capture_clients.update({"interviewer": BlockedSocket(), "candidate": Socket()})
            runtime._capture_ready.update({"interviewer", "candidate"})
            starting = asyncio.create_task(runtime.start_interview(ui))
            await entered.wait()
            draining = asyncio.create_task(registry.begin_deployment())
            await asyncio.sleep(0)
            self.assertFalse(draining.done())
            release.set()
            await starting
            self.assertFalse(await draining)
            self.assertEqual(await registry.deployment_state(), {"active": True, "draining": False})
            await registry.clear()
        with patch.dict(os.environ, {"INTERVIEW_START_DRAINED": "0"}), \
             patch("app.services.openai_realtime.ContextStore", return_value=SimpleNamespace(documents=lambda: [])):
            asyncio.run(run())

    def test_candidate_container_starts_with_registry_gate_closed(self):
        async def run():
            registry = InterviewRegistry()
            self.assertEqual(await registry.deployment_state(), {"active": False, "draining": True})
            with self.assertRaises(OpenAIRealtimeError):
                await registry.create()
            await registry.cancel_deployment()
            self.assertEqual(await registry.deployment_state(), {"active": False, "draining": False})
        with patch.dict(os.environ, {"INTERVIEW_START_DRAINED": "1"}):
            asyncio.run(run())

    def test_health_rejects_wrong_protocol_models_and_html(self):
        self.assertEqual(validate_health(dict(EXPECTED_HEALTH)), EXPECTED_HEALTH)
        for field in EXPECTED_HEALTH:
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_health({**EXPECTED_HEALTH, field: "wrong"})
        with self.assertRaises(ValueError):
            validate_health("<html>Login</html>")
        expected_release = {**EXPECTED_HEALTH, "release_id": "new-release"}
        with self.assertRaises(ValueError):
            validate_health({**EXPECTED_HEALTH, "release_id": "previous-release"}, expected_release)
        self.assertEqual(validate_health(expected_release, expected_release), expected_release)

    def test_rollback_snapshot_can_describe_previous_realtime_release(self):
        previous = {"status": "ok", "realtime_protocol": "realtime-interview-v4",
                    "realtime_model": "gpt-realtime-2.1", "realtime_transcription_model": "gpt-realtime-whisper",
                    "realtime_reasoning_effort": "low", "code_model": "gpt-6-astra", "release_id": "previous"}
        self.assertEqual(snapshot_health({**previous, "unrelated": "excluded"}), previous)
        self.assertEqual(validate_health(previous, snapshot_health(previous)), previous)
        with self.assertRaises(ValueError):
            validate_health(previous)  # Never accept the old release as the new one.

    def test_gate_rejects_active_or_unacknowledged_drain(self):
        self.assertEqual(validate_drain({"active": False, "draining": True}, drained=True), {"active": False, "draining": True})
        for value in ({"active": True, "draining": True}, {"active": False, "draining": False}, {"active": "false", "draining": True}):
            with self.assertRaises(ValueError):
                validate_drain(value, drained=True)

    def test_health_never_sends_access_token_and_admin_stays_loopback(self):
        class Reply(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *_args): self.close()
        with patch.dict(os.environ, {"INTERVIEW_ACCESS_TOKEN": "synthetic-only"}), patch("deploy.probe.urlopen", return_value=Reply(b'{}')) as opener:
            request_json("health", "https://interview.example/health")
            self.assertIsNone(opener.call_args.args[0].get_header("Authorization"))
        with patch.dict(os.environ, {"INTERVIEW_ACCESS_TOKEN": "synthetic-only"}), patch("deploy.probe.urlopen", return_value=Reply(b'{}')) as opener:
            request_json("begin", "https://ignored.example")
            self.assertEqual(opener.call_args.args[0].full_url, "http://127.0.0.1:8000/api/deployment")
            self.assertEqual(opener.call_args.args[0].get_header("Authorization"), "Bearer synthetic-only")

    def test_all_probes_identify_the_healthcheck_and_preserve_credential_boundaries(self):
        for action in ("health", "snapshot", "begin", "cancel", "status"):
            with self.subTest(action=action), patch.dict(os.environ, {"INTERVIEW_ACCESS_TOKEN": "synthetic-only"}), \
                 patch("deploy.probe.urlopen", return_value=io.BytesIO(b"{}")) as opener:
                request_json(action, "https://interview.example/health")
                request = opener.call_args.args[0]
                self.assertEqual(request.get_header("User-agent"), "Interview-Deployment-Healthcheck/1.0")
                if action in {"begin", "cancel", "status"}:
                    self.assertEqual(request.full_url, "http://127.0.0.1:8000/api/deployment")
                    self.assertEqual(request.get_header("Authorization"), "Bearer synthetic-only")
                else:
                    self.assertEqual(request.full_url, "https://interview.example/health")
                    self.assertIsNone(request.get_header("Authorization"))

    def test_compose_plan_changes_only_named_service_and_has_stable_start_mode(self):
        original = {"name": "siyi", "services": {"interview_api": {"build": {"context": "/opt/interview/server"}, "environment": {"EXISTING": "keep"}}, "unrelated": {"image": "keep"}}}
        candidate = release_config(original, "interview-release:fixture", drained=True)
        stable = release_config(original, "interview-release:fixture", drained=False)
        self.assertEqual(candidate["services"]["unrelated"], original["services"]["unrelated"])
        self.assertNotIn("image", original["services"]["interview_api"])
        self.assertEqual(candidate["services"]["interview_api"]["environment"]["INTERVIEW_START_DRAINED"], "1")
        self.assertEqual(stable["services"]["interview_api"]["environment"]["INTERVIEW_START_DRAINED"], "0")
        self.assertEqual(stable["name"], original["name"])

    def test_model_update_preserves_unrelated_environment_and_removes_duplicate_settings(self):
        result = update_environment("# keep\nINTERVIEW_ACCESS_TOKEN=synthetic\nOPENAI_CODE_MODEL=old\nOPENAI_CODE_MODEL=duplicate\n", release_id="current-release")
        self.assertIn("INTERVIEW_ACCESS_TOKEN=synthetic\n", result)
        self.assertIn("# keep\n", result)
        self.assertEqual(result.count("OPENAI_CODE_MODEL="), 1)
        self.assertIn("OPENAI_CODE_MODEL=gpt-6-astra\n", result)

    def test_transcription_upgrade_migrates_language_without_overwriting_plural_setting(self):
        result = update_environment("OPENAI_REALTIME_TRANSCRIPTION_MODEL=gpt-realtime-whisper\n"
            "OPENAI_REALTIME_TRANSCRIPTION_LANGUAGE=zh\n", release_id="current-release")
        self.assertIn("OPENAI_REALTIME_TRANSCRIPTION_MODEL=gpt-live-transcribe\n", result)
        self.assertIn("OPENAI_REALTIME_TRANSCRIPTION_LANGUAGES=zh\n", result)
        self.assertNotIn("OPENAI_REALTIME_TRANSCRIPTION_LANGUAGE=", result)
        result = update_environment("OPENAI_REALTIME_TRANSCRIPTION_LANGUAGE=zh\n"
            "OPENAI_REALTIME_TRANSCRIPTION_LANGUAGES=en,zh\n", release_id="current-release")
        self.assertIn("OPENAI_REALTIME_TRANSCRIPTION_LANGUAGES=en,zh\n", result)
        self.assertNotIn("OPENAI_REALTIME_TRANSCRIPTION_LANGUAGE=", result)

    def test_persisted_release_settings_survive_environment_reload_without_starting_drained(self):
        result = update_environment(
            "INTERVIEW_RELEASE_ID=previous-release\nINTERVIEW_RELEASE_ID=stale-duplicate\n"
            "INTERVIEW_START_DRAINED=1\nINTERVIEW_START_DRAINED=1\n",
            release_id="current-release",
        )
        self.assertEqual(result.count("INTERVIEW_RELEASE_ID="), 1)
        self.assertEqual(result.count("INTERVIEW_START_DRAINED="), 1)
        environment = dict(line.split("=", 1) for line in result.splitlines())
        with patch.dict(os.environ, environment):
            self.assertEqual(health()["release_id"], "current-release")
            self.assertFalse(InterviewRegistry().draining)

    def test_environment_update_rejects_invalid_release_id(self):
        for release_id in ("", "release\nINTERVIEW_START_DRAINED=1", "x" * 81):
            with self.subTest(release_id=release_id), self.assertRaisesRegex(ValueError, "Invalid release id"):
                update_environment("UNCHANGED=synthetic\n", release_id=release_id)

    def test_private_context_accepts_read_only_external_bind_and_named_volume(self):
        deploy = Path("/opt/interview/server")
        bind = {"Type": "bind", "Source": "/srv/interview-private", "Destination": "/private", "RW": False}
        environment = {"INTERVIEW_CONTEXT_DIR": "/private/documents"}
        config = {"services": {"interview_api": {"environment": environment,
                  "volumes": [{"type": "bind", "source": bind["Source"], "target": "/private", "read_only": True}]}}}
        self.assertEqual(context_mount_signature(environment, [bind], deploy_path=deploy),
                         compose_context_signature(config, deploy_path=deploy))
        volume = {"Type": "volume", "Name": "private-documents", "Destination": "/app/context", "RW": False}
        config = {"services": {"interview_api": {"volumes": [{"type": "volume", "source": "documents",
                  "target": "/app/context", "read_only": True}]}}, "volumes": {"documents": {"name": "private-documents"}}}
        self.assertEqual(context_mount_signature({}, [volume], deploy_path=deploy),
                         compose_context_signature(config, deploy_path=deploy))

    def test_private_context_rejects_missing_writable_internal_and_shadowed_mounts(self):
        deploy = Path("/opt/interview/server")
        valid = {"Type": "bind", "Source": "/srv/interview-private", "Destination": "/app/context", "RW": False}
        cases = [[], [{**valid, "RW": True}], [{**valid, "RW": "false"}],
                 [{**valid, "Source": "/opt/interview/server/private"}],
                 [valid, {**valid, "Destination": "/app/context/nested", "RW": True}],
                 [{**valid, "Destination": "/app"}, {**valid, "RW": True}],
                 [{**valid, "Type": "tmpfs"}]]
        for mounts in cases:
            with self.subTest(mounts=mounts), self.assertRaisesRegex(RuntimeError, "read-only mount"):
                context_mount_signature({}, mounts, deploy_path=deploy)
        # An ancestor bind can still map the effective context back into deployed source.
        with self.assertRaisesRegex(RuntimeError, "read-only mount"):
            context_mount_signature({"INTERVIEW_CONTEXT_DIR": "/host/interview/server/context"},
                [{**valid, "Source": "/opt", "Destination": "/host"}], deploy_path=deploy)
        with self.assertRaisesRegex(RuntimeError, "read-only mount"):
            context_mount_signature({"INTERVIEW_CONTEXT_DIR": "relative/context"}, [valid], deploy_path=deploy)

    def test_release_refuses_active_or_legacy_server_before_mutating_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            deploy, compose, staging = [root / name for name in ("server", "compose", "incoming")]
            for directory in (deploy, compose, staging / "deploy"):
                directory.mkdir(parents=True)
            (deploy / ".env").write_text("UNCHANGED=synthetic\n", encoding="utf-8")
            (staging / "Dockerfile").write_text("FROM synthetic", encoding="utf-8")
            (staging / "deploy" / "probe.py").write_text("# fake", encoding="utf-8")
            with patch("deploy.release.sys.platform", "linux"), patch("deploy.release.os.geteuid", return_value=0, create=True), \
                 patch("deploy.release.container_probe", side_effect=[dict(EXPECTED_HEALTH), RuntimeError("active or legacy")]), \
                 patch("deploy.release.run") as command:
                with self.assertRaisesRegex(RuntimeError, "Deployment refused"):
                    release_main(["--deploy-path", str(deploy), "--compose-path", str(compose), "--staging-path", str(staging),
                                  "--health-url", "https://interview.example/health", "--release-id", "fixture"])
            command.assert_not_called()
            self.assertEqual((deploy / ".env").read_text(encoding="utf-8"), "UNCHANGED=synthetic\n")

    def _release_fixture(self, failure):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            deploy, compose, staging = [root / name for name in ("server", "compose", "incoming")]
            for directory in (deploy / "context", compose, staging / "deploy"):
                directory.mkdir(parents=True)
            original_environment = "UNCHANGED=synthetic\nOPENAI_CODE_MODEL=old-model\nINTERVIEW_RELEASE_ID=previous-release\nINTERVIEW_START_DRAINED=0\n"
            (deploy / ".env").write_text(original_environment, encoding="utf-8")
            (deploy / "context" / "private.txt").write_text("synthetic private context", encoding="utf-8")
            (staging / "Dockerfile").write_text("FROM synthetic", encoding="utf-8")
            (staging / "deploy" / "probe.py").write_text("# fake", encoding="utf-8")
            config = {"name": "fixture", "services": {"interview_api": {"image": "fixture-original", "environment": {"PRESERVE": "synthetic"},
                "volumes": [{"type": "bind", "source": "/srv/interview-private", "target": "/app/context", "read_only": True}]}, "other": {"image": "untouched"}}}
            commands, gate_actions, health_checks = [], [], []

            def fake_command(command, **_kwargs):
                commands.append(command)
                if "inspect" in command:
                    if "{{json .Config.Env}}" in command:
                        return json.dumps(["OPENAI_CODE_MODEL=old-model", "INTERVIEW_RELEASE_ID=previous-release"])
                    if "{{json .Mounts}}" in command:
                        return json.dumps([{"Type": "bind", "Source": "/srv/interview-private", "Destination": "/app/context", "RW": failure == "unsafe-context"}])
                    return "sha256:synthetic-old-image"
                if "config" in command:
                    resolved = json.loads(json.dumps(config))
                    environment = dict(line.split("=", 1) for line in (deploy / ".env").read_text(encoding="utf-8").splitlines())
                    resolved["services"]["interview_api"]["environment"].update(environment)
                    if failure == "context-mismatch":
                        resolved["services"]["interview_api"]["volumes"][0]["source"] = "/srv/different-private"
                    return json.dumps(resolved)
                if "build" in command and failure == "build":
                    raise RuntimeError("synthetic build failure")
                return ""

            def fake_probe(_source, action, **_kwargs):
                gate_actions.append(action)
                if action == "snapshot":
                    return {**EXPECTED_HEALTH, "code_model": "old-model", "release_id": "previous-release"}
                if action == "begin" and gate_actions.count("begin") == 2 and failure == "active-after-finalize":
                    raise RuntimeError("synthetic active interview")
                return {"active": False, "draining": action != "cancel"}

            def fake_health(*_args, **kwargs):
                health_checks.append(kwargs)
                if len(health_checks) == 2 and failure == "active-after-finalize":
                    raise RuntimeError("synthetic public health failure")

            with patch("deploy.release.sys.platform", "linux"), patch("deploy.release.os.geteuid", return_value=0, create=True), \
                 patch("deploy.release.run", side_effect=fake_command), patch("deploy.release.container_probe", side_effect=fake_probe), \
                 patch("deploy.release.wait_health", side_effect=fake_health), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                arguments = ["--deploy-path", str(deploy), "--compose-path", str(compose), "--staging-path", str(staging),
                             "--health-url", "https://interview.example/health", "--release-id", "fixture"]
                if failure:
                    with self.assertRaises(RuntimeError):
                        release_main(arguments)
                else:
                    self.assertEqual(release_main(arguments), 0)
            if failure in {"unsafe-context", "context-mismatch"}:
                self.assertEqual((deploy / ".env").read_text(encoding="utf-8"), original_environment)
                self.assertFalse(any(command[0] == "rsync" or any(action in command for action in ("tag", "up", "build")) for command in commands))
                self.assertEqual(gate_actions, ["snapshot", "begin", "cancel"])
                return commands, gate_actions, health_checks
            retained = root / "server.deploy-backups" / "fixture"
            self.assertEqual((retained / "source" / ".env").read_text(encoding="utf-8"), original_environment)
            self.assertEqual((deploy / "context" / "private.txt").read_text(encoding="utf-8"), "synthetic private context")
            self.assertTrue((retained / "rollback-compose.json").is_file())
            rollback = json.loads((retained / "rollback-compose.json").read_text(encoding="utf-8"))
            self.assertEqual(rollback["services"]["interview_api"]["environment"]["INTERVIEW_RELEASE_ID"], "previous-release")
            for command in commands:
                self.assertNotIn("--remove-orphans", command)
                self.assertNotIn("rm", command)
                if "up" in command:
                    self.assertEqual(command[-1], "interview_api")
                    self.assertIn("--no-deps", command)
                if command[0] == "rsync":
                    self.assertIn("--exclude=/context", command)
            if failure == "build":
                self.assertEqual((deploy / ".env").read_text(encoding="utf-8"), original_environment)
            else:
                environment = dict(line.split("=", 1) for line in (deploy / ".env").read_text(encoding="utf-8").splitlines())
                self.assertEqual(environment["INTERVIEW_RELEASE_ID"], "fixture")
                self.assertEqual(environment["INTERVIEW_START_DRAINED"], "0")
                self.assertEqual(environment["UNCHANGED"], "synthetic")
                candidate = json.loads((retained / "candidate-compose.json").read_text(encoding="utf-8"))
                self.assertEqual(candidate["services"]["interview_api"]["environment"]["INTERVIEW_START_DRAINED"], "1")
                self.assertEqual(candidate["services"]["interview_api"]["environment"]["INTERVIEW_RELEASE_ID"], "fixture")
            return commands, gate_actions, health_checks

    def test_release_fixture_verifies_drained_candidate_then_stable_container(self):
        commands, actions, health = self._release_fixture(None)
        ups = [command[3] for command in commands if "up" in command]
        self.assertTrue(ups[0].endswith("candidate-compose.json"))
        self.assertTrue(ups[1].endswith("stable-compose.json"))
        self.assertEqual(len(ups), 2)
        self.assertEqual(actions, ["snapshot", "begin"])
        self.assertEqual(len(health), 2)
        self.assertTrue(all(check["public_url"] for check in health))
        self.assertTrue(all(check["expected"]["release_id"] == "fixture" for check in health))

    def test_release_fixture_restores_prior_environment_after_build_failure(self):
        commands, _actions, _health = self._release_fixture("build")
        ups = [command[3] for command in commands if "up" in command]
        self.assertTrue(ups[0].endswith("rollback-candidate-compose.json"))
        self.assertTrue(ups[1].endswith("rollback-compose.json"))

    def test_release_fixture_does_not_rollback_a_new_active_interview(self):
        commands, actions, _health = self._release_fixture("active-after-finalize")
        ups = [command[3] for command in commands if "up" in command]
        self.assertEqual(len(ups), 2)
        self.assertFalse(any("rollback" in path for path in ups))
        self.assertEqual(actions, ["snapshot", "begin", "begin"])

    def test_release_fixture_unlocks_before_mutation_if_private_mount_is_unsafe_or_changed(self):
        for failure in ("unsafe-context", "context-mismatch"):
            with self.subTest(failure=failure):
                self._release_fixture(failure)


if __name__ == "__main__":
    unittest.main()
