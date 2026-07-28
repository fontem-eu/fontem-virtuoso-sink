"""SPARQL UPDATE path tests — verify the big-data-const override."""
# Tests legitimately reach into the sink's private API: the directive
# behaviour we want to lock down sits on _sparql_update and _client.
# pylint: disable=protected-access,redefined-outer-name,import-outside-toplevel
from __future__ import annotations

import os
from collections import defaultdict
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def sink_env(monkeypatch):
    monkeypatch.setenv("VIRTUOSO_SPARQL_URL", "http://virtuoso.test:8890/sparql")
    monkeypatch.setenv("VIRTUOSO_DBA_USER", "dba")
    monkeypatch.setenv("VIRTUOSO_DBA_PASSWORD", "secret")
    monkeypatch.setenv("EVENTS_KAFKA_BOOTSTRAP", "kafka.test:9092")
    monkeypatch.setenv("EVENTS_TOPIC", "events.entity_events")
    monkeypatch.setenv("EVENTS_GROUP_ID", "virtuoso-sink-test")


def _make_sink():
    """Build a sink with the httpx client mocked. EventConsumer init
    needs Kafka — stub it; we only exercise the sparql_update path."""
    from virtuoso_sink.sink import VirtuosoSink

    with patch("virtuoso_sink.sink.EventConsumer.__init__", lambda self, *a, **k: None):
        sink = VirtuosoSink.__new__(VirtuosoSink)
        sink.sparql_endpoint = os.environ["VIRTUOSO_SPARQL_URL"]
        sink.dba_user = "dba"
        sink.dba_password = "secret"
        sink.timeout = 30.0
        sink._open_brackets = defaultdict(list)
        base = sink.sparql_endpoint.rstrip("/").removesuffix("/sparql")
        sink._update_url = f"{base}/sparql-auth"
        sink._crud_url = f"{base}/sparql-graph-crud-auth"
        sink._client = MagicMock()
        sink._client.post.return_value = MagicMock(
            raise_for_status=MagicMock(return_value=None),
        )
    return sink


def _ev(op: str, iri: str = "http://data.fontem.eu/id/Company/abc", domain: str = "company"):
    ev = MagicMock()
    ev.op = op
    ev.iri = iri
    ev.domain = domain
    ev.event_type = f"{domain}.{op}"
    return ev


def test_insert_update_prepends_big_data_const_directive(sink_env):  # pylint: disable=unused-argument
    """An upsert UPDATE must start with `define sql:big-data-const 1` —
    without it, /sparql-auth's silent `0` prepend pushes the query down
    the cache-consulting path that SR580s and leaks dirty hash entries.
    """
    sink = _make_sink()
    from virtuoso_sink.triples import Triple

    triples = [
        Triple(
            "http://data.fontem.eu/id/Company/abc",
            "http://www.w3.org/2000/01/rdf-schema#label",
            '"Acme"',
        ),
    ]
    sink._sparql_update(_ev("insert"), triples)

    sink._client.post.assert_called_once()
    body = sink._client.post.call_args.kwargs["data"]["query"]
    assert body.startswith("define sql:big-data-const 1\n"), body[:80]
    assert "DELETE WHERE" in body
    assert "INSERT DATA" in body


def test_delete_update_prepends_big_data_const_directive(sink_env):  # pylint: disable=unused-argument
    """DELETE-only events still walk the same hash-cache path; same
    SR580 risk, same directive required."""
    sink = _make_sink()
    sink._sparql_update(_ev("delete"), [])

    sink._client.post.assert_called_once()
    body = sink._client.post.call_args.kwargs["data"]["query"]
    assert body.startswith("define sql:big-data-const 1\n"), body[:80]
    assert "DELETE WHERE" in body
    assert "INSERT DATA" not in body


def test_translate_authority_name_is_scoped_replace(sink_env):  # pylint: disable=unused-argument
    """TranslateAuthorityName must NOT wipe the whole authority subject.
    It deletes only skos:altLabel for that subject (its own predicate),
    then inserts — so the authority's identity/name/country triples,
    written by UpsertAuthority, survive the enrichment."""
    sink = _make_sink()
    from virtuoso_sink.triples import Triple, SKOS_ALT_LABEL

    ev = _ev("upsert", iri="http://data.fontem.eu/id/Authority/a-1",
             domain="authority")
    ev.event_type = "TranslateAuthorityName"
    triples = [
        Triple("http://data.fontem.eu/id/Authority/a-1", SKOS_ALT_LABEL,
               '"Stadtamt"@de', is_literal=True),
    ]
    sink._sparql_update(ev, triples)

    body = sink._client.post.call_args.kwargs["data"]["query"]
    assert body.startswith("define sql:big-data-const 1\n"), body[:80]
    # Scoped: the DELETE names the skos:altLabel predicate, NOT `?p ?o`.
    assert "graph/authority-i18n" in body   # translations routed to the i18n graph
    assert "skos/core#altLabel> ?o" in body
    assert "?p ?o" not in body, "must not wipe the whole subject"
    assert "INSERT DATA" in body
    assert '"Stadtamt"@de' in body


