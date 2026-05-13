FROM python:3.12-slim

COPY void42-ca.crt /usr/local/share/ca-certificates/void42-ca.crt
RUN apt-get update -y \
 && apt-get install -y --no-install-recommends ca-certificates \
 && update-ca-certificates \
 && rm -rf /var/lib/apt/lists/*

ENV PIP_INDEX_URL=https://nexus.void42.internal/repository/pypi-proxy/simple/ \
    PIP_TRUSTED_HOST=nexus.void42.internal

WORKDIR /app
COPY pyproject.toml .
COPY virtuoso_sink/ ./virtuoso_sink/

# Install: gmr-events + gmr-event-schemas come from the internal
# Gitea generic registry once published; for now we install
# editable from sibling clones at build time.
COPY vendor/fontem-events/      /tmp/fontem-events/
COPY vendor/fontem-event-schemas/ /tmp/fontem-event-schemas/
RUN pip install --no-cache-dir /tmp/fontem-event-schemas \
                                /tmp/fontem-events \
                                . \
 && rm -rf /tmp/fontem-events /tmp/fontem-event-schemas

# Non-root
RUN useradd --create-home --shell /bin/bash sink
USER sink

EXPOSE 9100
ENTRYPOINT ["python", "-m", "virtuoso_sink"]
