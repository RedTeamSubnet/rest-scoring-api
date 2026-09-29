from unittest.mock import Mock
import importlib
import sys
import types
from types import SimpleNamespace

import requests

import pytest

from src.api.core_api import CoreApiClient
from src.api.commit_context import CommitContext, ScoringCommit
from src.api.config import ScoringApiMainConfig
from src.api.result_publisher import ResultPublisher
from src.api.seen_commits import SeenCommits
from src.api.utils.helpers import get_docker_hub_id


def test_scoring_api_port_comes_from_pydantic_config(monkeypatch, tmp_path):
    monkeypatch.setenv("RT_SCORING_API_PORT", "9123")
    config = ScoringApiMainConfig(
        CORE_API_KEY="secret",
        CACHE_DIR=str(tmp_path),
    )

    assert config.PORT == 9123


def test_get_docker_hub_id_parses_revealed_commit():
    assert get_docker_hub_id("key---registry/image:tag") == "registry/image:tag"
    assert get_docker_hub_id("invalid") is None


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


def test_result_publisher_uses_each_commits_core_ids():
    client = Mock()
    client.create.side_effect = [
        {"id": "result-a"},
        {"id": "result-b"},
        {"id": "validation-a"},
        {"id": "validation-b"},
    ]
    publisher = ResultPublisher(client)
    first = CommitContext(
        "commit-a",
        publisher.create_result("commit-a", "miner-a"),
        "challenge",
        "miner-a",
    )
    second = CommitContext(
        "commit-b",
        publisher.create_result("commit-b", "miner-b"),
        "challenge",
        "miner-b",
    )

    publisher.publish_validation(first, {"is_valid": True})
    publisher.publish_validation(second, {"is_valid": False, "reason": "Invalid"})

    assert client.create.call_args_list[0].args == (
        "/commit-results/",
        {"commit_id": "commit-a", "miner_id": "miner-a", "status": "PENDING"},
    )
    assert client.create.call_args_list[2].args[1]["commit_result_id"] == "result-a"
    assert client.create.call_args_list[3].args[1]["commit_result_id"] == "result-b"
    assert client.create.call_args_list[3].args[1]["commit_id"] == "commit-b"


def test_core_create_requires_resource_id():
    client = CoreApiClient("http://core", "secret")
    response = Mock()
    response.json.return_value = {"data": {"status": "PENDING"}}
    client.session.request = Mock(return_value=response)

    with pytest.raises(ValueError, match="created resource ID"):
        client.create("/commit-results/", {"commit_id": "commit-a"})


def test_core_updates_result_and_commit_with_put():
    client = CoreApiClient("http://core", "secret")
    response = Mock()
    response.json.return_value = {"data": {"id": "result-a"}}
    client.session.request = Mock(return_value=response)

    assert client.update("/commit-results/result-a", {"status": "RUNNING"}) == {
        "id": "result-a"
    }
    call = client.session.request.call_args
    assert call.args == ("PUT", "http://core/commit-results/result-a")
    assert call.kwargs["json"] == {"status": "RUNNING"}


def test_new_result_is_started_and_finalized_with_scores():
    client = Mock()
    client.list_commit_results.return_value = iter([])
    client.create.return_value = {"id": "result-a"}
    publisher = ResultPublisher(client)
    result, created = publisher.get_or_create_result("commit-a", "miner-a")
    assert created is True
    context = CommitContext("commit-a", result["id"], "challenge-a", "miner-a")

    publisher.start_result(result["id"])
    publisher.finish_result(
        context, accepted=True, evaluated_score=0.9, score=0.72, penalty=0.2
    )

    client.create.assert_called_once()
    started_path, started = client.update.call_args_list[0].args
    assert started_path == "/commit-results/result-a"
    assert started["status"] == "RUNNING"
    assert "meta" not in started
    assert started["finalized_at"] is None
    final = client.update.call_args_list[1].args[1]
    assert final["status"] == "ACCEPTED"
    assert final["evaluated_score"] == 0.9
    assert final["final_score"] == 0.72
    assert final["penalty_score"] == 0.2
    assert final["evaluated_at"] == final["finalized_at"]


