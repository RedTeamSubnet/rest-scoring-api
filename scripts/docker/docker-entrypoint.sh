#!/usr/bin/env bash
set -euo pipefail


echo "[INFO]: Running '${RT_SCORING_API_SLUG}' docker-entrypoint.sh..."

_run()
{
	_i=0
	while true; do
		if [ -d "${RT_BTCLI_WALLET_DIR:-${RT_BTCLI_DATA_DIR:-/var/lib/sidecar-btcli}/wallets}" ]; then
			break
		fi

		echo "[INFO]: Waiting for the wallet directory to be created..."
		_i=$((_i + 1))
		if [ "${_i}" -ge 60 ]; then
			echo "[ERROR]: Timeout waiting for the wallet directory to be created!" >&2
			exit 1
		fi

		sleep 1
	done

	if [ "${ENV:-}" != "PRODUCTION" ] && [ "${ENV:-}" != "STAGING" ]; then
		_i=0
		while true; do
			local _checkpoint_file_path="${RT_BTCLI_DATA_DIR:-/var/lib/sidecar-btcli}/${RT_BTCLI_CHECKPOINT_FNAME:-.checkpoint.txt}"
			if [ -f "${_checkpoint_file_path}" ]; then
				local _checkpoint_val=0
				_checkpoint_val=$(cat "${_checkpoint_file_path}")
				if [ "${_checkpoint_val}" -ge 4 ]; then
					break
				fi
			fi

			if [ $(( _i % 10 )) -eq 0 ]; then
				echo "[INFO]: Waiting for the wallets to be registered and ready..."
			fi
			_i=$((_i + 1))
			sleep 1
		done
	fi

	sleep 5
	echo "[INFO]: Starting ${RT_SCORING_API_SLUG}..."
	exec sg docker "exec python -u -m api \
		--wallet.name \"${RT_SCORING_API_WALLET_NAME:-scoring-api}\" \
		--wallet.path \"${RT_BTCLI_WALLET_DIR:-${RT_BTCLI_DATA_DIR:-/var/lib/sidecar-btcli}/wallets}\" \
		--wallet.hotkey \"default\" \
		--subtensor.network \"${RT_BT_SUBTENSOR_NETWORK:-${RT_BT_SUBTENSOR_WS_SCHEME:-ws}://${RT_BT_SUBTENSOR_HOST:-subtensor}:${RT_BT_SUBTENSOR_WS_PORT:-9944}}\" \
		--netuid \"${RT_BT_SUBNET_NETUID:-2}\" \
		--scoring_api.port \"${RT_SCORING_API_PORT:-47920}\" \
		--scoring_api.epoch_length \"${RT_SCORING_API_EPOCH_LENGTH:-60}\" \
		--validator.cache_dir \"${RT_SCORING_API_DATA_DIR:-/var/lib/rest-scoring-api}/.cache\" \
		--validator.hf_repo_id \"${RT_SCORING_API_HF_REPO:-redteamsubnet61/rest-scoring-api}\"" || exit 2

	exit 0
}


main()
{
	umask 0002 || exit 2

	find "${RT_HOME_DIR}" \
		"${RT_SCORING_API_CONFIGS_DIR}" \
		"${RT_SCORING_API_DATA_DIR}" \
		"${RT_SCORING_API_LOGS_DIR}" \
		"${RT_SCORING_API_TMP_DIR}" \
		\( \
			-type d -name ".git" -o \
			-type d -name ".venv" -o \
			-type d -name "modules" -o \
			-type d -name "volumes" -o \
			-type l -name ".env" \
		\) -prune -o -print0 | \
			sudo xargs -0 chown -c "${USER}:${GROUP}" || exit 2

	find "${RT_SCORING_API_DIR}" "${RT_SCORING_API_CONFIGS_DIR}" "${RT_SCORING_API_DATA_DIR}" \
		\( \
			-type d -name ".git" -o \
			-type d -name ".venv" -o \
			-type d -name "scripts" -o \
			-type d -name "modules" -o \
			-type d -name "volumes" \
		\) -prune -o -type d -exec \
			sudo chmod 770 {} + || exit 2

	find "${RT_SCORING_API_DIR}" "${RT_SCORING_API_CONFIGS_DIR}" "${RT_SCORING_API_DATA_DIR}" \
		\( \
			-type d -name ".git" -o \
			-type d -name ".venv" -o \
			-type d -name "scripts" -o \
			-type d -name "modules" -o \
			-type d -name "volumes" -o \
			-type l -name ".env" \
		\) -prune -o -type f -exec \
			sudo chmod 660 {} + || exit 2

	find "${RT_SCORING_API_DIR}" "${RT_SCORING_API_CONFIGS_DIR}" "${RT_SCORING_API_DATA_DIR}" \
		\( \
			-type d -name ".git" -o \
			-type d -name ".venv" -o \
			-type d -name "scripts" -o \
			-type d -name "modules" -o \
			-type d -name "volumes" \
		\) -prune -o -type d -exec \
			sudo chmod ug+s {} + || exit 2

	find "${RT_SCORING_API_LOGS_DIR}" "${RT_SCORING_API_TMP_DIR}" -type d -exec sudo chmod 775 {} + || exit 2
	find "${RT_SCORING_API_LOGS_DIR}" "${RT_SCORING_API_TMP_DIR}" -type f -exec sudo chmod 664 {} + || exit 2
	find "${RT_SCORING_API_LOGS_DIR}" "${RT_SCORING_API_TMP_DIR}" -type d -exec sudo chmod +s {} + || exit 2

	echo "${USER} ALL=(ALL) ALL" | sudo tee -a "/etc/sudoers.d/${USER}" > /dev/null || exit 2
	echo ""

	## Parsing input:
	case ${1:-} in
		"" | -s | --start | start | --run | run)
			_run;;
			# shift;;
		-b | --bash | bash | /bin/bash)
			shift
			if [ -z "${*:-}" ]; then
				echo "[INFO]: Starting bash..."
				/bin/bash
			else
				echo "[INFO]: Executing command -> ${*}"
				exec /bin/bash -c "${@}" || exit 2
			fi
			exit 0;;
		*)
			echo "[ERROR]: Failed to parsing input -> ${*}" >&2
			echo "[INFO]: USAGE: ${0}  -s, --start, start | -b, --bash, bash, /bin/bash"
			exit 1;;
	esac
}

main "${@:-}"
