import time
import traceback

import logging
from redteam_core.config.main import constants
from redteam_core.validator.models import ComparisonLog, ScoringLog

from ..commit_context import ReferenceCommit, ScoringCommit, ScoringWorkItem
from ..result_publisher import ResultPublisher
from ..utils import docker as docker_utils
from .comparison import Comparison
from .finalization import FinalOutput
from .scoring import Scoring
from .utils import ChallengeUtils
from .validation import Validation, ValidationOutput

logger = logging.getLogger(__name__)


class Controller(Comparison, Validation, Scoring, ChallengeUtils, FinalOutput):
    """Orchestrate one challenge evaluation and own all result persistence."""

    def __init__(
        self,
        challenge_name: str,
        challenge_info: dict,
        work_item: ScoringWorkItem,
        reference_comparison_commits: list[ReferenceCommit],
        miners_docker_info: dict[str, dict],
        result_publisher: ResultPublisher,
    ):
        """
        Initializes the Controller with the name of the challenge and the list of miner Docker images.
        Also sets up the Docker client for interacting with Docker containers.

        Args:
            challenge_name: The name of the challenge to be executed.
            miner_docker_images: A list of Docker images to be used for the miners.
        """

        self.challenge_name = challenge_name
        self.challenge_info = challenge_info
        self.work_item = work_item
        self.miner_commit = work_item.commit
        self.context = work_item.context
        self.failed = False
        self.result_publisher = result_publisher
        self.reference_comparison_commits = reference_comparison_commits
        self.miners_docker_info = miners_docker_info

        self.docker_client = docker_utils.create_docker_client()

        self.local_network = "redteam_local"
        self.miner_ip = None

        self.max_self_comparison_score = self.challenge_info["comparison_config"].get(
            "max_self_comparison_score", 0.9
        )
        self.challenge_min_acceptable_score = self.challenge_info.get(
            "challenge_min_acceptable_score", 0.9
        )
        comparison_config = self.challenge_info.get("comparison_config", {})
        self.comparison_min_acceptable_score = comparison_config.get(
            "min_acceptable_score", 0.7
        )

    def _setup_challenge(self):
        """
        Sets up the challenge environment by building and running the challenge container
        in an isolated Docker network. Includes building the image, creating the network,
        and verifying the container's health status.
        """

        # Remove existing challenge container
        docker_utils.remove_container(
            client=self.docker_client,
            container_name=self.challenge_name,
            stop_timeout=10,
            force=True,
            remove_volumes=True,
        )

        # Create network
        docker_utils.create_network(
            client=self.docker_client,
            network_name=self.local_network,
            allow_internet=False,
        )

        self.challenge_container = docker_utils.run_container(
            client=self.docker_client,
            image=self.challenge_info["challenge_image"],
            detach=True,
            ports={
                f"{constants.CHALLENGE_DOCKER_PORT}/tcp": constants.CHALLENGE_DOCKER_PORT
            },
            **self.challenge_info.get("challenge_container_run_kwargs", {}),
        )
        logger.info(
            f"[CONTROLLER] Challenge container started: {self.challenge_container.status}"
        )

        _protocol, _ssl_verify = self._check_protocol(is_challenger=True)
        docker_utils.check_container_alive(
            container=self.challenge_container,
            health_port=constants.CHALLENGE_DOCKER_PORT,
            protocol=_protocol,
            ssl_verify=_ssl_verify,
        )

    def start_challenge(self):
        """Score one commit and release its miner and challenge containers."""
        miner_commit = self.miner_commit
        uid, hotkey = miner_commit.miner_uid, miner_commit.miner_hotkey
        try:
            self._setup_challenge()
            try:
                self._setup_miner_container(miner_commit)
                Scoring.start(self, miner_commit)
                validation_output = Validation.start(self, miner_commit)
                self._store_validation_output(validation_output)
                if not validation_output.is_valid:
                    Comparison.reject_invalid_submission(miner_commit)
                else:
                    max_comparison_score = self._check_comparison_score(miner_commit)
                    if max_comparison_score >= 0.6:
                        logger.info(
                            f"[CONTROLLER] Miner {hotkey} has high comparison score "
                            f"{max_comparison_score}, skipping reference comparison."
                        )
                        miner_commit.comparison_logs = {
                            "skipped": [
                                ComparisonLog(
                                    similarity_score=max_comparison_score,
                                    reason="high similarity detected",
                                )
                            ]
                        }
                    else:
                        Comparison.start(self, miner_commit)
                    Scoring.score_new_inputs(self, miner_commit)
                    Comparison.same_score_comparison(self, miner_commit)
                FinalOutput.start(self, miner_commit)
            except Exception as exc:
                self.failed = True
                logger.error(
                    f"Error while processing miner {uid} - {hotkey}: {exc}"
                )
                logger.error(traceback.format_exc())
                if not miner_commit.scoring_logs:
                    miner_commit.scoring_logs.append(
                        ScoringLog(
                            miner_input=None,
                            miner_output=None,
                            score=0,
                            error=str(exc),
                        )
                    )
        finally:
            docker_utils.remove_container_by_port(
                client=self.docker_client,
                port=constants.MINER_DOCKER_PORT,
            )
            docker_utils.clean_docker_resources(
                client=self.docker_client,
                remove_containers=True,
                remove_images=True,
            )
            logger.debug("[CONTROLLER] Challenge completed, cleaning up container")
            docker_utils.remove_container(
                client=self.docker_client,
                container_name=self.challenge_name,
                stop_timeout=10,
                force=True,
                remove_volumes=True,
            )
            docker_utils.clean_docker_resources(
                client=self.docker_client,
                remove_containers=True,
                remove_images=False,
            )

    def _setup_miner_container(self, miner_commit: ScoringCommit):
        """Setup and validate miner container. Raises if validation or setup fails."""

        if not docker_utils.is_image_digest_format_valid(miner_commit.docker_hub_id):
            raise ValueError("Invalid image format")

        docker_utils.remove_container_by_port(
            client=self.docker_client,
            port=constants.MINER_DOCKER_PORT,
        )

        logger.info(
            f"[CONTROLLER] Running miner {miner_commit.miner_uid} - {miner_commit.docker_hub_id}"
        )

        miner_start_time = time.time()
        miner_docker_info = self.miners_docker_info.get(str(miner_commit.miner_uid), {})
        miner_container = docker_utils.run_container(
            is_miner=True,
            client=self.docker_client,
            image=miner_commit.docker_hub_id,
            detach=True,
            miner_docker_info=miner_docker_info,
            **self.challenge_info.get("miner_container_run_kwargs", {}),
        )
        miner_container.reload()
        _local_network = miner_container.attrs["NetworkSettings"]["Networks"].get(
            self.local_network, None
        )
        if _local_network:
            self.miner_ip = _local_network.get("IPAddress", None)
        else:
            self.miner_ip = "localhost"

        # Check miner container health
        _protocol, _ssl_verify = self._check_protocol(is_challenger=False)
        docker_utils.check_container_alive(
            container=miner_container,
            health_port=constants.MINER_DOCKER_PORT,
            protocol=_protocol,
            ssl_verify=_ssl_verify,
            timeout=self.challenge_info.get("docker_run_timeout", 600),
            start_time=miner_start_time,
            ip=self.miner_ip,
        )

    def _store_validation(self, *args, **kwargs):
        return self.result_publisher.publish_validation(*args, **kwargs)

    def _store_validation_output(self, output: ValidationOutput) -> None:
        self._store_validation(self.context, output.data)
        for check_name, check in output.checks.items():
            self._store_validation(self.context, check, check_name=check_name)

    def _store_comparison(self, *args, **kwargs):
        return self.result_publisher.publish_comparison(*args, **kwargs)

    def _store_file(self, *args, **kwargs):
        return self.result_publisher.publish_file(*args, **kwargs)

    def _store_output(self, *args, **kwargs):
        return self.result_publisher.publish_output(*args, **kwargs)


__all__ = ["Controller"]