def test_result_creation_race_reuses_existing_row():
    client = Mock()
    client.list_commit_results.side_effect = [
        iter([]),
        iter([{"id": "result-a", "miner_id": "miner-a", "status": "PENDING"}]),
    ]
    client.create.side_effect = requests.HTTPError("duplicate")

    result, created = ResultPublisher(client).get_or_create_result(
        "commit-a", "miner-a"
    )
    assert created is False

    assert result["id"] == "result-a"


def test_validation_retry_updates_existing_record():
    client = Mock()
    client.create.side_effect = requests.HTTPError("duplicate")
    client.list_commit_validations.return_value = iter([{"id": "validation-a"}])
    client.patch.return_value = {"id": "validation-a"}
    context = CommitContext("commit-a", "result-a", "challenge-a", "miner-a")

    validation_id = ResultPublisher(client).publish_validation(
        context, {"is_valid": False, "reason": "Invalid"}
    )

    assert validation_id == "validation-a"
    assert client.patch.call_args.args[0] == "/commit-validation-outputs/validation-a"
    assert client.patch.call_args.args[1]["is_valid"] is False


def test_file_and_output_publisher_store_content_and_retry_by_identity():
    client = Mock()
    client.create.return_value = {"id": "created"}
    publisher = ResultPublisher(client)
    context = CommitContext("commit-a", "result-a", "challenge-a", "miner-a")

    publisher.publish_file(context, {"file_name": "main.py", "content": "print(1)"}, 0)
    first = client.create.call_args.args[1]
    publisher.publish_file(context, {"file_name": "bot.py", "content": "print(2)"}, 1)
    second = client.create.call_args.args[1]
    assert first["role"] == "submission-0"
    assert second["role"] == "submission-1"
    assert first["checksum"] != second["checksum"]
    assert first["size_bytes"] == len("print(1)".encode())

    publisher.publish_output(context, {"final_score": 0.8})
    output = client.create.call_args.args[1]
    assert output["role"] == "result"
    assert output["data"] == '{"final_score": 0.8}'

    client.create.side_effect = requests.HTTPError("duplicate")
    client.list_commit_files.return_value = iter(
        [{"id": "file-a", "role": "submission-0"}]
    )
    client.update.return_value = {"id": "file-a"}
    assert (
        publisher.publish_file(
            context, {"file_name": "main.py", "content": "print(1)"}, 0
        )
        == "file-a"
    )
    client.update.assert_called_with("/commit-files/file-a", first)


def test_comparison_retry_updates_same_source_target_pair():
    client = Mock()
    client.create.side_effect = requests.HTTPError("duplicate")
    client.list_commit_comparisons.return_value = iter([{"id": "comparison-a"}])
    client.update.return_value = {"id": "comparison-a"}
    context = CommitContext("commit-a", "result-a", "challenge-a", "miner-a")

    result_id = ResultPublisher(client).publish_comparison(
        context, "reference-a", {"similarity_score": 0.4, "reason": "Different"}
    )

    assert result_id == "comparison-a"
    path, payload = client.update.call_args.args
    assert path == "/commit-comparisons/comparison-a"
    assert payload["source_commit_id"] == "commit-a"
    assert payload["target_commit_id"] == "reference-a"
    assert payload["similarity_score"] == 0.4


def test_validation_check_names_use_each_payload_field():
    client = Mock()
    client.create.return_value = {"id": "validation-a"}
    publisher = ResultPublisher(client)
    context = CommitContext("commit-a", "result-a", "challenge-a", "miner-a")

    publisher.publish_validation(
        context,
        {"is_good": True, "reason": "Clean script"},
        check_name="prompt_injection",
    )

    payload = client.create.call_args.args[1]
    assert payload["check_name"] == "PROMPT-INJECTION"
    assert payload["is_valid"] is True
    assert payload["reason"] == "Clean script"


