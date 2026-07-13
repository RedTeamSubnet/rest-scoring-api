import datetime
import json
import threading
import time
import traceback
from copy import deepcopy
from typing import Any

import bittensor as bt
import requests
from dotenv import load_dotenv

load_dotenv(".env", override=True)

from redteam_core.challenge_pool import ACTIVE_CHALLENGES
from redteam_core.validator import start_bittensor_log_listener
from redteam_core.validator.models import (
    MinerChallengeCommit,
    ScoringLog,
)
from redteam_core.validator.utils import create_validator_request_header_fn

from ._base import BaseScoringApi
from .config import ScoringApiMainConfig
from .router import start_ping_server


def _join_url(base: str, *parts: str) -> str:
    value = base.rstrip("/")
    for part in parts:
        if part:
            value = f"{value}/{part.strip('/')}"
    return value


class ScoringStorageClient:
    """Thin client for storage endpoints used by the stateless scorer."""

    def __init__(self, storage_url: str, header_fn, api_prefix: str):
        self.storage_url = storage_url.rstrip("/")
        self.header_fn = header_fn
        self.api_prefix = api_prefix.strip("/")

    def _headers(self, payload: Any) -> dict:
        return self.header_fn(payload)

    def _url(self, path: str) -> str:
        return _join_url(self.storage_url, self.api_prefix, path)

    def fetch_work_items(self, challenge_name: str | None, limit: int) -> list[dict]:
        params: dict[str, Any] = {"limit": limit}
        if challenge_name:
            params["challenge_name"] = challenge_name
        response = requests.get(
            self._url("/scoring/work-items"),
            params=params,
            headers=self._headers(params),
            timeout=120,
        )
        response.raise_for_status()
        return response.json().get("data", [])

    def fetch_docker_credentials(
        self, miner_ids: list[str], hotkey_addresses: list[str]
    ) -> list[dict]:
        payload = {
            "miner_ids": sorted(set(miner_ids)),
            "hotkey_addresses": sorted(set(hotkey_addresses)),
        }
        response = requests.post(
            self._url("/scoring/docker-credentials"),
            json=payload,
            headers=self._headers(payload),
            timeout=120,
        )
        response.raise_for_status()
        return response.json().get("data", [])

    def fetch_accepted_reference_commits(
        self, challenge_name: str, limit: int
    ) -> list[dict]:
        params = {"challenge_name": challenge_name, "limit": limit}
        response = requests.get(
            self._url("/scoring/accepted-reference-commits"),
            params=params,
            headers=self._headers(params),
            timeout=180,
        )
        response.raise_for_status()
        return response.json().get("data", [])

    def fetch_commit(self, commit_id: str) -> dict | None:
        response = requests.get(
            self._url(f"/commits/{commit_id}"),
            headers=self._headers({"commit_id": commit_id}),
            timeout=60,
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json().get("data")

    def upload_results(self, results: list[dict]) -> list[dict]:
        if not results:
            return []
        payload = {"results": results}
        response = requests.post(
            self._url("/scoring/results"),
            json=payload,
            headers=self._headers(payload),
            timeout=180,
        )
        response.raise_for_status()
        return response.json().get("data", [])


def _as_timestamp(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    if isinstance(value, datetime.datetime):
        return value.timestamp()
    return None


def _utc_iso(timestamp: float | None = None) -> str:
    if timestamp is None:
        dt = datetime.datetime.now(datetime.timezone.utc)
    else:
        dt = datetime.datetime.fromtimestamp(timestamp, tz=datetime.timezone.utc)
    return dt.isoformat()


def _docker_hub_id_from_plain_commit(challenge_name: str, plain_commit: str) -> str:
    prefix = f"{challenge_name}---"
    if plain_commit.startswith(prefix):
        return plain_commit[len(prefix) :]
    if "---" in plain_commit:
        return plain_commit.split("---", 1)[1]
    return plain_commit


def _json_loads_maybe(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except Exception:
        return value


def _extract_commit_files(payload: dict) -> tuple[list[dict], dict | None]:
    """Return replayable commit_files and optional telemetry from storage payload."""
    for output in payload.get("commit_outputs") or []:
        data = _json_loads_maybe(output.get("data"))
        if isinstance(data, dict) and isinstance(data.get("commit_files"), list):
            return data["commit_files"], data.get("telemetry")
        if isinstance(data, list):
            return data, None

    commit_files = []
    for file_data in payload.get("commit_files") or []:
        data = _json_loads_maybe(file_data.get("data"))
        if isinstance(data, dict) and isinstance(data.get("commit_files"), list):
            return data["commit_files"], data.get("telemetry")
        if isinstance(data, list):
            return data, None
        content = data if isinstance(data, str) else file_data.get("content")
        filename = (
            file_data.get("file_name")
            or file_data.get("orig_filename")
            or file_data.get("filename")
        )
        if filename and content is not None:
            commit_files.append({"file_name": filename, "content": content})
    return commit_files, None


class ScoringApi(BaseScoringApi):
    """
    Stateless centralized scoring service.

    The storage API owns commit aggregation, reveal/decrypt state, deduplication, and
    scored/unscored filtering. This process fetches work, runs challenge controllers,
    uploads final results, and does not persist local scoring state.
    """

    def __init__(self):
        super().__init__()

        self.hotkey = self.wallet.hotkey.ss58_address
        self.uid = self.scoring_api_config.UID
        self.validator_request_header_fn = create_validator_request_header_fn(
            validator_uid=self.uid,
            validator_hotkey=self.wallet.hotkey.ss58_address,
            keypair=self.wallet.hotkey,
        )

        storage_api_key = self._get_storage_api_key()
        if storage_api_key:
            start_bittensor_log_listener(api_key=storage_api_key)
        else:
            bt.logging.warning(
                "[INIT] Storage API key unavailable; centralized log listener disabled"
            )

        self.storage = ScoringStorageClient(
            storage_url=str(self.config.STORAGE_API_URL),
            header_fn=self.validator_request_header_fn,
            api_prefix=self.scoring_api_config.STORAGE_API_PREFIX,
        )
        self.active_challenges: dict = {}
        self._baseline_commit_id: str | None = None
        self._init_active_challenges()

        bt.logging.info(
            f"Scoring API constant values: {self.config.model_dump_json(indent=2)}"
        )

    def _init_active_challenges(self):
        self.active_challenges = deepcopy(ACTIVE_CHALLENGES)

    def _get_storage_api_key(self) -> str | None:
        storage_url = str(self.config.STORAGE_API_URL).rstrip("/")
        endpoint = f"{storage_url}/get-api-key"
        data = {"validator_uid": self.uid, "validator_hotkey": self.hotkey}
        header = self.validator_request_header_fn(data)
        try:
            response = requests.post(endpoint, json=data, headers=header, timeout=60)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response.json()["api_key"]
        except Exception:
            bt.logging.warning(
                f"[INIT] Failed to fetch storage API key: {traceback.format_exc()}"
            )
            return None

    def _work_item_to_commit(self, item: dict) -> MinerChallengeCommit:
        challenge_name = item["challenge_name"]
        plain_commit = item["plain_commit"]
        docker_hub_id = _docker_hub_id_from_plain_commit(challenge_name, plain_commit)
        return MinerChallengeCommit(
            miner_uid=item["miner_uid"],
            miner_hotkey=item["hotkey_address"],
            challenge_name=challenge_name,
            docker_hub_id=docker_hub_id,
            commit_timestamp=_as_timestamp(item.get("committed_at")),
            encrypted_commit=item["cipher_commit"],
            commit=plain_commit,
        )

    def _credentials_by_uid(self, work_items: list[dict]) -> dict[str, dict]:
        credentials = self.storage.fetch_docker_credentials(
            miner_ids=[item["miner_id"] for item in work_items if item.get("miner_id")],
            hotkey_addresses=[
                item["hotkey_address"] for item in work_items if item.get("hotkey_address")
            ],
        )
        return {
            str(item["miner_uid"]): {
                "dockerhub_username": item.get("username"),
                "personal_access_token": item.get("personal_access_token"),
                "registry_url": item.get("registry_url"),
            }
            for item in credentials
        }

    def _reference_commit_from_payload(self, item: dict) -> MinerChallengeCommit | None:
        commit_files, telemetry = _extract_commit_files(item)
        if not commit_files:
            return None

        challenge_name = item["challenge_name"]
        plain_commit = item["plain_commit"]
        output = {"commit_files": commit_files}
        if telemetry:
            output["telemetry"] = telemetry

        return MinerChallengeCommit(
            miner_uid=item["miner_uid"],
            miner_hotkey=item["hotkey_address"],
            challenge_name=challenge_name,
            docker_hub_id=_docker_hub_id_from_plain_commit(challenge_name, plain_commit),
            commit_timestamp=_as_timestamp(item.get("committed_at")),
            encrypted_commit=item["cipher_commit"],
            commit=plain_commit,
            score=item.get("final_score") or item.get("evaluated_score") or 0.0,
            penalty=item.get("penalty_score") or 0.0,
            accepted=True,
            scored_timestamp=_as_timestamp(item.get("finalized_at"))
            or _as_timestamp(item.get("evaluated_at")),
            scoring_logs=[
                ScoringLog(
                    score=item.get("evaluated_score") or item.get("final_score") or 0.0,
                    miner_output=output,
                )
            ],
        )

    def _fetch_reference_commits(
        self, challenge_name: str
    ) -> tuple[list[MinerChallengeCommit], dict[str, str]]:
        challenge_info = self.active_challenges[challenge_name]
        limit = challenge_info.get("comparison_config", {}).get("max_unique_commits", 15)
        payloads = self.storage.fetch_accepted_reference_commits(
            challenge_name=challenge_name,
            limit=limit,
        )

        references = []
        target_ids_by_key = {}
        for payload in payloads:
            commit = self._reference_commit_from_payload(payload)
            if not commit:
                continue
            references.append(commit)
            target_ids_by_key[
                f"{commit.miner_uid}_{commit.encrypted_commit[:10]}"
            ] = payload["commit_id"]
        return references, target_ids_by_key

    def _create_challenge_manager(self, challenge_name: str):
        return self.active_challenges[challenge_name]["challenge_manager"](
            challenge_info=self.active_challenges[challenge_name],
            metagraph=self.metagraph,
        )

    def _get_baseline_commit_id(self) -> str | None:
        if self._baseline_commit_id is not None:
            return self._baseline_commit_id
        try:
            baseline_commit = self.storage.fetch_commit(
                self.scoring_api_config.BASELINE_COMMIT_ID
            )
        except Exception:
            bt.logging.warning(
                f"[CENTRALIZED SCORING] Failed to fetch baseline commit: {traceback.format_exc()}"
            )
            return None
        if not baseline_commit:
            bt.logging.warning(
                "[CENTRALIZED SCORING] Baseline commit "
                f"{self.scoring_api_config.BASELINE_COMMIT_ID} not found"
            )
            return None
        self._baseline_commit_id = baseline_commit["id"]
        return self._baseline_commit_id

    def _result_payload_for_commit(
        self,
        work_item: dict,
        commit: MinerChallengeCommit,
        target_ids_by_key: dict[str, str],
    ) -> dict:
        scored_at = commit.scored_timestamp or datetime.datetime.now(
            datetime.timezone.utc
        ).timestamp()
        raw_score = commit.get_higest_scoring_score()
        final_score = commit.score or 0.0
        penalty_score = commit.penalty or 0.0
        accepted = bool(commit.accepted)
        status = "ACCEPTED" if accepted else "REJECTED"
        first_log = commit.scoring_logs[0] if commit.scoring_logs else ScoringLog()

        reason = "Accepted by centralized scorer" if accepted else "Rejected by centralized scorer"
        if first_log.error:
            reason = first_log.error[:256]

        commit_outputs = []
        if first_log.miner_output:
            commit_outputs.append(
                {
                    "filename": f"{work_item['commit_id']}__miner-output.json",
                    "role": "miner-output",
                    "kind": "STRUCTURED",
                    "mime_type": "application/json",
                    "data": first_log.miner_output,
                }
            )

        validation_outputs = []
        if first_log.validation_output is not None:
            validation_outputs.append(
                {
                    "check_name": "challenge-validation",
                    "is_valid": bool(first_log.validation_output.get("is_valid", False))
                    if isinstance(first_log.validation_output, dict)
                    else False,
                    "reason": json.dumps(first_log.validation_output, default=str)[:1024],
                    "failed_at": None if accepted else _utc_iso(scored_at),
                    "meta": first_log.validation_output
                    if isinstance(first_log.validation_output, dict)
                    else {"value": first_log.validation_output},
                }
            )

        comparisons = []
        for key, logs in commit.comparison_logs.items():
            target_commit_id = (
                self._get_baseline_commit_id()
                if key.startswith("baseline_")
                else target_ids_by_key.get(key)
            )
            if not target_commit_id:
                continue
            for log in logs:
                comparisons.append(
                    {
                        "target_commit_id": target_commit_id,
                        "similarity_score": log.similarity_score or 0.0,
                        "reason": (log.reason or "")[:256],
                        "error": log.error,
                        "meta": {
                            "reference_hotkey": log.reference_hotkey,
                            "reference_similarity_score": log.reference_similarity_score,
                        },
                    }
                )

        return {
            "commit_id": work_item["commit_id"],
            "challenge_name": work_item["challenge_name"],
            "plain_commit": work_item["plain_commit"],
            "commit_result": {
                "status": status,
                "evaluated_score": raw_score,
                "penalty_score": penalty_score,
                "final_score": final_score,
                "decay_state": "NONE",
                "decayed_score": final_score,
                "reason": reason,
                "error": first_log.error,
                "evaluated_at": _utc_iso(scored_at),
                "finalized_at": _utc_iso(scored_at),
                "meta": {
                    "docker_hub_id": commit.docker_hub_id,
                    "accepted": accepted,
                },
            },
            "commit_outputs": commit_outputs,
            "commit_validation_outputs": validation_outputs,
            "commit_comparisons": comparisons,
        }

    def _rejected_result_for_work_item(self, item: dict, reason: str) -> dict:
        now = _utc_iso()
        return {
            "commit_id": item["commit_id"],
            "challenge_name": item["challenge_name"],
            "plain_commit": item["plain_commit"],
            "commit_result": {
                "status": "REJECTED",
                "evaluated_score": 0.0,
                "penalty_score": 0.0,
                "final_score": 0.0,
                "decay_state": "NONE",
                "decayed_score": 0.0,
                "reason": reason[:256],
                "error": reason,
                "evaluated_at": now,
                "finalized_at": now,
            },
            "commit_outputs": [],
            "commit_validation_outputs": [],
            "commit_comparisons": [],
        }

    def _score_challenge_batch(
        self, challenge_name: str, work_items: list[dict], credentials: dict[str, dict]
    ) -> list[dict]:
        commits = [self._work_item_to_commit(item) for item in work_items]
        commits.sort(
            key=lambda commit: commit.commit_timestamp
            if commit.commit_timestamp is not None
            else float("inf")
        )
        references, target_ids_by_key = self._fetch_reference_commits(challenge_name)
        controller = self.active_challenges[challenge_name]["controller"](
            challenge_name=challenge_name,
            miners_docker_info=credentials,
            miner_commits=commits,
            reference_comparison_commits=references,
            challenge_info=self.active_challenges[challenge_name],
            seed_inputs=[],
        )
        controller.start_challenge()

        manager = self._create_challenge_manager(challenge_name)
        manager.update_miner_scores(controller.miner_commits)

        commits_by_docker_id = {commit.docker_hub_id: commit for commit in controller.miner_commits}
        results = []
        for item in work_items:
            docker_hub_id = _docker_hub_id_from_plain_commit(
                item["challenge_name"], item["plain_commit"]
            )
            commit = commits_by_docker_id.get(docker_hub_id)
            if commit is None:
                results.append(
                    self._rejected_result_for_work_item(
                        item, "Scoring controller did not return this commit."
                    )
                )
                continue
            results.append(
                self._result_payload_for_commit(item, commit, target_ids_by_key)
            )
        return results

    def forward(self):
        date_time = datetime.datetime.now(datetime.timezone.utc)
        bt.logging.info(f"[CENTRALIZED SCORING] Forwarding for {date_time}")
        self._init_active_challenges()

        work_items = self.storage.fetch_work_items(
            challenge_name=None,
            limit=self.scoring_api_config.BATCH_LIMIT,
        )
        if not work_items:
            bt.logging.info("[CENTRALIZED SCORING] No work items found")
            return

        credentials = self._credentials_by_uid(work_items)
        grouped: dict[str, list[dict]] = {}
        results: list[dict] = []
        for item in work_items:
            challenge_name = item.get("challenge_name")
            if challenge_name not in self.active_challenges:
                results.append(
                    self._rejected_result_for_work_item(
                        item, f"Challenge '{challenge_name}' is not active in scorer."
                    )
                )
                continue
            grouped.setdefault(challenge_name, []).append(item)

        for challenge_name, challenge_items in grouped.items():
            try:
                results.extend(
                    self._score_challenge_batch(
                        challenge_name=challenge_name,
                        work_items=challenge_items,
                        credentials=credentials,
                    )
                )
            except Exception:
                error = traceback.format_exc()
                bt.logging.error(
                    f"[CENTRALIZED SCORING] Challenge {challenge_name} failed: {error}"
                )
                for item in challenge_items:
                    results.append(self._rejected_result_for_work_item(item, error))

        uploaded = self.storage.upload_results(results)
        bt.logging.success(
            f"[CENTRALIZED SCORING] Uploaded {len(uploaded)} scoring results"
        )

    def run(self):
        bt.logging.info("Starting scoring API loop.")

        while True:
            if self.forward_thread is None or not self.forward_thread.is_alive():
                self.forward_thread = threading.Thread(
                    target=self._run_forward,
                    daemon=True,
                    name="scoring_api_forward_thread",
                )
                self.forward_thread.start()
                bt.logging.info("Started new forward thread")

            try:
                self.resync_metagraph()
                bt.logging.success("Resync metagraph completed")
            except Exception:
                bt.logging.error(f"Resync metagraph error: {traceback.format_exc()}")
            except KeyboardInterrupt:
                bt.logging.success("Keyboard interrupt detected. Exiting validator.")
                exit()

            time.sleep(self.config.EPOCH_LENGTH)


if __name__ == "__main__":
    scoring_api_config = ScoringApiMainConfig()
    server_thread = threading.Thread(
        target=start_ping_server, args=(scoring_api_config.PORT,), daemon=True
    )
    server_thread.start()
    with ScoringApi() as app:
        while True:
            bt.logging.info("ScoringApi is running...")
            time.sleep(app.config.EPOCH_LENGTH // 4)
