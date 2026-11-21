FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates docker.io docker-cli \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --create-home --home-dir /home/agent agent \
    && usermod -aG docker agent \
    && mkdir -p /var/lib/craftpilot-agent /srv/craftpilot/servers \
    && chown -R agent:agent /var/lib/craftpilot-agent /srv/craftpilot/servers

RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir cryptography==46.0.5 \
    && python -c "import cryptography; assert cryptography.__version__ == '46.0.5'"

WORKDIR /opt/craftpilot-runtime-agent
COPY craftpilot_runtime_agent.py migration_transfer.py file_tree_index.py process_runtime.py sftp_runtime.py ./
COPY test_migration_transfer.py ./

# Versionen som imagen rapporterar till mastern. Publish-workflowen skickar
# taggens namn (v0.3.0); en lokal `docker build` utan argument rapporterar
# "dev", precis som källkörning. Samma normalisering som craftpilot-agent.spec,
# så imagen och Windows-exe:n visar samma sträng.
ARG CRAFTPILOT_AGENT_VERSION=dev
RUN python - <<'PY'
import os
import pathlib
import re

version = os.environ.get("CRAFTPILOT_AGENT_VERSION", "").strip().lstrip("v") or "dev"
version = re.sub(r"[^A-Za-z0-9._+-]", ".", version) or "dev"
pathlib.Path("/opt/craftpilot-runtime-agent/_build_version.py").write_text(
    '"""Genererad av Dockerfile — ändra inte."""\n'
    f"BUILD_VERSION = {version!r}\n",
    encoding="utf-8",
)
PY
RUN python -c "import _build_version; print('agent version:', _build_version.BUILD_VERSION)"

RUN python -m unittest -q test_migration_transfer

RUN rm test_migration_transfer.py
USER agent
CMD ["python", "/opt/craftpilot-runtime-agent/craftpilot_runtime_agent.py"]
