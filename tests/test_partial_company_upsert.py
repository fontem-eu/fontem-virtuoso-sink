"""A partial UpsertCompany replaces the fields it states and no others.

Only load_gleif describes a whole company, and it always states
entity_kind. Every other producer sends a slice: load_gleif_relationships
sends {gmr_id, lei} so the node exists before a relationship hangs off
it, load_ted_contracts the supplier's name and country as one notice
spells them. The sink used to apply each slice as a whole-subject
replace, so the weekly relationships run -- four hours after load_gleif
-- cut every relationship participant's GLEIF record down to its LEI.
Neo4j applies the same events as a merge and kept the record, so the two
stores disagreed about every company a partial producer touched.

Measured on prod 2026-09-11: of 1,000 healthy company subjects the last
event was load_gleif for 785; of 500 subjects reduced to a bare
rdf:type it was a partial producer for 369.
"""
# pylint: disable=protected-access
import re
from types import SimpleNamespace

import pytest

from virtuoso_sink.sink import VirtuosoSink, _delete_clause
from virtuoso_sink.triples import (
    COMPANY_FIELD_PREDICATES, FONTEM, OWL_SAME_AS, RDF_TYPE, RDFS_LABEL,
    WDT_P17, company_partial_predicates, render_upsert_company,
)

_G = "http://data.fontem.eu/graph/company"
_GID = "3e01a95a-69e6-56fc-a818-3170a21d0124"
_S = f"http://data.fontem.eu/id/Company/{_GID}"


def _deleted(clause: str) -> set[str]:
    """Predicates a scoped clause deletes."""
    return set(re.findall(r"<([^>]+)> \?o", clause))


def _rendered(payload: dict) -> set[str]:
    return {t.p for t in render_upsert_company(payload)} - {RDF_TYPE}


def test_the_relationship_loader_no_longer_erases_the_gleif_record():
    clause = _delete_clause(_G, _S, "UpsertCompany",
                            {"gmr_id": _GID, "lei": "213800YL71WWVQURBQ56"})
    assert "?p ?o" not in clause
    assert _deleted(clause) == {f"{FONTEM}lei"}


def test_a_ted_supplier_replaces_only_what_the_notice_says():
    payload = {"gmr_id": _GID, "name": "BSP SOFTWAREDISTRIBUTION a.s.",
               "country": "SVK", "active": True}
    assert _deleted(_delete_clause(_G, _S, "UpsertCompany", payload)) == {
        RDFS_LABEL, WDT_P17, f"{FONTEM}active",
    }


def test_an_event_stating_nothing_deletes_nothing():
    """The whole-subject branch would reduce the company to a bare
    rdf:type -- the state 2.36M prod subjects were found in."""
    assert _delete_clause(_G, _S, "UpsertCompany", {"gmr_id": _GID}) == ""


def test_a_null_field_is_not_a_statement():
    """The Neo4j sink leaves a property alone when the event's value is
    null; this store must take the same fields from the same event."""
    payload = {"gmr_id": _GID, "name": None, "lei": "213800YL71WWVQURBQ56"}
    assert _deleted(_delete_clause(_G, _S, "UpsertCompany", payload)) == {
        f"{FONTEM}lei",
    }


def test_a_gleif_record_still_replaces_the_whole_subject():
    """entity_kind marks the full description, and the relabel cleanup
    depends on it refreshing the subject wholesale."""
    payload = {"gmr_id": _GID, "name": "X", "entity_kind": "GENERAL"}
    clause = _delete_clause(_G, _S, "UpsertCompany", payload)
    assert "?p ?o" in clause
    assert f"FILTER(?p != <{OWL_SAME_AS}>)" in clause


def test_neither_type_nor_same_as_is_ever_in_a_partial_scope():
    preds = set(COMPANY_FIELD_PREDICATES.values())
    assert RDF_TYPE not in preds
    assert OWL_SAME_AS not in preds


def test_other_events_are_not_partial():
    assert company_partial_predicates("UpsertAuthority", {"name": "x"}) is None
    assert company_partial_predicates("UpsertCompany",
                                      {"gmr_id": _GID, "entity_kind": "FUND"}) is None


def _value(key: str):
    return {"aliases": ["Alias"], "active": True}.get(key, "value")


@pytest.mark.parametrize("key", sorted(COMPANY_FIELD_PREDICATES))
def test_each_field_deletes_exactly_what_it_renders(key):
    """The drift guard: a field the renderer writes under one predicate
    and the scope deletes under another would accumulate a stale value
    per event."""
    payload = {"gmr_id": _GID, key: _value(key)}
    assert _rendered(payload) == {COMPANY_FIELD_PREDICATES[key]}
    if key != "entity_kind":
        assert set(company_partial_predicates("UpsertCompany", payload)) == \
            _rendered(payload)


def test_a_full_partial_payload_scopes_everything_it_renders():
    payload = {"gmr_id": _GID, **{k: _value(k) for k in COMPANY_FIELD_PREDICATES
                                  if k != "entity_kind"}}
    assert set(company_partial_predicates("UpsertCompany", payload)) == \
        _rendered(payload)


def test_the_update_sent_for_a_partial_event_keeps_the_rest_of_the_subject():
    sink = VirtuosoSink.__new__(VirtuosoSink)
    payload = {"gmr_id": _GID, "lei": "213800YL71WWVQURBQ56"}
    ev = SimpleNamespace(op="upsert", event_type="UpsertCompany", iri=_S,
                         domain="company", payload=payload)
    update = sink._build_update(ev, render_upsert_company(payload))
    assert "?p ?o" not in update
    assert f"<{_S}> <{FONTEM}lei> ?o" in update
    assert "INSERT DATA" in update
