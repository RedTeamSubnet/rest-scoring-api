"""Small, synchronous client for the rest-core API used by the scorer."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import requests


class CoreApiClient:
    """Read-only rest-core client.

    The scorer deliberately owns no core state transitions yet.  Keeping this
    client read-only makes that boundary explicit until result write-back is
    introduced.
    """

    def __init__(self, base_url: str, api_key: str, *, timeout: float = 30.0):
        if not base_url:
            raise ValueError("RT_SCORING_API_CORE_API_URL must be configured")
        if not api_key:
            raise ValueError("RT_SCORING_API_CORE_API_KEY must be configured")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"X-API-KEY": api_key})

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = self.session.request(
            method,
            f"{self.base_url}{path}",
            timeout=self.timeout,
            **kwargs,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError(f"Unexpected core response for {path}")
        return payload

    @staticmethod
    def _items(payload: dict[str, Any]) -> list[dict[str, Any]]:
        data = payload.get("data", payload)
        return [item for item in data if isinstance(item, dict)]

    def _paginate(
        self, path: str, params: dict[str, Any] | None = None
    ) -> Iterator[dict[str, Any]]:
        query = dict(params or {})
        limit = int(query.setdefault("limit", 100))
        skip = int(query.setdefault("skip", 0))
        while True:
            query["skip"] = skip
            items = self._items(self._request("GET", path, params=query))
            yield from items
            if len(items) < limit:
                return
            skip += len(items)

    def list_commits(
        self,
        *,
        state: str | None = None,
        challenge_id: str | None = None,
        expands: list[str] | None = None,
    ) -> Iterator[dict[str, Any]]:
        params: dict[str, Any] = {}
        if state:
            params["state"] = state
        if challenge_id:
            params["challenge_id"] = challenge_id
        if expands:
            params["expands"] = expands
        return self._paginate("/commits/", params)

    def list_challenges(self) -> Iterator[dict[str, Any]]:
        return self._paginate("/challenges/")

    def list_neurons(
        self, *, uid: int, hotkey_address: str, only_registered: bool = True
    ) -> Iterator[dict[str, Any]]:
        return self._paginate(
            "/neurons/",
            {
                "uid": uid,
                "hotkey_address": hotkey_address,
                "only_registered": only_registered,
            },
        )

    def get_neuron(self, neuron_id: str) -> dict[str, Any]:
        payload = self._request("GET", f"/neurons/{neuron_id}")
        data = payload.get("data", payload)
        if not isinstance(data, dict):
            raise ValueError(f"Unexpected neuron response for {neuron_id}")
        return data

    def list_commit_files(self, commit_id: str) -> Iterator[dict[str, Any]]:
        return self._paginate(
            "/commit-files/", {"commit_id": commit_id, "include_data": True}
        )

    def list_miner_docker_registries(self, neuron_id: str) -> Iterator[dict[str, Any]]:
        return self._paginate("/miner-docker-registries/", {"neuron_id": neuron_id})

    def decrypt_miner_docker_registry(self, registry_id: str) -> str:
        payload = self._request(
            "POST", f"/miner-docker-registries/{registry_id}/decrypt"
        )
        data = payload.get("data", payload)
        if not isinstance(data, dict) or not isinstance(data.get("token"), str):
            raise ValueError(
                f"Core did not return a Docker registry token for {registry_id}"
            )
        return data["token"]