def _scoring_api_for_forward(monkeypatch, result):
    challenge_pool = types.ModuleType("redteam_core.challenge_pool")
    challenge_pool.ACTIVE_CHALLENGES = {}
    monkeypatch.setitem(sys.modules, "redteam_core.challenge_pool", challenge_pool)
    scoring_module = importlib.import_module("src.api.__main__")
    api = scoring_module.ScoringApi.__new__(scoring_module.ScoringApi)
    api._init_active_challenges = Mock()
    api._active_core_challenges = Mock(return_value={"challenge-a": "challenge"})
    api._accepted_core_commits = Mock(return_value=[])
    api._docker_info_for = Mock(return_value={})
    api.core_api = Mock()
    api.core_api.list_commits.return_value = [
        {
            "id": "commit-a",
            "challenge_id": "challenge-a",
            "miner_id": "miner-a",
            "cipher_commit": "cipher-a",
            "plain_commit": "key---docker-a",
        }
    ]
    api._registered_miner = Mock(return_value=(1, "hotkey-a"))
    api.result_publisher = Mock()
    api.result_publisher.get_or_create_result.return_value = result, True
    api.metagraph = Mock()
    api.active_challenges = {"challenge": {}}
    return api, scoring_module, None


def _successful_controller(**kwargs):
    controller = Mock(failed=False)

    def score_commit():
        commit = kwargs["work_item"].commit
        evaluated_score = 0.8 if commit.miner_uid == 1 else 0.6
        commit.scoring_logs = [SimpleNamespace(score=evaluated_score)]
        commit.score = 0.7 if commit.miner_uid == 1 else 0.6
        commit.penalty = 0.1 if commit.miner_uid == 1 else 0.0
        commit.accepted = True

    controller.start_challenge.side_effect = score_commit
    return controller


def test_forward_finalizes_result_and_commit(monkeypatch):
    api, scoring_module, commit = _scoring_api_for_forward(
        monkeypatch,
        {"id": "result-a", "miner_id": "miner-a", "status": "PENDING"},
    )
    monkeypatch.setattr(
        scoring_module, "Controller", Mock(side_effect=_successful_controller)
    )

    api.forward()

    work_item = scoring_module.Controller.call_args.kwargs["work_item"]
    assert isinstance(work_item.commit, scoring_module.ScoringCommit)
    assert work_item.commit.docker_hub_id == "docker-a"
    api.result_publisher.start_result.assert_called_once_with("result-a")
    assert api.result_publisher.finish_result.call_args.kwargs == {
        "accepted": True,
        "evaluated_score": 0.8,
        "score": 0.7,
        "penalty": 0.1,
    }
    api.core_api.update.assert_called_once_with("/commits/commit-a", {"state": "DONE"})


def test_result_publish_failure_marks_result_and_commit_failed(monkeypatch):
    api, scoring_module, _ = _scoring_api_for_forward(
        monkeypatch,
        {"id": "result-a", "miner_id": "miner-a", "status": "PENDING"},
    )
    monkeypatch.setattr(
        scoring_module, "Controller", Mock(side_effect=_successful_controller)
    )
    api.result_publisher.finish_result.side_effect = RuntimeError("core unavailable")

    api.forward()

    api.result_publisher.fail_result.assert_called_once()
    api.core_api.update.assert_called_once_with(
        "/commits/commit-a", {"state": "FAILED"}
    )


def test_scoring_failure_marks_result_and_commit_failed(monkeypatch):
    api, scoring_module, _ = _scoring_api_for_forward(
        monkeypatch,
        {"id": "result-a", "miner_id": "miner-a", "status": "PENDING"},
    )
    monkeypatch.setattr(
        scoring_module, "Controller", Mock(return_value=Mock(failed=True))
    )

    api.forward()

    api.result_publisher.finish_result.assert_not_called()
    api.result_publisher.fail_result.assert_called_once()
    api.core_api.update.assert_called_once_with(
        "/commits/commit-a", {"state": "FAILED"}
    )


def test_forward_reconciles_finished_result_without_rescoring(monkeypatch):
    api, scoring_module, _ = _scoring_api_for_forward(
        monkeypatch,
        {"id": "result-a", "miner_id": "miner-a", "status": "ACCEPTED"},
    )
    controller_class = Mock()
    api.result_publisher.get_or_create_result.return_value = (
        {"id": "result-a", "miner_id": "miner-a", "status": "ACCEPTED"},
        False,
    )
    monkeypatch.setattr(scoring_module, "Controller", controller_class)

    api.forward()

    controller_class.assert_not_called()
    api.result_publisher.start_result.assert_not_called()
    api.core_api.update.assert_called_once_with("/commits/commit-a", {"state": "DONE"})


