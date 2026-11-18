# The golden road's agent runtime (spec 2026-11-17 A1): what every agent app runs. release-agent-runtime.yml builds and signs it; the
# builder's generated Dockerfile for an agent is FROM its digest (platform.json images.agent.runtime) plus agent.yaml, nothing else.
# The base is the builder's Python runtime image (src/builder/data/base_images.json): a base-image fix is one bump for both.
# It runs on a read-only root: nothing is written at run time (no bytecode; the venv and the package are root's, read only).
FROM docker.io/library/python:3.12-slim-trixie@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54 AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY requirements.txt /tmp/requirements.txt
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --require-hashes --only-binary=:all: -r /tmp/requirements.txt

FROM docker.io/library/python:3.12-slim-trixie@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54
RUN groupadd --gid 10001 agent && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin agent
ENV PATH=/opt/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/opt/agent-runtime
COPY --from=build /opt/venv /opt/venv
COPY agent_runtime /opt/agent-runtime/agent_runtime
WORKDIR /app
USER 10001:10001
EXPOSE 8080
CMD ["python", "-m", "agent_runtime"]
