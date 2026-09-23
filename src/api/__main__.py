import datetime
import os
import threading
import time
import traceback
from copy import deepcopy

import bittensor as bt
from dotenv import load_dotenv

from redteam_core.challenge_pool import ACTIVE_CHALLENGES
from redteam_core.config import ENV_PREFIX_SCORING_API
from redteam_core.validator import ChallengeManager
from redteam_core.validator.models import MinerChallengeCommit

from ._base import BaseScoringApi
from .core_api import CoreApiClient
from .router import start_ping_server
from .seen_commits import SeenCommits
from .challenge.controller import Controller

load_dotenv(".env", override=True)

SCORING_API_PORT = int(os.getenv(f"{ENV_PREFIX_SCORING_API}PORT", 8000))
_BATCH_SIZE = 3


class ScoringApi(BaseScoringApi):
    """Score unseen queued rest-core submissions once per configured epoch."""

    def __init__(self):
        super().__init__()
        self.hotkey = self.wallet.hotkey.ss58_address
        self.uid = self.scoring_api_config.UID
        self.core_api = CoreApiClient(
            base_url=self.scoring_api_config.CORE_API_URL,
            api_key=self.scoring_api_config.CORE_API_KEY,
        )
        self.seen_commits = SeenCommits(self.scoring_api_config.CACHE_DIR)
        self.challenge_managers: dict[str, ChallengeManager] = {}
        self.active_challenges: dict = {}
        self.miners_docker_info: dict[str, dict] = {}

        self._init_active_challenges()
        self._initialize_seen_commits()

    def _init_active_challenges(self) -> None:
        """Refresh challenge controllers against the latest metagraph."""
        self.active_challenges = deepcopy(ACTIVE_CHALLENGES)
        for challenge_name, challenge_info in self.active_challenges.items():
            if challenge_name not in self.challenge_managers:
                self.challenge_managers[challenge_name] = challenge_info[
                    "challenge_manager"
                ](
                    challenge_info=challenge_info,
                    metagraph=self.metagraph,
                )
        self.challenge_managers = {
            challenge_name: self.challenge_managers[challenge_name]
            for challenge_name in self.active_challenges
        }

    def _initialize_seen_commits(self) -> None:
        """Seed durable dedupe state from every commit already in rest-core."""
        count = 0
        try:
            for core_commit in self.core_api.list_commits():
                challenge_id = core_commit.get("challenge_id")
                cipher_commit = core_commit.get("cipher_commit")
                if isinstance(challenge_id, str) and isinstance(cipher_commit, str):
                    self.seen_commits.add(challenge_id, cipher_commit)
                    count += 1
            self.seen_commits.save()
            bt.logging.success(f"[CORE INIT] Seeded seen cache from {count} commits")
        except Exception:
            bt.logging.error(
                f"[CORE INIT] Failed to seed seen cache: {traceback.format_exc()}"
            )

    @staticmethod
    def _core_timestamp(value: object) -> float | None:
        if not isinstance(value, str):
            return None
        try:
            return datetime.datetime.fromisoformat(
                value.replace("Z", "+00:00")
            ).timestamp()
        except ValueError:
            return None

    @staticmethod
    def _docker_hub_id(plain_commit: object) -> str | None:
        if not isinstance(plain_commit, str) or "---" not in plain_commit:
            return None
        _, docker_hub_id = plain_commit.split("---", 1)
        return docker_hub_id or None

    def _active_core_challenges(self) -> dict[str, str]:
        """Map rest-core challenge IDs to currently active controller names."""
        mappings: dict[str, str] = {}
        for challenge in self.core_api.list_challenges():
            challenge_id = challenge.get("id")
            challenge_name = challenge.get("name")
            if challenge_name in self.active_challenges:
                mappings[challenge_id] = challenge_name
        return mappings

    def _core_commit_to_miner_commit(
        self,
        core_commit: dict,
        challenge_name: str,
        *,
        require_registered: bool,
    ) -> tuple[MinerChallengeCommit, str] | None:
        """Resolve one core commit into the controller's input model."""
        miner_id = core_commit.get("miner_id")
        cipher_commit = core_commit.get("cipher_commit")
        plain_commit = core_commit.get("plain_commit")
        docker_hub_id = self._docker_hub_id(plain_commit)
        if (
            not isinstance(miner_id, str)
            or not isinstance(cipher_commit, str)
            or not isinstance(plain_commit, str)
            or not docker_hub_id
        ):
            bt.logging.warning(
                "[CORE] Skipping commit missing miner, ciphertext, or revealed Docker ID"
            )
            return None

        try:
            neuron = self.core_api.get_neuron(miner_id)
            uid = neuron.get("uid")
            hotkey = neuron.get("hotkey_address")
            if not isinstance(uid, int) or not isinstance(hotkey, str):
                raise ValueError("neuron is missing UID or hotkey")
            if require_registered and not list(
                self.core_api.list_neurons(
                    uid=uid,
                    hotkey_address=hotkey,
                    only_registered=True,
                )
            ):
                bt.logging.info(f"[CORE] Skipping deregistered miner {uid}/{hotkey}")
                return None
            return (
                MinerChallengeCommit(
                    miner_uid=uid,
                    miner_hotkey=hotkey,
                    challenge_name=challenge_name,
                    docker_hub_id=docker_hub_id,
                    commit_timestamp=self._core_timestamp(
                        core_commit.get("committed_at")
                    ),
                    encrypted_commit=cipher_commit,
                    commit=plain_commit,
                ),
                miner_id,
            )
        except Exception:
            bt.logging.error(
                f"[CORE] Failed to resolve commit miner: {traceback.format_exc()}"
            )
            return None

    def _accepted_core_commits(
        self,
        challenge_id: str,
        challenge_name: str,
    ) -> list[MinerChallengeCommit]:
        """Load accepted reference commits and all files attached to each one."""
        accepted: list[MinerChallengeCommit] = []
        for core_commit in self.core_api.list_commits(
            state="DONE",
            challenge_id=challenge_id,
            expands=["commit_result"],
        ):
            result = core_commit.get("commit_result")
            commit_id = core_commit.get("id")
            if (
                not isinstance(result, dict)
                or result.get("accepted") is not True
                or not isinstance(commit_id, str)
            ):
                continue
            try:
                file_count = len(list(self.core_api.list_commit_files(commit_id)))
                bt.logging.debug(
                    f"[CORE] Loaded {file_count} reference files for {commit_id}"
                )
            except Exception:
                bt.logging.error(
                    f"[CORE] Failed to fetch files for reference {commit_id}: "
                    f"{traceback.format_exc()}"
                )
                continue
            resolved = self._core_commit_to_miner_commit(
                core_commit,
                challenge_name,
                require_registered=False,
            )
            if resolved:
                accepted.append(resolved[0])
        return accepted

    def _docker_info_for(self, miner_id: str, miner_uid: int) -> dict[str, dict]:
        registries = list(self.core_api.list_miner_docker_registries(miner_id))
        registry = next(
            (item for item in registries if item.get("is_active", True)), None
        )
        if not registry or not isinstance(registry.get("id"), str):
            return {}
        username = registry.get("username")
        if not isinstance(username, str):
            return {}
        token = self.core_api.decrypt_miner_docker_registry(registry["id"])
        return {
            str(miner_uid): {
                "dockerhub_username": username,
                "personal_access_token": token,
            }
        }

    def forward(self) -> None:
        """Fetch unseen queued commits from core and score them in small batches."""
        self._init_active_challenges()
        try:
            challenge_names = self._active_core_challenges()
            queued_by_challenge: dict[str, list[tuple[MinerChallengeCommit, str]]] = {}
            for core_commit in self.core_api.list_commits(state="QUEUED"):
                challenge_id = core_commit.get("challenge_id")
                cipher_commit = core_commit.get("cipher_commit")
                if (
                    not isinstance(challenge_id, str)
                    or not isinstance(cipher_commit, str)
                    or challenge_id not in challenge_names
                    or self.seen_commits.contains(challenge_id, cipher_commit)
                ):
                    continue

                # Deliberate policy: once fetched, a commit is never retried
                # automatically, including registration/controller failures.
                self.seen_commits.add(challenge_id, cipher_commit)
                self.seen_commits.save()
                resolved = self._core_commit_to_miner_commit(
                    core_commit,
                    challenge_names[challenge_id],
                    require_registered=True,
                )
                if resolved:
                    queued_by_challenge.setdefault(challenge_id, []).append(resolved)

            if not queued_by_challenge:
                bt.logging.info("[CORE] No unseen queued commits")
                return

            for challenge_id, queued in queued_by_challenge.items():
                challenge_name = challenge_names[challenge_id]
                references = self._accepted_core_commits(
                    challenge_id,
                    challenge_name,
                )
                for batch_start in range(0, len(queued), _BATCH_SIZE):
                    batch = queued[batch_start : batch_start + _BATCH_SIZE]
                    commits = [commit for commit, _ in batch]
                    self.challenge_managers[challenge_name].update_miner_infos(commits)
                    self.miners_docker_info = {}
                    for commit, miner_id in batch:
                        try:
                            self.miners_docker_info.update(
                                self._docker_info_for(miner_id, commit.miner_uid)
                            )
                        except Exception:
                            bt.logging.error(
                                "[CORE] Failed to load Docker registry: "
                                f"{traceback.format_exc()}"
                            )
                    controller = Controller(
                        challenge_name=challenge_name,
                        miners_docker_info=self.miners_docker_info,
                        miner_commits=commits,
                        reference_comparison_commits=references,
                        challenge_info=self.active_challenges[challenge_name],
                    )
                    controller.start_challenge()
                    self.challenge_managers[challenge_name].update_miner_scores(
                        controller.miner_commits
                    )
        except Exception:
            bt.logging.error(f"[CORE] Forward failed: {traceback.format_exc()}")


if __name__ == "__main__":
    server_thread = threading.Thread(
        target=start_ping_server,
        args=(SCORING_API_PORT,),
        daemon=True,
    )
    server_thread.start()
    with ScoringApi() as app:
        while True:
            bt.logging.info("ScoringApi is running...")
            time.sleep(app.config.EPOCH_LENGTH // 4)
