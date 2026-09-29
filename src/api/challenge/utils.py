import logging
import requests
from redteam_core.config.main import constants

logger = logging.getLogger(__name__)


class ChallengeUtils:
    """Shared protocol, payload, and challenge HTTP helpers."""

    def _check_protocol(self, is_challenger: bool = True) -> tuple[str, bool | None]:
        """Check the protocol scheme and SSL/TLS verification for the challenger or miner.

        Args:
            is_challenger (bool, optional): Flag to check the protocol for the challenger or miner. Defaults to True.

        Returns:
            Tuple[str, Union[bool, None]]: A tuple containing the protocol scheme and SSL/TLS verification.
        """

        _protocol = "http"
        _ssl_verify: bool | None = None

        if "protocols" in self.challenge_info:
            _protocols = self.challenge_info["protocols"]

            if is_challenger:
                if "challenger" in _protocols:
                    _protocol = _protocols["challenger"]

                if "challenger_ssl_verify" in _protocols:
                    _ssl_verify = _protocols["challenger_ssl_verify"]

            if not is_challenger:
                if "miner" in _protocols:
                    _protocol = _protocols["miner"]

                if "miner_ssl_verify" in _protocols:
                    _ssl_verify = _protocols["miner_ssl_verify"]

        return _protocol, _ssl_verify

    def _exclude_output_keys(self, miner_output: dict, reference_output: dict):
        """Remove large challenge-specific fields from stored comparison outputs."""
        default_keys = ["commit_files", "scoring_results"]
        if self.challenge_info.get("challenge_type") == "ada":
            default_keys.append("telemetry")
        keys = self.challenge_info.get("excluded_output_keys", default_keys)
        for key in keys:
            miner_output[key] = None
            reference_output[key] = None

    def _get_telemetry_from_challenge(self, path: str = "/telemetry") -> dict:
        return self._get_challenge_data(path)

    def _get_challenge_data(self, path: str, headers: dict | None = None) -> dict:
        protocol, ssl_verify = self._check_protocol(is_challenger=True)
        url = f"{protocol}://localhost:{constants.CHALLENGE_DOCKER_PORT}/{path.lstrip('/')}"
        try:
            response = requests.get(
                url, timeout=5, verify=ssl_verify, headers=headers or {}
            )
            response.raise_for_status()
            return response.json() if response.content else {}
        except Exception as exc:
            logger.error(f"[CONTROLLER] Unable to fetch {path}: {exc}")
            return {}
