"""Renderer tests — pure functions, no Virtuoso required."""
from __future__ import annotations

from virtuoso_sink.triples import (
    render_upsert_investment_fund,
    render_translate_authority_name, SKOS_ALT_LABEL,
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


def test_translate_authority_name_renders_lang_tagged_alt_labels() -> None:
    """Each translation becomes a skos:altLabel with its language tag on
    the Authority subject; the source name is NOT re-asserted here."""
    triples = render_translate_authority_name({
        "authority_id": "a-1", "name": "Urzad Miasta",
        "translations": {"de": "Stadtamt", "en": "City Office"},
    })
    subj = "http://data.fontem.eu/id/Authority/a-1"
    assert {(t.p, t.o) for t in triples} == {
        (SKOS_ALT_LABEL, '"Stadtamt"@de'),
        (SKOS_ALT_LABEL, '"City Office"@en'),
    }
    assert all(t.s == subj and t.is_literal for t in triples)


def test_translate_authority_name_empty_translations_is_empty() -> None:
    assert not render_translate_authority_name(
        {"authority_id": "a-2", "translations": {}})


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
        "TranslateAuthorityName",
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


def test_contract_rollup_partial_is_skipped_not_wiped() -> None:
    """This sink upserts by full per-subject wipe+replace, so a
    collapse_modifications rollup-only UpsertContract (which carries no real
    contract triples) must render nothing — the sink's handle() then drops it
    without deleting the contract's existing triples. RDF current_value needs
    a dedicated additive path (follow-up); skipping is the safe interim."""
    out = render_upsert_contract({
        "ted_notice_id": "2025-OJS111-000001",
        "current_value": 42.0,
        "is_current": False,
        "contract_key": "proc:P-1",
    })
    assert not out  # rollup-only payload is skipped (empty render)


def test_full_contract_still_renders_with_rollup_fields_present() -> None:
    """A real contract emit is unaffected by the rollup guard."""
    out = render_upsert_contract({
        "ted_notice_id": "2025-OJS111-000002",
        "title": "Bridge works",
        "value_eur": 100.0,
        "company_gmr_id": "c-1",
    })
    assert any("awardedTo" in str(t) for t in out)
    assert any("valueEur" in str(t) for t in out)


# ── Contract/Notice grain (contract_key events) ───────────────────


def _notice_grain_payload(**over):
    base = {
        "ted_notice_id": "6a1e0e87-0000-5000-8000-000000000001",
        "contract_key": "proc:11111111-2222-5333-8444-555555555555",
        "notice_kind": "award",
        "title": "Bridge works",
        "authority_id": "a-1",
        "company_gmr_id": "c-1",
        "publication_date": "2026-05-01",
        "value_eur": 1000000.0,
        "value_currency": "EUR",
        "tenders_received": 2,
        "parties": [
            {"company_gmr_id": "c-1", "name": "Alpha", "role": "winner",
             "is_consortium_member": False},
            {"company_gmr_id": "c-2", "name": "Beta", "role": "winner",
             "is_consortium_member": True, "tendering_party_id": "tp-1"},
            {"company_gmr_id": "c-3", "name": "Gamma",
             "role": "named_tenderer"},
        ],
    }
    base.update(over)
    return base


def test_contract_notice_grain_renders_notice_subject() -> None:
    """contract_key routes the event to the Notice subject: per-notice
    literals + a noticeOf edge to the Contract node reference."""
    triples = render_upsert_contract(_notice_grain_payload())
    notice = "http://data.fontem.eu/id/Notice/6a1e0e87-0000-5000-8000-000000000001"
    contract = ("http://data.fontem.eu/id/Contract/"
                "proc:11111111-2222-5333-8444-555555555555")
    f = "http://data.fontem.eu/ontology#"
    notice_triples = [t for t in triples if t.s == notice]
    pmap = {t.p: t.o for t in notice_triples}
    assert pmap["http://www.w3.org/1999/02/22-rdf-syntax-ns#type"].endswith(
        "#Notice>")
    assert pmap[f + "noticeOf"] == f"<{contract}>"
    assert pmap[f + "noticeKind"] == '"award"'
    assert pmap[f + "tedNoticeId"] == '"6a1e0e87-0000-5000-8000-000000000001"'
    # Per-notice literals live on the Notice subject, never on Contract.
    assert f + "valueEur" in pmap
    assert f + "publicationDate" in pmap
    assert pmap[f + "tendersReceived"] == \
        '"2"^^<http://www.w3.org/2001/XMLSchema#integer>'
    assert f + "awardedBy" in pmap
    assert f + "awardedTo" in pmap


def test_contract_notice_grain_contract_subject_is_monotone_identity() -> None:
    """The Contract subject carries ONLY the monotone identity set
    (rdf:type + contractKey) — never mutable values. Everything else a
    notice knows stays on the Notice subject, because the Contract
    subject aggregates many notices and must survive every one of their
    wipe-and-replace upserts."""
    triples = render_upsert_contract(_notice_grain_payload())
    contract = ("http://data.fontem.eu/id/Contract/"
                "proc:11111111-2222-5333-8444-555555555555")
    contract_triples = [t for t in triples if t.s == contract]
    preds = {t.p for t in contract_triples}
    assert preds == {
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#type",
        "http://data.fontem.eu/ontology#contractKey",
    }
    pmap = {t.p: t.o for t in contract_triples}
    assert pmap["http://www.w3.org/1999/02/22-rdf-syntax-ns#type"].endswith(
        "#Contract>")
    assert pmap["http://data.fontem.eu/ontology#contractKey"] == \
        '"proc:11111111-2222-5333-8444-555555555555"'
    # And nothing else escapes to a third subject.
    notice = "http://data.fontem.eu/id/Notice/6a1e0e87-0000-5000-8000-000000000001"
    assert {t.s for t in triples} == {notice, contract}


def test_contract_notice_grain_party_roles() -> None:
    """parties[] → fontem:winner / fontem:namedTenderer Company edges on
    the Notice subject. Flat by design: rank / consortium structure would
    need blank nodes, which orphan under wipe-and-replace."""
    triples = render_upsert_contract(_notice_grain_payload())
    f = "http://data.fontem.eu/ontology#"
    winners = sorted(t.o for t in triples if t.p == f + "winner")
    named = [t.o for t in triples if t.p == f + "namedTenderer"]
    assert winners == [
        "<http://data.fontem.eu/id/Company/c-1>",
        "<http://data.fontem.eu/id/Company/c-2>",
    ]
    assert named == ["<http://data.fontem.eu/id/Company/c-3>"]
    notice = "http://data.fontem.eu/id/Notice/6a1e0e87-0000-5000-8000-000000000001"
    assert all(t.s == notice for t in triples
               if t.p in (f + "winner", f + "namedTenderer"))
    # No blank nodes anywhere in the render.
    assert not any(t.s.startswith("_:") or t.o.startswith("_:")
                   for t in triples)


def test_contract_without_contract_key_is_byte_identical_to_main() -> None:
    """Backward compat lock: an event WITHOUT contract_key renders
    byte-for-byte what current main renders (golden snapshot generated
    from main's renderer), so a replay from seq 0 is stable."""
    payload = {
        "ted_notice_id": "2022-708565",
        "title": "Road maintenance",
        "authority_id": "a-9",
        "company_gmr_id": "c-9",
        "publication_date": "2022-11-30",
        "value_eur": 5000000.0,
        "value_currency": "HUF",
        "value_original": 2000000000.0,
        "cpv": "45233141",
        "nuts": "HU110",
        "language": "hu",
        "procedure_type": "open",
        "tenders_received": 3,
        "award_criterion_type": "meat",
        "is_framework": False,
        "eu_funded": True,
        "procedure_id": "proc-1",
        "notice_type": "can-standard",
    }
    s = "<http://data.fontem.eu/id/Contract/2022-708565>"
    f = "<http://data.fontem.eu/ontology#"
    xsd = "http://www.w3.org/2001/XMLSchema#"
    expected = "\n".join([
        f"{s} <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> "
        "<http://data.fontem.eu/ontology#Contract> .",
        f'{s} {f}tedNoticeId> "2022-708565" .',
        f'{s} <http://www.w3.org/2000/01/rdf-schema#label> "Road maintenance"@en .',
        f"{s} {f}awardedBy> <http://data.fontem.eu/id/Authority/a-9> .",
        f"{s} {f}awardedTo> <http://data.fontem.eu/id/Company/c-9> .",
        f'{s} {f}publicationDate> "2022-11-30"^^<{xsd}date> .',
        f'{s} {f}valueEur> "5000000.0"^^<{xsd}decimal> .',
        f'{s} {f}valueCurrency> "HUF" .',
        f'{s} {f}valueOriginal> "2000000000.0"^^<{xsd}decimal> .',
        f'{s} {f}cpv> "45233141" .',
        f'{s} {f}nuts> "HU110" .',
        f'{s} {f}language> "hu" .',
        f'{s} {f}procedureType> "open" .',
        f'{s} {f}tendersReceived> "3"^^<{xsd}integer> .',
        f'{s} {f}awardCriterionType> "meat" .',
        f'{s} {f}procedureId> "proc-1" .',
        f'{s} {f}noticeType> "can-standard" .',
        f'{s} {f}isFramework> "false"^^<{xsd}boolean> .',
        f'{s} {f}euFunded> "true"^^<{xsd}boolean> .',
        f'{s} {f}isSingleBidder> "false"^^<{xsd}boolean> .',
        f'{s} {f}isNonOpen> "false"^^<{xsd}boolean> .',
        f'{s} {f}isNoCall> "false"^^<{xsd}boolean> .',
        f'{s} {f}isPriceOnly> "false"^^<{xsd}boolean> .',
        f'{s} {f}integrityRedFlags> "0"^^<{xsd}integer> .',
    ]) + "\n"
    assert to_turtle(render_upsert_contract(payload)) == expected


def test_contract_notice_grain_replay_is_deterministic() -> None:
    """Replaying the same event yields identical statements — the
    monotone Contract triples are set-semantics idempotent and the
    Notice triples are wipe-and-replace stable."""
    p = _notice_grain_payload()
    assert render_upsert_contract(p) == render_upsert_contract(p)


def test_modification_notice_has_its_own_subject() -> None:
    """A modification notice must NOT share (and thus never wipes) the
    award notice's subject: different ted_notice_id → different Notice
    IRIs, converging on the same Contract node reference."""
    award = render_upsert_contract(_notice_grain_payload())
    mod = render_upsert_contract(_notice_grain_payload(
        ted_notice_id="6a1e0e87-0000-5000-8000-000000000002",
        notice_kind="modification",
        value_eur=1500000.0,
    ))
    contract = ("http://data.fontem.eu/id/Contract/"
                "proc:11111111-2222-5333-8444-555555555555")
    award_notice = {t.s for t in award} - {contract}
    mod_notice = {t.s for t in mod} - {contract}
    assert award_notice != mod_notice           # disjoint wipe scopes
    assert len(award_notice) == len(mod_notice) == 1
    f = "http://data.fontem.eu/ontology#"
    # Both link to the SAME contract node.
    assert [t.o for t in award if t.p == f + "noticeOf"] == \
        [t.o for t in mod if t.p == f + "noticeOf"] == [f"<{contract}>"]


def test_notice_grain_rollup_partial_still_skipped() -> None:
    """A collapse_modifications rollup partial under the new grain
    (contract_key present) must render NOTHING — not even the monotone
    Contract-identity triples. Any non-empty render would make the sink
    run its DELETE-then-INSERT update against the Notice subject and
    destroy the notice's real triples."""
    assert not render_upsert_contract({
        "ted_notice_id": "6a1e0e87-0000-5000-8000-000000000001",
        "contract_key": "proc:P-1",
        "current_value": 1500000.0,
        "is_current": True,
    })
    assert not render_upsert_contract({
        "ted_notice_id": "6a1e0e87-0000-5000-8000-000000000001",
        "contract_key": "proc:P-1",
    })