def test_forward_reconciles_expanded_result_before_miner_lookup(monkeypatch):
    api, scoring_module, _ = _scoring_api_for_forward(
        monkeypatch,
        {"id": "result-a", "miner_id": "miner-a", "status": "ACCEPTED"},
    )
    api.core_api.list_commits.return_value[0]["commit_result"] = {
        "id": "result-a",
        "status": "ACCEPTED",
    }
    monkeypatch.setattr(scoring_module, "Controller", Mock())

    api.forward()

    api._registered_miner.assert_not_called()
    api.core_api.update.assert_called_once_with("/commits/commit-a", {"state": "DONE"})


def test_forward_skips_existing_failed_result(monkeypatch):
    api, scoring_module, _ = _scoring_api_for_forward(
        monkeypatch,
        {
            "id": "result-a",
            "miner_id": "miner-a",
            "status": "FAILED",
        },
    )
    controller_class = Mock()
    api.result_publisher.get_or_create_result.return_value = (
        {"id": "result-a", "miner_id": "miner-a", "status": "FAILED"},
        False,
    )
    monkeypatch.setattr(scoring_module, "Controller", controller_class)

    api.forward()

    controller_class.assert_not_called()
    api.result_publisher.start_result.assert_not_called()
    api.core_api.update.assert_called_once_with(
        "/commits/commit-a", {"state": "FAILED"}
    )


def test_forward_skips_existing_running_result(monkeypatch):
    api, scoring_module, _ = _scoring_api_for_forward(
        monkeypatch,
        {
            "id": "result-a",
            "miner_id": "miner-a",
            "status": "RUNNING",
        },
    )
    controller_class = Mock()
    api.result_publisher.get_or_create_result.return_value = (
        {"id": "result-a", "miner_id": "miner-a", "status": "RUNNING"},
        False,
    )
    monkeypatch.setattr(scoring_module, "Controller", controller_class)

    api.forward()

    controller_class.assert_not_called()
    api.core_api.update.assert_not_called()
    api.result_publisher.fail_result.assert_not_called()


def test_forward_runs_one_commit_per_controller(monkeypatch):
    api, scoring_module, _ = _scoring_api_for_forward(
        monkeypatch,
        {"id": "result-a", "miner_id": "miner-a", "status": "PENDING"},
    )
    api.core_api.list_commits.return_value.append(
        {
            "id": "commit-b",
            "challenge_id": "challenge-a",
            "miner_id": "miner-b",
            "cipher_commit": "cipher-b",
            "plain_commit": "key---docker-b",
        }
    )
    api._registered_miner.side_effect = [(1, "hotkey-a"), (2, "hotkey-b")]
    api.result_publisher.get_or_create_result.side_effect = [
        ({"id": "result-a", "miner_id": "miner-a", "status": "PENDING"}, True),
        ({"id": "result-b", "miner_id": "miner-b", "status": "PENDING"}, True),
    ]
    controller_class = Mock(side_effect=[Mock(failed=False), Mock(failed=False)])
    monkeypatch.setattr(scoring_module, "Controller", controller_class)

    api.forward()

    assert controller_class.call_count == 2
    assert [
        call.kwargs["work_item"].commit.miner_uid
        for call in controller_class.call_args_list
    ] == [1, 2]


def test_accepted_references_load_files_and_identifiers(monkeypatch):
    api, scoring_module, _ = _scoring_api_for_forward(
        monkeypatch,
        {"id": "result-a", "miner_id": "miner-a", "status": "PENDING"},
    )
    api.core_api.list_commits.return_value = [
        {
            "id": "reference-a",
            "miner_id": "miner-a",
            "commit_result": {
                "status": "ACCEPTED",
                "evaluated_score": 0.8,
                "final_score": 0.7,
                "penalty_score": 0.1,
            },
        }
    ]
    api.core_api.list_commit_files.return_value = [
        {"role": "submission-0", "orig_filename": "main.py", "data": "print(1)"},
        {"role": "submission-1", "orig_filename": "bot.py", "data": "print(2)"},
        {"role": "artifact", "orig_filename": "empty.txt", "data": ""},
    ]

    api.core_api.get_neuron.return_value = {
        "uid": 1,
        "hotkey_address": "hotkey-a",
    }
    references = scoring_module.ScoringApi._accepted_core_commits(api, "challenge-a")

    assert len(references) == 1
    assert references[0].commit_id == "reference-a"
    assert references[0].miner_uid == 1
    assert references[0].score == 0.8
    assert references[0].files == [
        {"file_name": "main.py", "content": "print(1)"},
        {"file_name": "bot.py", "content": "print(2)"},
        {"file_name": "empty.txt", "content": ""},
    ]


