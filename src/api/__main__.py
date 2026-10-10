import base64
import datetime
import threading
import traceback

import logging
from dotenv import load_dotenv

from redteam_core.config import constants

from ._base import BaseScoringApi
from .core_api import CoreApiClient
from .commit_context import (
    CommitContext,
    ReferenceCommit,
    ScoringCommit,
    ScoringWorkItem,
)
from .result_publisher import ResultPublisher
from .router import start_ping_server
from .challenge.main import Controller
from .utils.challenge_info import prepare_challenge_info
from .utils.helpers import get_docker_hub_id

logger = logging.getLogger(__name__)

load_dotenv(".env", override=True)


class ScoringApi(BaseScoringApi):
    """Score queued rest-core submissions one commit at a time."""

    def __init__(self):
        super().__init__()
        self.core_api = CoreApiClient(
            base_url=self.scoring_api_config.CORE_API_URL,
            api_key=self.scoring_api_config.CORE_API_KEY,
        )
        self.result_publisher = ResultPublisher(self.core_api)
        self.active_challenges: dict = {}
        self.active_challenge_ids: dict[str, str] = {}

        self._init_active_challenges()

    def _init_active_challenges(self) -> None:
        """Refresh active challenge configs from rest-core storage."""
        active_challenges: dict[str, dict] = {}
        active_challenge_ids: dict[str, str] = {}

        for challenge in self.core_api.list_challenges(expands=["config"]):
            if challenge.get("end_at") is not None:
                continue

            challenge_id = challenge.get("id")
            challenge_name = challenge.get("name")
            expanded_config = challenge.get("config")
            spec = (
                expanded_config.get("spec")
                if isinstance(expanded_config, dict)
                else None
            )
            if not (
                isinstance(challenge_id, str)
                and isinstance(challenge_name, str)
                and isinstance(spec, dict)
            ):
                logger.warning(
                    "[CORE] Skipping active challenge with missing ID, name, or config spec"
                )
                continue

            challenge_info = prepare_challenge_info(spec)
            challenge_info.setdefault("name", challenge_name)
            active_challenges[challenge_name] = challenge_info
            active_challenge_ids[challenge_id] = challenge_name

        self.active_challenges = active_challenges
        self.active_challenge_ids = active_challenge_ids

    def _active_core_challenges(self) -> dict[str, str]:
        """Map rest-core challenge IDs to currently active controller names."""
        return dict(self.active_challenge_ids)

    def _registered_miner(self, miner_id: str) -> tuple[int, str] | None:
        """Return the registered UID and hotkey for a core miner."""
        try:
            neuron = self.core_api.get_neuron(miner_id)
            uid = neuron.get("uid")
            hotkey = neuron.get("hotkey_address")
            if not isinstance(uid, int) or not isinstance(hotkey, str):
                raise ValueError("neuron is missing UID or hotkey")
            if neuron.get("deregistered_at") is not None:
                logger.info(f"[CORE] Skipping deregistered miner {uid}/{hotkey}")
                return None
            return uid, hotkey
        except Exception:
            logger.error(
                f"[CORE] Failed to resolve commit miner: {traceback.format_exc()}"
            )
            return None

    def _accepted_core_commits(
        self,
        challenge_id: str,
    ) -> list[ReferenceCommit]:
        """Load accepted reference files and comparison identifiers."""
        accepted: list[ReferenceCommit] = []
        for core_commit in self.core_api.list_commits(
            state="DONE",
            challenge_id=challenge_id,
            expands=["commit_result"],
        ):
            result = core_commit.get("commit_result")
            commit_id = core_commit.get("id")
            if (
                not isinstance(result, dict)
                or result.get("status") != "ACCEPTED"
                or not isinstance(commit_id, str)
            ):
                continue
            try:
                files = list(self.core_api.list_commit_files(commit_id))
                commit_files = []
                for file in files:
                    content = file.get("data")
                    if isinstance(content, bytes):
                        content = content.decode("utf-8")
                    if not isinstance(content, str):
                        raise ValueError("commit file data is missing")
                    size_bytes = file.get("size_bytes")
                    if (
                        isinstance(size_bytes, int)
                        and len(content.encode()) != size_bytes
                    ):
                        content = base64.b64decode(content, validate=True).decode(
                            "utf-8"
                        )
                    filename = file.get("orig_filename") or file.get("filename")
                    if not isinstance(filename, str):
                        raise ValueError("commit filename is missing")
                    commit_files.append(
                        {
                            "file_name": filename,
                            "content": content,
                        }
                    )
                if not commit_files:
                    continue
            except Exception:
                logger.error(
                    f"[CORE] Failed to fetch files for reference {commit_id}: "
                    f"{traceback.format_exc()}"
                )
                continue
            miner_id = core_commit.get("miner_id")
            if not isinstance(miner_id, str):
                continue
            try:
                neuron = self.core_api.get_neuron(miner_id)
                uid = neuron.get("uid")
                hotkey = neuron.get("hotkey_address")
                if not isinstance(uid, int) or not isinstance(hotkey, str):
                    continue
                accepted.append(
                    ReferenceCommit(
                        commit_id=commit_id,
                        miner_uid=uid,
                        miner_hotkey=hotkey,
                        files=commit_files,
                        score=float(result.get("evaluated_score") or 0.0),
                        penalty=float(result.get("penalty_score") or 0.0),
                    )
                )
            except Exception:
                logger.error(
                    f"[CORE] Failed to resolve reference {commit_id}: "
                    f"{traceback.format_exc()}"
                )
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

    def _reconcile_result(self, result: dict, commit_id: str) -> None:
        status = result.get("status")
        if status in {"ACCEPTED", "REJECTED"}:
            self.core_api.update(f"/commits/{commit_id}", {"state": "DONE"})
        elif status == "FAILED":
            self.core_api.update(f"/commits/{commit_id}", {"state": "FAILED"})
        else:
            logger.info(
                f"[CORE] Commit {commit_id} already has result status {status}; skipping"
            )

    def _prepare_work_item(
        self, core_commit: dict, challenge_name: str
    ) -> ScoringWorkItem | None:
        commit_id = core_commit.get("id")
        challenge_id = core_commit.get("challenge_id")
        miner_id = core_commit.get("miner_id")
        if not all(
            isinstance(value, str) for value in (commit_id, challenge_id, miner_id)
        ):
            return None

        expanded_result = core_commit.get("commit_result")
        if isinstance(expanded_result, dict):
            self._reconcile_result(expanded_result, commit_id)
            return None

        cipher_commit = core_commit.get("cipher_commit")
        docker_hub_id = get_docker_hub_id(core_commit.get("plain_commit"))
        if not isinstance(cipher_commit, str) or not docker_hub_id:
            logger.warning(
                "[CORE] Skipping commit missing ciphertext or revealed Docker ID"
            )
            return None
        identity = self._registered_miner(miner_id)
        if identity is None:
            return None
        uid, hotkey = identity
        commit = ScoringCommit(
            miner_uid=uid,
            miner_hotkey=hotkey,
            challenge_name=challenge_name,
            docker_hub_id=docker_hub_id,
            encrypted_commit=cipher_commit,
        )
        try:
            result, created = self.result_publisher.get_or_create_result(
                commit_id, miner_id
            )
        except Exception:
            self.core_api.update(f"/commits/{commit_id}", {"state": "FAILED"})
            raise
        if not created:
            self._reconcile_result(result, commit_id)
            return None
        item = ScoringWorkItem(
            commit=commit,
            context=CommitContext(commit_id, result["id"], challenge_id, miner_id),
        )
        try:
            self.result_publisher.start_result(result["id"])
        except Exception as exc:
            self._fail_work_item(item, str(exc))
            return None
        return item

    def _score_work_item(self, item: ScoringWorkItem, challenge_name: str) -> None:
        challenge_info = self.active_challenges[challenge_name]
        commit = item.commit
        try:
            self.core_api.update(
                f"/commits/{item.context.commit_id}", {"state": "VALIDATING"}
            )
        except Exception:
            logger.warning(
                f"[CORE] Failed to set commit {item.context.commit_id} to VALIDATING: "
                f"{traceback.format_exc()}"
            )
        try:
            references = self._accepted_core_commits(item.context.challenge_id)
            try:
                docker_info = self._docker_info_for(
                    item.context.miner_id, commit.miner_uid
                )
            except Exception:
                logger.error(
                    f"[CORE] Failed to load Docker registry: {traceback.format_exc()}"
                )
                docker_info = {}
            controller = Controller(
                challenge_name=challenge_name,
                challenge_info=challenge_info,
                work_item=item,
                reference_comparison_commits=references,
                miners_docker_info=docker_info,
                result_publisher=self.result_publisher,
            )
            controller.start_challenge()
            if controller.failed:
                raise RuntimeError("Miner scoring failed")
            if commit.score is None or commit.accepted is None:
                raise ValueError("Scoring produced no final result")
        except Exception as exc:
            logger.error(f"[CORE] Scoring failed: {traceback.format_exc()}")
            self._fail_work_item(item, str(exc))
            return

        try:
            self.result_publisher.finish_result(
                item.context,
                accepted=commit.accepted,
                evaluated_score=commit.get_higest_scoring_score(),
                score=commit.score,
                penalty=commit.penalty or 0.0,
            )
        except Exception as exc:
            logger.error(f"[CORE] Failed to publish result: {traceback.format_exc()}")
            self._fail_work_item(item, str(exc))
            return
        try:
            self.core_api.update(
                f"/commits/{item.context.commit_id}", {"state": "DONE"}
            )
        except Exception:
            logger.error(f"[CORE] Failed to finalize commit: {traceback.format_exc()}")

    def _fail_work_item(self, item: ScoringWorkItem, error: str) -> None:
        try:
            self.result_publisher.fail_result(item.context, error)
        except Exception:
            logger.error(f"[CORE] Failed to publish error: {traceback.format_exc()}")
        try:
            self.core_api.update(
                f"/commits/{item.context.commit_id}", {"state": "FAILED"}
            )
        except Exception:
            logger.error(
                f"[CORE] Failed to mark commit FAILED: {traceback.format_exc()}"
            )

    def _transition_committed_to_queued(self) -> None:
        """Transition COMMITTED commits to QUEUED after the cooldown period."""
        cooldown = constants.COMMIT_COOLDOWN
        now = datetime.datetime.now(datetime.timezone.utc)
        queued_commits = []
        for core_commit in self.core_api.list_commits(state="COMMITTED"):
            commit_id = core_commit.get("id")
            committed_at_str = core_commit.get("committed_at")
            if not isinstance(commit_id, str) or not isinstance(committed_at_str, str):
                continue
            try:
                committed_at = datetime.datetime.fromisoformat(
                    str(committed_at_str).replace("Z", "+00:00")
                )
            except ValueError:
                logger.warning(
                    f"[CORE] Commit {commit_id} has unparseable committed_at: "
                    f"{committed_at_str}"
                )
                continue
            if committed_at.tzinfo is None:
                committed_at = committed_at.replace(tzinfo=datetime.timezone.utc)
            elapsed = (now - committed_at).total_seconds()
            if elapsed < cooldown:
                continue
            try:
                self.core_api.update(f"/commits/{commit_id}", {"state": "QUEUED"})
                logger.info(
                    f"[CORE] Commit {commit_id} transitioned COMMITTED -> QUEUED "
                    f"(elapsed {int(elapsed)}s > cooldown {cooldown}s)"
                )
                queued_commits.append(core_commit)
            except Exception:
                logger.error(
                    f"[CORE] Failed to transition commit {commit_id} to QUEUED: "
                    f"{traceback.format_exc()}"
                )

    def forward(self) -> None:
        """Score queued commits sequentially; rest-core result status tracks progress."""
        self._init_active_challenges()
        queued_commits = self.core_api.list_commits(state="QUEUED")
        self._transition_committed_to_queued()
        try:
            challenge_names = self._active_core_challenges()
            for core_commit in queued_commits:
                challenge_name = challenge_names.get(core_commit.get("challenge_id"))
                if challenge_name is None:
                    continue
                try:
                    item = self._prepare_work_item(core_commit, challenge_name)
                    if item is not None:
                        self._score_work_item(item, challenge_name)
                except Exception:
                    logger.error(
                        f"[CORE] Commit processing failed: {traceback.format_exc()}"
                    )
        except Exception:
            logger.error(f"[CORE] Forward failed: {traceback.format_exc()}")


if __name__ == "__main__":
    app = ScoringApi()
    server_thread = threading.Thread(
        target=start_ping_server,
        args=(app.scoring_api_config.PORT,),
        daemon=True,
    )
    server_thread.start()
    try:
        app.run()
    except KeyboardInterrupt:
        logger.info("Scoring API stopped.")
