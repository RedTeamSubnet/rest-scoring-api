import datetime
import json
from typing import Any


def join_url(base: str, *parts: str) -> str:
    value = base.rstrip("/")
    for part in parts:
        if part:
            value = f"{value}/{part.strip('/')}"
    return value


def as_timestamp(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    if isinstance(value, datetime.datetime):
        return value.timestamp()
    return None


def docker_hub_id_from_plain_commit(challenge_name: str, plain_commit: str) -> str:
    prefix = f"{challenge_name}---"
    if plain_commit.startswith(prefix):
        return plain_commit[len(prefix) :]
    if "---" in plain_commit:
        return plain_commit.split("---", 1)[1]
    return plain_commit


def json_loads_maybe(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except Exception:
        return value


def extract_commit_files(payload: dict) -> tuple[list[dict], dict | None]:
    """Return replayable commit files and optional telemetry from a storage payload."""
    for output in payload.get("commit_outputs") or []:
        data = json_loads_maybe(output.get("data"))
        if isinstance(data, dict) and isinstance(data.get("commit_files"), list):
            return data["commit_files"], data.get("telemetry")
        if isinstance(data, list):
            return data, None

    commit_files = []
    for file_data in payload.get("commit_files") or []:
        data = json_loads_maybe(file_data.get("data"))
        if isinstance(data, dict) and isinstance(data.get("commit_files"), list):
            return data["commit_files"], data.get("telemetry")
        if isinstance(data, list):
            return data, None
        content = data if isinstance(data, str) else file_data.get("content")
        filename = (
            file_data.get("file_name")
            or file_data.get("orig_filename")
            or file_data.get("filename")
        )
        if filename and content is not None:
            commit_files.append({"file_name": filename, "content": content})
    return commit_files, None