def _controller_for_artifacts(monkeypatch):
    _scoring_api_for_forward(
        monkeypatch,
        {"id": "result-a", "miner_id": "miner-a", "status": "PENDING"},
    )
    controller_module = importlib.import_module("src.api.challenge.main")
    controller = controller_module.Controller.__new__(controller_module.Controller)
    controller.context = CommitContext("commit-a", "result-a", "challenge-a", "miner-a")
    controller.result_publisher = Mock()
    controller.challenge_info = {
        "script_path_identifier": "commit_files",
        "challenge_type": "ada",
        "name": "challenge",
        "telemetry_path": None,
    }
    controller.comparison_min_acceptable_score = 0.6
    return controller, controller_module


def test_controller_main_stores_validation_and_stops_invalid_submission(monkeypatch):
    main_module = importlib.import_module("src.api.challenge.main")
    validation_module = importlib.import_module("src.api.challenge.validation")
    controller = main_module.Controller.__new__(main_module.Controller)
    controller.challenge_name = "challenge"
    controller.context = CommitContext("commit-a", "result-a", "challenge-a", "miner-a")
    controller.docker_client = Mock()
    controller.failed = False
    controller.result_publisher = Mock()
    controller.miner_commit = ScoringCommit(
        miner_uid=1,
        miner_hotkey="hotkey",
        challenge_name="challenge",
        docker_hub_id="image:latest",
        encrypted_commit="cipher",
    )
    controller._setup_challenge = Mock()
    controller._setup_miner_container = Mock()

    scoring_start = Mock()
    validation_start = Mock(
        return_value=validation_module.ValidationOutput(
            {"is_valid": False, "reason": "Invalid"},
            {"format": {"is_valid": False}},
        )
    )
    reject_invalid = Mock()
    comparison_start = Mock()
    score_new_inputs = Mock()
    same_score_comparison = Mock()
    finalize = Mock()
    monkeypatch.setattr(main_module.Scoring, "start", scoring_start)
    monkeypatch.setattr(main_module.Validation, "start", validation_start)
    monkeypatch.setattr(
        main_module.Comparison, "reject_invalid_submission", reject_invalid
    )
    monkeypatch.setattr(main_module.Comparison, "start", comparison_start)
    monkeypatch.setattr(main_module.Scoring, "score_new_inputs", score_new_inputs)
    monkeypatch.setattr(
        main_module.Comparison, "same_score_comparison", same_score_comparison
    )
    monkeypatch.setattr(main_module.FinalOutput, "start", finalize)
    monkeypatch.setattr(main_module.docker_utils, "remove_container_by_port", Mock())
    monkeypatch.setattr(main_module.docker_utils, "clean_docker_resources", Mock())
    monkeypatch.setattr(main_module.docker_utils, "remove_container", Mock())

    controller.start_challenge()

    controller.result_publisher.publish_validation.assert_any_call(
        controller.context, {"is_valid": False, "reason": "Invalid"}
    )
    controller.result_publisher.publish_validation.assert_any_call(
        controller.context, {"is_valid": False}, check_name="format"
    )
    reject_invalid.assert_called_once_with(controller.miner_commit)
    comparison_start.assert_not_called()
    score_new_inputs.assert_not_called()
    same_score_comparison.assert_not_called()
    finalize.assert_called_once_with(controller, controller.miner_commit)


