from unittest.mock import Mock

import pytest

from src.api.core_api import CoreApiClient
from src.api.seen_commits import SeenCommits


def test_seen_commits_persists_by_challenge(tmp_path):
    seen = SeenCommits(str(tmp_path))
    seen.add("challenge-a", "cipher-a")
    seen.add("challenge-a", "cipher-b")
    seen.save()

    restored = SeenCommits(str(tmp_path))

    assert restored.contains("challenge-a", "cipher-a")
    assert restored.contains("challenge-a", "cipher-b")
    assert not restored.contains("challenge-b", "cipher-a")


def test_core_client_requires_url_and_api_key():
    with pytest.raises(ValueError, match="CORE_API_URL"):
        CoreApiClient("", "key")
    with pytest.raises(ValueError, match="CORE_API_KEY"):
        CoreApiClient("http://core", "")


def test_list_queued_commits_uses_server_side_state_filter():
    client = CoreApiClient("http://core", "secret", timeout=1)
    first = Mock()
    first.raise_for_status.return_value = None
    first.json.return_value = {"data": [{"id": "one"}, {"id": "two"}]}
    second = Mock()
    second.raise_for_status.return_value = None
    second.json.return_value = {"data": []}
    client.session.request = Mock(side_effect=[first, second])

    commits = list(client.list_commits(state="QUEUED"))

    assert commits == [{"id": "one"}, {"id": "two"}]
    first_call = client.session.request.call_args_list[0]
    assert first_call.args[:2] == ("GET", "http://core/commits/")
    assert first_call.kwargs["params"]["state"] == "QUEUED"
    assert client.session.headers["X-API-KEY"] == "secret"


def test_registered_neuron_query_includes_uid_hotkey_and_registration_flag():
    client = CoreApiClient("http://core", "secret", timeout=1)
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"data": []}
    client.session.request = Mock(return_value=response)

    assert list(client.list_neurons(uid=42, hotkey_address="5hotkey")) == []

    params = client.session.request.call_args.kwargs["params"]
    assert params["uid"] == 42
    assert params["hotkey_address"] == "5hotkey"
    assert params["only_registered"] is True
