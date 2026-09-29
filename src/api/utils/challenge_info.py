"""Helpers for preparing challenge configuration received from rest-core."""

import json
import os
import re
from copy import deepcopy
from typing import Any

import yaml


_ENV_RE = re.compile(
    r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?::-(?P<default>[^}]*))?\}"
)


def _expand_string(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group("name")
        default = match.group("default")

        env_value = os.getenv(name)

        if env_value:
            return env_value
        if default is not None:
            return default

        return match.group(0)

    return _ENV_RE.sub(replace, value)


def _expand_environment_variables(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _expand_environment_variables(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_environment_variables(v) for v in value]
    if isinstance(value, str):
        return _expand_string(value)
    return value


def prepare_challenge_info(challenge_info: dict) -> dict:
    """Expand environment placeholders and normalize Docker environment values."""
    prepared = _expand_environment_variables(deepcopy(challenge_info))
    run_kwargs = prepared.get("challenge_container_run_kwargs", {})
    environment = run_kwargs.get("environment")
    if not isinstance(environment, dict):
        return prepared

    for key, value in environment.items():
        structured_value = value
        if isinstance(value, str):
            try:
                structured_value = yaml.safe_load(value)
            except yaml.YAMLError:
                pass
        if isinstance(structured_value, (dict, list)):
            environment[key] = json.dumps(structured_value, separators=(",", ":"))

    return prepared
