"""Single-service, drain-protected release with retained rollback artifacts.

Run only on the Linux deployment host as root. This script never calls OpenAI.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath

if __package__:
    from .probe import EXPECTED_HEALTH, validate_health
else:
    from probe import EXPECTED_HEALTH, validate_health


MODEL_SETTINGS = {
    "OPENAI_LIVE_MODEL": "gpt-live-1",
    "OPENAI_REALTIME_TRANSCRIPTION_MODEL": "gpt-live-transcribe",
    "OPENAI_CODE_MODEL": "gpt-6-astra",
    "OPENAI_CODE_REASONING_EFFORT": "high",
}


def release_config(config: dict, image: str, *, drained: bool, release_id: str | None = None) -> dict:
    result = json.loads(json.dumps(config))
    service = result.get("services", {}).get("interview_api")
    if not isinstance(service, dict):
        raise ValueError("Compose must contain interview_api.")
    # A resolved compose config preserves paths, env-file values and project
    # identity. Operate on this one named service, never remove shared orphans.
    service["image"] = image
    environment = service.setdefault("environment", {})
    if not isinstance(environment, dict):
        raise ValueError("Compose environment was not resolved.")
    environment["INTERVIEW_START_DRAINED"] = "1" if drained else "0"
    if release_id is not None:
        environment["INTERVIEW_RELEASE_ID"] = release_id
    return result


def service_image(config: dict) -> str:
    service = config["services"]["interview_api"]
    image = service.get("image") or f"{config['name']}-interview_api"
    if not isinstance(image, str) or not image:
        raise ValueError("Compose image identity is missing.")
    return image


def context_mount_signature(environment: dict, mounts: object, *, deploy_path: Path) -> tuple:
    """Check mount metadata only; never read or report private document contents."""
    failure = "Private context must use a verifiable read-only mount outside the deployed source."
    configured_context = environment.get("INTERVIEW_CONTEXT_DIR", "")
    if not isinstance(configured_context, str):
        raise RuntimeError(failure)
    context = PurePosixPath(configured_context.strip() or "/app/context")
    if not context.is_absolute() or ".." in context.parts or not isinstance(mounts, list):
        raise RuntimeError(failure)
    destinations = []
    for mount in mounts:
        if not isinstance(mount, dict) or not isinstance(mount.get("Destination"), str):
            raise RuntimeError(failure)
        destination = PurePosixPath(mount["Destination"])
        if not destination.is_absolute() or ".." in destination.parts:
            raise RuntimeError(failure)
        destinations.append((destination, mount))
    covering = [(destination, mount) for destination, mount in destinations
                if destination == context or destination in context.parents]
    if not covering:
        raise RuntimeError(failure)
    selected = max(covering, key=lambda entry: len(entry[0].parts))
    relevant = [selected] + [(destination, mount) for destination, mount in destinations
                             if context in destination.parents]
    signature = []
    deployed = deploy_path.resolve()
    for destination, mount in relevant:
        kind = mount.get("Type")
        if mount.get("RW") is not False or kind not in {"bind", "volume"}:
            raise RuntimeError(failure)
        if kind == "bind":
            source = mount.get("Source")
            if not isinstance(source, str) or not PurePosixPath(source).is_absolute():
                raise RuntimeError(failure)
            source_path = Path(source).resolve()
            relative_context = context.relative_to(destination) if destination in context.parents else PurePosixPath(".")
            effective_source = source_path.joinpath(*relative_context.parts).resolve()
            if any(path == deployed or deployed in path.parents for path in (source_path, effective_source)):
                raise RuntimeError(failure)
            identity = str(source_path)
        else:
            identity = mount.get("Name")
            if not isinstance(identity, str) or not identity:
                raise RuntimeError(failure)
        signature.append((str(destination), kind, identity))
    return str(context), tuple(sorted(signature))


def compose_context_signature(config: dict, *, deploy_path: Path) -> tuple:
    service = config.get("services", {}).get("interview_api", {})
    environment = service.get("environment", {})
    volumes = service.get("volumes", [])
    if not isinstance(environment, dict) or not isinstance(volumes, list):
        raise RuntimeError("Could not verify the Compose private context mount.")
    mounts = []
    for volume in volumes:
        if not isinstance(volume, dict):
            raise RuntimeError("Could not verify the Compose private context mount.")
        mount = {"Type": volume.get("type"), "Destination": volume.get("target"),
                 "Source": volume.get("source"), "RW": volume.get("read_only") is not True}
        if mount["Type"] == "volume":
            definition = config.get("volumes", {}).get(volume.get("source"), {})
            mount["Name"] = definition.get("name")
        mounts.append(mount)
    return context_mount_signature(environment, mounts, deploy_path=deploy_path)


def update_environment(text: str, *, release_id: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", release_id):
        raise ValueError("Invalid release id.")
    settings = {**MODEL_SETTINGS, "INTERVIEW_RELEASE_ID": release_id, "INTERVIEW_START_DRAINED": "0"}
    # One-time environment migration; the runtime only uses the native plural field.
    if not re.search(r"(?m)^\s*OPENAI_REALTIME_TRANSCRIPTION_LANGUAGES\s*=", text):
        previous_language = re.search(r"(?m)^\s*OPENAI_REALTIME_TRANSCRIPTION_LANGUAGE\s*=([^\r\n]*)", text)
        if previous_language:
            settings["OPENAI_REALTIME_TRANSCRIPTION_LANGUAGES"] = previous_language.group(1).strip()
    remaining = dict(settings)
    result = []
    for line in text.splitlines():
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in {"OPENAI_REALTIME_MODEL", "OPENAI_REALTIME_REASONING_EFFORT", "OPENAI_REALTIME_TRANSCRIPTION_LANGUAGE"}:
            continue
        if key in settings:
            if key in remaining:
                result.append(f"{key}={remaining.pop(key)}")
        else:
            result.append(line)
    result.extend(f"{key}={value}" for key, value in remaining.items())
    return "\n".join(result) + "\n"


def private_json(path: Path, payload: dict) -> None:
    with path.open("x", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        json.dump(payload, stream)


def run(command: list[str], *, cwd: Path | None = None, stdin: str | None = None) -> str:
    completed = subprocess.run(command, cwd=cwd, input=stdin, text=True, capture_output=True, timeout=900)
    if completed.returncode:
        # Docker/config output can contain secrets. Do not print stdout/stderr.
        raise RuntimeError(f"Deployment operation failed: {command[0]} {command[1] if len(command) > 1 else ''}.")
    return completed.stdout


def container_probe(probe_source: str, action: str, *, expected: dict | None = None) -> dict:
    command = ["docker", "exec", "-i", "interview_api", "python", "-", action]
    if expected is not None:
        command.extend(["--expected-json", json.dumps(expected)])
    return json.loads(run(command, stdin=probe_source))


def compose_up(config: Path) -> None:
    run(["docker", "compose", "-f", str(config), "up", "-d", "--no-deps", "--no-build", "--pull", "never", "interview_api"])


def wait_health(probe_source: str, *, expected: dict, public_url: str | None = None, probe_path: Path | None = None) -> None:
    for attempt in range(24):
        try:
            container_probe(probe_source, "health", expected=expected)
            if public_url:
                output = run([sys.executable, str(probe_path), "health", "--url", public_url,
                              "--expected-json", json.dumps(expected)])
                validate_health(json.loads(output), expected)
            return
        except (RuntimeError, ValueError):
            if attempt == 23:
                raise RuntimeError("Release health/protocol/model verification failed.") from None
            time.sleep(5)


def safe_directory(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or path.resolve() == Path("/"):
        raise ValueError("Deployment directories must be explicit absolute paths below root.")
    return path.resolve()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deploy-path", required=True)
    parser.add_argument("--compose-path", required=True)
    parser.add_argument("--staging-path", required=True)
    parser.add_argument("--health-url", required=True)
    parser.add_argument("--release-id", required=True)
    args = parser.parse_args(argv)
    if sys.platform != "linux" or os.geteuid() != 0:
        raise RuntimeError("Release must run as root on the Linux deployment host.")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", args.release_id):
        raise ValueError("Invalid release id.")
    deploy, compose, staging = map(safe_directory, (args.deploy_path, args.compose_path, args.staging_path))
    if deploy == staging or deploy in staging.parents or staging in deploy.parents:
        raise ValueError("Staging must be a separate sibling of the deployed source.")
    if not deploy.is_dir() or not compose.is_dir() or not (staging / "Dockerfile").is_file():
        raise ValueError("Deployment, compose, or staged source is missing.")
    probe_path = staging / "deploy" / "probe.py"
    probe_source = probe_path.read_text(encoding="utf-8")
    backup = deploy.parent / (deploy.name + ".deploy-backups") / args.release_id
    backup.mkdir(parents=True, exist_ok=False, mode=0o700)
    os.chmod(backup, 0o700)
    old_health = container_probe(probe_source, "snapshot")
    # Fail closed on active interviews or legacy servers without the gate. In
    # particular, do not rsync source or edit .env before this atomic operation.
    try:
        container_probe(probe_source, "begin")
    except RuntimeError:
        raise RuntimeError("Deployment refused: the server is active, unreachable, or lacks the atomic drain endpoint. See deploy/README.md for first-install maintenance.") from None
    mutated = False
    finalized = False
    try:
        old_compose = json.loads(run(["docker", "compose", "config", "--format", "json"], cwd=compose))
        # The live container may have been started from the preceding release's
        # resolved override. Preserve its actual environment (including release
        # ID), rather than assuming the base Compose files exactly match it.
        old_environment = json.loads(run(["docker", "inspect", "--format", "{{json .Config.Env}}", "interview_api"]))
        if not isinstance(old_environment, list) or any(not isinstance(entry, str) or "=" not in entry for entry in old_environment):
            raise ValueError("Could not snapshot the running container environment.")
        old_environment = dict(entry.split("=", 1) for entry in old_environment)
        old_mounts = json.loads(run(["docker", "inspect", "--format", "{{json .Mounts}}", "interview_api"]))
        actual_context = context_mount_signature(old_environment, old_mounts, deploy_path=deploy)
        if compose_context_signature(old_compose, deploy_path=deploy) != actual_context:
            raise RuntimeError("Compose private context does not match the running container; release refused.")
        print("Private context mount check passed.")
        old_image = run(["docker", "inspect", "--format", "{{.Image}}", "interview_api"]).strip()
        rollback_image = f"interview-rollback:{args.release_id}"
        run(["docker", "tag", old_image, rollback_image])
        old_compose["services"]["interview_api"]["environment"] = old_environment
        private_json(backup / "rollback-candidate-compose.json", release_config(old_compose, rollback_image, drained=True))
        private_json(backup / "rollback-compose.json", release_config(old_compose, rollback_image, drained=False))
        private_json(backup / "old-health.json", old_health)
        shutil.copytree(deploy, backup / "source", symlinks=True)
        # Private documents and existing environment/backups are not part of a
        # code release. Production context must remain outside the checkout.
        mutated = True
        run(["rsync", "-a", "--delete", "--exclude=.env*", "--exclude=/context",
             "--exclude=/.venv", "--exclude=*backup*", "--exclude=*.bak*",
             str(staging) + "/", str(deploy) + "/"])
        env_file = deploy / ".env"
        env_file.write_text(update_environment(env_file.read_text(encoding="utf-8"), release_id=args.release_id), encoding="utf-8")
        new_compose = json.loads(run(["docker", "compose", "config", "--format", "json"], cwd=compose))
        new_image = f"interview-release:{args.release_id}"
        candidate_config = backup / "candidate-compose.json"
        stable_config = backup / "stable-compose.json"
        private_json(candidate_config, release_config(new_compose, new_image, drained=True, release_id=args.release_id))
        private_json(stable_config, release_config(new_compose, new_image, drained=False, release_id=args.release_id))
        expected_health = {**EXPECTED_HEALTH, "release_id": args.release_id}
        run(["docker", "compose", "-f", str(candidate_config), "build", "interview_api"])
        compose_up(candidate_config)
        wait_health(probe_source, expected=expected_health, public_url=args.health_url, probe_path=probe_path)
        # This replacement still happens while the candidate gate excludes all
        # interviews. The stable container must not keep START_DRAINED=1, or an
        # ordinary later crash/restart would strand users in deployment mode.
        finalized = True
        compose_up(stable_config)
        wait_health(probe_source, expected=expected_health, public_url=args.health_url, probe_path=probe_path)
        run(["docker", "tag", new_image, service_image(new_compose)])
        print(f"Release verified. Retained rollback directory: {backup}")
        return 0
    except BaseException:
        if finalized:
            try:
                container_probe(probe_source, "begin")
            except (RuntimeError, ValueError):
                print("Automatic rollback stopped: could not safely drain the finalized service. Preserve any active interview; use the retained rollback after a confirmed idle period.", file=sys.stderr)
                raise
        if mutated:
            run(["rsync", "-a", "--delete", "--exclude=.env*", "--exclude=/context",
                 "--exclude=/.venv", "--exclude=*backup*", "--exclude=*.bak*",
                 str(backup / "source") + "/", str(deploy) + "/"])
            shutil.copy2(backup / "source" / ".env", deploy / ".env")
            compose_up(backup / "rollback-candidate-compose.json")
            wait_health(probe_source, expected=old_health)
            compose_up(backup / "rollback-compose.json")
            wait_health(probe_source, expected=old_health)
            run(["docker", "tag", rollback_image, service_image(old_compose)])
        else:
            container_probe(probe_source, "cancel")
        print(f"Release failed; prior service restored/unlocked. Rollback artifacts: {backup}", file=sys.stderr)
        raise


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
