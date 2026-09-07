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
    ADDITIVE_EVENTS, OWL_SAME_AS, RENDERERS, RETRACTION_EVENTS,
    SCOPED_REPLACE_PREDICATES, Triple, rollup_scoped_predicates,
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


# Predicates a whole-subject replace must leave alone because a different
# event stream owns them. Keep this minimal — anything listed here stops
# being cleaned up by the producer that writes the rest of the subject.
_PRESERVED_ON_REPLACE: tuple[str, ...] = (OWL_SAME_AS,)

# The characters a subject IRI may carry unencoded. Every subject the
# sink writes goes through quote() with this set, which makes the
# encoded form a fixed point: quote(encoded) == encoded, and no input
# maps to a raw non-ASCII IRI. That is why a subject written before
# 37af28e (2026-06-07) cannot be named by any Delete* event, and why
# PurgeSubject exists.
_IRI_SAFE = "%:/?#[]@!$&\'()*+,;=._-~"


def _delete_clause(
    g_iri: str, s_iri: str, event_type: str, payload: dict | None = None,
) -> str:
    """The DELETE half of an upsert UPDATE. A scoped-replace event clears
    only its enrichment predicate(s) for the subject so the subject's
    other triples survive; every other event wipes the whole subject.

    `payload` is consulted for scopes that depend on the event's CONTENT
    rather than its type — a contract value-collapse rollup arrives as an
    UpsertContract exactly like a full one, and wiping the subject for it
    would delete the contract to write two fields.
    """
    if payload is not None:
        scoped = rollup_scoped_predicates(event_type, payload)
        if scoped:
            return "".join(
                f"DELETE WHERE {{ GRAPH <{g_iri}> {{ <{s_iri}> <{pred}> ?o }} }} ; "
                for pred in scoped
            )
    if event_type in ADDITIVE_EVENTS:
        # Nothing is cleared: the event states one discrete fact that
        # accumulates alongside the subject's others. Deleting first is
        # what made a subject able to hold only one owl:sameAs.
        return ""
    scoped = SCOPED_REPLACE_PREDICATES.get(event_type)
    if scoped:
        return "".join(
            f"DELETE WHERE {{ GRAPH <{g_iri}> {{ <{s_iri}> <{pred}> ?o }} }} ; "
            for pred in scoped
        )
    # Whole-subject replace, minus the predicates another producer owns.
    #
    # An Upsert carries the entity's full description, so replacing the
    # subject wholesale is right for everything the ETL asserts. It is
    # wrong for owl:sameAs, which the consolidator asserts about the same
    # subject from a different event stream: a plain wipe deletes an
    # equivalence the upsert knows nothing about and was never asked to
    # retract.
    #
    # That is the other half of the 2026-09-02 incident. Scoping
    # AssertSameAs stopped it destroying company attributes, but upserts
    # flow continuously from the ETL and kept destroying the equivalences,
    # so only ~15% of emitted AssertSameAs survived as triples. Both
    # directions have to be fixed or the two streams still overwrite each
    # other, just more slowly.
    #
    # Staleness is still handled: AssertSameAs is itself a scoped replace,
    # so a re-consolidation that finds fewer matches clears the subject's
    # previous set and writes the new one. Genuine entity deletion is
    # unaffected — ev.op == "delete" takes its own full wipe above and
    # never reaches here.
    preserved = " ".join(f"FILTER(?p != <{pred}>)" for pred in _PRESERVED_ON_REPLACE)
    return (
        f"DELETE {{ GRAPH <{g_iri}> {{ <{s_iri}> ?p ?o }} }} "
        f"WHERE {{ GRAPH <{g_iri}> {{ <{s_iri}> ?p ?o . {preserved} }} }} ; "
    )


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

        # How many per-event updates go in one HTTP request. The sink
        # used to send one request per event, which made a full replay
        # round-trip-bound rather than Virtuoso-bound.
        #
        # 25, not the 200 this started at. The updates are joined with
        # `;` and Virtuoso runs the whole request as ONE transaction, so
        # the batch size is also the transaction size: 200 subjects'
        # worth of locks and undo held until commit. Measured on the
        # shared replay against identical events (seq 89,000 onward,
        # from a freshly restarted Virtuoso each time):
        #
        #   batch 200 -> 320 events/s, +0.038 MiB anon per event
        #   batch  25 -> 548 events/s, +0.032 MiB anon per event
        #
        # Bigger batches are both slower and hungrier here. The earlier
        # assumption that batching wins on round trips stops holding
        # once Virtuoso, not the network, is the bottleneck.
        self._update_batch = int(os.environ.get("VIRTUOSO_UPDATE_BATCH", "25"))

        # Triples held in memory before a graph-replace bracket flushes a
        # chunk to its staging graph. Bounds the sink's memory to this
        # many triples rather than to the size of the largest graph in
        # the log — the edgar bracket alone spans 66,971 events.
        self._bracket_chunk = int(
            os.environ.get("VIRTUOSO_BRACKET_CHUNK", "20000")
        )

        # One Client + one DigestAuth for the sink's lifetime: keepalive
        # the TCP connection and cache the digest challenge after the
        # first 401. Prior code recreated both per request, so every
        # event paid a TCP handshake + a 401-challenge round-trip; that
        # capped throughput at ~17 evt/s in production replay.
        base = self.sparql_endpoint.rstrip("/").removesuffix("/sparql")
        self._update_url = f"{base}/sparql-auth"
        self._crud_url = f"{base}/sparql-graph-crud-auth"
        # This Accept is right for /sparql-auth, which answers in
        # SPARQL results JSON. It is wrong for /sparql-graph-crud-auth,
        # which answers a graph-store write with a plain status document
        # and returns 406 rather than ignore an Accept it cannot honour.
        # _stage_chunk overrides it; see the note there before removing
        # that override.
        self._client = httpx.Client(
            timeout=self.timeout,
            auth=httpx.DigestAuth(self.dba_user, self.dba_password),
            headers={"Accept": "application/sparql-results+json"},
        )

    def close(self) -> None:
        self._client.close()

    def is_retryable(self, exc: Exception) -> bool:
        """Is this Virtuoso being unavailable rather than a bad event?

        The consumer skips an event that fails max_attempts times in a
        row, which is right for bad data and wrong for an outage: on
        2026-09-06 Virtuoso was OOM-killed during a shared replay and
        the sink skipped 4,006 events on "Connection refused". Nothing
        re-emits a skipped event, so those triples were simply gone.

        Everything here is a property of the transport or the server's
        health, never of the payload: the same request succeeds once
        Virtuoso is back. A 4xx other than 429 is NOT retryable — that
        is Virtuoso rejecting this particular update, which is exactly
        the poison case the skip exists for.
        """
        if isinstance(exc, (httpx.TransportError, httpx.StreamError)):
            return True
        if isinstance(exc, httpx.HTTPStatusError):
            code = exc.response.status_code
            return code == 429 or 500 <= code < 600
        return False

    # ── EventConsumer hook ────────────────────────────────

    def _handle_bracket_event(
        self,
        ev: EventEnvelope,
        open_brackets: dict[str, list[Triple]],
        pending: list[str],
    ) -> None:
        """Open or close a graph-replace bracket.

        Split out of handle() to keep it readable: the bracket lifecycle
        is its own concern and the walk is easier to follow without it.
        """
        graph = ev.payload["graph_iri"]
        if ev.event_type == "BeginGraphReplace":
            # Open or reset the bracket. Re-opening with the same key
            # wipes any half-buffered state from a prior crash window,
            # and clearing staging drops whatever an interrupted run
            # left behind there.
            open_brackets[graph] = []
            self._clear_staging(graph)
            logger.info("bracket-begin %s", graph)
            return

        triples = open_brackets.pop(graph, None)
        if triples is None:
            logger.warning(
                "EndGraphReplace for %s without matching Begin; "
                "treating as no-op", graph,
            )
            return
        # Ordering: buffered per-event updates must land BEFORE the
        # graph is replaced, or one of them would be silently discarded.
        self._post_updates(pending)
        pending.clear()
        self._stage_chunk(graph, triples)
        self._swap_staging_in(graph)

    def handle(self, batch: list[EventEnvelope]) -> None:
        """Walk events left-to-right, group by Begin/End bracket
        per graph_iri, emit either a bulk PUT (closed bracket)
        or a per-event SPARQL UPDATE (no bracket open).

        Brackets persist across handle() calls — see __init__.
        """
        open_brackets = self._open_brackets
        # Per-event updates are accumulated and sent in groups; see
        # _post_updates. `pending` must be flushed before anything that
        # writes by another route (a bracket PUT), or a buffered update
        # would land AFTER a replace that was meant to precede it.
        pending: list[str] = []

        for ev in batch:
            if ev.event_type in ("BeginGraphReplace", "EndGraphReplace"):
                self._handle_bracket_event(ev, open_brackets, pending)
                continue

            if ev.event_type == "PurgeSubject":
                self._queue(pending, self._purge_subject_update(ev))
                continue

            renderer = RENDERERS.get(ev.event_type)
            if renderer is None:
                logger.debug("ignoring %s (no renderer)", ev.event_type)
                continue

            triples = renderer(ev.payload)
            if not triples:
                continue

            if not self._accumulate_into_bracket(ev, triples, open_brackets):
                self._queue(pending, self._build_update(ev, triples))

        self._post_updates(pending)
        pending.clear()

        # Any brackets still open at end-of-batch are stashed
        # for the next call — they'll close cleanly when the
        # producer's End event lands. The consumer offset has
        # NOT advanced past those events yet, so a crash
        # mid-bracket re-reads them on resume.

    def _queue(self, pending: list[str], update: str) -> None:
        """Add one update to the pending group, flushing when full.

        Extracted so the three call sites cannot drift on the flush
        threshold, and so handle() stays a routing table rather than a
        routing table with batching interleaved.
        """
        pending.append(update)
        if len(pending) >= self._update_batch:
            self._post_updates(pending)
            pending.clear()

    def _accumulate_into_bracket(
        self, ev: EventEnvelope, triples: list[Triple],
        open_brackets: dict[str, list[Triple]],
    ) -> bool:
        """Buffer triples into an open bracket. False when none applies.

        We expect one bracket open per domain at a time; events carry
        domain so we cannot naively pick a bracket. Convention: the
        producer asserts that everything between Begin(graph_X) and
        End(graph_X) is destined for graph_X, so we take the one open
        bracket whose IRI matches the event's domain.
        """
        bracket_graph = self._find_open_bracket_for_domain(
            open_brackets, ev.domain,
        )
        if bracket_graph is None:
            return False
        buf = open_brackets[bracket_graph]
        buf.extend(triples)
        if len(buf) >= self._bracket_chunk:
            self._stage_chunk(bracket_graph, buf)
            buf.clear()
        return True

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

    @staticmethod
    def _staging_graph(graph_iri: str) -> str:
        """Where a graph-replace accumulates before it is swapped in."""
        return f"{graph_iri.rstrip('/')}/_replace_staging"

    def _stage_chunk(self, graph_iri: str, triples: list[Triple]) -> None:
        """APPEND a chunk of a graph-replace to its staging graph.

        POST is merge semantics in the graph-store protocol, so chunks
        accumulate. This is what keeps a bracket's memory bounded: the
        sink holds at most VIRTUOSO_BRACKET_CHUNK triples instead of the
        whole graph.
        """
        if not triples:
            return
        body = to_turtle(triples)
        r = self._client.post(
            self._crud_url,
            params={"graph": self._staging_graph(graph_iri)},
            content=body,
            headers={"Content-Type": "text/turtle", "Accept": "*/*"},
        )
        r.raise_for_status()
        logger.debug(
            "stage-chunk <%s>: %d triples (%d bytes)",
            graph_iri, len(triples), len(body),
        )

    def _clear_staging(self, graph_iri: str) -> None:
        """Drop whatever a previous, interrupted run left staged."""
        staging = self._staging_graph(graph_iri)
        r = self._client.post(
            self._update_url,
            data={"query": _BIG_DATA_CONST_OVERRIDE
                  + f"CLEAR SILENT GRAPH <{staging}>"},
        )
        r.raise_for_status()

    def _swap_staging_in(self, graph_iri: str) -> None:
        """Replace the live graph with the staged one, atomically.

        MOVE GRAPH is a single SPARQL 1.1 operation: it replaces the
        destination with the source and drops the source. That matters
        more than the memory saving. The old code accumulated the whole
        graph in RAM and PUT it in one request, and a PUT that dies
        partway leaves a PARTIAL graph written over the real one —
        silently, because a half-finished replace looks like a
        successful small one. The sink WAS OOM-killed mid-bracket
        replaying shared on 2026-09-06, so the window was real.

        (An earlier version of this comment claimed that incident took
        graph/sanctions from 60,250 triples to 21,646. It did not.
        21,646 is the exact output of the FIRST sanctions bracket,
        seq 1..1588 — reproduced to the triple on a clean replay. The
        graph was small because the interrupted replay stopped at seq
        264,006, long before the final bracket at seq 7,217,131 that
        produces 60,250. The hazard below is structural, not something
        we have caught in the act.)

        Staging inverts the failure: a crash leaves the live graph
        untouched and abandons a partial staging graph, which the next
        Begin clears.
        """
        staging = self._staging_graph(graph_iri)
        self._carry_over_preserved(graph_iri, staging)
        r = self._client.post(
            self._update_url,
            data={"query": _BIG_DATA_CONST_OVERRIDE
                  + f"MOVE GRAPH <{staging}> TO <{graph_iri}>"},
        )
        r.raise_for_status()
        logger.info("swap-in <%s> from staging", graph_iri)

    def _carry_over_preserved(self, graph_iri: str, staging: str) -> None:
        """Copy predicates another producer owns into staging before the swap.

        _PRESERVED_ON_REPLACE keeps a whole-SUBJECT replace from deleting
        predicates a different event stream writes. A whole-GRAPH replace
        needs the same protection and never had it: MOVE GRAPH replaces
        everything, so the bulk loader silently destroyed the
        consolidator's owl:sameAs every time it reloaded.

        Measured on shared after the 2026-09-06 full replay: of 38
        AssertSameAs events routed to graph/sanctions, only the 6
        asserted after the last EndGraphReplace survived. The other 32
        were written, then wiped by the sanctions bracket at seq
        7,217,131. The same exposure applies to financials/edgar and
        financials/esef — every bracketed graph.

        Carrying them into staging before the MOVE keeps the swap
        atomic: no window exists where the live graph lacks them.
        """
        values = ", ".join(f"<{p}>" for p in _PRESERVED_ON_REPLACE)
        r = self._client.post(
            self._update_url,
            data={"query": _BIG_DATA_CONST_OVERRIDE + f"""
INSERT {{ GRAPH <{staging}> {{ ?s ?p ?o }} }}
WHERE {{ GRAPH <{graph_iri}> {{ ?s ?p ?o }} FILTER(?p IN ({values})) }}
"""},
        )
        r.raise_for_status()

    def _purge_subject_update(self, ev: EventEnvelope) -> str:
        """DELETE one subject, using its IRI exactly as given.

        Every other write path percent-encodes the subject. That is what
        makes this event necessary and what makes it dangerous, so the
        IRI is passed through untouched here and guarded instead.

        The guard: refuse any subject the normal path could address. If
        quote(iri) == iri then a Delete* event can name that subject and
        should be used, because it goes through the ordinary rules —
        stale-twin cleanup, preserved predicates, the lot. Only a
        subject quote() can never produce is legitimately unreachable,
        and only those may be purged. Without this, a typo'd PurgeSubject
        would be an unguarded whole-subject delete on live data.
        """
        from urllib.parse import quote  # pylint: disable=import-outside-toplevel
        subject = ev.payload["subject_iri"]
        graph = ev.payload["graph_iri"]
        if quote(subject, safe=_IRI_SAFE) == subject:
            raise ValueError(
                f"PurgeSubject refused for <{subject}>: this subject is "
                "addressable by the normal write path, so a Delete* event "
                "should remove it. PurgeSubject is only for subjects "
                "percent-encoding can never produce."
            )
        logger.info(
            "purge-subject <%s> from <%s>: %s",
            subject, graph, ev.payload.get("reason", "(no reason given)"),
        )
        g_iri = quote(graph, safe=_IRI_SAFE)
        return (
            f"DELETE WHERE {{ GRAPH <{g_iri}> {{ <{subject}> ?p ?o }} }}"
        )

    def _build_update(self, ev: EventEnvelope, triples: list[Triple]) -> str:
        # No bracket → infer the target graph from the event's
        # domain. For now we use the same name->graph convention
        # as the existing migrate script.
        graph_iri = self._domain_default_graph(ev.domain)
        # Translations live in a SEPARATE graph from the entity itself.
        # An UpsertAuthority wipe-and-replaces the authority subject in
        # graph/authority; routing the machine-translated skos:altLabels
        # to graph/authority-i18n keeps them out of that blast radius, so
        # a re-loaded authority never loses its translations. The scoped
        # replace below still clears only skos:altLabel within this graph.
        if ev.event_type == "TranslateAuthorityName":
            graph_iri = "http://data.fontem.eu/graph/authority-i18n"
        # Percent-encode the subject IRI so non-ASCII characters
        # (Greek company names, Cyrillic listings, etc.) don't crash
        # Virtuoso's SPARQL parser. Same reasoning as _iri() in
        # triples.py — Virtuoso doesn't fully implement RFC 3987.
        from urllib.parse import quote  # pylint: disable=import-outside-toplevel
        _safe = _IRI_SAFE
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
        elif ev.event_type in RETRACTION_EVENTS:
            # A retraction removes exactly the triples it renders and
            # touches nothing else. DELETE DATA on an absent triple is a
            # no-op in SPARQL, so retracting twice, or retracting a
            # direction that was never written, is harmless.
            triples_ttl = to_turtle(triples).rstrip()
            update = f"DELETE DATA {{ GRAPH <{g_iri}> {{ {triples_ttl} }} }}"
        else:
            # Upsert: delete then insert, in one transaction. Scoped-
            # replace events clear only their enrichment predicate(s);
            # additive events clear nothing; everything else wipes the
            # whole subject (see _delete_clause).
            triples_ttl = to_turtle(triples).rstrip()
            update = (
                extra_cleanup
                + _delete_clause(g_iri, s_iri, ev.event_type, ev.payload)
                + f"INSERT DATA {{ GRAPH <{g_iri}> {{ {triples_ttl} }} }}"
            )
        logger.debug(
            "sparql-update %s on <%s>: %d triples",
            ev.event_type, graph_iri, len(triples),
        )
        return update

    def _post_updates(self, updates: list[str]) -> None:
        """Send N per-event updates as ONE request.

        The sink did one HTTP round trip per event. Measured on the
        shared replay: ~133 events/s, ~7.5s per 1000-event consumer
        batch, essentially all of it round-trip latency. At that rate a
        full prod replay of 66.3M events is ~5.75 days.

        SPARQL update requests are a `;`-separated sequence applied in
        order, which is exactly the per-event semantics already relied
        on — a later event's whole-subject replace must win over an
        earlier one — so concatenating preserves it.

        On failure the group is re-applied one at a time. A batch that
        fails tells you nothing about WHICH event was bad, and the
        consumer's dead-letter records a seq; isolating keeps that
        precise instead of poisoning 199 good events with one bad one.
        """
        if not updates:
            return
        joined = " ;\n".join(u.strip().rstrip(";").strip() for u in updates)
        try:
            self._post_one(joined)
            return
        except Exception as exc:  # pylint: disable=broad-exception-caught
            if self.is_retryable(exc):
                # Virtuoso is down. Isolating would just make N more
                # failed connections and learn nothing; the consumer
                # holds the offset and replays this batch intact.
                raise
            logger.warning(
                "batched update of %d failed; retrying individually to "
                "isolate the offending event", len(updates),
            )
        for update in updates:
            self._post_one(update)

    def _post_one(self, update: str) -> None:
        r = self._client.post(
            self._update_url,
            data={"query": _BIG_DATA_CONST_OVERRIDE + update},
        )
        # SPARQL endpoint accepts updates via ?query= too,
        # but Virtuoso prefers the dedicated /sparql-auth.
        r.raise_for_status()

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
