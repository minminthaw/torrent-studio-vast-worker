import asyncio
import hashlib
import json
import os
import re
import shutil
import signal
from collections import deque
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp
from aiohttp import web


ROOT = Path(os.environ.get("TORRENT_JOB_ROOT", "/workspace/torrent-serverless-jobs")).resolve()
MODEL_PORT = int(os.environ.get("TORRENT_MODEL_PORT", "8080"))
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
SAFE_JOB = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")
JOBS: dict[str, dict[str, Any]] = {}


def json_error(message: str, status: int = 400) -> web.Response:
    return web.json_response({"ok": False, "error": message}, status=status)


async def payload(request: web.Request) -> dict:
    data = await request.json()
    return data if isinstance(data, dict) else {}


def job_dir(job_id: str) -> Path:
    if not SAFE_JOB.fullmatch(job_id):
        raise ValueError("Invalid job id")
    directory = (ROOT / job_id).resolve()
    if directory.parent != ROOT:
        raise ValueError("Invalid job directory")
    return directory


def filename(value: str) -> str:
    if not isinstance(value, str) or not SAFE_NAME.fullmatch(value) or ".." in value:
        raise ValueError("Invalid filename")
    return value


def validate_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Source URL must be HTTP or HTTPS")
    return value


def validate_command(command: Any, sources: set[str], outputs: set[str]) -> list[str]:
    if not isinstance(command, list) or not 10 <= len(command) <= 1000 or not all(isinstance(v, str) for v in command):
        raise ValueError("Invalid ffmpeg command")
    if command[:4] != ["nice", "-n", "10", "ffmpeg"]:
        raise ValueError("Only the managed NVENC ffmpeg command is allowed")
    if any("\x00" in value or ".." in value or value.startswith("/") for value in command):
        raise ValueError("Unsafe ffmpeg argument")
    for index, value in enumerate(command[:-1]):
        if value == "-i":
            candidate = command[index + 1]
            if candidate not in sources and not candidate.startswith("anullsrc="):
                raise ValueError(f"Unknown input file: {candidate}")
    if not outputs or not outputs.issubset(set(command)):
        raise ValueError("Expected output is missing from ffmpeg command")
    return command


async def health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def benchmark(_: web.Request) -> web.Response:
    return web.json_response({"ok": True, "ready": True})


async def download_source(session: aiohttp.ClientSession, url: str, target: Path, state: dict) -> None:
    timeout = aiohttp.ClientTimeout(total=None, connect=60, sock_read=120)
    async with session.get(url, timeout=timeout, allow_redirects=True) as response:
        response.raise_for_status()
        total = int(response.headers.get("Content-Length", "0") or 0)
        received = 0
        with target.open("wb") as handle:
            async for chunk in response.content.iter_chunked(1024 * 1024):
                handle.write(chunk)
                received += len(chunk)
                state["downloaded_bytes"] = received
                state["download_total"] = total


async def drain_process(process: asyncio.subprocess.Process, state: dict, duration: float) -> None:
    while True:
        line = await process.stdout.readline()
        if not line:
            break
        text = line.decode("utf-8", "replace").strip()
        if text:
            state["log"].append(text)
        if text.startswith("out_time_ms="):
            try:
                seconds = int(text.split("=", 1)[1]) / 1_000_000
                state["encoded_seconds"] = seconds
                state["progress"] = min(99.9, seconds / duration * 100) if duration > 0 else 0
            except (TypeError, ValueError):
                pass


async def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(4 * 1024 * 1024):
            digest.update(chunk)
            await asyncio.sleep(0)
    return digest.hexdigest()


