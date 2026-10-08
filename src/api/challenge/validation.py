from dataclasses import dataclass, field

import logging
import requests
from redteam_core.config.main import constants

from ..commit_context import ScoringCommit

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ValidationOutput:
    """Validation payload plus independently persisted named checks."""

    data: dict
    checks: dict[str, dict] = field(default_factory=dict)

    @property
    def is_valid(self) -> bool:
        return bool(self.data.get("is_valid", False))


class Validation:
    """Validate a miner submission without writing to the core API."""

    def start(self, miner_commit: ScoringCommit) -> ValidationOutput:
        miner_output = miner_commit.scoring_logs[0].miner_output or {}
        script_identifier = self.challenge_info.get("script_path_identifier")
        miner_script = miner_output.get(script_identifier)
        if not miner_script:
            logger.warning(
                f"[CONTROLLER] Miner {miner_commit.miner_hotkey} "
                "has no valid script output for validation."
            )
            return ValidationOutput(
                {"is_valid": False, "reason": "Missing script output"}
            )

        try:
            internal_services_url = str(constants.INTERNAL_SERVICES.API_URL).rstrip("/")
            challenge_type = self.challenge_info.get("challenge_type", "default")
            endpoint = f"{internal_services_url}/check/challenge/{challenge_type}/"
            response = requests.post(
                endpoint,
                timeout=self.challenge_info.get("challenge_compare_timeout", 240),
                verify=False,  # nosec
                json={
                    "miner_script": miner_script,
                    "user_id": miner_commit.docker_hub_id,
                },
                headers={
                    "Content-Type": "application/json",
                    "X-API-KEY": constants.INTERNAL_SERVICES.API_KEY,
                },
            )
            data = response.json().get("data", {})
            if not isinstance(data, dict):
                raise ValueError("Validation response data must be an object")
            logger.info(f"Validation response data: {data}")
        except Exception as exc:
            logger.error(f"Error in validation request: {exc}")
            return ValidationOutput({"is_valid": False, "reason": str(exc)})

        miner_commit.scoring_logs[0].validation_output = data
        checks: dict[str, dict] = {}
        for check_name, check in data.items():
            if check_name in {"is_valid", "reason"}:
                continue
            if isinstance(check, bool):
                checks[check_name] = {"is_valid": check}
            elif isinstance(check, dict) and (
                "is_good" in check or "is_valid" in check
            ):
                checks[check_name] = check
        if not checks:
            failed_at = data.get("failed_at", "UNKNOWN")
            reason = data.get("reason", "No reason provided")
            checks[failed_at] = {"is_valid": False, "reason": reason}

        return ValidationOutput(data=data, checks=checks)


__all__ = ["Validation", "ValidationOutput"]
