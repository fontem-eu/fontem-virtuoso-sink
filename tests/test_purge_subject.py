"""PurgeSubject — deleting a subject no other event can name.

Every subject the sink writes goes through quote() with _IRI_SAFE.
That makes the encoded form a fixed point — quote(encoded) == encoded
— and there is no input quote() maps to a raw non-ASCII IRI. So a
subject written before 37af28e (2026-06-07) is unreachable: a Delete*
event naming it encodes to the LIVE subject and deletes that instead.

Shared holds 3,166 such subjects in graph/listing, each a stale twin
contradicting a live listing and never expiring.

The danger is the mirror of the need: this is the one path that skips
the encoding, so it is also the one path that could whole-subject
delete live data if pointed at an addressable IRI. The guard is what
these tests mostly pin.
"""
# pylint: disable=protected-access,import-outside-toplevel
from collections import defaultdict
from unittest.mock import MagicMock, patch

import pytest

RAW = "http://data.fontem.eu/id/Listing/BJÖRN.ST"
ENC = "http://data.fontem.eu/id/Listing/BJ%C3%96RN.ST"
G = "http://data.fontem.eu/graph/listing"


@pytest.fixture(name="sink")
def _sink(monkeypatch):
    monkeypatch.setenv("VIRTUOSO_SPARQL_URL", "http://virtuoso.test:8890/sparql")
    from virtuoso_sink.sink import VirtuosoSink

    with patch("virtuoso_sink.sink.EventConsumer.__init__", lambda self, *a, **k: None):
        s = VirtuosoSink.__new__(VirtuosoSink)
        s.sparql_endpoint = "http://virtuoso.test:8890/sparql"
        s.dba_user, s.dba_password, s.timeout = "dba", "secret", 30.0
        s._open_brackets = defaultdict(list)
        s._update_batch = 25
        s._bracket_chunk = 20000
        base = s.sparql_endpoint.rstrip("/").removesuffix("/sparql")
        s._update_url = f"{base}/sparql-auth"
        s._crud_url = f"{base}/sparql-graph-crud-auth"
        s._client = MagicMock()
        s._client.post.return_value = MagicMock(
            raise_for_status=MagicMock(return_value=None))
    return s


def _ev(subject, graph=G, reason="stranded by 37af28e"):
    ev = MagicMock()
    ev.event_type = "PurgeSubject"
    ev.payload = {
        "subject_iri": subject, "graph_iri": graph, "reason": reason,
    }
    ev.domain = "listing"
    ev.iri = subject
    ev.op = "control"
    return ev


def test_deletes_the_subject_verbatim(sink):
    """The raw IRI must reach Virtuoso unencoded — encoding it would
    name the live subject instead, which is the whole failure mode."""
    update = sink._purge_subject_update(_ev(RAW))
    assert f"<{RAW}>" in update
    assert "%C3%96" not in update, "encoded the very IRI it exists to reach"
    assert update.startswith("DELETE WHERE")
    assert f"GRAPH <{G}>" in update


def test_refuses_an_addressable_subject(sink):
    """The guard. An IRI quote() maps to itself can be deleted by a
    normal Delete* event, which applies the ordinary rules. Allowing it
    here would make a typo an unguarded whole-subject delete on live
    data."""
    with pytest.raises(ValueError, match="addressable"):
        sink._purge_subject_update(_ev(ENC))


def test_refuses_a_plain_ascii_subject(sink):
    """Same guard, the ordinary case: nothing about a UUID-keyed
    subject is unreachable."""
    ascii_iri = "http://data.fontem.eu/id/Company/b3a154e1-0646-516c-abb4-e8eee6bc9497"
    with pytest.raises(ValueError, match="addressable"):
        sink._purge_subject_update(_ev(ascii_iri))


def test_purge_flows_through_handle(sink):
    """End to end: handle() dispatches it and the update is posted."""
    sink.handle([_ev(RAW)])
    posted = [
        c.kwargs["data"]["query"] for c in sink._client.post.call_args_list
        if c.args and c.args[0] == sink._update_url
    ]
    assert any(f"<{RAW}>" in q and "DELETE WHERE" in q for q in posted), posted


def test_a_refused_purge_raises_rather_than_silently_skipping(sink):
    """It must reach the consumer as a failure. A purge that quietly
    did nothing would read as success in the log and leave the operator
    believing the cleanup ran."""
    with pytest.raises(ValueError):
        sink.handle([_ev(ENC)])
