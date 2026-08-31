"""Regression test for the Accept header on the graph-store endpoint.

The sink's httpx client sets a client-wide
``Accept: application/sparql-results+json``, which is correct for
/sparql-auth. /sparql-graph-crud-auth cannot produce that media type
for a write and returns 406 rather than ignoring an Accept it cannot
honour — before the body is even parsed, so payload size and content
are irrelevant.

That broke every bracketed graph-replace silently: the PUT failed on
every run, the whole in-flight batch went to the dead-letter table, and
the target graph was never actually replaced. Verified against prod
Virtuoso — identical body, Accept ``*/*`` → 200, Accept
``application/sparql-results+json`` → 406.
"""
# pylint: disable=protected-access,import-outside-toplevel
from collections import defaultdict
from unittest.mock import MagicMock, patch

import pytest

from virtuoso_sink.triples import Triple


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
        base = sink.sparql_endpoint.rstrip("/").removesuffix("/sparql")
        sink._update_url = f"{base}/sparql-auth"
        sink._crud_url = f"{base}/sparql-graph-crud-auth"
        sink._client = MagicMock()
        sink._client.put.return_value = MagicMock(
            raise_for_status=MagicMock(return_value=None),
        )
    return sink


def test_put_replace_widens_accept(crud_sink):
    """The PUT must not inherit the client's SPARQL-results Accept."""
    crud_sink._put_replace(
        "http://data.fontem.eu/graph/sanctions",
        [Triple("http://s", "http://p", '"o"')],
    )
    headers = crud_sink._client.put.call_args.kwargs["headers"]
    assert headers["Accept"] == "*/*"


def test_put_replace_still_declares_turtle(crud_sink):
    """Widening Accept must not disturb the request Content-Type —
    Virtuoso needs it to pick the turtle parser."""
    crud_sink._put_replace(
        "http://data.fontem.eu/graph/sanctions",
        [Triple("http://s", "http://p", '"o"')],
    )
    headers = crud_sink._client.put.call_args.kwargs["headers"]
    assert headers["Content-Type"] == "text/turtle"


def test_end_graph_replace_puts_with_widened_accept(crud_sink):
    """End-to-end through handle(): the bracket close is the only path
    that reaches the graph-store endpoint, and it is the path that was
    failing in prod every day."""
    graph = "http://data.fontem.eu/graph/sanctions"

    def _ev(event_type, payload=None):
        ev = MagicMock()
        ev.event_type = event_type
        ev.payload = payload or {"graph_iri": graph}
        ev.domain = "sanctions"
        ev.iri = graph
        ev.op = "control"
        return ev

    crud_sink.handle([_ev("BeginGraphReplace"), _ev("EndGraphReplace")])
    crud_sink._client.put.assert_called_once()
    assert crud_sink._client.put.call_args.kwargs["headers"]["Accept"] == "*/*"