async def execute_job(job_id: str, request_data: dict, directory: Path, state: dict) -> None:
    try:
        state["phase"] = "downloading_inputs"
        connector = aiohttp.TCPConnector(limit=4)
        async with aiohttp.ClientSession(connector=connector) as session:
            for item in request_data["sources"]:
                if state["cancelled"]:
                    raise asyncio.CancelledError
                state["current_source"] = item["name"]
                await download_source(session, item["url"], directory / item["name"], state)

        state.update({"phase": "encoding", "downloaded_bytes": 0, "download_total": 0})
        process = await asyncio.create_subprocess_exec(
            *request_data["command"],
            cwd=directory,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        state["process"] = process
        await drain_process(process, state, request_data["duration"])
        code = await process.wait()
        state["process"] = None
        if state["cancelled"]:
            raise asyncio.CancelledError
        if code != 0:
            raise RuntimeError(f"ffmpeg exited with code {code}")

        metadata = {}
        state["phase"] = "verifying_outputs"
        for name in request_data["outputs"]:
            path = directory / name
            if not path.is_file() or path.stat().st_size <= 0:
                raise RuntimeError(f"Expected output is missing: {name}")
            metadata[name] = {"size": path.stat().st_size, "sha256": await hash_file(path)}

        state.update({"state": "completed", "phase": "completed", "progress": 100.0, "outputs": metadata})
    except asyncio.CancelledError:
        state.update({"state": "cancelled", "phase": "cancelled", "error": "Task cancelled"})
    except Exception as exc:
        state.update({"state": "failed", "phase": "failed", "error": str(exc)})


async def start_task(request: web.Request) -> web.Response:
    try:
        data = await payload(request)
        job_id = data.get("job_id", "")
        directory = job_dir(job_id)
        duration = max(0.01, float(data.get("duration", 0)))
        raw_sources = data.get("sources")
        raw_outputs = data.get("outputs")
        if not isinstance(raw_sources, list) or not raw_sources or not isinstance(raw_outputs, list) or not raw_outputs:
            raise ValueError("Sources and outputs are required")
        sources = [{"name": filename(item["name"]), "url": validate_url(item["url"])} for item in raw_sources]
        output_names = [filename(value) for value in raw_outputs]
        command = validate_command(data.get("command"), {item["name"] for item in sources}, set(output_names))
    except (KeyError, TypeError, ValueError) as exc:
        return json_error(str(exc))

    current = JOBS.get(job_id)
    if current and current["state"] in {"queued", "running"}:
        return json_error("Job is already running", 409)
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True, mode=0o700)

    state = {
        "state": "running",
        "phase": "queued",
        "progress": 0.0,
        "encoded_seconds": 0.0,
        "duration": duration,
        "downloaded_bytes": 0,
        "download_total": 0,
        "current_source": None,
        "outputs": {},
        "error": None,
        "cancelled": False,
        "process": None,
        "log": deque(maxlen=80),
        "task": None,
    }
    JOBS[job_id] = state
    normalized = {"sources": sources, "outputs": output_names, "command": command, "duration": duration}
    state["task"] = asyncio.create_task(execute_job(job_id, normalized, directory, state))
    return web.json_response({"ok": True, "job_id": job_id, "status": public_status(state)}, status=202)


def public_status(state: dict) -> dict:
    return {
        "state": state["state"],
        "phase": state["phase"],
        "progress": round(float(state["progress"]), 2),
        "encoded_seconds": round(float(state["encoded_seconds"]), 3),
        "duration": state["duration"],
        "downloaded_bytes": state["downloaded_bytes"],
        "download_total": state["download_total"],
        "current_source": state["current_source"],
        "outputs": state["outputs"],
        "error": state["error"],
        "log_tail": list(state["log"])[-12:],
    }


async def status(request: web.Request) -> web.Response:
    data = await payload(request)
    state = JOBS.get(data.get("job_id", ""))
    if not state:
        return json_error("Unknown job", 404)
    return web.json_response({"ok": True, "status": public_status(state)})


async def download(request: web.Request) -> web.StreamResponse:
    try:
        data = await payload(request)
        job_id = data.get("job_id", "")
        name = filename(data.get("filename", ""))
        offset = max(0, int(data.get("offset", 0)))
        state = JOBS.get(job_id)
        if not state or state["state"] != "completed" or name not in state["outputs"]:
            return json_error("Output is not ready", 409)
        path = job_dir(job_id) / name
        size = path.stat().st_size
        if offset > size:
            return json_error("Offset exceeds file size", 416)
    except (OSError, TypeError, ValueError) as exc:
        return json_error(str(exc))

    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "application/x-download-stream",
            "X-File-Size": str(size),
            "X-File-SHA256": state["outputs"][name]["sha256"],
        },
    )
    await response.prepare(request)
    with path.open("rb") as handle:
        handle.seek(offset)
        while chunk := handle.read(1024 * 1024):
            await response.write(chunk)
    await response.write_eof()
    return response


async def stop_job(job_id: str, remove: bool) -> dict:
    state = JOBS.get(job_id)
    if state:
        state["cancelled"] = True
        process = state.get("process")
        if process and process.returncode is None:
            process.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), timeout=15)
            except asyncio.TimeoutError:
                process.kill()
        task = state.get("task")
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    if remove:
        try:
            directory = job_dir(job_id)
            if directory.exists():
                shutil.rmtree(directory)
        except ValueError:
            pass
        JOBS.pop(job_id, None)
    return {"ok": True, "removed": remove}


async def cancel_task(request: web.Request) -> web.Response:
    data = await payload(request)
    return web.json_response(await stop_job(data.get("job_id", ""), False))


async def cleanup(request: web.Request) -> web.Response:
    data = await payload(request)
    return web.json_response(await stop_job(data.get("job_id", ""), True))


async def shutdown(_: web.Application) -> None:
    await asyncio.gather(*(stop_job(job_id, False) for job_id in list(JOBS)), return_exceptions=True)


def create_app() -> web.Application:
    ROOT.mkdir(parents=True, exist_ok=True)
    application = web.Application(client_max_size=1024 * 1024)
    application.add_routes([
        web.get("/health", health),
        web.post("/health", health),
        web.post("/benchmark", benchmark),
        web.post("/start_task", start_task),
        web.post("/status", status),
        web.post("/download", download),
        web.post("/cancel_task", cancel_task),
        web.post("/cleanup", cleanup),
    ])
    application.on_shutdown.append(shutdown)
    return application


if __name__ == "__main__":
    print("Torrent encoder ready", flush=True)
    web.run_app(create_app(), host="127.0.0.1", port=MODEL_PORT, print=None)
