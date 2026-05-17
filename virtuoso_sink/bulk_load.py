"""Bulk-load static RDF files into Virtuoso.

The steady-state event sink (``virtuoso_sink.sink``) is the right tool
for the platform's *own* event stream — it accumulates triples inside a
``BeginGraphReplace``/``EndGraphReplace`` bracket and PUTs them in one
shot. But for the external datasets we mirror (EuroVoc, Wikidata, CELLAR,
CORDIS) the files arrive as static N-Triples / Turtle / RDF-XML
artefacts, not as events. This module loads those directly.

Two transport modes, picked by the file's reachability:

  * **HTTP LOAD** (``--mode http``, default for small files): one SPARQL
    ``LOAD <url>`` per file. Virtuoso fetches the file itself. Works for
    URLs Virtuoso can reach (a sidecar HTTP server, a public CDN, etc.).

  * **file:// LOAD** (``--mode file``): same SPARQL ``LOAD`` directive
    but with a ``file:///database/staging/...`` URL. The path must be in
    Virtuoso's ``DirsAllowed`` list. This is the typical path on our
    cluster because the staging directory sits on the same PVC as the
    Virtuoso ``/database`` mount.

For very large loads (full Wikidata truthy dump, ~1.5 TiB raw) HTTP LOAD
serialises through Virtuoso's single request handler and is slow. The
runbook for those calls for a maintenance-window ``kubectl exec`` into
the Virtuoso pod to run native ``ld_dir`` + ``rdf_loader_run`` via isql.
This tool covers everything below that ceiling: EuroVoc end-to-end,
Wikidata filtered slices, CORDIS-generated Turtle, CELLAR sectoral
subsets.

Usage::

    python -m virtuoso_sink.bulk_load \
        --dir /database/staging/eurovoc \
        --pattern '*.rdf' \
        --graph http://data.fontem.eu/graph/eu/eurovoc \
        --mode file

"""
from __future__ import annotations

import argparse
import fnmatch
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx

logger = logging.getLogger(__name__)

# Virtuoso's SPARQL `LOAD <url>` reads the file as a string and refuses
# anything over 10 MiB with FA008. For larger files we fall back to
# isql + ld_dir + rdf_loader_run, which streams from disk instead of
# materialising the file in memory.
SPARQL_LOAD_MAX_BYTES = 10 * 1024 * 1024
ISQL_BIN = os.environ.get(
    "ISQL_BIN", "/opt/virtuoso-opensource/bin/isql"
)


def _list_files(directory: Path, pattern: str) -> list[Path]:
    """Return the matching files sorted lexicographically — load order
    is reproducible across runs, which matters for the
    drop-and-reload pattern."""
    if not directory.is_dir():
        raise ValueError(f"Not a directory: {directory}")
    files = [
        p for p in directory.iterdir()
        if p.is_file() and fnmatch.fnmatch(p.name, pattern)
    ]
    files.sort()
    return files


def _file_uri(path: Path) -> str:
    """Render a file path as a Virtuoso-compatible file:// URI. The
    path is already inside ``DirsAllowed``; we URL-encode the segment
    portion to handle filenames with spaces / non-ASCII."""
    # quote() with safe='/' keeps slashes literal so the path stays
    # readable in logs but spaces, etc. become %20.
    return "file://" + quote(str(path.resolve()), safe="/")


def _load_one(
    client: httpx.Client,
    endpoint: str,
    url: str,
    graph: str,
    timeout_s: float,
) -> None:
    """Issue a single SPARQL ``LOAD <url> INTO GRAPH <graph>`` against
    the Virtuoso SPARQL endpoint. Raises on non-2xx. Auth is configured
    on the client (Digest, set up in ``load_directory``).

    Caller is responsible for keeping files under
    ``SPARQL_LOAD_MAX_BYTES`` — Virtuoso refuses larger payloads with
    FA008. Use ``_load_via_isql`` for anything bigger."""
    query = f"LOAD <{url}> INTO GRAPH <{graph}>"
    resp = client.post(
        endpoint,
        data={"query": query},
        headers={"Accept": "application/sparql-results+json"},
        timeout=timeout_s,
    )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"SPARQL LOAD failed (HTTP {resp.status_code}) for {url}: "
            f"{resp.text[:500]}"
        )


