"""Regression tests for the graph-store write path of a graph replace.

Two invariants live here.

1. The Accept header. The sink's httpx client sets a client-wide
   ``Accept: application/sparql-results+json``, which is correct for
   /sparql-auth. /sparql-graph-crud-auth cannot produce that media type
   for a write and returns 406 rather than ignoring an Accept it cannot
   honour — before the body is even parsed, so payload size and content
   are irrelevant. That broke every bracketed graph-replace silently:
   the write failed on every run, the whole in-flight batch went to the
   dead-letter table, and the target graph was never actually replaced.
   Verified against prod Virtuoso — identical body, Accept ``*/*`` →
   200, Accept ``application/sparql-results+json`` → 406.

2. The staging/swap shape. A replace streams chunks into a staging
   graph and swaps it in with MOVE GRAPH at End. The live graph must
   never be written incrementally: the previous buffer-then-PUT design
   wrote a partial graph over the real one when the sink died
   mid-bracket (shared, 2026-09-06: graph/sanctions 60,250 → 21,646).
"""
# pylint: disable=protected-access,import-outside-toplevel
from collections import defaultdict
from unittest.mock import MagicMock, patch

import pytest

from virtuoso_sink.triples import Triple

GRAPH = "http://data.fontem.eu/graph/sanctions"
STAGING = f"{GRAPH}/_replace_staging"


@pytest.fixture(name="crud_sink")
def _crud_sink(monkeypatch):
    monkeypatch.setenv("VIRTUOSO_SPARQL_URL", "http://virtuoso.test:8890/sparql")
    from virtuoso_sink.sink import VirtuosoSink

    with patch("virtuoso_sink.sink.EventConsumer.__init__", lambda self, *a, **k: None):
        sink = VirtuosoSink.__new__(VirtuosoSink)
        sink.sparql_endpoint = "http://virtuoso.test:8890/sparql"
        sink.dba_user = "dba"
        sink.dba_password = "secret"
        sink.timeout = 30.0
        sink._open_brackets = defaultdict(list)
        sink._update_batch = 200
        sink._bracket_chunk = 3
        base = sink.sparql_endpoint.rstrip("/").removesuffix("/sparql")
        sink._update_url = f"{base}/sparql-auth"
        sink._crud_url = f"{base}/sparql-graph-crud-auth"
        sink._client = MagicMock()
        ok = MagicMock(raise_for_status=MagicMock(return_value=None))
        sink._client.post.return_value = ok
        sink._client.put.return_value = ok
    return sink


def _crud_posts(sink):
    """Graph-store calls only — /sparql-auth POSTs share the mock."""
    return [
        c for c in sink._client.post.call_args_list
        if c.args and c.args[0] == sink._crud_url
    ]


def _update_queries(sink):
    return [
        c.kwargs["data"]["query"] for c in sink._client.post.call_args_list
        if c.args and c.args[0] == sink._update_url
    ]


def test_stage_chunk_widens_accept(crud_sink):
    """The graph-store write must not inherit the client's
    SPARQL-results Accept."""
    crud_sink._stage_chunk(GRAPH, [Triple("http://s", "http://p", '"o"')])
    headers = _crud_posts(crud_sink)[0].kwargs["headers"]
    assert headers["Accept"] == "*/*"


def test_stage_chunk_still_declares_turtle(crud_sink):
    """Widening Accept must not disturb the request Content-Type —
    Virtuoso needs it to pick the turtle parser."""
    crud_sink._stage_chunk(GRAPH, [Triple("http://s", "http://p", '"o"')])
    headers = _crud_posts(crud_sink)[0].kwargs["headers"]
    assert headers["Content-Type"] == "text/turtle"


def test_stage_chunk_targets_staging_not_the_live_graph(crud_sink):
    """The live graph is only ever touched by the atomic swap."""
    crud_sink._stage_chunk(GRAPH, [Triple("http://s", "http://p", '"o"')])
    assert _crud_posts(crud_sink)[0].kwargs["params"] == {"graph": STAGING}


def _ev(event_type, payload=None):
    ev = MagicMock()
    ev.event_type = event_type
    ev.payload = payload or {"graph_iri": GRAPH}
    ev.domain = "sanctions"
    ev.iri = GRAPH
    ev.op = "control"
    return ev


def test_end_graph_replace_writes_with_widened_accept(crud_sink):
    """End-to-end through handle(): the bracket close is the only path
    that reaches the graph-store endpoint, and it is the path that was
    failing in prod every day."""
    crud_sink.handle([_ev("BeginGraphReplace"), _ev("EndGraphReplace")])
    assert all(
        c.kwargs["headers"]["Accept"] == "*/*" for c in _crud_posts(crud_sink)
    )


def test_begin_clears_staging_then_end_swaps_it_in(crud_sink):
    """Begin drops what an interrupted run abandoned; End moves the
    staged graph over the live one in a single operation."""
    crud_sink.handle([_ev("BeginGraphReplace"), _ev("EndGraphReplace")])
    queries = _update_queries(crud_sink)
    assert any(
        f"CLEAR SILENT GRAPH <{STAGING}>" in q for q in queries
    ), queries
    assert any(
        f"MOVE GRAPH <{STAGING}> TO <{GRAPH}>" in q for q in queries
    ), queries
    assert queries.index(next(q for q in queries if "CLEAR" in q)) < queries.index(
        next(q for q in queries if "MOVE" in q)
    )


def test_bracket_streams_in_chunks_rather_than_buffering(crud_sink, monkeypatch):
    """The whole point of staging: memory is bounded by
    VIRTUOSO_BRACKET_CHUNK, not by the size of the graph. With a chunk
    of 3 and 7 triples in flight, the sink must have flushed twice
    before End rather than holding all 7."""
    monkeypatch.setattr(
        "virtuoso_sink.sink.RENDERERS",
        {"UpsertSanction": lambda payload: [
            Triple(f"http://s/{payload['n']}", "http://p", '"o"'),
        ]},
    )
    batch = [_ev("BeginGraphReplace")]
    for i in range(7):
        e = _ev("UpsertSanction", {"n": i})
        e.op = "upsert"
        batch.append(e)
    crud_sink.handle(batch)

    staged_before_end = len(_crud_posts(crud_sink))
    assert staged_before_end == 2, f"expected 2 flushes of 3, got {staged_before_end}"
    assert not any("MOVE GRAPH" in q for q in _update_queries(crud_sink))

    crud_sink.handle([_ev("EndGraphReplace")])
    # The 1 remainder triple goes out with the close.
    assert len(_crud_posts(crud_sink)) == 3
    assert any("MOVE GRAPH" in q for q in _update_queries(crud_sink))
