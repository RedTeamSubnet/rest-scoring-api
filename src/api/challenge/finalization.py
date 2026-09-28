import math

from ..commit_context import ScoringCommit


class FinalOutput:
    """Turn scoring and comparison logs into the final commit outcome."""

    def start(self, miner_commit: ScoringCommit) -> None:
        self._update_miner_score(miner_commit)

    def _update_miner_score(self, miner_commit: ScoringCommit) -> None:
        """Finalize one commit from its scoring and comparison logs."""
        if not miner_commit.scoring_logs:
            raise ValueError("Scoring produced no scoring logs")

        raw_score = float(miner_commit.get_higest_scoring_score())
        penalty = (
            float(miner_commit.get_higest_comparison_score())
            if miner_commit.comparison_logs
            else None
        )

        miner_commit.penalty = penalty
        miner_commit.accepted = raw_score >= self.challenge_min_acceptable_score and (
            penalty is None or 0 <= penalty <= self.comparison_min_acceptable_score
        )
        miner_commit.score = self._adjust_score_by_similarity(raw_score, penalty)

    @staticmethod
    def _ease_circle_in_out_shifted(value: float) -> float:
        value = value**1.5
        if value < 0.5:
            return 0.5 * (1 - math.sqrt(1 - (2 * value) ** 2))
        return 0.5 * (math.sqrt(1 - (2 * value - 2) ** 2) + 1)

    def _scaling_from_similarity(self, similarity: float) -> float:
        max_similarity = 0.4
        break_point = 0.6
        max_input = 1.0

        if similarity <= break_point:
            progress = (similarity - max_similarity) / (break_point - max_similarity)
            normalized_break = (break_point - max_similarity) / (
                max_input - max_similarity
            )
            value_break = self._ease_circle_in_out_shifted(normalized_break)
            return progress * value_break

        progress = (similarity - max_similarity) / (max_input - max_similarity)
        return self._ease_circle_in_out_shifted(progress)

    def _adjust_score_by_similarity(
        self, raw_score: float, similarity: float | None
    ) -> float:
        """Reduce a score only when comparison provides evidence of copying."""
        if similarity is None or similarity < 0.4:
            return raw_score
        return raw_score * (1 - self._scaling_from_similarity(similarity))
