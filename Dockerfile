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
COPY craftpilot_runtime_agent.py migration_transfer.py ./
COPY test_migration_transfer.py ./
RUN python -m unittest -q test_migration_transfer

RUN rm test_migration_transfer.py
USER agent
CMD ["python", "/opt/craftpilot-runtime-agent/craftpilot_runtime_agent.py"]
