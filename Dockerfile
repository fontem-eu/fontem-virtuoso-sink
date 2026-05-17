# The bulk-load CLI needs the Virtuoso isql binary to drive ld_dir +
# rdf_loader_run for files larger than SPARQL `LOAD <url>`'s ~10 MB
# string-content limit (FA008 error). Pulled in via a multi-stage
# copy from the Virtuoso image — same pattern as virtuoso-exporter.
FROM contribute.void42.internal/fontem/virtuoso-opensource-7:7.2.14 AS virtuoso

FROM python:3.12-slim

COPY void42-ca.crt /usr/local/share/ca-certificates/void42-ca.crt
RUN apt-get update -y \
 && apt-get install -y --no-install-recommends ca-certificates libgcc-s1 \
 && update-ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# isql + the OpenLink shared libs it links against.
COPY --from=virtuoso /opt/virtuoso-opensource/bin/isql /opt/virtuoso-opensource/bin/isql
COPY --from=virtuoso /opt/virtuoso-opensource/lib/ /opt/virtuoso-opensource/lib/
ENV LD_LIBRARY_PATH=/opt/virtuoso-opensource/lib

ENV PIP_INDEX_URL=https://nexus.void42.internal/repository/pypi-proxy/simple/ \
    PIP_TRUSTED_HOST=nexus.void42.internal

WORKDIR /app
COPY pyproject.toml .
COPY virtuoso_sink/ ./virtuoso_sink/

# Install: fontem-events + fontem-event-schemas come from the internal
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
