"""Translate scoring records into rest-core resources."""

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
                "reason": "Accepted" if accepted else "Score or comparison threshold not met",
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
        self, context: CommitContext, validation: dict[str, Any]
    ) -> str:
        payload = {
            "commit_id": context.commit_id,
            "commit_result_id": context.commit_result_id,
            "challenge_id": context.challenge_id,
            "check_name": "SUBMISSION",
            "is_valid": bool(validation.get("is_valid", False)),
            "reason": str(validation.get("reason") or "")[:1024],
            "meta": validation,
        }
        try:
            record = self.client.create("/commit-validation-outputs/", payload)
        except requests.HTTPError:
            existing = next(
                self.client.list_commit_validations(
                    context.commit_result_id, "SUBMISSION"
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
        record = self.client.create(
            "/commit-comparisons/",
            {
                **result,
                "source_commit_id": context.commit_id,
                "target_commit_id": target_commit_id,
                "commit_result_id": context.commit_result_id,
                "challenge_id": context.challenge_id,
            },
        )
        return record["id"]

    def publish_file(self, context: CommitContext, file: dict[str, Any]) -> str:
        record = self.client.create(
            "/commit-files/", {**file, "commit_id": context.commit_id}
        )
        return record["id"]

    def publish_output(self, context: CommitContext, output: dict[str, Any]) -> str:
        record = self.client.create(
            "/commit-outputs/", {**output, "commit_id": context.commit_id}
        )
        return record["id"]
