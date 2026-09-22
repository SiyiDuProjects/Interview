"""Unpaid configuration/drain probes; never calls a model provider."""

from __future__ import annotations

import argparse
import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


EXPECTED_HEALTH = {
    "status": "ok",
    "realtime_protocol": "realtime-interview-v5",
    "live_model": "gpt-live-1",
    "realtime_transcription_model": "gpt-live-transcribe",
    "code_reasoning_effort": "high",
    "code_model": "gpt-6-astra",
}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, _request, _file, _code, _message, _headers, _new_url):
        return None


def urlopen(request: Request, *, timeout: float):
    # In particular, never forward the deployment bearer to a redirect target.
    return build_opener(_NoRedirect()).open(request, timeout=timeout)


def validate_health(payload: object, expected: dict[str, str] | None = None) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise ValueError("Health response must be a JSON object.")
    fields = EXPECTED_HEALTH if expected is None else expected
    for field, value in fields.items():
        if payload.get(field) != value:
            raise ValueError(f"Health field did not match the release: {field}.")
    return {field: str(payload[field]) for field in fields}


def validate_drain(payload: object, *, drained: bool) -> dict[str, bool]:
    if not isinstance(payload, dict) or type(payload.get("active")) is not bool or type(payload.get("draining")) is not bool:
        raise ValueError("Deployment state is invalid.")
    if payload["draining"] is not drained or (drained and payload["active"]):
        raise ValueError("Deployment gate is not in the requested safe state.")
    return {"active": payload["active"], "draining": payload["draining"]}


def snapshot_health(payload: object) -> dict[str, str]:
    # A rollback baseline describes the running release, including the previous
    # Realtime architecture; it must not require fields only in the new release.
    required = {"status", "realtime_protocol", "realtime_transcription_model", "code_model"}
    if not isinstance(payload, dict) or any(not isinstance(payload.get(key), str) or not payload[key] for key in required):
        raise ValueError("Existing health response is incomplete.")
    if payload["status"] != "ok" or not (payload.get("live_model") or payload.get("realtime_model")):
        raise ValueError("Existing service is not healthy or has no primary model.")
    allowed = set(EXPECTED_HEALTH) | {"realtime_model", "realtime_reasoning_effort", "release_id"}
    return {key: value for key, value in payload.items() if key in allowed and isinstance(value, str) and value}


def request_json(action: str, url: str | None = None) -> object:
    if action in {"begin", "cancel", "status"}:
        # Credentials never leave the container's loopback interface or appear
        # on a shell command line. Browser cookies cannot authorize deployment.
        target = "http://127.0.0.1:8000/api/deployment"
        method = {"begin": "POST", "cancel": "DELETE", "status": "GET"}[action]
        headers = {"Authorization": "Bearer " + os.environ.get("INTERVIEW_ACCESS_TOKEN", "")}
    else:
        target = url or "http://127.0.0.1:8000/health"
        parsed = urlsplit(target)
        if parsed.username or parsed.password or parsed.scheme not in {"http", "https"}:
            raise ValueError("Health URL must be HTTP(S), without credentials.")
        if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Public health URL must use HTTPS.")
        method, headers = "GET", {}
    headers["User-Agent"] = "Interview-Deployment-Healthcheck/1.0"
    request = Request(target, method=method, headers=headers)
    try:
        with urlopen(request, timeout=10) as response:
            return json.loads(response.read(65537))
    except HTTPError as exc:
        if action == "begin" and exc.code == 404:
            raise RuntimeError("Drain endpoint is unavailable. Automatic deployment refused; install this version in a confirmed maintenance window first.") from None
        if action == "begin" and exc.code == 409:
            raise RuntimeError("An interview is active. No deployment changes were made.") from None
        raise RuntimeError(f"Service probe failed with HTTP {exc.code}.") from None
    except (URLError, ValueError, TimeoutError):
        raise RuntimeError("Service probe did not return valid JSON.") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("health", "snapshot", "begin", "cancel", "status"))
    parser.add_argument("--url")
    parser.add_argument("--expected-json")
    args = parser.parse_args(argv)
    try:
        payload = request_json(args.action, args.url)
        if args.action == "health":
            expected = json.loads(args.expected_json) if args.expected_json else None
            result = validate_health(payload, expected)
        elif args.action == "snapshot":
            result = snapshot_health(payload)
        elif args.action in {"begin", "cancel"}:
            result = validate_drain(payload, drained=args.action == "begin")
        else:
            if not isinstance(payload, dict):
                raise ValueError("Deployment state is invalid.")
            result = validate_drain(payload, drained=payload.get("draining") is True)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