def test_generate_scoring_logs_publishes_every_commit_file(monkeypatch):
    controller, _ = _controller_for_artifacts(monkeypatch)
    files = [
        {"file_name": "main.py", "content": "print(1)"},
        {"file_name": "Dockerfile", "content": "FROM python:3"},
    ]
    controller._submit_challenge_to_miner = Mock(
        return_value=({"commit_files": files}, "")
    )
    commit = Mock(miner_hotkey="hotkey", scoring_logs=[])

    controller._generate_scoring_logs(commit)

    assert controller.result_publisher.publish_file.call_count == 2
    assert controller.result_publisher.publish_file.call_args_list[0].args == (
        controller.context,
        files[0],
        0,
    )
    assert controller.result_publisher.publish_file.call_args_list[1].args == (
        controller.context,
        files[1],
        1,
    )
    assert commit.scoring_logs[0].miner_output["commit_files"] == files


def test_validation_publishes_all_named_checks(monkeypatch):
    controller, _ = _controller_for_artifacts(monkeypatch)
    validation_module = importlib.import_module("src.api.challenge.validation")
    data = {
        "is_valid": True,
        "format": True,
        "prompt_injection": {"is_good": True, "reason": "Clean script"},
        "obfuscation": {"is_good": True, "reason": "Readable"},
        "integrity": {"is_good": True, "reason": "Allowed"},
    }
    response = Mock()
    response.json.return_value = {"data": data}
    monkeypatch.setattr(validation_module.requests, "post", Mock(return_value=response))
    log = SimpleNamespace(
        miner_output={"commit_files": [{"file_name": "main.py", "content": "x"}]},
        validation_output=None,
    )
    commit = Mock(scoring_logs=[log], docker_hub_id="docker", miner_hotkey="hotkey")

    output = validation_module.Validation.start(controller, commit)
    controller._store_validation_output(output)

    assert output.is_valid is True

    calls = controller.result_publisher.publish_validation.call_args_list
    assert [call.kwargs.get("check_name", "SUBMISSION") for call in calls] == [
        "SUBMISSION",
        "format",
        "prompt_injection",
        "obfuscation",
        "integrity",
    ]
    assert calls[2].args[1]["reason"] == "Clean script"
    assert log.validation_output == data


def test_scoring_results_and_comparison_are_published(monkeypatch):
    controller, _ = _controller_for_artifacts(monkeypatch)
    comparison_module = importlib.import_module("src.api.challenge.comparison")
    controller._score_challenge = Mock(return_value=0.8)
    controller._get_results_from_challenge = Mock(return_value={"score": 0.8})
    log = SimpleNamespace(miner_output={"commit_files": []}, score=None, error=None)
    commit = Mock(scoring_logs=[log], miner_hotkey="hotkey")
    commit.get_higest_comparison_score.return_value = 0.2

    controller._score_miner_with_new_inputs(commit)

    controller.result_publisher.publish_output.assert_called_once_with(
        controller.context, {"score": 0.8}
    )
    assert log.miner_output["scoring_results"] == {"score": 0.8}

    response = Mock(status_code=200)
    response.json.return_value = {
        "data": {"similarity_score": 0.4, "reason": "Different"}
    }
    monkeypatch.setattr(comparison_module.requests, "post", Mock(return_value=response))
    reference_files = [{"file_name": "reference.py", "content": "print(2)"}]
    result = controller._compare_outputs(
        {"commit_files": [{"file_name": "miner.py", "content": "print(1)"}]},
        reference_files,
        target_commit_id="reference-a",
    )
    assert comparison_module.requests.post.call_args.kwargs["json"][
        "reference_script"
    ] == (reference_files)
    assert result["similarity_score"] == 0.4
    controller.result_publisher.publish_comparison.assert_called_once_with(
        controller.context, "reference-a", result
    )


def test_high_similarity_gate_publishes_reference_comparison(monkeypatch):
    controller, controller_module = _controller_for_artifacts(monkeypatch)
    comparison_module = importlib.import_module("src.api.challenge.comparison")
    controller.challenge_info["comparison_config"] = {}
    reference = controller_module.ReferenceCommit(
        commit_id="reference-a",
        miner_uid=2,
        miner_hotkey="hotkey-b",
        files=[{"file_name": "main.py", "content": "print(1)"}],
        score=0.8,
        penalty=0.0,
    )
    controller.reference_comparison_commits = [reference]
    miner = Mock(miner_uid=1, miner_hotkey="hotkey", docker_hub_id="docker")
    miner.scoring_logs = [SimpleNamespace(miner_output={"commit_files": []})]
    response = Mock()
    response.json.return_value = {"data": {"similarity_score": 0.9}}
    monkeypatch.setattr(comparison_module.requests, "post", Mock(return_value=response))

    assert controller._check_comparison_score(miner) == 0.9
    assert comparison_module.requests.post.call_args.kwargs["json"] == {
        "challenge_type": "ada",
        "miner_script": [],
        "reference_script": reference.files,
        "user_id": "docker",
    }

    controller.result_publisher.publish_comparison.assert_called_once_with(
        controller.context,
        "reference-a",
        {"similarity_score": 0.9, "reason": "Initial comparison"},
    )


