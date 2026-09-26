"""Translate scoring records into rest-core resources."""

import hashlib
import json
import mimetypes
from datetime import datetime, timezone
from typing import Any

import requests

from .commit_context import CommitContext
from .core_api import CoreApiClient


class ResultPublisher:
    def __init__(self, client: CoreApiClient):
        self.client = client

    def create_result(self, commit_id: str, miner_id: str) -> str:
        result = self.client.create(
            "/commit-results/",
            {"commit_id": commit_id, "miner_id": miner_id, "status": "PENDING"},
        )
        return result["id"]

    def get_or_create_result(self, commit_id: str, miner_id: str) -> dict[str, Any]:
        existing = next(self.client.list_commit_results(commit_id), None)
        if existing is None:
            try:
                result_id = self.create_result(commit_id, miner_id)
            except requests.HTTPError:
                # The unique commit_id constraint may have been won by another scorer.
                existing = next(self.client.list_commit_results(commit_id), None)
                if existing is None:
                    raise
            else:
                return {"id": result_id, "status": "PENDING", "miner_id": miner_id}
        if existing.get("miner_id") != miner_id:
            raise ValueError(f"Commit result miner mismatch for {commit_id}")
        if not isinstance(existing.get("id"), str):
            raise ValueError(f"Commit result has no ID for {commit_id}")
        return existing

    def start_result(self, result_id: str, attempts: int) -> None:
        self.client.update(
            f"/commit-results/{result_id}",
            {
                "status": "RUNNING",
                "error": None,
                "reason": "",
                "evaluated_score": 0.0,
                "penalty_score": 0.0,
                "final_score": 0.0,
                "evaluated_at": None,
                "finalized_at": None,
                "meta": {"attempts": attempts},
            },
        )

    def finish_result(
        self,
        context: CommitContext,
        *,
        accepted: bool,
        evaluated_score: float,
        score: float,
        penalty: float,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.client.update(
            f"/commit-results/{context.commit_result_id}",
            {
                "status": "ACCEPTED" if accepted else "REJECTED",
                "evaluated_score": evaluated_score,
                "penalty_score": penalty,
                "final_score": score,
                "reason": (
                    "Accepted" if accepted else "Score or comparison threshold not met"
                ),
                "evaluated_at": now,
                "finalized_at": now,
                "error": None,
            },
        )

    def fail_result(self, context: CommitContext, error: str) -> None:
        self.client.update(
            f"/commit-results/{context.commit_result_id}",
            {
                "status": "FAILED",
                "reason": "Scoring failed",
                "error": error[:1024],
                "finalized_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    def publish_validation(
        self,
        context: CommitContext,
        validation: dict[str, Any],
        *,
        check_name: str = "SUBMISSION",
    ) -> str:
        check_name = check_name.upper().replace("_", "-")
        payload = {
            "commit_id": context.commit_id,
            "commit_result_id": context.commit_result_id,
            "challenge_id": context.challenge_id,
            "check_name": check_name,
            "is_valid": bool(
                validation.get("is_valid", validation.get("is_good", False))
            ),
            "reason": str(validation.get("reason") or "")[:1024],
            "meta": validation,
        }
        try:
            record = self.client.create("/commit-validation-outputs/", payload)
        except requests.HTTPError:
            existing = next(
                self.client.list_commit_validations(
                    context.commit_result_id, check_name
                ),
                None,
            )
            if existing is None:
                raise
        else:
            return record["id"]
        record = self.client.patch(
            f"/commit-validation-outputs/{existing['id']}", payload
        )
        return record["id"]

    def publish_comparison(
        self, context: CommitContext, target_commit_id: str, result: dict[str, Any]
    ) -> str:
        payload = {
            "source_commit_id": context.commit_id,
            "target_commit_id": target_commit_id,
            "commit_result_id": context.commit_result_id,
            "challenge_id": context.challenge_id,
            "similarity_score": float(result.get("similarity_score", 0.0)),
            "reason": str(result.get("reason") or "")[:256],
            "meta": result,
        }
        try:
            record = self.client.create("/commit-comparisons/", payload)
        except requests.HTTPError:
            existing = next(
                self.client.list_commit_comparisons(
                    context.commit_id, target_commit_id
                ),
                None,
            )
            if existing is None:
                raise
            record = self.client.update(
                f"/commit-comparisons/{existing['id']}", payload
            )
        return record["id"]

    def publish_file(
        self, context: CommitContext, file: dict[str, Any], index: int
    ) -> str:
        name = file.get("file_name")
        content = file.get("content")
        if not isinstance(name, str) or not name or not isinstance(content, str):
            raise ValueError("Commit file needs file_name and text content")
        data = content.encode("utf-8")
        role = f"submission-{index}"
        mime_type = mimetypes.guess_type(name)[0]
        if not mime_type or len(mime_type) > 32:
            mime_type = "text/plain"
        payload = {
            "filename": f"{context.commit_id}-file-{index}",
            "orig_filename": name[:256],
            "role": role,
            "kind": "TEXT",
            "mime_type": mime_type,
            "size_bytes": len(data),
            "checksum": hashlib.sha256(data).hexdigest(),
            "data": content,
            "commit_id": context.commit_id,
        }
        try:
            record = self.client.create("/commit-files/", payload)
        except requests.HTTPError:
            existing = next(
                (
                    item
                    for item in self.client.list_commit_files(context.commit_id)
                    if item.get("role") == role
                ),
                None,
            )
            if existing is None:
                raise
            record = self.client.update(f"/commit-files/{existing['id']}", payload)
        return record["id"]

    def publish_output(self, context: CommitContext, output: dict[str, Any]) -> str:
        content = json.dumps(output, ensure_ascii=False, sort_keys=True)
        data = content.encode("utf-8")
        filename = f"{context.commit_id}-scoring-results.json"
        payload = {
            "filename": filename,
            "role": "result",
            "kind": "STRUCTURED",
            "mime_type": "application/json",
            "size_bytes": len(data),
            "checksum": hashlib.sha256(data).hexdigest(),
            "data": content,
            "commit_id": context.commit_id,
        }
        try:
            record = self.client.create("/commit-outputs/", payload)
        except requests.HTTPError:
            existing = next(
                (
                    item
                    for item in self.client.list_commit_outputs(context.commit_id)
                    if item.get("filename") == filename
                ),
                None,
            )
            if existing is None:
                raise
            record = self.client.update(f"/commit-outputs/{existing['id']}", payload)
        return record["id"]
