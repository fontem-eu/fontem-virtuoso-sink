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


def _ev(subject, graph=G, reason="stranded by 37af28e",
        only_predicates=None):
    ev = MagicMock()
    ev.event_type = "PurgeSubject"
    ev.payload = {
        "subject_iri": subject, "graph_iri": graph, "reason": reason,
    }
    if only_predicates is not None:
        ev.payload["only_predicates"] = only_predicates
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


def test_refuses_a_producible_iri_with_no_evidence(sink):
    """The guard. An IRI quote() maps to itself may well belong to a
    live subject that a normal Delete* should remove, and the IRI alone
    cannot say otherwise. Without only_predicates a typo would be an
    unguarded whole-subject delete on live data."""
    _predicates_reply(sink, ["http://data.fontem.eu/ontology#ticker"])
    with pytest.raises(ValueError, match="only_predicates"):
        sink._purge_subject_update(_ev(ENC))


def test_refuses_a_plain_ascii_subject_with_no_evidence(sink):
    """Same guard, the ordinary case: nothing about a UUID-keyed
    subject is unreachable on its own."""
    ascii_iri = "http://data.fontem.eu/id/Company/b3a154e1-0646-516c-abb4-e8eee6bc9497"
    _predicates_reply(sink, ["http://data.fontem.eu/ontology#name"])
    with pytest.raises(ValueError, match="only_predicates"):
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
    _predicates_reply(sink, ["http://data.fontem.eu/ontology#ticker"])
    with pytest.raises(ValueError):
        sink.handle([_ev(ENC)])


# The second way a subject becomes unreachable: its IRI is perfectly
# producible, but no renderer writes that family any more. Shared has
# 27,142 such Notice subjects, left by a contract value rollup routed
# there before #130. The encoding test cannot see the difference
# between one of those and a live subject, so the event carries
# only_predicates and the sink checks it against the store.

ORPHAN = "http://data.fontem.eu/id/Notice/639139-2020"
IS_CURRENT = "http://data.fontem.eu/ontology#isCurrent"
CURRENT_VALUE = "http://data.fontem.eu/ontology#currentValue"
ROLLUP_PREDS = [IS_CURRENT, CURRENT_VALUE]


def _predicates_reply(sink, predicates):
    """Point the fake client at a DISTINCT ?p result."""
    sink._client.post.return_value = MagicMock(
        raise_for_status=MagicMock(return_value=None),
        json=MagicMock(return_value={"results": {"bindings": [
            {"p": {"value": p}} for p in predicates
        ]}}),
    )


def test_purges_a_producible_iri_when_the_store_agrees(sink):
    """The subject carries nothing outside what the event declared, so
    it is a leftover and the purge proceeds."""
    _predicates_reply(sink, [IS_CURRENT])
    update = sink._purge_subject_update(
        _ev(ORPHAN, only_predicates=ROLLUP_PREDS))
    assert update.startswith("DELETE WHERE")
    assert f"<{ORPHAN}>" in update


def test_refuses_when_the_subject_carries_more_than_declared(sink):
    """The check that makes this safe. If the subject holds a predicate
    the event did not account for, it is not the leftover the event
    describes — purging would delete real triples."""
    _predicates_reply(sink, [
        IS_CURRENT, "http://data.fontem.eu/ontology#tedNoticeId",
    ])
    with pytest.raises(ValueError, match="tedNoticeId"):
        sink._purge_subject_update(_ev(ORPHAN, only_predicates=ROLLUP_PREDS))


def test_a_subject_with_no_triples_passes(sink):
    """Replay idempotency: a purge redelivered after it already applied
    finds nothing, which is trivially within any declared set. It must
    be a no-op, not a failure that dead-letters on every replay."""
    _predicates_reply(sink, [])
    update = sink._purge_subject_update(
        _ev(ORPHAN, only_predicates=ROLLUP_PREDS))
    assert update.startswith("DELETE WHERE")


def test_an_unproducible_iri_needs_no_evidence(sink):
    """Case (1) is unchanged: the IRI itself is proof, so no store
    round-trip happens and no only_predicates is required."""
    sink._client.post.reset_mock()
    update = sink._purge_subject_update(_ev(RAW))
    assert f"<{RAW}>" in update
    assert not sink._client.post.called, (
        "checked the store for a subject whose IRI is already proof")


def test_the_evidence_query_names_the_subject_verbatim(sink):
    """The check must ask about the same bytes the delete will use, or
    it would verify one subject and delete another."""
    _predicates_reply(sink, [IS_CURRENT])
    sink._purge_subject_update(_ev(ORPHAN, only_predicates=ROLLUP_PREDS))
    asked = sink._client.post.call_args.kwargs["data"]["query"]
    assert f"<{ORPHAN}>" in asked
    assert "SELECT DISTINCT ?p" in asked
    assert f"GRAPH <{G}>" in asked


def test_no_evidence_and_an_empty_subject_is_a_no_op(sink):
    """Replay stability from seq 0. Shared carries 27,142 PurgeSubject
    events emitted before only_predicates existed. On a fresh replay the
    rollup never creates their subjects (#130), so by the time those
    events come round there is nothing there — and a purge that would
    delete nothing must pass, not dead-letter on every replay forever."""
    _predicates_reply(sink, [])
    update = sink._purge_subject_update(_ev(ORPHAN))
    assert update.startswith("DELETE WHERE")
    assert f"<{ORPHAN}>" in update


def test_no_evidence_names_what_it_found_when_it_refuses(sink):
    """A refusal has to be diagnosable: the operator needs to see what
    the subject actually holds to decide between a Delete* and a purge
    with declared predicates."""
    _predicates_reply(sink, ["http://data.fontem.eu/ontology#tedNoticeId"])
    with pytest.raises(ValueError, match="tedNoticeId"):
        sink._purge_subject_update(_ev(ORPHAN))
