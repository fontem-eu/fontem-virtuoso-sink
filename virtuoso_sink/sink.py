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

import httpx
from fontem_event_schemas import EventEnvelope
from fontem_events import EventConsumer

from .triples import (
    RENDERERS, SCOPED_REPLACE_PREDICATES, Triple,
    contract_notice_subject, to_turtle,
)

logger = logging.getLogger(__name__)


# Virtuoso's /sparql-auth endpoint silently prepends
# ``define sql:big-data-const 0`` before any UPDATE we send. That
# variant of the inline-constant path consults the RDF_OBJ hash
# cache for large literal/IRI hashes, and entities whose previous
# write left stale hash-cache entries blow up with SR580 ("RDF box
# refers to row with RO_ID = X of table RDF_OBJ, but no such row in
# the table"). Each SR580 leaves a dirty hash entry behind; under
# the sink's write rate they accumulate until the Virtuoso process
# OOM-kills. Setting the directive back to 1 forces the
# fresh-insertion path that doesn't touch the cache. The endpoint's
# prepend goes first; ours lands after; Virtuoso honours the last
# define for any given directive. Mirrors the same fix already
# applied in fontem-api's wikidata_writer.
_BIG_DATA_CONST_OVERRIDE = "define sql:big-data-const 1\n"


def _stale_entity_subject(ev) -> "str | None":
    """The opposite-label subject IRI to drop so a Company/InvestmentFund
    relabel converges (an entity has exactly one subject). Returns None
    when the event doesn't move a label."""
    gmr = ev.payload.get("gmr_id")
    if not gmr:
        return None
    if ev.event_type == "UpsertInvestmentFund":
        return f"http://data.fontem.eu/id/Company/{gmr}"
    if ev.event_type == "UpsertCompany" and ev.payload.get("entity_kind"):
        # GENERAL reverts a fund -> drop the InvestmentFund subject.
        # FUND is rendered at the InvestmentFund subject while ev.iri is
        # still Company (dropped by the main DELETE); we also refresh the
        # InvestmentFund subject cleanly first -> same stale target.
        return f"http://data.fontem.eu/id/InvestmentFund/{gmr}"
    return None


def _delete_clause(g_iri: str, s_iri: str, event_type: str) -> str:
    """The DELETE half of an upsert UPDATE. A scoped-replace event clears
    only its enrichment predicate(s) for the subject so the subject's
    other triples survive; every other event wipes the whole subject."""
    scoped = SCOPED_REPLACE_PREDICATES.get(event_type)
    if scoped:
        return "".join(
            f"DELETE WHERE {{ GRAPH <{g_iri}> {{ <{s_iri}> <{pred}> ?o }} }} ; "
            for pred in scoped
        )
    return f"DELETE WHERE {{ GRAPH <{g_iri}> {{ <{s_iri}> ?p ?o }} }} ; "


class VirtuosoSink(EventConsumer):  # pylint: disable=too-many-instance-attributes
    """Subclass of the gmr-events EventConsumer base class.

    Holds the 8 connection knobs Virtuoso requires (sparql endpoint,
    DBA user/pwd, HTTP timeout, max retry count, batch byte cap, the
    httpx client + the stream-load tempdir) — none are mergeable into
    a smaller surface without introducing a config dataclass that just
    renames the same fields.
    """

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
        # Percent-encode the subject IRI so non-ASCII characters
        # (Greek company names, Cyrillic listings, etc.) don't crash
        # Virtuoso's SPARQL parser. Same reasoning as _iri() in
        # triples.py — Virtuoso doesn't fully implement RFC 3987.
        from urllib.parse import quote  # pylint: disable=import-outside-toplevel
        _safe = "%:/?#[]@!$&\'()*+,;=._-~"
        # Notice-grain contracts (contract_key present): the event's
        # wipe-and-replace identity is the Notice subject, not ev.iri.
        # The renderer's monotone Contract-identity triples ride in the
        # INSERT DATA below but are never in the DELETE's scope — the
        # Contract subject aggregates many notices and must NOT be wiped
        # by any single notice's upsert (each notice would destroy the
        # other notices' contributions).
        if ev.op != "delete" and ev.event_type == "UpsertContract":
            s_iri = quote(contract_notice_subject(ev.payload) or ev.iri,
                          safe=_safe)
        else:
            s_iri = quote(ev.iri, safe=_safe)
        g_iri = quote(graph_iri, safe=_safe)
        extra_cleanup = ""
        # Relabel convergence: an entity has ONE subject IRI, chosen from
        # its label. When the label changes (or on any UpsertCompany that
        # states a kind), drop the opposite subject in the same update so
        # replays and reverts converge. UpsertInvestmentFund is emitted at
        # the InvestmentFund subject -> drop Company; an UpsertCompany that
        # states entity_kind is emitted at whichever subject the renderer
        # chose (ev.iri, already DELETE'd below) -> drop InvestmentFund.
        stale_iri = _stale_entity_subject(ev)
        if stale_iri:
            stale_q = quote(stale_iri, safe=_safe)
            comp_g = quote(self._domain_default_graph("company"), safe=_safe)
            extra_cleanup = (
                f"DELETE WHERE {{ GRAPH <{comp_g}> "
                f"{{ <{stale_q}> ?p ?o }} }} ; "
            )
        if ev.op == "delete":
            update = (
                f"DELETE WHERE {{ GRAPH <{g_iri}> "
                f"{{ <{s_iri}> ?p ?o }} }}"
            )
        else:
            # Upsert: delete then insert, in one transaction. Scoped-
            # replace events clear only their enrichment predicate(s);
            # everything else wipes the whole subject (see _delete_clause).
            triples_ttl = to_turtle(triples).rstrip()
            update = (
                extra_cleanup
                + _delete_clause(g_iri, s_iri, ev.event_type)
                + f"INSERT DATA {{ GRAPH <{g_iri}> {{ {triples_ttl} }} }}"
            )
        r = self._client.post(
            self._update_url,
            data={"query": _BIG_DATA_CONST_OVERRIDE + update},
        )
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
        #
        # Company and InvestmentFund are ONE corporate identity space:
        # the subtype lives in the subject IRI (.../id/Company/<gmr> vs
        # .../id/InvestmentFund/<gmr>), not the graph. The retired "fund"
        # domain (legacy UpsertInvestmentFund, #270 dropped the producer)
        # therefore routes to the company graph too, so a relabel — and a
        # full replay of those historical fund events — converges to a
        # single subject in a single graph instead of leaving a stale twin
        # in graph/fund. The stale-subject cleanup already targets the
        # company graph, so both directions stay convergent. (#270)
        if domain == "fund":
            domain = "company"
        return f"http://data.fontem.eu/graph/{domain}"
