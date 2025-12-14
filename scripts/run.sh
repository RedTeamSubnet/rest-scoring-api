#!/usr/bin/env bash
set -euo pipefail


## --- Base --- ##
_SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]:-"$0"}")" >/dev/null 2>&1 && pwd -P)"
_PROJECT_DIR="$(cd "${_SCRIPT_DIR}/.." >/dev/null 2>&1 && pwd)"
cd "${_PROJECT_DIR}" || exit 2


# shellcheck disable=SC1091
[ -f .env ] && . .env


if ! command -v python >/dev/null 2>&1; then
	echo "[ERROR]: Not found 'python' command, please install it first!" >&2
	exit 1
fi
## --- Base --- ##


## --- Variables --- ##
RT_SCORING_API_UID=${RT_SCORING_API_UID:--1}
## --- Variables --- ##


## --- Main --- ##
main()
{
	echo "[INFO]: Starting Scoring API..."
	python -u -m src.api \
		--wallet.name "${RT_SCORING_API_WALLET_NAME:-scoring-api}" \
		--wallet.path "${RT_BTCLI_WALLET_DIR:-${RT_BTCLI_DATA_DIR:-/var/lib/sidecar-btcli}/wallets}" \
		--wallet.hotkey "default" \
		--subtensor.network "${RT_BT_SUBTENSOR_NETWORK:-${RT_BT_SUBTENSOR_WS_SCHEME:-ws}://${RT_BT_SUBTENSOR_HOST:-subtensor}:${RT_BT_SUBTENSOR_WS_PORT:-9944}}" \
		--network "${RT_SUBTENSOR_NETWORK:-test}" \
		--netuid "${RT_BT_SUBNET_NETUID:-2}" \
		--scoring_api.port "${RT_SCORING_API_PORT:-47920}" \
		--scoring_api.epoch_length "${RT_SCORING_API_EPOCH_LENGTH:-60}" \
		--validator.cache_dir "${RT_SCORING_API_DATA_DIR:-/var/lib/rest-scoring-api}/.cache" \
		--validator.hf_repo_id "${RT_SCORING_API_HF_REPO:-redteamsubnet61/rest-scoring-api}" || exit 2

	echo "[OK]: Done."
	exit 0
}

main
## --- Main --- ##