def _sparql_host_port(sparql_endpoint: str) -> tuple[str, int]:
    """Pull the host:port the isql connector should target out of the
    HTTP SPARQL endpoint we already have configured. Virtuoso's isql
    listens on port 1111 by default; we follow that convention."""
    u = urlparse(sparql_endpoint)
    if not u.hostname:
        raise ValueError(f"Cannot parse host from {sparql_endpoint!r}")
    isql_port = int(os.environ.get("VIRTUOSO_ISQL_PORT", "1111"))
    return u.hostname, isql_port


def _shell_quote_sql(s: str) -> str:
    """Quote a string for embedding into a Virtuoso SQL/SPARQL command
    passed via ``isql exec=...``. We only need to escape single quotes
    since isql is fed a single argv parameter, not a shell command."""
    return s.replace("'", "''")


def _load_via_isql(
    *,
    host: str,
    port: int,
    user: str,
    password: str,
    directory: Path,
    pattern: str,
    graph: str,
    timeout_s: float,
) -> str:
    """Drive Virtuoso's native bulk loader (``ld_dir`` + ``rdf_loader_run``)
    via isql. Streams files from disk so it tolerates >10 MB payloads
    that ``LOAD <url>`` chokes on. Returns the isql stdout for logs."""
    if not Path(ISQL_BIN).exists():
        raise RuntimeError(
            f"isql binary not found at {ISQL_BIN}. The bulk_load CLI "
            "needs isql for files > "
            f"{SPARQL_LOAD_MAX_BYTES} bytes — install via the sink's "
            "Dockerfile multi-stage copy."
        )
    g = _shell_quote_sql(graph)
    d = _shell_quote_sql(str(directory.resolve()))
    p = _shell_quote_sql(pattern)
    cmd = [
        ISQL_BIN, f"{host}:{port}", user, password,
        f"exec=ld_dir('{d}', '{p}', '{g}'); rdf_loader_run();",
    ]
    logger.info("isql: ld_dir('%s', '%s', '%s') + rdf_loader_run()",
                d, p, g)
    res = subprocess.run(
        cmd, capture_output=True, text=True,
        timeout=timeout_s, check=False,
    )
    if res.returncode != 0:
        raise RuntimeError(
            f"isql ld_dir failed (rc={res.returncode}): "
            f"stderr={res.stderr[:500]} stdout={res.stdout[:500]}"
        )
    return res.stdout


