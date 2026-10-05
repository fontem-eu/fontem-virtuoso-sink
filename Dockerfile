# The bulk-load CLI needs the Virtuoso isql binary to drive ld_dir +
# rdf_loader_run for files larger than SPARQL `LOAD <url>`'s ~10 MB
# string-content limit (FA008 error). Pulled in via a multi-stage
# copy from the Virtuoso image — same pattern as virtuoso-exporter.
# Base images are pinned by digest: a tag can be re-pushed upstream and
# change the build with no commit of ours (Docker Hub swapped Virtuoso
# 7.2.17 for a 7.2.18-dev build in August 2026).
# Our patched Virtuoso image (same OpenLink build), which declares Virtuoso
# for SBOMs: sbom-declare.py reads that declaration.
FROM contribute.void42.internal/fontem/virtuoso-opensource-7:7.2.16-r3@sha256:6e93fc5364b16105cfba9d36c37c0bb8538011bdc376dcf6317e1b2b2781c308 AS virtuoso

FROM python:3.14-slim@sha256:c3e521df8b2b498a7a682e7e18676771cb80c6b75b8699af886b2d554ce40151

COPY void42-ca.crt /usr/local/share/ca-certificates/void42-ca.crt
# libedit2 is isql's line-editing library. Without it isql does not load
# ("libedit.so.2: cannot open shared object file"), so bulk_load could
# never drive ld_dir/rdf_loader_run.
RUN apt-get update -y \
 && apt-get install -y --no-install-recommends ca-certificates libgcc-s1 libedit2 \
 && update-ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# isql + the OpenLink shared libs it links against.
COPY --from=virtuoso /opt/virtuoso-opensource/bin/isql /opt/virtuoso-opensource/bin/isql
COPY --from=virtuoso /opt/virtuoso-opensource/lib/ /opt/virtuoso-opensource/lib/
ENV LD_LIBRARY_PATH=/opt/virtuoso-opensource/lib
# isql, the ODBC libraries and CPython's own extension modules are not in any
# package database: declare them for the SBOM (sbom-declare.py), which
# docker-build-sign requires to cover every executable file.
COPY --from=virtuoso /usr/share/void42/sbom/declared.json /tmp/virtuoso-declared.json
COPY sbom-declare.py /tmp/sbom-declare.py
RUN mkdir -p /usr/share/void42/sbom \
 && python3 /tmp/sbom-declare.py /tmp/virtuoso-declared.json > /usr/share/void42/sbom/declared.json \
 && rm /tmp/virtuoso-declared.json /tmp/sbom-declare.py

ENV PIP_INDEX_URL=https://nexus.void42.internal/repository/pypi-proxy/simple/ \
    PIP_TRUSTED_HOST=nexus.void42.internal

WORKDIR /app
COPY pyproject.toml .
COPY virtuoso_sink/ ./virtuoso_sink/

# Vendored wheels — pinned by filename. To bump fontem-events or
# fontem-event-schemas: build a new wheel in the producing repo,
# drop it into vendor/, delete the old one, and update the version
# pins in pyproject.toml. The pin + the wheel filename must agree;
# pip refuses to satisfy the pin from a wheel with a different
# version, so a mismatch fails the build.
COPY vendor/*.whl /tmp/wheels/
RUN pip install --no-cache-dir /tmp/wheels/*.whl . \
 && rm -rf /tmp/wheels

# Non-root
RUN useradd --create-home --shell /bin/bash sink
USER sink

EXPOSE 9100
ENTRYPOINT ["python", "-m", "virtuoso_sink"]
