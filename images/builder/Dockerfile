FROM docker.io/library/python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY images/builder/requirements.txt /tmp/requirements.txt
RUN pip install --require-hashes --only-binary=:all: -r /tmp/requirements.txt
COPY pyproject.toml /opt/golden-road/pyproject.toml
COPY src /opt/golden-road/src
RUN pip install --no-deps --no-build-isolation --no-index /opt/golden-road && rm -rf /opt/golden-road
COPY platform/platform.json /etc/golden-road/platform.json
COPY platform/required-checks.json /etc/golden-road/required-checks.json
COPY platform/overrides/ /etc/golden-road/overrides/
# Fail the build (closed) on a malformed required-checks list rather than shipping it.
RUN python -c "from golden_road.checks import load_required; load_required('/etc/golden-road/required-checks.json')"
ENV BUILDER_PLATFORM_CONFIG=/etc/golden-road/platform.json \
    GOLDEN_ROAD_REQUIRED_CHECKS=/etc/golden-road/required-checks.json \
    GOLDEN_ROAD_OVERRIDES=/etc/golden-road/overrides
RUN groupadd --gid 10001 gr && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin gr
USER 10001:10001
WORKDIR /src