def load_directory(
    *,
    directory: Path,
    pattern: str,
    graph: str,
    endpoint: str,
    auth: tuple[str, str],
    mode: str,
    url_prefix: str | None = None,
    per_file_timeout_s: float = 1800.0,
) -> dict:
    """Load every matching file from ``directory`` into ``graph``.

    Returns a counts summary suitable for logging.
    """
    files = _list_files(directory, pattern)
    if not files:
        logger.warning("No files matched %s/%s — nothing to load",
                       directory, pattern)
        return {"files": 0, "elapsed_s": 0.0, "errors": 0}

    # Decide upfront whether any file is over the HTTP-LOAD ceiling.
    # If so, the isql path handles the whole batch in one call (much
    # faster than per-file HTTP anyway); otherwise the HTTP path is
    # fine and avoids needing isql in PATH.
    max_size = max(p.stat().st_size for p in files)
    needs_isql = max_size > SPARQL_LOAD_MAX_BYTES

    logger.info(
        "Loading %d files into graph %s (mode=%s, path=%s, max_size=%.1f MB)",
        len(files), graph, mode, "isql" if needs_isql else "http-sparql",
        max_size / (1024 * 1024),
    )
    started = time.time()
    errors = 0

    if needs_isql:
        # One isql call drains the whole directory. ld_dir builds the
        # work list, rdf_loader_run streams each file into the target
        # graph. Faster than N HTTP POSTs even when the files do fit
        # under the size cap, but we only need it when they don't.
        host, port = _sparql_host_port(endpoint)
        try:
            out = _load_via_isql(
                host=host, port=port, user=auth[0], password=auth[1],
                directory=directory, pattern=pattern, graph=graph,
                timeout_s=per_file_timeout_s * len(files),
            )
            logger.info("isql output:\n%s", out)
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            errors = len(files)
            logger.error("isql bulk load FAILED: %s", exc)
    else:
        # Virtuoso's /sparql-auth endpoint speaks HTTP Digest, not Basic
        # — same auth pattern the steady-state sink uses
        # (see sink.py:138).
        digest_auth = httpx.DigestAuth(auth[0], auth[1])
        with httpx.Client(auth=digest_auth) as client:
            for i, path in enumerate(files, start=1):
                if mode == "file":
                    url = _file_uri(path)
                elif mode == "http":
                    if not url_prefix:
                        raise ValueError(
                            "mode=http requires --url-prefix pointing "
                            "at a host reachable from Virtuoso"
                        )
                    url = url_prefix.rstrip("/") + "/" + quote(path.name)
                else:
                    raise ValueError(f"Unknown mode: {mode}")

                t0 = time.time()
                try:
                    _load_one(client, endpoint, url, graph,
                              per_file_timeout_s)
                except (httpx.HTTPError, RuntimeError) as exc:
                    errors += 1
                    logger.error("[%d/%d] FAILED %s: %s",
                                 i, len(files), path.name, exc)
                    continue
                elapsed = time.time() - t0
                size_mb = path.stat().st_size / (1024 * 1024)
                logger.info("[%d/%d] %s (%.1f MB) in %.1fs",
                            i, len(files), path.name, size_mb, elapsed)

    summary = {
        "files": len(files) - errors,
        "elapsed_s": round(time.time() - started, 1),
        "errors": errors,
    }
    logger.info(
        "Done: loaded=%d errors=%d in %.1fs",
        summary["files"], summary["errors"], summary["elapsed_s"],
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bulk-load static RDF files into a Virtuoso named graph"
    )
    parser.add_argument("--dir", required=True, type=Path,
                        help="Directory containing the RDF files")
    parser.add_argument("--pattern", default="*",
                        help="Glob pattern, e.g. '*.nt' or '*.ttl'")
    parser.add_argument("--graph", required=True,
                        help="Target named-graph IRI")
    parser.add_argument("--mode", choices=("file", "http"), default="file",
                        help="How Virtuoso should fetch each file. "
                        "'file' = file:// URI (default; path must be in "
                        "Virtuoso's DirsAllowed). "
                        "'http' = sidecar HTTP server "
                        "(requires --url-prefix).")
    parser.add_argument("--url-prefix",
                        help="Base URL when --mode http (e.g. "
                        "http://my-sidecar.fontem-prod.svc:8000)")
    parser.add_argument("--per-file-timeout", type=float, default=1800.0,
                        help="Per-file load timeout in seconds (default 1800)")

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    endpoint = os.environ.get("VIRTUOSO_SPARQL_URL")
    if not endpoint:
        parser.error("VIRTUOSO_SPARQL_URL must be set")
    # Allow the bulk-load tool to point at the auth-gated endpoint
    # explicitly via VIRTUOSO_SPARQL_AUTH_URL; falls back to swapping
    # /sparql → /sparql-auth on the configured URL.
    auth_endpoint = os.environ.get("VIRTUOSO_SPARQL_AUTH_URL")
    if not auth_endpoint:
        auth_endpoint = endpoint.replace("/sparql", "/sparql-auth", 1)

    dba_user = os.environ.get("VIRTUOSO_DBA_USER", "dba")
    dba_password = os.environ.get("VIRTUOSO_DBA_PASSWORD")
    if not dba_password:
        parser.error("VIRTUOSO_DBA_PASSWORD must be set")

    summary = load_directory(
        directory=args.dir,
        pattern=args.pattern,
        graph=args.graph,
        endpoint=auth_endpoint,
        auth=(dba_user, dba_password),
        mode=args.mode,
        url_prefix=args.url_prefix,
        per_file_timeout_s=args.per_file_timeout,
    )
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
