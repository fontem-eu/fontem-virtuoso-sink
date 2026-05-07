"""Renderer tests — pure functions, no Virtuoso required."""
from __future__ import annotations

from virtuoso_sink.triples import (
    RENDERERS, render_upsert_authority,
    render_upsert_company, render_upsert_contract,
    render_upsert_filing, render_upsert_listing,
    render_upsert_sanctioned_entity, to_turtle,
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


def test_listing_links_to_company() -> None:
    triples = render_upsert_listing({
        "ticker": "AAPL",
        "company_gmr_id": "00040372-dad6-5d34-882c-8b8624b4e734",
        "exchange": "US", "currency": "USD", "active": True,
    })
    pmap = {t.p: t.o for t in triples}
    assert "http://data.fontem.eu/ontology#listingOf" in pmap
    assert "Company" in pmap["http://data.fontem.eu/ontology#listingOf"]
    assert pmap["http://data.fontem.eu/ontology#exchange"] == '"US"'
    # Listing IRI is keyed by ticker.
    assert triples[0].s.endswith("/Listing/AAPL")


def test_authority_renders_minimal() -> None:
    triples = render_upsert_authority({
        "authority_id": "11111111-2222-5333-8444-555555555555",
        "name": "Test Authority", "country": "FR",
        "authority_type": "regulator",
    })
    pmap = {t.p: t.o for t in triples}
    assert any("Authority" in t.o for t in triples)
    assert "http://www.wikidata.org/prop/direct/P17" in pmap
    assert "http://data.fontem.eu/ontology#authorityType" in pmap


def test_contract_links_authority_and_company() -> None:
    triples = render_upsert_contract({
        "ted_notice_id": "2025-OJS123-456789",
        "title": "Some contract",
        "authority_id": "auth-1",
        "company_gmr_id": "11111111-2222-5333-8444-555555555555",
        "value_eur": 1000000.0,
    })
    pmap = {t.p: t.o for t in triples}
    awarded_by = pmap["http://data.fontem.eu/ontology#awardedBy"]
    awarded_to = pmap["http://data.fontem.eu/ontology#awardedTo"]
    assert "Authority/auth-1" in awarded_by
    assert "Company/" in awarded_to
    assert "http://data.fontem.eu/ontology#valueEur" in pmap


def test_renderer_registry_covers_all_event_types() -> None:
    # Sentinel: if we add a new event type without a renderer
    # entry, this test fails.
    expected = {
        "BeginGraphReplace", "EndGraphReplace",
        "UpsertCompany", "UpsertListing",
        "UpsertSanctionedEntity", "UpsertFiling",
        "UpsertAuthority", "UpsertContract",
        "AssertSameAs",
    }
    assert set(RENDERERS) == expected
