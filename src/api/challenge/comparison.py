import logging
import requests
from redteam_core.config.main import constants
from redteam_core.validator.models import ComparisonLog

from ..commit_context import ScoringCommit

logger = logging.getLogger(__name__)


class Comparison:
    """Run reference, baseline, and same-score comparisons."""

    def start(self, miner_commit: ScoringCommit) -> None:
        self._run_reference_comparison_inputs(miner_commit)

    @staticmethod
    def reject_invalid_submission(miner_commit: ScoringCommit) -> None:
        logger.warning(
            f"[CONTROLLER] Skipping comparison for miner "
            f"{miner_commit.miner_hotkey} due to invalid submission."
        )
        log = miner_commit.scoring_logs[0]
        log.score = 0.0
        error = "Invalid submission"
        log.error = f"{log.error} | {error}" if log.error else error
        miner_commit.comparison_logs["check/validation"] = [
            ComparisonLog(similarity_score=1, reason=error)
        ]

    # -- shared helpers --

    @staticmethod
    def _get_common_headers() -> dict:
        return {
            "Content-Type": "application/json",
            "X-API-KEY": constants.INTERNAL_SERVICES.API_KEY,
        }

    def _get_compare_timeout(self, default: int = 300) -> int:
        return self.challenge_info.get("challenge_compare_timeout", default)

    def _get_limited_reference_commits(self) -> list:
        reference_commits = self.reference_comparison_commits
        limit = self.challenge_info["comparison_config"].get(
            "max_unique_commits", None
        )
        if limit:
            reference_commits = reference_commits[:limit]
        return reference_commits

    @staticmethod
    def _build_unique_commit_key(reference_commit) -> str:
        return f"{reference_commit.miner_uid}_{reference_commit.commit_id[:10]}"

    # -- reference comparison --

    def _run_reference_comparison_inputs(self, miner_commit: ScoringCommit):
        """Run miner with reference comparison commits inputs to compare performance."""
        reference_commits = self._get_limited_reference_commits()

        for reference_commit in reference_commits:
            should_stop = self._process_single_reference_comparison(
                miner_commit, reference_commit
            )
            if should_stop:
                return

        self._compare_with_baseline(miner_commit)

    def _should_skip_reference_comparison(
        self, reference_commit, miner_commit: ScoringCommit
    ) -> bool:
        if (
            not reference_commit.files
            or not miner_commit.scoring_logs
            or miner_commit.scoring_logs[0].miner_output is None
        ):
            logger.warning(
                f"[CONTROLLER] Skipping comparison with {reference_commit.commit_id} "
                f"because files or miner output are missing."
            )
            return True
        return False

    def _should_skip_self_comparison(
        self,
        miner_commit: ScoringCommit,
        reference_commit,
        similarity_score: float,
        unique_key: str,
    ) -> bool:
        if (
            miner_commit.miner_hotkey == reference_commit.miner_hotkey
            and similarity_score < self.max_self_comparison_score
        ):
            logger.warning(
                f"[CONTROLLER] Skipping self-comparison for {miner_commit.miner_hotkey}"
                f" with {reference_commit.miner_hotkey} due to low similarity score "
                f"{similarity_score}"
            )
            if unique_key in miner_commit.comparison_logs:
                del miner_commit.comparison_logs[unique_key]
            return True
        return False

    def _process_single_reference_comparison(
        self, miner_commit: ScoringCommit, reference_commit
    ) -> bool:
        """Returns True if similarity threshold reached (stop further comparisons)."""
        unique_key = self._build_unique_commit_key(reference_commit)
        logger.info(
            f"[CONTROLLER] Running comparison with reference commit {unique_key}"
        )

        if unique_key not in miner_commit.comparison_logs:
            miner_commit.comparison_logs[unique_key] = []

        if self._should_skip_reference_comparison(reference_commit, miner_commit):
            return False

        miner_output = miner_commit.scoring_logs[0].miner_output.copy()
        reference_output = {"commit_files": reference_commit.files}

        compare_result = self._compare_outputs(
            miner_output=miner_output,
            reference_files=reference_commit.files,
            user_id=miner_commit.docker_hub_id,
            target_commit_id=reference_commit.commit_id,
        )
        similarity_score = compare_result.get("similarity_score", 1.0)
        similarity_reason = compare_result.get("reason", "Unknown")

        self._exclude_output_keys(miner_output, reference_output)

        if self._should_skip_self_comparison(
            miner_commit, reference_commit, similarity_score, unique_key
        ):
            return False

        comparison_log = ComparisonLog(
            miner_input=None,
            miner_output=miner_output,
            reference_output=reference_output,
            reference_hotkey=reference_commit.miner_hotkey,
            reference_similarity_score=reference_commit.penalty,
            similarity_score=similarity_score,
            reason=similarity_reason,
        )
        miner_commit.comparison_logs[unique_key].append(comparison_log)

        min_acceptable = self.challenge_info["comparison_config"].get(
            "min_acceptable_score", 0.6
        )
        if similarity_score > min_acceptable:
            logger.warning(
                f"[CONTROLLER] Stopping comparison because of high similarity threshold "
                f"is reached, similarity score {similarity_score}"
            )
            return True

        if (
            unique_key in miner_commit.comparison_logs
            and not miner_commit.comparison_logs[unique_key]
        ):
            logger.info(
                f"[CONTROLLER] Removing empty comparison logs for {unique_key} for miner."
            )
            del miner_commit.comparison_logs[unique_key]

        return False

    # -- compare outputs (reference) --

    def _build_compare_payload(
        self, miner_output: dict, reference_files: list[dict], user_id: str | None
    ) -> dict:
        return {
            "challenge_type": self.challenge_info.get("challenge_type", None),
            "challenge_name": self.challenge_info.get("name", None),
            "miner_script": miner_output.get("commit_files"),
            "reference_script": reference_files,
            "identifier": self.challenge_info.get("script_path_identifier", None),
            "user_id": user_id,
        }

    def _send_compare_request(self, payload: dict) -> dict:
        try:
            response = requests.post(
                f"{constants.INTERNAL_SERVICES.API_URL}/compare",
                timeout=self._get_compare_timeout(),
                verify=False,  # nosec
                json=payload,
                headers=self._get_common_headers(),
            )
            if response.status_code == 404:
                logger.warning("No accepted submission to compare against.")
                data = {
                    "similarity_score": 0.0,
                    "reason": "No accepted submission to compare against.",
                }
            else:
                response_data = response.json()
                data = response_data.get("data", {})
            if not isinstance(data, dict):
                raise ValueError("Comparison response data must be an object")
            return data
        except Exception as e:
            logger.error(f"Error in comparison request: {str(e)}")
            return {
                "target": "Error while comparing outputs",
                "similarity_score": 0.0,
                "reason": f"Error: {str(e)}",
            }

    def _compare_outputs(
        self,
        miner_output: dict,
        reference_files: list[dict],
        user_id: str | None = None,
        target_commit_id: str | None = None,
    ) -> dict:
        """Send comparison request to challenge container's /compare endpoint."""
        payload = self._build_compare_payload(miner_output, reference_files, user_id)
        data = self._send_compare_request(payload)
        if target_commit_id is not None:
            self._store_comparison(self.context, target_commit_id, data)
        return data

    # -- same-score comparison --

    def same_score_comparison(self, miner_commit: ScoringCommit) -> None:
        scoring_log = self._get_scoring_log_for_same_score(miner_commit)
        if scoring_log is None:
            return

        commits_in_range = self._get_commits_in_score_range(scoring_log.score)
        if not commits_in_range:
            logger.info(
                f"[CONTROLLER] No reference commits found with score in range for miner "
                f"{miner_commit.miner_hotkey}, skipping same score comparison."
            )
            return

        for ref_commit in commits_in_range[:3]:
            self._process_single_same_score_comparison(
                miner_commit, ref_commit, scoring_log
            )

    def _get_scoring_log_for_same_score(self, miner_commit: ScoringCommit):
        if not miner_commit.scoring_logs:
            logger.warning(
                f"[CONTROLLER] No scoring logs found for miner {miner_commit.miner_hotkey}, "
                f"skipping same score comparison."
            )
            return None

        scoring_log = miner_commit.scoring_logs[0]
        commit_score = scoring_log.score

        if commit_score is None or commit_score <= self.challenge_min_acceptable_score:
            return None

        return scoring_log

    def _get_commits_in_score_range(self, score: float) -> list:
        return [
            ref_commit
            for ref_commit in self.reference_comparison_commits
            if abs(ref_commit.score - score) <= 0.1
        ]

    def _process_single_same_score_comparison(
        self, miner_commit: ScoringCommit, ref_commit, scoring_log
    ) -> None:
        comparison_logs = self._compare_same_score_outputs(
            miner_output=scoring_log.miner_output,
            reference_files=ref_commit.files,
            reference_score=ref_commit.score,
            user_id=miner_commit.docker_hub_id,
        )

        if not (
            "similarity_score" in comparison_logs
            and comparison_logs["similarity_score"]
            >= self.comparison_min_acceptable_score
        ):
            return

        similarity_score = comparison_logs["similarity_score"]

        if (
            ref_commit.miner_hotkey == miner_commit.miner_hotkey
            and similarity_score < self.max_self_comparison_score
        ):
            logger.info(
                f"[CONTROLLER] Skipping same-score self-comparison for miner "
                f"UID {miner_commit.miner_hotkey}: similarity score "
                f"{similarity_score} is below "
                f"{self.max_self_comparison_score}."
            )
            return

        unique_key = self._build_unique_commit_key(ref_commit)
        miner_commit.comparison_logs[unique_key] = [
            ComparisonLog(
                similarity_score=similarity_score,
                reason=comparison_logs.get(
                    "reason", "similarity score above threshold"
                ),
            )
        ]
        self._store_comparison(
            self.context, ref_commit.commit_id, comparison_logs
        )

    # -- same-score comparison request --

    def _compare_same_score_outputs(
        self,
        miner_output: dict,
        reference_files: list[dict],
        reference_score: float,
        user_id: str = "default_user",
    ) -> list[dict]:
        """Send comparison request to challenge container's /compare/same-score endpoint."""
        miner_metadata = {
            "score": miner_output.get("score", 0),
            "telemetry": miner_output.get("telemetry", {}),
        }
        reference_metadata = {
            "score": reference_score,
            "telemetry": {},
        }
        payload = {
            "challenge_type": self.challenge_info.get("challenge_type", None),
            "challenge_name": self.challenge_info.get("name", None),
            "miner_script": miner_output.get("commit_files"),
            "reference_script": reference_files,
            "miner_metadata": miner_metadata,
            "reference_metadata": reference_metadata,
            "user_id": user_id,
        }
        try:
            response = requests.post(
                f"{constants.INTERNAL_SERVICES.API_URL}/compare/same-score",
                timeout=self._get_compare_timeout(),
                verify=False,  # nosec
                json=payload,
                headers=self._get_common_headers(),
            )
            if response.status_code == 404:
                logger.warning("No accepted submission to compare against.")
                return {
                    "similarity_score": 0.0,
                    "reason": "No accepted submission to compare against.",
                }

            response_data = response.json()
            return response_data.get("data", [])
        except Exception as e:
            logger.error(f"Error in same-score comparison request: {str(e)}")
            return [
                {
                    "target": "Error while comparing outputs",
                    "similarity_score": 0.0,
                    "reason": f"Error: {str(e)}",
                }
            ]

    # -- check comparison score --

    def _check_comparison_score(self, miner_commit: ScoringCommit) -> float:
        compare_url = f"{constants.INTERNAL_SERVICES.API_URL}/compare/all"
        max_score = 0.0
        comparisons: list[tuple[str, dict]] = []
        try:
            miner_files = miner_commit.scoring_logs[0].miner_output.get("commit_files")
            reference_commits = self._get_limited_reference_commits()
            headers = self._get_common_headers()

            for reference_commit in reference_commits:
                if reference_commit.miner_uid == miner_commit.miner_uid:
                    continue

                payload = {
                    "challenge_type": self.challenge_info.get("challenge_type", None),
                    "miner_script": miner_files,
                    "reference_script": reference_commit.files,
                    "user_id": miner_commit.docker_hub_id,
                }
                response = requests.post(
                    compare_url,
                    json=payload,
                    timeout=100,
                    verify=False,  # nosec
                    headers=headers,
                )
                response.raise_for_status()
                data = response.json()

                similarity_score = data.get("data", {}).get("similarity_score", 0.0)
                if similarity_score:
                    max_score = max(max_score, similarity_score)
                comparisons.append(
                    (
                        reference_commit.commit_id,
                        {
                            "similarity_score": similarity_score,
                            "reason": "Initial comparison",
                        },
                    )
                )

            logger.info(
                f"Max comparison score for miner {miner_commit.miner_hotkey}: {max_score}"
            )
        except Exception as exc:
            logger.error(
                f"[CONTROLLER] Error while checking comparison score: {exc}"
            )

        for target_commit_id, result in comparisons:
            self._store_comparison(self.context, target_commit_id, result)
        return max_score

    # -- baseline comparison --

    def _build_baseline_payload(self, miner_commit: ScoringCommit) -> tuple[dict, dict]:
        miner_output = miner_commit.scoring_logs[0].miner_output.copy()
        if not miner_output:
            raise ValueError("Miner output is None or empty.")

        miner_submission_script = miner_output.get(
            self.challenge_info.get("script_path_identifier", None), None
        )
        payload = {
            "challenge_type": self.challenge_info.get("challenge_type", None),
            "miner_script": miner_submission_script,
            "identifier": self.challenge_info.get("script_path_identifier", None),
            "user_id": miner_commit.docker_hub_id,
        }
        return payload, miner_output

    def _send_baseline_request(self, payload: dict) -> list:
        internal_service_url = str(constants.INTERNAL_SERVICES.API_URL).rstrip("/")
        response = requests.post(
            f"{internal_service_url}/compare/baseline-scripts",
            timeout=self._get_compare_timeout(default=240),
            verify=False,  # nosec
            json=payload,
            headers=self._get_common_headers(),
        )
        response_data = response.json()
        return response_data.get("data", {})

    def _process_baseline_results(
        self, miner_commit: ScoringCommit, data: list, miner_output: dict
    ) -> None:
        baseline_commit_id = self.result_publisher.get_or_create_baseline_commit(
            self.context.challenge_id, self.context.miner_id
        )

        all_baseline_results: list[dict] = []
        max_similarity = 0.0

        for outputs in data:
            target_script = outputs.get("target", "script_1")
            similarity_score = outputs.get("similarity_score", 1.0)

            if isinstance(similarity_score, int):
                similarity_score = float(similarity_score)
            elif not isinstance(similarity_score, float):
                similarity_score = 1.0

            comparison_log = ComparisonLog(
                miner_output=miner_output,
                similarity_score=similarity_score,
                reason=outputs.get("reason", "Unknown"),
            )
            key = f"baseline_{target_script}"
            if key not in miner_commit.comparison_logs:
                miner_commit.comparison_logs[key] = []

            miner_commit.comparison_logs[key].append(comparison_log)

            all_baseline_results.append(outputs)
            max_similarity = max(max_similarity, similarity_score)

        if all_baseline_results:
            self._store_comparison(
                self.context,
                baseline_commit_id,
                {
                    "similarity_score": max_similarity,
                    "reason": "baseline comparison",
                    "baseline_results": all_baseline_results,
                },
            )

    def _compare_with_baseline(self, miner_commit: ScoringCommit):
        try:
            payload, miner_output = self._build_baseline_payload(miner_commit)
            data = self._send_baseline_request(payload)

            if not data:
                logger.warning(
                    f"[CONTROLLER] No baseline comparison data returned for miner "
                    f"{miner_commit.miner_hotkey}."
                )
                return

            self._process_baseline_results(miner_commit, data, miner_output)
        except Exception as e:
            logger.error(f"Error in baseline comparison request: {str(e)}")
            return
