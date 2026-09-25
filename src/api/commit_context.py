"""Core identifiers attached to one scoring run."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redteam_core.validator.models import MinerChallengeCommit


@dataclass(frozen=True)
class CommitContext:
    commit_id: str
    commit_result_id: str
    challenge_id: str
    miner_id: str


@dataclass(frozen=True)
class ScoringWorkItem:
    commit: MinerChallengeCommit
    context: CommitContext
    attempts: int = 0
