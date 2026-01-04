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
	python -u -m src.api || exit 2

	echo "[OK]: Done."
	exit 0
}

main
## --- Main --- ##
