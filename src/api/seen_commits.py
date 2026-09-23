"""Durable local ciphertext deduplication for scorer input."""

from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile


class SeenCommits:
    def __init__(self, cache_dir: str):
        self.path = Path(cache_dir).expanduser() / "seen_commits.json"
        self.commits: dict[str, set[str]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                self.commits = {
                    str(challenge_id): {str(value) for value in values if isinstance(value, str)}
                    for challenge_id, values in payload.items()
                    if isinstance(values, list)
                }
        except (OSError, json.JSONDecodeError):
            # A corrupt cache must not stop scoring; it will be rebuilt from core.
            self.commits = {}

    def contains(self, challenge_id: str, cipher_commit: str) -> bool:
        return cipher_commit in self.commits.get(challenge_id, set())

    def add(self, challenge_id: str, cipher_commit: str) -> None:
        self.commits.setdefault(challenge_id, set()).add(cipher_commit)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {key: sorted(values) for key, values in sorted(self.commits.items())}
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=self.path.parent, delete=False
        ) as temporary_file:
            json.dump(payload, temporary_file, sort_keys=True)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
            temporary_path = temporary_file.name
        os.replace(temporary_path, self.path)
