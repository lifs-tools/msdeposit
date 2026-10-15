# msdeposit-tools: the Python package plus the bundled offline validators (design §10, CONTRACTS.md §10).
#
#   docker build -t msdeposit-tools:dev tools/msdeposit-tools                     # runtime image (default target)
#   docker build --target test -t msdeposit-tools:test tools/msdeposit-tools && \
#     docker run --rm msdeposit-tools:test                                         # pytest -m external inside
#
# External tools are downloaded with pinned versions and SHA-256 checks by docker/fetch_tools.sh. Files of the same
# name placed in docker/tools/ (gitignored) are used instead of downloads (offline builds, vanished URLs); checksums
# can be overridden with --build-arg (e.g. JMZTABM_VALIDATOR_SHA256 when the dev-latest 2.1 validator moves on).

ARG PYTHON_IMAGE=python:3.12.12-slim-bookworm

# --- external tools ---------------------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS tools
ARG OPA_SHA256
ARG MTBLS_BUNDLE_SHA256
ARG JMZTABM_CLI_SHA256
ARG MZTAB_MAPPING_SHA256
ARG JMZTABM_VALIDATOR_SHA256
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
COPY docker/fetch_tools.sh /build/fetch_tools.sh
COPY docker/tools/ /build/tools/
# build args that are not given (empty) fall back to the pins in fetch_tools.sh
RUN sh /build/fetch_tools.sh /opt/msdeposit/tools /build/tools

# --- runtime ----------------------------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS base
LABEL org.opencontainers.image.title="msdeposit-tools" \
      org.opencontainers.image.description="Domain logic and bundled validators of the lifs-tools/msdeposit pipeline" \
      org.opencontainers.image.source="https://github.com/lifs-tools/msdeposit" \
      org.opencontainers.image.licenses="MIT"
# JRE for jmzTab-m 1.0.6; procps for Nextflow (ps)
RUN apt-get update \
    && apt-get install -y --no-install-recommends openjdk-17-jre-headless procps \
    && rm -rf /var/lib/apt/lists/*
COPY --from=tools /opt/msdeposit/tools /opt/msdeposit/tools
COPY pyproject.toml README.md /build/msdeposit-tools/
COPY docker/constraints.txt /build/constraints.txt
COPY src /build/msdeposit-tools/src
RUN pip install --no-cache-dir --constraint /build/constraints.txt /build/msdeposit-tools \
    && rm -rf /build/msdeposit-tools
ENV MSDEPOSIT_TOOLS_DIR=/opt/msdeposit/tools \
    PATH=/opt/msdeposit/tools:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
RUN useradd --create-home --uid 1000 --user-group msdeposit
USER msdeposit
WORKDIR /home/msdeposit

# --- tests with the bundled tools (not the default target) ----------------------------------------------------------
FROM base AS test
USER root
RUN pip install --no-cache-dir --constraint /build/constraints.txt pytest
COPY pyproject.toml /opt/msdeposit/pyproject.toml
COPY tests /opt/msdeposit/tests
USER msdeposit
WORKDIR /opt/msdeposit
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "-m", "external", "tests"]

# --- default target ---------------------------------------------------------------------------------------------
FROM base AS runtime
CMD ["msdeposit-tools", "--help"]
