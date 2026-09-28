"""Core identifiers attached to one scoring run."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CommitContext:
    commit_id: str
    commit_result_id: str
    challenge_id: str
    miner_id: str


@dataclass
class ScoringCommit:
    miner_uid: int
    miner_hotkey: str
    challenge_name: str
    docker_hub_id: str
    encrypted_commit: str
    scoring_logs: list[Any] = field(default_factory=list)
    comparison_logs: dict[str, list[Any]] = field(default_factory=dict)
    scored_timestamp: float | None = None
    score: float | None = None
    penalty: float | None = None
    accepted: bool | None = None

    def get_higest_scoring_score(self) -> float:
        scores = [log.score for log in self.scoring_logs if log.score is not None]
        return max(scores, default=0.0)

    def get_higest_comparison_score(self) -> float:
        scores = [
            log.similarity_score
            for logs in self.comparison_logs.values()
            for log in logs
            if log.similarity_score is not None
        ]
        return max(scores, default=0.0)


@dataclass(frozen=True)
class ScoringWorkItem:
    commit: ScoringCommit
    context: CommitContext


@dataclass(frozen=True)
class ReferenceCommit:
    commit_id: str
    miner_uid: int
    miner_hotkey: str
    files: list[dict]
    score: float
    penalty: float
