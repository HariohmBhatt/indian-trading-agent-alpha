FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    RUNNER_HOME=/home/runner \
    RUNNER_DIR=/opt/actions-runner \
    RUNNER_ALLOW_RUNASROOT=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        docker-buildx \
        docker.io \
        docker-compose-v2 \
        git \
        jq \
        libicu74 \
        libkrb5-3 \
        libssl3 \
        libunwind8 \
        sudo \
        tar \
        unzip \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /bin/bash runner \
    && mkdir -p "$RUNNER_DIR" "$RUNNER_HOME/_work" \
    && chown -R runner:runner "$RUNNER_HOME" "$RUNNER_DIR"

WORKDIR $RUNNER_DIR

RUN runner_version="$(curl -fsSL https://api.github.com/repos/actions/runner/releases/latest | jq -r '.tag_name | ltrimstr("v")')" \
    && test -n "$runner_version" \
    && curl -fsSL "https://github.com/actions/runner/releases/download/v${runner_version}/actions-runner-linux-x64-${runner_version}.tar.gz" \
      | tar -xz --strip-components=0 \
    && chown -R runner:runner "$RUNNER_DIR"

COPY entrypoint.sh /entrypoint.sh
RUN chmod 0755 /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
