import datetime

from src.api.__main__ import (
    _docker_hub_id_from_plain_commit,
    _extract_commit_files,
    _as_timestamp,
    _join_url,
)


def test_docker_hub_id_from_plain_commit_supports_legacy_prefix():
    assert (
        _docker_hub_id_from_plain_commit(
            "ab_sniffer_v6", "ab_sniffer_v6---repo/image@sha256:abc"
        )
        == "repo/image@sha256:abc"
    )


def test_docker_hub_id_from_plain_commit_supports_raw_digest():
    assert (
        _docker_hub_id_from_plain_commit("flowradar_v2", "repo/image@sha256:def")
        == "repo/image@sha256:def"
    )


def test_extract_commit_files_from_commit_output_payload():
    commit_files, telemetry = _extract_commit_files(
        {
            "commit_outputs": [
                {
                    "role": "miner-output",
                    "data": {
                        "commit_files": [{"file_name": "headless.js", "content": "x"}],
                        "telemetry": {"score": 1.0},
                    },
                }
            ]
        }
    )

    assert commit_files == [{"file_name": "headless.js", "content": "x"}]
    assert telemetry == {"score": 1.0}


def test_extract_commit_files_from_file_rows():
    commit_files, telemetry = _extract_commit_files(
        {
            "commit_files": [
                {
                    "orig_filename": "headless.js",
                    "data": "console.log('ok')",
                }
            ]
        }
    )

    assert commit_files == [
        {"file_name": "headless.js", "content": "console.log('ok')"}
    ]
    assert telemetry is None


def test_as_timestamp_accepts_iso_datetime():
    timestamp = _as_timestamp("2026-06-30T00:00:00+00:00")

    assert timestamp == datetime.datetime(
        2026, 6, 30, tzinfo=datetime.timezone.utc
    ).timestamp()


def test_join_url_handles_api_prefix():
    assert (
        _join_url("http://storage:9978/", "/api/v1", "/scoring/work-items")
        == "http://storage:9978/api/v1/scoring/work-items"
    )
