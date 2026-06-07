"""Renderer tests — pure functions, no Virtuoso required."""
from __future__ import annotations

from virtuoso_sink.triples import (
    RENDERERS, render_upsert_authority,
    render_upsert_company, render_upsert_contract,
    render_upsert_disclosure, render_upsert_exchange_rate,
    render_upsert_filing, render_upsert_listing,
    render_upsert_relationship, render_upsert_sanctioned_entity,
    render_upsert_taxonomy_code, to_turtle,
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


def test_taxonomy_code_iri_per_system() -> None:
    triples = render_upsert_taxonomy_code({
        "system": "cpv", "code": "45000000",
        "label": "Construction work", "label_lang": "en",
    })
    assert triples[0].s.endswith("/Cpv/45000000")
    pmap = {t.p: t.o for t in triples}
    assert pmap["http://data.fontem.eu/ontology#code"] == '"45000000"'
    assert any("Construction work" in t.o for t in triples)


def test_taxonomy_code_parent_emits_skos_broader() -> None:
    triples = render_upsert_taxonomy_code({
        "system": "nuts", "code": "FR101", "parent_code": "FR1",
    })
    pmap = {t.p: t.o for t in triples}
    broader = pmap["http://www.w3.org/2004/02/skos/core#broader"]
    assert "/Nuts/FR1" in broader


def test_relationship_predicate_resolves_to_fontem() -> None:
    triples = render_upsert_relationship({
        "src_iri": "http://data.fontem.eu/id/Company/A",
        "dst_iri": "http://data.fontem.eu/id/Company/B",
        "predicate": "parentOf",
    })
    assert len(triples) == 1
    assert triples[0].p == "http://data.fontem.eu/ontology#parentOf"


def test_disclosure_links_to_filer_company() -> None:
    triples = render_upsert_disclosure({
        "system": "cdp",
        "disclosure_id": "EU-TR-12345",
        "company_gmr_id": "00040372-dad6-5d34-882c-8b8624b4e734",
        "year": 2024, "title": "Annual declaration",
        "details": {"total_eur_min": 200000, "fte_lobbyists": 4},
    })
    pmap = {t.p: t.o for t in triples}
    filed_by = pmap["http://data.fontem.eu/ontology#filedBy"]
    assert "Company/00040372" in filed_by
    # Flat detail-key projection.
    assert any(t.p.endswith("detail_total_eur_min") for t in triples)
    assert any(t.p.endswith("detail_fte_lobbyists") for t in triples)


def test_disclosure_omits_filed_by_when_no_company() -> None:
    """EU lobbying register entries are filed by the Lobbyist itself —
    no parent Company. The renderer must skip the filedBy triple
    rather than hardcoding a missing company IRI."""
    triples = render_upsert_disclosure({
        "system": "eu-lobbying",
        "disclosure_id": "EU-TR-12345",
        "year": 2024,
        "details": {"members_fte": 4},
    })
    preds = {t.p for t in triples}
    assert "http://data.fontem.eu/ontology#filedBy" not in preds
    # The Disclosure node still gets typed + carries its system
    assert any(t.o.endswith("Disclosure>") for t in triples)
    assert any(t.p.endswith("disclosureSystem") for t in triples)


def test_exchange_rate_iri_keyed_by_triple() -> None:
    triples = render_upsert_exchange_rate({
        "base": "EUR", "target": "USD",
        "date": "2025-09-15", "rate": 1.0473, "source": "ecb",
    })
    assert triples[0].s.endswith("/ExchangeRate/EUR-USD-2025-09-15")
    pmap = {t.p: t.o for t in triples}
    assert pmap["http://data.fontem.eu/ontology#rateSource"] == '"ecb"'


def test_renderer_registry_covers_all_event_types() -> None:
    # Sentinel: if we add a new event type without a renderer
    # entry, this test fails.
    expected = {
        "BeginGraphReplace", "EndGraphReplace",
        "UpsertCompany", "UpsertListing",
        "UpsertSanctionedEntity", "UpsertFiling",
        "UpsertAuthority", "UpsertContract",
        "UpsertTaxonomyCode", "UpsertRelationship",
        "UpsertDisclosure", "UpsertExchangeRate",
        "AssertSameAs",
    }
    assert set(RENDERERS) == expected



def test_iri_percent_encodes_non_ascii_characters():
    """Greek/Cyrillic/etc. characters in an IRI body must be percent-
    encoded so Virtuoso's SPARQL parser doesn't 500 the entire
    UPDATE. Without this fix we lost ~1k events for every cluster
    of Unicode-named Companies/Listings in the prod load — Virtuoso
    rejected the batch and the consumer offset wouldn't advance."""
    from virtuoso_sink.triples import _iri  # pylint: disable=import-outside-toplevel
    # Greek Listing IRI seen in prod load
    out = _iri("http://data.fontem.eu/id/Listing/ΤΕΧΝΙΚΗ.AT")
    expected = (
        "<http://data.fontem.eu/id/Listing/"
        "%CE%A4%CE%95%CE%A7%CE%9D%CE%99%CE%9A%CE%97.AT>"
    )
    assert out == expected
    # Cyrillic Listing IRI seen in prod load
    out2 = _iri("http://data.fontem.eu/id/Listing/ТОВАРИСТ_8282.PFTS")
    assert "%D0" in out2  # Cyrillic UTF-8 high byte
    assert out2.endswith("_8282.PFTS>")
    # Plain ASCII passes through unchanged (no double-encoding even
    # if input already had % escapes from upstream).
    assert _iri("http://data.fontem.eu/id/Company/abc-123") == "<http://data.fontem.eu/id/Company/abc-123>"
    # Already-encoded input must not be double-encoded.
    assert _iri("http://data.fontem.eu/id/x/%CE%A4") == "<http://data.fontem.eu/id/x/%CE%A4>"