def test_same_score_comparison_sends_reference_files(monkeypatch):
    controller, _ = _controller_for_artifacts(monkeypatch)
    comparison_module = importlib.import_module("src.api.challenge.comparison")
    reference_files = [{"file_name": "reference.py", "content": "print(2)"}]
    response = Mock(status_code=200)
    response.json.return_value = {"data": {"similarity_score": 0.4}}
    monkeypatch.setattr(comparison_module.requests, "post", Mock(return_value=response))

    result = controller._compare_same_score_outputs(
        {"commit_files": [{"file_name": "miner.py", "content": "print(1)"}]},
        reference_files,
        reference_score=0.8,
        user_id="docker",
    )

    assert result == {"similarity_score": 0.4}
    payload = comparison_module.requests.post.call_args.kwargs["json"]
    assert payload["reference_script"] == reference_files
    assert payload["reference_metadata"] == {"score": 0.8, "telemetry": {}}


def test_publisher_keeps_context_ids_when_publishing_records():
    client = Mock()
    client.create.return_value = {"id": "created"}
    publisher = ResultPublisher(client)
    context = CommitContext("commit-a", "result-a", "challenge-a", "miner-a")

    publisher.publish_comparison(
        context, "reference-a", {"similarity_score": 0.4, "source_commit_id": "wrong"}
    )
    path, payload = client.create.call_args.args
    assert path == "/commit-comparisons/"
    assert payload["source_commit_id"] == "commit-a"
    assert payload["target_commit_id"] == "reference-a"
    assert payload["commit_result_id"] == "result-a"

    publisher.publish_file(
        context, {"file_name": "submission.py", "content": "print(1)"}, 0
    )
    file_payload = client.create.call_args.args[1]
    assert file_payload["commit_id"] == "commit-a"
    assert file_payload["orig_filename"] == "submission.py"
    assert file_payload["data"] == "print(1)"
    publisher.publish_output(context, {"score": 0.9})
    output_payload = client.create.call_args.args[1]
    assert output_payload["commit_id"] == "commit-a"
    assert output_payload["data"] == '{"score": 0.9}'


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


def test_list_challenges_requests_expanded_config():
    client = CoreApiClient("http://core", "secret", timeout=1)
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"data": []}
    client.session.request = Mock(return_value=response)

    assert list(client.list_challenges(expands=["config"])) == []

    client.session.request.assert_called_once_with(
        "GET",
        "http://core/challenges/",
        timeout=1,
        params={"expands": ["config"], "limit": 100, "skip": 0},
    )


