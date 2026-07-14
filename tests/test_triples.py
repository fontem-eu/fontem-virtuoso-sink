"""Renderer tests — pure functions, no Virtuoso required."""
from __future__ import annotations

from virtuoso_sink.triples import (
    render_upsert_investment_fund,
    RENDERERS, render_upsert_authority,
    render_upsert_company, render_upsert_contract,
    render_upsert_disclosure, render_upsert_exchange_rate,
    render_upsert_filing, render_upsert_listing,
    render_upsert_petition, render_upsert_relationship,
    render_upsert_sanctioned_entity,
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


def test_disclosure_detail_string_escapes_embedded_quotes() -> None:
    """Reproduces the eu-cohesion poison event at seq 4,806,634 that
    jammed virtuoso_sink from 2026-06-09 to 2026-06-12. The Guadeloupe
    project description contained an embedded "multi-hazard flooding"
    quoted phrase; the renderer wrapped the raw string in '"..."'
    without escaping, Virtuoso closed the literal at the first inner
    quote and 400'd. Detail-string values must escape through _lit so
    embedded quotes survive as \\u0022."""
    triples = render_upsert_disclosure({
        "system": "eu-cohesion",
        "disclosure_id": "Q7356886",
        "year": 2024,
        "details": {
            "description": (
                'A "multi-hazard flooding" approach addresses '
                'flood risk regardless of origin.'
            ),
        },
    })
    detail_objs = [t.o for t in triples if t.p.endswith("detail_description")]
    assert len(detail_objs) == 1
    body = detail_objs[0]
    # No bare unescaped inner quotes — the only quotes that survive
    # in the Turtle literal are the outer wrappers.
    assert body.startswith('"') and body.endswith('"')
    assert '"' not in body[1:-1]
    # The inner quotes must have been escaped to the Unicode codepoint.
    assert '\\u0022' in body


def test_disclosure_detail_string_drops_empty_values() -> None:
    """_lit returns None for empty/whitespace-only strings; the detail
    branch must skip them rather than emit an empty literal."""
    triples = render_upsert_disclosure({
        "system": "cdp",
        "disclosure_id": "X-1",
        "year": 2024,
        "details": {"description": "  ", "country": "FRA"},
    })
    detail_preds = {t.p for t in triples if "detail_" in t.p}
    assert any(p.endswith("detail_country") for p in detail_preds)
    assert not any(p.endswith("detail_description") for p in detail_preds)


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
        "UpsertCompany", "UpsertInvestmentFund", "UpsertListing",
        "UpsertPetition", "UpsertSanctionedEntity", "UpsertFiling",
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
    plain = "http://data.fontem.eu/id/Company/abc-123"
    assert _iri(plain) == f"<{plain}>"
    # Already-encoded input must not be double-encoded.
    assert _iri("http://data.fontem.eu/id/x/%CE%A4") == "<http://data.fontem.eu/id/x/%CE%A4>"



def test_lit_escapes_quote_with_unicode_codepoint():
    """Virtuoso's SPARQL parser terminates a "..." literal at the
    first \" and chokes on the rest — confirmed in prod against the
    Polish company name PRZEDSIĘBIORSTWO \"KONSPOL-BIS\" SPÓŁKA.
    Escape doublequotes via \u0022 so Virtuoso reads them as a
    single character in the string body, not as a delimiter."""
    from virtuoso_sink.triples import _lit  # pylint: disable=import-outside-toplevel
    out = _lit('PRZEDSIĘBIORSTWO "KONSPOL-BIS" SPÓŁKA')
    assert out is not None
    assert '\\u0022' in out
    # End quote is still a literal " (the wrapping pair)
    assert out.startswith('"')
    assert out.endswith('"')


def test_lit_escapes_backslash_with_unicode_codepoint():
    from virtuoso_sink.triples import _lit  # pylint: disable=import-outside-toplevel
    out = _lit('C:\\path\\to\\thing')
    assert out is not None
    assert '\\u005C' in out
    assert '\\\\' not in out  # no double-backslash style escapes


def test_lit_escapes_newline_with_unicode_codepoint():
    from virtuoso_sink.triples import _lit  # pylint: disable=import-outside-toplevel
    out = _lit('first\nsecond')
    assert out is not None
    assert '\\u000A' in out


def test_contract_emits_integrity_fields_and_flags() -> None:
    """Integrity fields + keystone red flags are emitted as typed triples,
    matching the Neo4j sink: a single-bidder no-call price-only award."""
    triples = render_upsert_contract({
        "ted_notice_id": "n-int",
        "tenders_received": 1,
        "procedure_type": "neg-wo-call",
        "award_criterion_type": "price",
        "submission_deadline": "2026-03-15T12:00:00",
        "funding_programme": "HORIZON-EU",
        "is_framework": False,
        "eu_funded": True,
    })
    pmap = {t.p: t.o for t in triples}
    f = "http://data.fontem.eu/ontology#"
    assert pmap[f + "tendersReceived"] == '"1"^^<http://www.w3.org/2001/XMLSchema#integer>'
    assert pmap[f + "submissionDeadline"] == '"2026-03-15"^^<http://www.w3.org/2001/XMLSchema#date>'
    assert pmap[f + "fundingProgramme"] == '"HORIZON-EU"'
    assert f + "procedureType" in pmap
    assert "true" in pmap[f + "isSingleBidder"]
    assert "true" in pmap[f + "isNonOpen"]
    assert "true" in pmap[f + "isNoCall"]
    assert "true" in pmap[f + "isPriceOnly"]
    assert pmap[f + "integrityRedFlags"] == '"4"^^<http://www.w3.org/2001/XMLSchema#integer>'
    assert "false" in pmap[f + "isFramework"]   # meaningful boolean false
    assert "true" in pmap[f + "euFunded"]


def test_contract_emits_all_optional_scalar_fields() -> None:
    """Exercise every optional branch of render_upsert_contract (dates,
    value variants, cpv, nuts, language) so new-code coverage is complete."""
    triples = render_upsert_contract({
        "ted_notice_id": "n-full",
        "title": "Full contract",
        "authority_id": "a1",
        "company_gmr_id": "c1",
        "publication_date": "2026-02-01T00:00:00",
        "value_eur": 1234.50,
        "value_currency": "EUR",
        "value_original": 1000.00,
        "value_before_eur": 800.00,
        "value_before_original": 640.00,
        "cpv": "72000000",
        "nuts": "HU110",
        "language": "hu",
    })
    f = "http://data.fontem.eu/ontology#"
    pmap = {t.p: t.o for t in triples}
    assert f + "publicationDate" in pmap
    assert f + "valueEur" in pmap
    assert pmap[f + "valueCurrency"] == '"EUR"'
    assert f + "valueOriginal" in pmap
    assert f + "valueBeforeEur" in pmap
    assert f + "valueBeforeOriginal" in pmap
    assert pmap[f + "cpv"] == '"72000000"'
    assert pmap[f + "nuts"] == '"HU110"'
    assert pmap[f + "language"] == '"hu"'
    assert pmap[f + "awardedBy"].startswith("<http://data.fontem.eu/id/Authority/a1")
    assert pmap[f + "awardedTo"].startswith("<http://data.fontem.eu/id/Company/c1")


def test_contract_emits_procedure_and_modification_triples() -> None:
    """procedure_id + notice_type (and modifies_publication_number on
    modifications) render as triples — the join keys for MODIFIES."""
    triples = render_upsert_contract({
        "ted_notice_id": "n-mod",
        "procedure_id": "proc-7bcd",
        "notice_type": "can-modif",
        "modifies_publication_number": "708565-2022",
    })
    pmap = {t.p: t.o for t in triples}
    f = "http://data.fontem.eu/ontology#"
    assert pmap[f + "procedureId"] == '"proc-7bcd"'
    assert pmap[f + "noticeType"] == '"can-modif"'
    assert pmap[f + "modifiesPublicationNumber"] == '"708565-2022"'


# ── InvestmentFund entity + fund-unit listings ────────────────────


def test_investment_fund_renderer_types_and_props():
    triples = render_upsert_investment_fund({
        "gmr_id": "0b6cbfa6-6a30-5efc-9b4f-3e56d0f3f5a2",
        "name": "EXAMPLE UCITS FUND",
        "lei": "2138008K5B3Z4E8DHN12",
        "fund_type": "Open-End Fund",
    })
    subj = "http://data.fontem.eu/id/InvestmentFund/0b6cbfa6-6a30-5efc-9b4f-3e56d0f3f5a2"
    by_pred = {t.p: t for t in triples}
    assert all(t.s == subj for t in triples)
    assert "InvestmentFund" in by_pred[
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"].o
    assert by_pred["http://data.fontem.eu/ontology#fundType"].o == '"Open-End Fund"'


def test_listing_of_fund_unit_points_at_investment_fund():
    """A fund-class security_type routes listingOf at the
    InvestmentFund subject, not the Company one."""
    triples = render_upsert_listing({
        "ticker": "TPLGMFB", "company_gmr_id": "g1",
        "exchange": "GU", "security_type": "Open-End Fund",
    })
    listing_of = [t for t in triples
                  if t.p == "http://data.fontem.eu/ontology#listingOf"][0]
    assert "/id/InvestmentFund/g1" in listing_of.o
    sec = [t for t in triples
           if t.p == "http://data.fontem.eu/ontology#securityType"][0]
    assert sec.o == '"Open-End Fund"'


def test_listing_of_common_stock_still_points_at_company():
    triples = render_upsert_listing({
        "ticker": "EGL", "company_gmr_id": "g1",
        "exchange": "PL", "security_type": "Common Stock",
    })
    listing_of = [t for t in triples
                  if t.p == "http://data.fontem.eu/ontology#listingOf"][0]
    assert "/id/Company/g1" in listing_of.o


def test_listing_without_security_type_defaults_to_company():
    """Legacy events (no security_type) keep the Company linkage —
    behaviour-preserving for every event already in the log."""
    triples = render_upsert_listing({
        "ticker": "EGL", "company_gmr_id": "g1",
    })
    listing_of = [t for t in triples
                  if t.p == "http://data.fontem.eu/ontology#listingOf"][0]
    assert "/id/Company/g1" in listing_of.o
    assert not [t for t in triples
                if t.p == "http://data.fontem.eu/ontology#securityType"]


def test_contract_quarantine_renders_marker_not_values():
    triples = render_upsert_contract({
        "ted_notice_id": "n-1",
        "value_quarantined": True,
        "value_quarantine_reason": "implausible_magnitude",
    })
    preds = {t.p.split("#")[-1] for t in triples}
    assert "valueQuarantined" in preds
    assert "valueQuarantineReason" in preds
    assert "valueEur" not in preds     # event omits values; none rendered


# ── GLEIF identity block + entity_kind subject routing ────────────


def test_company_renders_identity_block_at_company_subject():
    triples = render_upsert_company({
        "gmr_id": "g1", "name": "CARLSBERG A/S", "country": "DNK",
        "entity_kind": "GENERAL", "registered_as": "61056416",
        "registered_at": "RA000170", "jurisdiction": "DK",
        "aliases": ["Carlsberg Group", "Carlsberg Breweries"],
    })
    subj = "http://data.fontem.eu/id/Company/g1"
    assert all(t.s == subj for t in triples)
    by = {t.p.split("#")[-1]: t.o for t in triples}
    assert "GENERAL" in by["entityKind"]
    assert "61056416" in by["registeredAs"]
    aliases = [t.o for t in triples if t.p.endswith("#alias")]
    assert len(aliases) == 2


def test_company_fund_kind_routes_to_investmentfund_subject():
    triples = render_upsert_company({
        "gmr_id": "f1", "name": "A UCITS Fund", "entity_kind": "FUND",
    })
    subj = "http://data.fontem.eu/id/InvestmentFund/f1"
    assert all(t.s == subj for t in triples)
    rdf_type = [t for t in triples if t.p.endswith("#type")][0]
    assert "InvestmentFund" in rdf_type.o


def test_company_no_kind_stays_company():
    triples = render_upsert_company({"gmr_id": "e1", "name": "Edgar Co"})
    assert all(t.s == "http://data.fontem.eu/id/Company/e1" for t in triples)


def test_sanctioned_entity_subject_type():
    person = render_upsert_sanctioned_entity({
        "entity_id": "p-1", "eu_reference": "EU.1", "subject_type": "person",
    })
    assert any("subjectType" in t.p and "person" in t.o for t in person)

    # absent subject_type (pre-2026-07-14 events) emits no such triple
    silent = render_upsert_sanctioned_entity({
        "entity_id": "e-1", "eu_reference": "EU.2",
    })
    assert not any("subjectType" in t.p for t in silent)


def test_petition_triples():
    triples = render_upsert_petition({
        "system": "eu-eci", "petition_id": "ECI(2024)000007",
        "title": "Stop Destroying Videogames", "status": "ANSWERED",
        "total_supporters": 1294188,
        "organizer_names": ["Daniel ONDRUSKA"],
        "answer_refs": ["C(2026)4110"],
    })
    assert any("EuEciPetition/ECI(2024)000007" in t.s for t in triples)
    assert any("totalSupporters" in t.p and "1294188" in t.o for t in triples)
    assert any("organizerName" in t.p for t in triples)
    assert any("answerRef" in t.p for t in triples)
