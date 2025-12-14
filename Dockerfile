# syntax=docker/dockerfile:1
# check=skip=SecretsUsedInArgOrEnv

ARG PYTHON_VERSION=3.10
ARG BASE_IMAGE=python:${PYTHON_VERSION}-slim-trixie

ARG DEBIAN_FRONTEND=noninteractive
ARG RT_SCORING_API_SLUG="rest-scoring-api"


## Here is the builder image:
FROM ${BASE_IMAGE} AS builder

ARG DEBIAN_FRONTEND
ARG RT_SCORING_API_SLUG

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

WORKDIR "/usr/src/${RT_SCORING_API_SLUG}"

RUN --mount=type=cache,target=/root/.cache,sharing=locked \
	_BUILD_TARGET_ARCH=$(uname -m) && \
	echo "BUILDING TARGET ARCHITECTURE: ${_BUILD_TARGET_ARCH}" && \
	rm -rfv /var/lib/apt/lists/* /var/cache/apt/archives/* /tmp/* && \
	apt-get clean -y && \
	apt-get update --fix-missing -o Acquire::CompressionTypes::Order::=gz && \
	apt-get install -y --no-install-recommends \
		git && \
	python -m pip install --timeout 60 -U pip

RUN	--mount=type=cache,target=/root/.cache,sharing=locked \
	--mount=type=bind,source=./requirements.txt,target=requirements.txt \
	python -m pip install --prefix=/install -r ./requirements.txt


## Here is the base image:
FROM ${BASE_IMAGE} AS base

ARG DEBIAN_FRONTEND
ARG RT_SCORING_API_SLUG

ARG RT_HOME_DIR="/app"
ARG RT_SCORING_API_DIR="${RT_HOME_DIR}/${RT_SCORING_API_SLUG}"
ARG RT_SCORING_API_CONFIGS_DIR="/etc/${RT_SCORING_API_SLUG}"
ARG RT_SCORING_API_DATA_DIR="/var/lib/${RT_SCORING_API_SLUG}"
ARG RT_SCORING_API_LOGS_DIR="/var/log/${RT_SCORING_API_SLUG}"
ARG RT_SCORING_API_TMP_DIR="/tmp/${RT_SCORING_API_SLUG}"
ARG RT_SCORING_API_PORT=47920
## IMPORTANT!: Get hashed password from build-arg!
## echo "RT_SCORING_API_PASSWORD123" | openssl passwd -6 -stdin
ARG HASH_PASSWORD="\$6\$i31iAbid3nrpBYVQ\$p2aOWyMbVQ7QaFCGyBlbj6fKPEbgKO5/L2nxn8TElACmUZmDgP9PxsD3ZdtY31.ccHVTQLbcDo86aZPvSq5VH0"
ARG UID=1000
ARG GID=11000
ARG USER=rt-user
ARG GROUP=rt-group

ENV RT_SCORING_API_SLUG="${RT_SCORING_API_SLUG}" \
	RT_HOME_DIR="${RT_HOME_DIR}" \
	RT_SCORING_API_DIR="${RT_SCORING_API_DIR}" \
	RT_SCORING_API_CONFIGS_DIR="${RT_SCORING_API_CONFIGS_DIR}" \
	RT_SCORING_API_DATA_DIR="${RT_SCORING_API_DATA_DIR}" \
	RT_SCORING_API_LOGS_DIR="${RT_SCORING_API_LOGS_DIR}" \
	RT_SCORING_API_TMP_DIR="${RT_SCORING_API_TMP_DIR}" \
	RT_SCORING_API_PORT=${RT_SCORING_API_PORT} \
	UID=${UID} \
	GID=${GID} \
	USER=${USER} \
	GROUP=${GROUP} \
	PYTHONIOENCODING=utf-8 \
	PYTHONUNBUFFERED=1

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN --mount=type=secret,id=HASH_PASSWORD \
	rm -rfv /var/lib/apt/lists/* /var/cache/apt/archives/* /tmp/* /root/.cache/* && \
	apt-get clean -y && \
	# echo "Acquire::http::Pipeline-Depth 0;" >> /etc/apt/apt.conf.d/99fixbadproxy && \
	# echo "Acquire::http::No-Cache true;" >> /etc/apt/apt.conf.d/99fixbadproxy && \
	# echo "Acquire::BrokenProxy true;" >> /etc/apt/apt.conf.d/99fixbadproxy && \
	apt-get update --fix-missing -o Acquire::CompressionTypes::Order::=gz && \
	apt-get install -y --no-install-recommends \
		sudo \
		locales \
		tzdata \
		procps \
		iputils-ping \
		iproute2 \
		curl \
		git \
		nano && \
	curl -fsSL https://get.docker.com/ | sh && \
	apt-get clean -y && \
	sed -i -e 's/# en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/' /etc/locale.gen && \
	sed -i -e 's/# en_AU.UTF-8 UTF-8/en_AU.UTF-8 UTF-8/' /etc/locale.gen && \
	dpkg-reconfigure --frontend=noninteractive locales && \
	update-locale LANG=en_US.UTF-8 && \
	echo "LANGUAGE=en_US.UTF-8" >> /etc/default/locale && \
	echo "LC_ALL=en_US.UTF-8" >> /etc/default/locale && \
	addgroup --gid ${GID} ${GROUP} && \
	useradd -lmN -d "/home/${USER}" -s /bin/bash -g ${GROUP} -G sudo -u ${UID} ${USER} && \
	usermod -aG docker ${USER} && \
	echo "${USER} ALL=(ALL) NOPASSWD: ALL" > "/etc/sudoers.d/${USER}" && \
	chmod 0440 "/etc/sudoers.d/${USER}" && \
	if [ -f "/run/secrets/HASH_PASSWORD" ]; then \
		echo -e "${USER}:$(cat /run/secrets/HASH_PASSWORD)" | chpasswd -e; \
	else \
		echo -e "${USER}:${HASH_PASSWORD}" | chpasswd -e; \
	fi && \
	echo -e "\nalias ls='ls -aF --group-directories-first --color=auto'" >> /root/.bashrc && \
	echo -e "alias ll='ls -alhF --group-directories-first --color=auto'\n" >> /root/.bashrc && \
	echo -e "\numask 0002" >> "/home/${USER}/.bashrc" && \
	echo "alias ls='ls -aF --group-directories-first --color=auto'" >> "/home/${USER}/.bashrc" && \
	echo -e "alias ll='ls -alhF --group-directories-first --color=auto'\n" >> "/home/${USER}/.bashrc" && \
	rm -rfv /var/lib/apt/lists/* /var/cache/apt/archives/* /tmp/* /root/.cache/* "/home/${USER}/.cache/*" && \
	mkdir -pv "${RT_SCORING_API_DIR}" \
		"${RT_SCORING_API_CONFIGS_DIR}" \
		"${RT_SCORING_API_DATA_DIR}" \
		"${RT_SCORING_API_LOGS_DIR}" \
		"${RT_SCORING_API_TMP_DIR}" && \
	chown -Rc "${USER}:${GROUP}" \
		"${RT_HOME_DIR}" \
		"${RT_SCORING_API_CONFIGS_DIR}" \
		"${RT_SCORING_API_DATA_DIR}" \
		"${RT_SCORING_API_LOGS_DIR}" \
		"${RT_SCORING_API_TMP_DIR}" && \
	find "${RT_SCORING_API_DIR}" \
		"${RT_SCORING_API_CONFIGS_DIR}" \
		"${RT_SCORING_API_DATA_DIR}" -type d -exec chmod -c 770 {} + && \
	find "${RT_SCORING_API_DIR}" \
		"${RT_SCORING_API_CONFIGS_DIR}" \
		"${RT_SCORING_API_DATA_DIR}" -type d -exec chmod -c ug+s {} + && \
	find "${RT_SCORING_API_LOGS_DIR}" "${RT_SCORING_API_TMP_DIR}" -type d -exec chmod -c 775 {} + && \
	find "${RT_SCORING_API_LOGS_DIR}" "${RT_SCORING_API_TMP_DIR}" -type d -exec chmod -c +s {} +

ENV LANG=en_US.UTF-8 \
	LANGUAGE=en_US.UTF-8 \
	LC_ALL=en_US.UTF-8

COPY --from=builder /install /usr/local


## Here is the final image:
FROM base AS app

WORKDIR "${RT_SCORING_API_DIR}"
COPY --chown=${UID}:${GID} ./src ${RT_SCORING_API_DIR}
COPY --chown=${UID}:${GID} --chmod=770 ./scripts/docker/*.sh /usr/local/bin/

# VOLUME ["${RT_SCORING_API_DATA_DIR}"]
# EXPOSE ${RT_SCORING_API_PORT}

USER ${UID}:${GID}
# HEALTHCHECK --start-period=30s --start-interval=1s --interval=5m --timeout=5s --retries=3 \
# 	CMD curl -f http://localhost:${RT_SCORING_API_PORT}/api/v${RT_SCORING_API_VERSION:-1}/ping || exit 1

ENTRYPOINT ["docker-entrypoint.sh"]
