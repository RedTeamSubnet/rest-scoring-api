# RedTeam subnet - Scoring REST API

## Overview

The Scoring API is a standalone service that provides centralized scoring for RedTeam challenges. It scores and compares miner submissions received through rest-core without connecting to Subtensor or managing validator weights.

## Key Features

- Centralized scoring infrastructure for all subnet validators
- Aggregation of miner submissions from all active validators
- Automatic deduplication and versioning of submissions
- Challenge-specific scoring and comparison controllers
- Daily score processing at predetermined hours (SCORING_HOUR constant)
- In-memory and persistent caching of scoring results
- RESTful API for validators to retrieve scoring results
- Prometheus metrics for monitoring

## Technical Architecture

### State Management

The scoring API server maintains several important state variables:

- `validators_miner_commits`: Stores current miner commits from all validators, indexed by validator UID and hotkey
- `miner_commits`: Aggregated miner commits from all validators, indexed by miner UID and hotkey
- `miner_commits_cache`: Quick lookup cache mapping challenge+encrypted_commit to commit objects
- `scoring_results`: Cache for scored submissions with scoring logs and comparison logs

### Processing Workflow

1. **Validation Loop (`forward` method)**
   - Updates validator and miner commit state
   - Scores new submissions
   - Finalizes daily scoring at the designated scoring hour
   - Stores results in persistent storage

2. **Submission Collection**
   - `_update_validators_miner_commits`: Fetches commits from all validators with sufficient stake
   - `_update_miner_commits`: Aggregates commits from all validators, keeping the latest versions
   - Maintains submission history and versioning based on commit timestamps

3. **Scoring Process**
   - `_score_and_compare_new_miner_commits`: Scores new submissions using challenge controllers
   - `_compare_miner_commits`: Compares submissions for similarity detection
   - Caches scoring results for each docker_hub_id to prevent duplicate processing
   - Handles both daily incremental scoring and final scoring at the designated hour

4. **Storage Integration**
   - `_store_centralized_scoring`: Persists scoring results to storage service
   - `_fetch_centralized_scoring`: Retrieves scoring results from storage
   - `_sync_scoring_results_from_storage_to_cache`: Syncs storage data to local cache
   - Maintains local backup in `scoring_results.json`

5. **API Interface**
   - FastAPI server running in a separate thread
   - `/get_scoring_result`: Returns scoring and comparison results for specific challenge and commits, validator can use this endpoint to get the scoring results of the miner commits
   - Prometheus metrics for monitoring

## API Endpoints

### `/get_scoring_result`

- **Method**: POST
- **Parameters**:
    - `challenge_name`: Name of the challenge
    - `encrypted_commits`: List of encrypted commits to look up
- **Response**:

    ```json
    {
    "status": "success",
    "message": "Scoring results retrieved successfully",
    "data": {
        "commits": {
        "<encrypted_commit>": {
            "miner_uid": 123,
            "miner_hotkey": "...",
            "challenge_name": "...",
            "scoring_logs": [...],
            "comparison_logs": {...},
            "score": 0.95,
            "penalty": 0.0
        }
        },
        "is_done": true/false
    }
    }
    ```

## Setup

### Running the Server

```bash
RT_SCORING_API_CORE_API_URL=http://localhost:8000/api/v1 \
RT_SCORING_API_CORE_API_KEY=replace-me \
RT_SCORING_API_PORT=47920 \
RT_SCORING_API_POLL_INTERVAL=1200 \
python -u -m src.api
```

The service reads scoring settings from `RT_SCORING_API_*` environment variables. It does not require a wallet or Subtensor connection.

## Integration for Validators

Validators can use the centralized scoring service by:

1. Adding the `--validator.use_centralized_scoring` flag to their validator command
2. The validator will automatically fetch scoring results from the scoring API server
3. This eliminates the need for each validator to run scoring infrastructure

## Development & Troubleshooting

### Monitoring

- Check the Prometheus metrics endpoint for performance monitoring
- Review logs for scoring errors and processing delays
- The `scoring_results.json` file provides a backup of all scoring data

### Common Issues

- If scoring results aren't being updated, verify the rest-core URL and API key
- Ensure the service can access the Docker daemon and required challenge images

### Security Considerations

- The scoring API has access to all validator submissions and must maintain data integrity
- Uses validation headers for secure communication with storage service
- Maintains proper synchronization between memory cache and persistent storage
