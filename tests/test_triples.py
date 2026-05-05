"""Renderer tests — pure functions, no Virtuoso required."""
from __future__ import annotations

from virtuoso_sink.triples import (
    RENDERERS, render_upsert_company,
    render_upsert_filing, render_upsert_sanctioned_entity,
    to_turtle,
)


def test_company_renders_minimal() -> None:
    triples = render_upsert_company({"gmr_id": "abc"})
    assert any(t.p.endswith("type") and "Company" in t.o for t in triples)
    assert all(not t.s.startswith("<") for t in triples)  # bare IRI strings


def test_company_renders_full() -> None:
    triples = render_upsert_company({
        "gmr_id": "abc", "name": "Foo Corp",
        "country": "DE", "lei": "1234567890ABCDEFGHIJ",
        "active": True,
    })
    pmap = {t.p: t.o for t in triples}
    assert "http://data.fontem.eu/ontology#lei" in pmap
    assert "http://www.wikidata.org/prop/direct/P17" in pmap
    assert "http://data.fontem.eu/ontology#active" in pmap
    assert pmap["http://data.fontem.eu/ontology#active"] == "true"


def test_sanction_renders_with_aliases() -> None:
    triples = render_upsert_sanctioned_entity({
        "entity_id": "x", "eu_reference": "EU.1",
        "name": "Bad Corp", "aliases": ["BC", "Bad"],
        "designation_date": "2024-01-15",
    })
    alts = [t for t in triples if t.p.endswith("altLabel")]
    assert len(alts) == 2


def test_filing_iri_is_uuid5_deterministic() -> None:
    rec = {"gmr_id": "00000000-0000-5000-8000-000000000001",
           "year": 2024, "source": "edgar", "revenue": 1000.0}
    a = render_upsert_filing(rec)
    b = render_upsert_filing(rec)
    assert a[0].s == b[0].s


def test_to_turtle_emits_one_line_per_triple() -> None:
    triples = render_upsert_company(
        {"gmr_id": "abc", "name": "Foo", "country": "DE"}
    )
    out = to_turtle(triples)
    assert out.count(" .\n") == len(triples)


def test_renderer_registry_covers_all_event_types() -> None:
    # Sentinel: if we add a new event type without a renderer
    # entry, this test fails.
    expected = {
        "BeginGraphReplace", "EndGraphReplace",
        "UpsertCompany", "UpsertSanctionedEntity", "UpsertFiling",
        "AssertSameAs",
    }
    assert set(RENDERERS) == expected
