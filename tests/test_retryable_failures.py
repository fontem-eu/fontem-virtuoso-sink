"""A store outage must never be mistaken for a poison event.

EventConsumer skips an event that fails ``max_attempts`` times in a
row. That is right for bad data and catastrophic for an outage: on
2026-09-06 Virtuoso was OOM-killed during a shared replay, every batch
failed with "Connection refused", and the sink skipped 4,006 events.
Nothing re-emits a skipped event — ``--since``-windowed ETL only ever
emits new rows — so those triples were permanently missing from the
projection.

``is_retryable`` is what keeps the two apart, so these tests pin both
directions: transport and server-health failures retry, and a
Virtuoso rejection of one particular update still counts as poison.
"""
# pylint: disable=protected-access,import-outside-toplevel
from collections import defaultdict
from unittest.mock import MagicMock, patch

import httpx
import pytest


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
    return s


def _status_error(code):
    request = httpx.Request("POST", "http://virtuoso.test:8890/sparql-auth")
    response = httpx.Response(code, request=request)
    return httpx.HTTPStatusError(f"{code}", request=request, response=response)


@pytest.mark.parametrize("exc", [
    httpx.ConnectError("connection refused"),
    httpx.ConnectTimeout("timed out"),
    httpx.ReadTimeout("timed out"),
    httpx.PoolTimeout("pool exhausted"),
    httpx.RemoteProtocolError("server disconnected"),
])
def test_transport_failures_are_retryable(sink, exc):
    """These are all "Virtuoso is not answering". The same event
    succeeds once it is back, so the offset must not advance."""
    assert sink.is_retryable(exc) is True


@pytest.mark.parametrize("code", [500, 502, 503, 504, 429])
def test_server_health_statuses_are_retryable(sink, code):
    """5xx is Virtuoso unhealthy; 429 is Virtuoso asking us to slow
    down. Neither says anything about the event."""
    assert sink.is_retryable(_status_error(code)) is True


@pytest.mark.parametrize("code", [400, 401, 403, 404, 409, 422])
def test_client_errors_stay_poison(sink, code):
    """A 4xx other than 429 is Virtuoso rejecting THIS update — the
    poison case the skip exists for. Retrying it forever would
    recreate the 2026-06-09 jam, where one un-escaped quote had the
    sink retrying the same batch 15,727 times over 66 hours."""
    assert sink.is_retryable(_status_error(code)) is False


def test_unrelated_exceptions_stay_poison(sink):
    """A renderer bug is not an outage."""
    assert sink.is_retryable(KeyError("gmr_id")) is False
    assert sink.is_retryable(ValueError("bad payload")) is False


def test_outage_does_not_trigger_the_isolation_pass(sink):
    """_post_updates isolates a failed batch one event at a time to
    find the offending event. During an outage that is N more failed
    connections that learn nothing, and it delays the consumer's
    backoff. The batch replays intact anyway."""
    sink._client.post.side_effect = httpx.ConnectError("connection refused")
    with pytest.raises(httpx.ConnectError):
        sink._post_updates([f"INSERT DATA {{ <http://s/{i}> <http://p> 1 }}"
                            for i in range(25)])
    assert sink._client.post.call_count == 1, (
        "isolated a batch that failed because the store was down"
    )


def test_poison_batch_still_isolates(sink):
    """The isolation pass is what keeps a dead-letter row pointed at
    the one bad seq instead of poisoning 24 good events with it."""
    request = httpx.Request("POST", sink._update_url)

    def _post(*_a, **_k):
        return MagicMock(raise_for_status=MagicMock(
            side_effect=httpx.HTTPStatusError(
                "400", request=request,
                response=httpx.Response(400, request=request)),
        ))

    sink._client.post.side_effect = _post
    with pytest.raises(httpx.HTTPStatusError):
        sink._post_updates([f"INSERT DATA {{ <http://s/{i}> <http://p> 1 }}"
                            for i in range(25)])
    # 1 batched attempt + the isolation pass.
    assert sink._client.post.call_count == 2
