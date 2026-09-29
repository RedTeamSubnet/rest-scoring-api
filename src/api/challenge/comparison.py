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

    def _run_reference_comparison_inputs(self, miner_commit: ScoringCommit):
        """
        Run miner with reference comparison commits inputs to compare performance.
        This method handles both baseline reference cache and similarity scoring.
        """

        reference_commits = self.reference_comparison_commits
        _reference_commit_limit = self.challenge_info["comparison_config"].get(
            "max_unique_commits", None
        )
        if _reference_commit_limit:
            reference_commits = reference_commits[:_reference_commit_limit]

        for reference_commit in reference_commits:

            _unique_commit_key = (
                f"{reference_commit.miner_uid}_{reference_commit.commit_id[:10]}"
            )
            logger.info(
                f"[CONTROLLER] Running comparison with reference commit {_unique_commit_key}"
            )
            if _unique_commit_key not in miner_commit.comparison_logs:
                miner_commit.comparison_logs[_unique_commit_key] = []

            if (
                not reference_commit.files
                or not miner_commit.scoring_logs
                or miner_commit.scoring_logs[0].miner_output is None
            ):
                logger.warning(
                    f"[CONTROLLER] Skipping comparison with {reference_commit.commit_id} because files or miner output are missing."
                )
                continue

            _miner_output = miner_commit.scoring_logs[0].miner_output.copy()
            _reference_output = {"commit_files": reference_commit.files}

            _compare_result = self._compare_outputs(
                miner_output=_miner_output,
                reference_files=reference_commit.files,
                user_id=miner_commit.docker_hub_id,
                target_commit_id=reference_commit.commit_id,
            )
            _similarity_score = _compare_result.get("similarity_score", 1.0)
            _similarity_reason = _compare_result.get("reason", "Unknown")

            self._exclude_output_keys(_miner_output, _reference_output)

            if (
                miner_commit.miner_hotkey == reference_commit.miner_hotkey
                and _similarity_score < self.max_self_comparison_score
            ):
                logger.warning(
                    f"[CONTROLLER] Skipping self-comparison for {miner_commit.miner_hotkey}\
                          with {reference_commit.miner_hotkey} due to low similarity score {_similarity_score}"
                )
                if _unique_commit_key in miner_commit.comparison_logs:
                    del miner_commit.comparison_logs[_unique_commit_key]
                continue

            comparison_log = ComparisonLog(
                miner_input=None,
                miner_output=_miner_output,
                reference_output=_reference_output,
                reference_hotkey=reference_commit.miner_hotkey,
                reference_similarity_score=reference_commit.penalty,
                similarity_score=_similarity_score,
                reason=_similarity_reason,
            )

            miner_commit.comparison_logs[_unique_commit_key].append(comparison_log)
            if _similarity_score > self.challenge_info["comparison_config"].get(
                "min_acceptable_score", 0.6
            ):
                logger.warning(
                    f"[CONTROLLER] Stopping comparison because of high similarity threshold is reached,\
                          similarity score {_similarity_score}"
                )
                return

            if (
                _unique_commit_key in miner_commit.comparison_logs
                and not miner_commit.comparison_logs[_unique_commit_key]
            ):
                logger.info(
                    f"[CONTROLLER] Removing empty comparison logs for {_unique_commit_key} for miner."
                )
                del miner_commit.comparison_logs[_unique_commit_key]
        self._compare_with_baseline(miner_commit)
        return

    def _compare_outputs(
        self,
        miner_output: dict,
        reference_files: list[dict],
        user_id: str | None = None,
        target_commit_id: str | None = None,
    ) -> dict:
        """
        Send comparison request to challenge container's /compare endpoint.

        Args:
            miner_output: The output from the current miner
            reference_files: The submitted files from the reference miner

        Returns:
            dict: Comparison score between 0 and 1, and reason for the score
        """

        try:
            payload = {
                "challenge_type": self.challenge_info.get("challenge_type", None),
                "challenge_name": self.challenge_info.get("name", None),
                "miner_script": miner_output.get("commit_files"),
                "reference_script": reference_files,
                "identifier": self.challenge_info.get("script_path_identifier", None),
                "user_id": user_id,
            }
            headers = {
                "Content-Type": "application/json",
                "X-API-KEY": constants.INTERNAL_SERVICES.API_KEY,
            }

            response = requests.post(
                f"{constants.INTERNAL_SERVICES.API_URL}/compare",
                timeout=self.challenge_info.get("challenge_compare_timeout", 300),
                verify=False,  # nosec
                json=payload,
                headers=headers,
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

        except Exception as e:
            logger.error(f"Error in comparison request: {str(e)}")
            data = {
                "target": "Error while comparing outputs",
                "similarity_score": 0.0,
                "reason": f"Error: {str(e)}",
            }
        if target_commit_id is not None:
            self._store_comparison(self.context, target_commit_id, data)
        return data

    def same_score_comparison(self, miner_commit: ScoringCommit) -> None:
        if not miner_commit.scoring_logs:
            logger.warning(
                f"[CONTROLLER] No scoring logs found for miner {miner_commit.miner_hotkey}, \
                    skipping same score comparison."
            )
            return
        _scoring_log = miner_commit.scoring_logs[0]
        _commit_score = _scoring_log.score
        if (
            _commit_score is None
            or _commit_score <= self.challenge_min_acceptable_score
        ):
            return
        reference_commits_in_range = []
        for ref_commit in self.reference_comparison_commits:
            if abs(ref_commit.score - _commit_score) <= 0.1:
                reference_commits_in_range.append(ref_commit)
        if not reference_commits_in_range:
            logger.info(
                f"[CONTROLLER] No reference commits found with score in range for miner {miner_commit.miner_hotkey}, \
                    skipping same score comparison."
            )
            return
        for ref_commit in reference_commits_in_range[:3]:
            _comparison_logs = self._compare_same_score_outputs(
                miner_output=_scoring_log.miner_output,
                reference_files=ref_commit.files,
                reference_score=ref_commit.score,
                user_id=miner_commit.docker_hub_id,
            )
            if (
                "similarity_score" in _comparison_logs
                and _comparison_logs["similarity_score"]
                >= self.comparison_min_acceptable_score
            ):
                if (
                    ref_commit.miner_hotkey == miner_commit.miner_hotkey
                    and _comparison_logs["similarity_score"]
                    < self.max_self_comparison_score
                ):
                    logger.info(
                        f"[CONTROLLER] Skipping same-score self-comparison for miner "
                        f"UID {miner_commit.miner_hotkey}: similarity score "
                        f"{_comparison_logs['similarity_score']} is below "
                        f"{self.max_self_comparison_score}."
                    )
                    continue
                _unique_commit_key = (
                    f"{ref_commit.miner_uid}_{ref_commit.commit_id[:10]}"
                )
                miner_commit.comparison_logs[_unique_commit_key] = [
                    ComparisonLog(
                        similarity_score=_comparison_logs["similarity_score"],
                        reason=_comparison_logs.get(
                            "reason", "similarity score above threshold"
                        ),
                    )
                ]
                self._store_comparison(
                    self.context, ref_commit.commit_id, _comparison_logs
                )

    def _compare_same_score_outputs(
        self,
        miner_output: dict,
        reference_files: list[dict],
        reference_score: float,
        user_id: str = "default_user",
    ) -> list[dict]:
        """
        Send comparison request to challenge container's /compare endpoint.

        Args:
            miner_input: The input used for both outputs
            miner_output: The output from the current miner
            reference_files: The submitted files from the reference miner
            reference_score: The reference miner's evaluated score

        Returns:
            dict: Comparison score between 0 and 1, and reason for the score
        """
        _miner_metadata = {
            "score": miner_output.get("score", 0),
            "telemetry": miner_output.get("telemetry", {}),
        }
        reference_metadata = {
            "score": reference_score,
            "telemetry": {},
        }
        try:
            payload = {
                "challenge_type": self.challenge_info.get("challenge_type", None),
                "challenge_name": self.challenge_info.get("name", None),
                "miner_script": miner_output.get("commit_files"),
                "reference_script": reference_files,
                "miner_metadata": _miner_metadata,
                "reference_metadata": reference_metadata,
                "user_id": user_id,
            }
            headers = {
                "Content-Type": "application/json",
                "X-API-KEY": constants.INTERNAL_SERVICES.API_KEY,
            }

            response = requests.post(
                f"{constants.INTERNAL_SERVICES.API_URL}/compare/same-score",
                timeout=self.challenge_info.get("challenge_compare_timeout", 300),
                verify=False,  # nosec
                json=payload,
                headers=headers,
            )
            if response.status_code == 404:
                logger.warning("No accepted submission to compare against.")
                return {
                    "similarity_score": 0.0,
                    "reason": "No accepted submission to compare against.",
                }

            response_data = response.json()
            data = response_data.get("data", [])

            return data

        except Exception as e:
            logger.error(f"Error in same-score comparison request: {str(e)}")
            return [
                {
                    "target": "Error while comparing outputs",
                    "similarity_score": 0.0,
                    "reason": f"Error: {str(e)}",
                }
            ]

    def _check_comparison_score(self, miner_commit: ScoringCommit) -> float:
        compare_url = f"{constants.INTERNAL_SERVICES.API_URL}/compare/all"
        max_score = 0.0
        comparisons: list[tuple[str, dict]] = []
        try:
            miner_files = miner_commit.scoring_logs[0].miner_output.get("commit_files")

            reference_commits = self.reference_comparison_commits
            _reference_commit_limit = self.challenge_info["comparison_config"].get(
                "max_unique_commits", None
            )
            if _reference_commit_limit:
                reference_commits = reference_commits[:_reference_commit_limit]

            headers = {
                "Content-Type": "application/json",
                "X-API-KEY": constants.INTERNAL_SERVICES.API_KEY,
            }

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

    def _compare_with_baseline(self, miner_commit: ScoringCommit):
        try:
            _miner_output = miner_commit.scoring_logs[0].miner_output.copy()
            if not _miner_output:
                raise ValueError("Miner output is None or empty.")

            _miner_submission_script = _miner_output.get(
                self.challenge_info.get("script_path_identifier", None), None
            )
            payload = {
                "challenge_type": self.challenge_info.get("challenge_type", None),
                "miner_script": _miner_submission_script,
                "identifier": self.challenge_info.get("script_path_identifier", None),
                "user_id": miner_commit.docker_hub_id,
            }
            headers = {
                "Content-Type": "application/json",
                "X-API-KEY": constants.INTERNAL_SERVICES.API_KEY,
            }
            _internal_service_url = str(constants.INTERNAL_SERVICES.API_URL).rstrip("/")
            response = requests.post(
                f"{_internal_service_url}/compare/baseline-scripts",
                timeout=self.challenge_info.get("challenge_compare_timeout", 240),
                verify=False,  # nosec
                json=payload,
                headers=headers,
            )

            response_data = response.json()
            data = response_data.get("data", {})
            if not data:
                logger.warning(
                    f"[CONTROLLER] No baseline comparison data returned for miner {miner_commit.miner_hotkey}."
                )
                return
            for _outputs in data:

                _target_script = _outputs.get("target", "script_1")
                _similarity_score = _outputs.get("similarity_score", 1.0)

                if isinstance(_similarity_score, int):
                    _similarity_score = float(_similarity_score)
                elif not isinstance(_similarity_score, float):
                    _similarity_score = 1.0

                comparison_log = ComparisonLog(
                    miner_output=_miner_output,
                    similarity_score=_similarity_score,
                    reason=_outputs.get("reason", "Unknown"),
                )
                if f"baseline_{_target_script}" not in miner_commit.comparison_logs:
                    miner_commit.comparison_logs[f"baseline_{_target_script}"] = []

                miner_commit.comparison_logs[f"baseline_{_target_script}"].append(
                    comparison_log
                )

            return

        except Exception as e:
            logger.error(f"Error in baseline comparison request: {str(e)}")
            return
