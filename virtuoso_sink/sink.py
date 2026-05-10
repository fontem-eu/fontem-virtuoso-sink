"""Virtuoso sink — projects events.entity_events into Virtuoso.

Two write paths:

  1. Inside a ``BeginGraphReplace`` / ``EndGraphReplace`` bracket
     keyed by ``graph_iri``: accumulate triples, then flush as a
     single PUT replace at end. Preserves the bulk-snapshot
     semantics the Neo4j-era loaders had.

  2. Outside a bracket (consolidator outputs, per-entity drift
     fixes): one SPARQL UPDATE per event, INSERT-only by default.
     Delete events translate to DELETE WHERE { <iri> ?p ?o }.

Auth: HTTP Digest against /sparql-graph-crud-auth (PUT) and
/sparql-auth (UPDATE). Same pattern as RdfFilingsWriter.
"""
from __future__ import annotations

import logging
import os
from collections import defaultdict
from typing import Iterable

import httpx
from gmr_event_schemas import EventEnvelope
from gmr_events import EventConsumer

from .triples import RENDERERS, Triple, to_turtle

logger = logging.getLogger(__name__)


class VirtuosoSink(EventConsumer):
    """Subclass of the gmr-events EventConsumer base class."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sparql_endpoint = os.environ["VIRTUOSO_SPARQL_URL"]
        self.dba_user = os.environ.get("VIRTUOSO_DBA_USER", "dba")
        self.dba_password = os.environ["VIRTUOSO_DBA_PASSWORD"]
        self.timeout = float(os.environ.get("VIRTUOSO_HTTP_TIMEOUT", "1800"))
        # Bracket state is sink-instance scope, NOT per-handle().
        # A single Begin/EndGraphReplace bracket can span many
        # handle() calls because batch_size caps each fetch.
        self._open_brackets: dict[str, list[Triple]] = defaultdict(list)

        # One Client + one DigestAuth for the sink's lifetime: keepalive
        # the TCP connection and cache the digest challenge after the
        # first 401. Prior code recreated both per request, so every
        # event paid a TCP handshake + a 401-challenge round-trip; that
        # capped throughput at ~17 evt/s in production replay.
        base = self.sparql_endpoint.rstrip("/").removesuffix("/sparql")
        self._update_url = f"{base}/sparql-auth"
        self._crud_url = f"{base}/sparql-graph-crud-auth"
        self._client = httpx.Client(
            timeout=self.timeout,
            auth=httpx.DigestAuth(self.dba_user, self.dba_password),
            headers={"Accept": "application/sparql-results+json"},
        )

    def close(self) -> None:
        self._client.close()

    # ── EventConsumer hook ────────────────────────────────

    def handle(self, batch: list[EventEnvelope]) -> None:
        """Walk events left-to-right, group by Begin/End bracket
        per graph_iri, emit either a bulk PUT (closed bracket)
        or a per-event SPARQL UPDATE (no bracket open).

        Brackets persist across handle() calls — see __init__.
        """
        open_brackets = self._open_brackets

        for ev in batch:
            if ev.event_type == "BeginGraphReplace":
                graph = ev.payload["graph_iri"]
                # Open or reset the bracket. Re-opening with the
                # same key wipes any half-buffered state from a
                # prior crash window.
                open_brackets[graph] = []
                logger.info("bracket-begin %s", graph)
                continue

            if ev.event_type == "EndGraphReplace":
                graph = ev.payload["graph_iri"]
                triples = open_brackets.pop(graph, None)
                if triples is None:
                    logger.warning(
                        "EndGraphReplace for %s without matching Begin; "
                        "treating as no-op", graph,
                    )
                    continue
                self._put_replace(graph, triples)
                continue

            renderer = RENDERERS.get(ev.event_type)
            if renderer is None:
                logger.debug("ignoring %s (no renderer)", ev.event_type)
                continue

            triples = renderer(ev.payload)
            if not triples:
                continue

            # Inside a bracket? Determine which one. We expect
            # one bracket open per domain at a time; events
            # carry domain so we can't naively pick a bracket.
            # Convention: the producer asserts that the events
            # between Begin(graph_X) and End(graph_X) are all
            # destined for graph_X. We pick the one open bracket
            # (if any) whose domain matches the event's domain.
            bracket_graph = self._find_open_bracket_for_domain(
                open_brackets, ev.domain,
            )
            if bracket_graph is not None:
                open_brackets[bracket_graph].extend(triples)
                continue

            # No bracket → per-event update.
            self._sparql_update(ev, triples)

        # Any brackets still open at end-of-batch are stashed
        # for the next call — they'll close cleanly when the
        # producer's End event lands. The consumer offset has
        # NOT advanced past those events yet, so a crash
        # mid-bracket re-reads them on resume.

    # ── implementation ────────────────────────────────────

    @staticmethod
    def _find_open_bracket_for_domain(
        brackets: dict[str, list[Triple]], domain: str,
    ) -> str | None:
        # Heuristic: the graph IRI typically ends with the
        # domain name (e.g. ".../graph/sanctions"). If we have
        # multiple open brackets we pick the one whose IRI
        # contains the domain; otherwise None.
        candidates = [g for g in brackets if domain in g]
        if len(candidates) == 1:
            return candidates[0]
        # Fall back: if there's only one open bracket overall,
        # use it. Multiple brackets in flight is a producer bug
        # (we deliberately serialise per-domain emit).
        if len(brackets) == 1:
            return next(iter(brackets))
        return None

    def _put_replace(self, graph_iri: str, triples: list[Triple]) -> None:
        body = to_turtle(triples)
        r = self._client.put(
            self._crud_url,
            params={"graph": graph_iri},
            content=body,
            headers={"Content-Type": "text/turtle"},
        )
        r.raise_for_status()
        logger.info(
            "put-replace <%s>: %d triples (%d bytes)",
            graph_iri, len(triples), len(body),
        )

    def _sparql_update(self, ev: EventEnvelope, triples: list[Triple]) -> None:
        # No bracket → infer the target graph from the event's
        # domain. For now we use the same name->graph convention
        # as the existing migrate script.
        graph_iri = self._domain_default_graph(ev.domain)
        if ev.op == "delete":
            update = (
                f"DELETE WHERE {{ GRAPH <{graph_iri}> "
                f"{{ <{ev.iri}> ?p ?o }} }}"
            )
        else:
            # Upsert: delete the existing subject, then insert
            # the new triples. Within one transaction.
            triples_ttl = to_turtle(triples).rstrip()
            update = (
                f"DELETE WHERE {{ GRAPH <{graph_iri}> "
                f"{{ <{ev.iri}> ?p ?o }} }} ; "
                f"INSERT DATA {{ GRAPH <{graph_iri}> {{ {triples_ttl} }} }}"
            )
        r = self._client.post(self._update_url, data={"query": update})
        # SPARQL endpoint accepts updates via ?query= too,
        # but Virtuoso prefers the dedicated /sparql-auth.
        r.raise_for_status()
        logger.debug(
            "sparql-update %s on <%s>: %d triples",
            ev.event_type, graph_iri, len(triples),
        )

    @staticmethod
    def _domain_default_graph(domain: str) -> str:
        # Convention: domain → graph IRI. Override in producers
        # by emitting a Begin/End bracket; this is the fallback
        # for non-bracketed (per-entity) events.
        return f"http://data.fontem.eu/graph/{domain}"
