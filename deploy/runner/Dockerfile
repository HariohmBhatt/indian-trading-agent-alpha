FROM ubuntu:24.04@sha256:4fbb8e6a8395de5a7550b33509421a2bafbc0aab6c06ba2cef9ebffbc7092d90

ARG RUNNER_VERSION=2.336.0
ARG RUNNER_SHA256=04cf0be1aff4c3ec3554466c39124ca250e3effd8873bb7e8d68535aa9505d5d
ARG OCI_SOURCE="https://github.com/HariohmBhatt/indian-trading-agent-alpha"
ARG OCI_REVISION="local"

LABEL org.opencontainers.image.source="${OCI_SOURCE}" \
      org.opencontainers.image.revision="${OCI_REVISION}"

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

RUN curl --fail --location --silent --show-error \
      --output /tmp/actions-runner.tar.gz \
      "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz" \
    && echo "${RUNNER_SHA256}  /tmp/actions-runner.tar.gz" | sha256sum --check --strict \
    && tar -xzf /tmp/actions-runner.tar.gz --strip-components=0 \
    && rm -f /tmp/actions-runner.tar.gz \
    && chown -R runner:runner "$RUNNER_DIR"

COPY entrypoint.sh /entrypoint.sh
RUN chmod 0755 /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