def test_update_post_targets_sparql_auth_endpoint(sink_env):  # pylint: disable=unused-argument
    """The override is moot if the request lands at the wrong endpoint.
    Lock the URL down so a future refactor that swaps it silently still
    trips this test."""
    sink = _make_sink()
    from virtuoso_sink.triples import Triple

    sink._sparql_update(
        _ev("insert"),
        [Triple("http://x/a", "http://x/p", '"v"')],
    )
    assert sink._client.post.call_args.args[0] == "http://virtuoso.test:8890/sparql-auth"


def test_domain_default_graph_unifies_fund_into_company():
    """Company and InvestmentFund share ONE corporate graph — the subtype
    is in the subject IRI, not the graph. The retired 'fund' domain must
    resolve to the same graph as 'company' so relabels/replays converge
    (#270)."""
    from virtuoso_sink.sink import VirtuosoSink
    company = VirtuosoSink._domain_default_graph("company")
    assert VirtuosoSink._domain_default_graph("fund") == company
    assert company == "http://data.fontem.eu/graph/company"
    # unrelated domains are unaffected
    assert VirtuosoSink._domain_default_graph("contract") == \
        "http://data.fontem.eu/graph/contract"


def test_fund_domain_event_writes_to_company_graph(sink_env):  # pylint: disable=unused-argument
    """A legacy UpsertInvestmentFund (domain='fund') must land in the
    company graph, never graph/fund — otherwise a replayed fund event
    leaves a stale twin the company-graph relabel can't reach (#270)."""
    sink = _make_sink()
    from virtuoso_sink.triples import Triple
    ev = _ev("insert",
             iri="http://data.fontem.eu/id/InvestmentFund/f1", domain="fund")
    ev.event_type = "UpsertInvestmentFund"
    ev.payload = {"gmr_id": "f1"}
    sink._sparql_update(ev, [Triple(
        "http://data.fontem.eu/id/InvestmentFund/f1",
        "http://www.w3.org/2000/01/rdf-schema#label", '"A Fund"')])
    body = sink._client.post.call_args.kwargs["data"]["query"]
    assert "graph/company" in body
    assert "graph/fund" not in body


def test_notice_grain_contract_wipes_notice_subject_not_contract(sink_env):  # pylint: disable=unused-argument
    """A contract_key UpsertContract must DELETE the Notice subject —
    never ev.iri's Contract subject, and never the Contract/<key> node
    the monotone identity triples target. The Contract subject
    aggregates many notices; wiping it from any single notice's upsert
    would destroy the other notices' contributions."""
    sink = _make_sink()
    from virtuoso_sink.triples import render_upsert_contract

    payload = {
        "ted_notice_id": "n-77",
        "contract_key": "proc:P-77",
        "notice_kind": "award",
        "value_eur": 10.0,
    }
    ev = _ev("insert",
             iri="http://data.fontem.eu/id/Contract/n-77", domain="contract")
    ev.event_type = "UpsertContract"
    ev.payload = payload
    sink._sparql_update(ev, render_upsert_contract(payload))

    body = sink._client.post.call_args.kwargs["data"]["query"]
    assert body.startswith("define sql:big-data-const 1\n")
    delete_clause, insert_clause = body.split("INSERT DATA", 1)
    # Wipe scope: the Notice subject only.
    assert "<http://data.fontem.eu/id/Notice/n-77> ?p ?o" in delete_clause
    assert "Contract/" not in delete_clause
    # The Contract node reference + monotone identity ride insert-only.
    assert "<http://data.fontem.eu/id/Contract/proc:P-77>" in insert_clause
    assert "noticeOf" in insert_clause


def test_legacy_contract_still_wipes_ev_iri(sink_env):  # pylint: disable=unused-argument
    """Contracts without contract_key keep the pre-regrain behaviour:
    the DELETE targets ev.iri (the Contract/<ted_notice_id> subject)."""
    sink = _make_sink()
    from virtuoso_sink.triples import render_upsert_contract

    payload = {"ted_notice_id": "n-legacy", "value_eur": 5.0}
    ev = _ev("insert",
             iri="http://data.fontem.eu/id/Contract/n-legacy",
             domain="contract")
    ev.event_type = "UpsertContract"
    ev.payload = payload
    sink._sparql_update(ev, render_upsert_contract(payload))

    body = sink._client.post.call_args.kwargs["data"]["query"]
    delete_clause = body.split("INSERT DATA", 1)[0]
    assert "<http://data.fontem.eu/id/Contract/n-legacy> ?p ?o" in delete_clause
    assert "Notice/" not in body