def test_scoring_api_loads_active_challenge_configs_from_core(monkeypatch):
    scoring_module = importlib.import_module("src.api.__main__")
    monkeypatch.setenv("CHALLENGE_TOKEN", "loaded-secret")
    monkeypatch.setenv("EMPTY_CHALLENGE_TOKEN", "")

    spec = {
        "scoring_headers": {
            "X-API-KEY": "Bearer ${CHALLENGE_TOKEN}",
            "literal": "$CHALLENGE_TOKEN",
            "missing": "${MISSING_CHALLENGE_TOKEN}",
            "default": "${MISSING_CHALLENGE_TOKEN:-fallback}",
            "empty": "${EMPTY_CHALLENGE_TOKEN:-empty-fallback}",
        },
        "nested": ["${CHALLENGE_TOKEN}", 1],
        "challenge_container_run_kwargs": {
            "environment": {"TASKS": "- one\n- ${CHALLENGE_TOKEN}"}
        },
    }
    api = scoring_module.ScoringApi.__new__(scoring_module.ScoringApi)
    api.core_api = Mock()
    api.core_api.list_challenges.return_value = [
        {
            "id": "challenge-id",
            "name": "challenge-name",
            "end_at": None,
            "config": {"id": "config-id", "spec": spec},
        },
        {
            "id": "ended-challenge-id",
            "name": "ended-challenge",
            "end_at": "2026-09-01T00:00:00Z",
            "config": {"id": "ended-config-id", "spec": spec},
        },
    ]

    api._init_active_challenges()

    api.core_api.list_challenges.assert_called_once_with(expands=["config"])
    assert api._active_core_challenges() == {"challenge-id": "challenge-name"}
    assert "ended-challenge" not in api.active_challenges
    challenge = api.active_challenges["challenge-name"]
    assert challenge["name"] == "challenge-name"
    assert challenge["scoring_headers"] == {
        "X-API-KEY": "Bearer loaded-secret",
        "literal": "$CHALLENGE_TOKEN",
        "missing": "${MISSING_CHALLENGE_TOKEN}",
        "default": "fallback",
        "empty": "empty-fallback",
    }
    assert challenge["nested"] == ["loaded-secret", 1]
    assert challenge["challenge_container_run_kwargs"]["environment"]["TASKS"] == (
        '["one","loaded-secret"]'
    )
    assert spec["scoring_headers"]["X-API-KEY"] == "Bearer ${CHALLENGE_TOKEN}"


def test_final_output_finalizes_commit_without_comparison():
    finalization_module = importlib.import_module("src.api.challenge.finalization")
    finalizer = finalization_module.FinalOutput()
    finalizer.challenge_min_acceptable_score = 0.9
    finalizer.comparison_min_acceptable_score = 0.7
    commit = ScoringCommit(
        miner_uid=0,
        miner_hotkey="hotkey",
        challenge_name="challenge",
        docker_hub_id="image:latest",
        encrypted_commit="cipher",
        scoring_logs=[SimpleNamespace(score=0.95)],
    )

    finalizer.start(commit)

    assert commit.accepted is True
    assert commit.score == 0.95
    assert commit.penalty is None


def test_final_output_applies_similarity_penalty_and_rejects_commit():
    finalization_module = importlib.import_module("src.api.challenge.finalization")
    finalizer = finalization_module.FinalOutput()
    finalizer.challenge_min_acceptable_score = 0.9
    finalizer.comparison_min_acceptable_score = 0.7
    commit = ScoringCommit(
        miner_uid=0,
        miner_hotkey="hotkey",
        challenge_name="challenge",
        docker_hub_id="image:latest",
        encrypted_commit="cipher",
        scoring_logs=[SimpleNamespace(score=0.95)],
        comparison_logs={"reference": [SimpleNamespace(similarity_score=0.8)]},
    )

    finalizer.start(commit)

    assert commit.accepted is False
    assert commit.penalty == 0.8
    assert commit.score < 0.95


def test_get_neuron_uses_single_resource_request():
    client = CoreApiClient("http://core", "secret", timeout=1)
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "data": {
            "id": "miner-a",
            "uid": 42,
            "hotkey_address": "5hotkey",
            "deregistered_at": None,
        }
    }
    client.session.request = Mock(return_value=response)

    assert client.get_neuron("miner-a")["uid"] == 42
    client.session.request.assert_called_once_with(
        "GET", "http://core/neurons/miner-a", timeout=1
    )


def test_registered_miner_uses_deregistered_at_from_neuron(monkeypatch):
    api, scoring_module, _ = _scoring_api_for_forward(
        monkeypatch,
        {"id": "result-a", "miner_id": "miner-a", "status": "PENDING"},
    )
    api.core_api.get_neuron.return_value = {
        "uid": 42,
        "hotkey_address": "5hotkey",
        "deregistered_at": None,
    }

    assert scoring_module.ScoringApi._registered_miner(api, "miner-a") == (
        42,
        "5hotkey",
    )
    api.core_api.get_neuron.assert_called_once_with("miner-a")

    api.core_api.get_neuron.return_value["deregistered_at"] = "2026-09-26T00:00:00Z"
    assert scoring_module.ScoringApi._registered_miner(api, "miner-a") is None
