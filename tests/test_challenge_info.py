from src.api.utils.challenge_info import prepare_challenge_info


def test_prepare_challenge_info_expands_env_and_formats_yaml(monkeypatch):
    monkeypatch.setenv("CHALLENGE_TOKEN", "loaded-secret")
    monkeypatch.setenv("EMPTY_CHALLENGE_TOKEN", "")
    challenge_info = {
        "headers": {
            "token": "Bearer ${CHALLENGE_TOKEN}",
            "missing": "${MISSING_CHALLENGE_TOKEN}",
            "default": "${MISSING_CHALLENGE_TOKEN:-fallback}",
            "empty": "${EMPTY_CHALLENGE_TOKEN:-empty-fallback}",
        },
        "challenge_container_run_kwargs": {
            "environment": {"TASKS": "- one\n- ${CHALLENGE_TOKEN}"}
        },
    }

    prepared = prepare_challenge_info(challenge_info)

    assert prepared["headers"] == {
        "token": "Bearer loaded-secret",
        "missing": "${MISSING_CHALLENGE_TOKEN}",
        "default": "fallback",
        "empty": "empty-fallback",
    }
    assert prepared["challenge_container_run_kwargs"]["environment"]["TASKS"] == (
        '["one","loaded-secret"]'
    )
    assert challenge_info["headers"]["token"] == "Bearer ${CHALLENGE_TOKEN}"
