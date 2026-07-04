"""Event payload → Turtle triples renderers.

Each entity event type maps to a small render function that
returns a list of (subject_iri, predicate_iri, value) triples.
The sink accumulates triples per target graph and flushes them
as Turtle when the Begin/EndGraphReplace bracket closes.

Add new event types by:
  1. Add a function ``render_<event_type>(payload) -> list[Triple]``
  2. Register it in ``RENDERERS``.

Returning ``None`` for an event type means "the sink ignores
this event for Virtuoso". Useful for control events (Begin/End)
which the sink handles structurally rather than as triples.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Callable

from fontem_event_schemas.integrity import contract_red_flags
from fontem_event_schemas.securities import is_fund_security_type

FONTEM = "http://data.fontem.eu/ontology#"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
SKOS_ALT_LABEL = "http://www.w3.org/2004/02/skos/core#altLabel"
WDT_P17 = "http://www.wikidata.org/prop/direct/P17"  # country
XSD_DATE = "http://www.w3.org/2001/XMLSchema#date"
XSD_DECIMAL = "http://www.w3.org/2001/XMLSchema#decimal"
XSD_GYEAR = "http://www.w3.org/2001/XMLSchema#gYear"
XSD_BOOLEAN = "http://www.w3.org/2001/XMLSchema#boolean"
XSD_INTEGER = "http://www.w3.org/2001/XMLSchema#integer"

OWL_SAME_AS = "http://www.w3.org/2002/07/owl#sameAs"


@dataclass(frozen=True)
class Triple:
    s: str  # subject IRI
    p: str  # predicate IRI
    o: str  # object: full IRI or already-formatted Turtle literal
    is_literal: bool = False


_QUOTE = None  # cached at module level via the helper below


def _percent_encode_iri(s: str) -> str:
    """Lazily import urllib.parse.quote on first use so the module
    import path stays cheap; cached for subsequent calls."""
    global _QUOTE  # pylint: disable=global-statement
    if _QUOTE is None:
        from urllib.parse import quote  # pylint: disable=import-outside-toplevel
        _QUOTE = quote
    return _QUOTE(s, safe="%:/?#[]@!$&\'()*+,;=._-~")


def _iri(s: str) -> str:
    """Wrap an IRI string in angle brackets, percent-encoding any
    non-ASCII characters in the path/fragment.

    Virtuoso's SPARQL parser doesn't fully implement RFC 3987 IRIs —
    a raw Greek/Cyrillic/etc. character in the IRI body crashes the
    parser and the whole SPARQL UPDATE 500s. RFC 3986 says non-ASCII
    must be percent-encoded for safe interchange, so we do that here
    and let Virtuoso see plain ASCII. Round-trip is lossless: the IRI
    decodes back to the same Unicode string in any client that
    follows the RFC.

    The `safe` set covers every reserved + unreserved character that
    can legally appear unencoded in a URI, plus the percent itself so
    we don't double-encode an already-encoded IRI from upstream."""
    return f"<{_percent_encode_iri(s)}>"


def _lit(value, *, lang: str | None = None,
         datatype: str | None = None) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    # Virtuoso's SPARQL parser doesn't honour \" inside a "..."
    # literal — it terminates the string at the first \" and chokes
    # on whatever follows. Escape to the Unicode codepoint \u0022
    # instead; that's portable across all RFC-compliant SPARQL
    # implementations and Virtuoso's parser handles it cleanly.
    # Same logic for the other control chars: use the Turtle/SPARQL
    # \u escape syntax, not the C-style \n.
    s = (
        str(value)
        .replace("\\", "\\u005C")
        .replace('"', '\\u0022')
        .replace("\n", "\\u000A")
        .replace("\r", "\\u000D")
        .replace("\t", "\\u0009")
    )
    out = f'"{s}"'
    if lang:
        out += f"@{lang}"
    elif datatype:
        out += f"^^<{datatype}>"
    return out


def _decimal(value) -> str | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f'"{f!r}"^^<{XSD_DECIMAL}>'


# ── renderers ────────────────────────────────────────────────────

def render_upsert_company(p: dict) -> list[Triple]:
    iri = f"http://data.fontem.eu/id/Company/{p['gmr_id']}"
    out: list[Triple] = [
        Triple(iri, RDF_TYPE, _iri(f"{FONTEM}Company"), is_literal=False),
    ]
    if name := _lit(p.get("name"), lang="en"):
        out.append(Triple(iri, RDFS_LABEL, name, is_literal=True))
    if lei := _lit(p.get("lei")):
        out.append(Triple(iri, f"{FONTEM}lei", lei, is_literal=True))
    if vat := _lit(p.get("vat")):
        out.append(Triple(iri, f"{FONTEM}vat", vat, is_literal=True))
    if cik := _lit(p.get("cik")):
        out.append(Triple(iri, f"{FONTEM}cik", cik, is_literal=True))
    if country := _lit(p.get("country")):
        out.append(Triple(iri, WDT_P17, country, is_literal=True))
    if (active := p.get("active")) is not None:
        out.append(Triple(iri, f"{FONTEM}active",
                          "true" if active else "false", is_literal=True))
    if lf := _lit(p.get("legal_form")):
        out.append(Triple(iri, f"{FONTEM}legalForm", lf, is_literal=True))
    if pc := _lit(p.get("postal_code")):
        out.append(Triple(iri, f"{FONTEM}postalCode", pc, is_literal=True))
    return out


def render_upsert_sanctioned_entity(p: dict) -> list[Triple]:
    iri = f"http://data.fontem.eu/id/Sanction/{p['entity_id']}"
    out: list[Triple] = [
        Triple(iri, RDF_TYPE, _iri(f"{FONTEM}SanctionedEntity")),
        Triple(iri, f"{FONTEM}euReference",
               _lit(p["eu_reference"]) or '""', is_literal=True),
    ]
    if name := _lit(p.get("name"), lang="en"):
        out.append(Triple(iri, RDFS_LABEL, name, is_literal=True))
    for alias in p.get("aliases") or ():
        if a := _lit(alias):
            out.append(Triple(iri, SKOS_ALT_LABEL, a, is_literal=True))
    if dt := (p.get("designation_date") or "").strip()[:10]:
        out.append(Triple(iri, f"{FONTEM}designationDate",
                          f'"{dt}"^^<{XSD_DATE}>', is_literal=True))
    if reg := _lit(p.get("sanction_regime")):
        out.append(Triple(iri, f"{FONTEM}sanctionRegime", reg, is_literal=True))
    if lb := _lit(p.get("legal_basis")):
        out.append(Triple(iri, f"{FONTEM}legalBasis", lb, is_literal=True))
    if lr := _lit(p.get("listing_reason")):
        out.append(Triple(iri, f"{FONTEM}listingReason", lr, is_literal=True))
    return out


_FILING_FIELD_PROPS: dict[str, str] = {
    "revenue": f"{FONTEM}revenue",
    "gross_profit": f"{FONTEM}grossProfit",
    "operating_income": f"{FONTEM}operatingIncome",
    "net_income": f"{FONTEM}netIncome",
    "eps": f"{FONTEM}eps",
    "total_assets": f"{FONTEM}totalAssets",
    "total_liabilities": f"{FONTEM}totalLiabilities",
    "equity": f"{FONTEM}equity",
    "cash_and_equivalents": f"{FONTEM}cash",
    "cash": f"{FONTEM}cash",
    "capex": f"{FONTEM}capex",
    "operating_cashflow": f"{FONTEM}operatingCashflow",
    "free_cashflow": f"{FONTEM}freeCashflow",
    "current_assets": f"{FONTEM}currentAssets",
    "current_liabilities": f"{FONTEM}currentLiabilities",
    "shares_outstanding": f"{FONTEM}sharesOutstanding",
    "long_term_debt": f"{FONTEM}longTermDebt",
    "interest_expense": f"{FONTEM}interestExpense",
    "income_tax_expense": f"{FONTEM}incomeTaxExpense",
    "depreciation_amortization": f"{FONTEM}depreciationAmortization",
    "inventory": f"{FONTEM}inventory",
}


def render_upsert_filing(p: dict) -> list[Triple]:
    seed = f"filing:{p['gmr_id']}:{p['year']}:{p['source']}"
    fid = uuid.uuid5(uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8"), seed)
    iri = f"http://data.fontem.eu/id/Filing/{fid}"
    company_iri = f"http://data.fontem.eu/id/Company/{p['gmr_id']}"
    out: list[Triple] = [
        Triple(iri, RDF_TYPE, _iri(f"{FONTEM}Filing")),
        Triple(iri, f"{FONTEM}filedBy", _iri(company_iri)),
        Triple(iri, f"{FONTEM}fiscalYear",
               f'"{int(p["year"])}"^^<{XSD_GYEAR}>', is_literal=True),
        Triple(iri, f"{FONTEM}filingSource",
               _lit(p["source"]) or '""', is_literal=True),
    ]
    if fd := (p.get("filing_date") or "").strip()[:10]:
        out.append(Triple(iri, f"{FONTEM}filingDate",
                          f'"{fd}"^^<{XSD_DATE}>', is_literal=True))
    for key, prop in _FILING_FIELD_PROPS.items():
        v = _decimal(p.get(key))
        if v is not None:
            out.append(Triple(iri, prop, v, is_literal=True))
    return out


def render_assert_same_as(p: dict) -> list[Triple]:
    return [
        Triple(p["a_iri"], OWL_SAME_AS, _iri(p["b_iri"])),
    ]


# ── generic-schema renderers ─────────────────────────────────────


def render_upsert_taxonomy_code(p: dict) -> list[Triple]:
    """TaxonomyCode IRI is per-system: id/{System}/{code}.
    Label-cased system name keeps IRIs human-readable (CPV, NUTS, …)."""
    sys_camel = p["system"].replace("-", "_").title().replace("_", "")
    iri = f"http://data.fontem.eu/id/{sys_camel}/{p['code']}"
    out: list[Triple] = [
        Triple(iri, RDF_TYPE, _iri(f"{FONTEM}TaxonomyCode")),
        Triple(iri, RDF_TYPE, _iri(f"{FONTEM}{sys_camel}")),
        Triple(iri, f"{FONTEM}taxonomySystem",
               _lit(p["system"]) or '""', is_literal=True),
        Triple(iri, f"{FONTEM}code",
               _lit(p["code"]) or '""', is_literal=True),
    ]
    if label := _lit(p.get("label"), lang=p.get("label_lang")):
        out.append(Triple(iri, RDFS_LABEL, label, is_literal=True))
    if (parent := p.get("parent_code")):
        parent_iri = f"http://data.fontem.eu/id/{sys_camel}/{parent}"
        # SKOS broader → hierarchical parent.
        out.append(Triple(
            iri, "http://www.w3.org/2004/02/skos/core#broader",
            _iri(parent_iri),
        ))
    if (lvl := p.get("level")) is not None:
        out.append(Triple(iri, f"{FONTEM}level",
                          str(int(lvl)), is_literal=True))
    if desc := _lit(p.get("description")):
        out.append(Triple(iri, f"{FONTEM}description", desc, is_literal=True))
    return out


def render_upsert_relationship(p: dict) -> list[Triple]:
    """A typed edge between two existing IRIs. Predicate is either
    a full IRI or a fontem-shorthand resolved against the FONTEM
    namespace."""
    pred = p["predicate"]
    if not pred.startswith("http"):
        pred = f"{FONTEM}{pred}"
    out: list[Triple] = [
        Triple(p["src_iri"], pred, _iri(p["dst_iri"])),
    ]
    # valid_from / valid_to are surfaced as reified edge metadata
    # via fontem:hasRelationshipMeta — kept off the bare triple so
    # OWL2-RL / SHACL queries stay simple. Skipped here because the
    # producer hasn't asked for time-windowing yet; revisit when
    # gleif_relationships starts caring about expirations.
    return out


def render_upsert_disclosure(p: dict) -> list[Triple]:
    """Disclosure IRI is per-system: id/{System}Disclosure/{disclosure_id}.
    company_gmr_id is optional — when absent (e.g. EU lobbying register
    where the registrant is the Lobbyist itself), the fontem:filedBy
    edge is skipped and the registrant identity rides in details.
    """
    sys_camel = p["system"].replace("-", "_").title().replace("_", "")
    iri = f"http://data.fontem.eu/id/{sys_camel}Disclosure/{p['disclosure_id']}"
    out: list[Triple] = [
        Triple(iri, RDF_TYPE, _iri(f"{FONTEM}Disclosure")),
        Triple(iri, RDF_TYPE, _iri(f"{FONTEM}{sys_camel}Disclosure")),
        Triple(iri, f"{FONTEM}disclosureSystem",
               _lit(p["system"]) or '""', is_literal=True),
        Triple(iri, f"{FONTEM}disclosureId",
               _lit(p["disclosure_id"]) or '""', is_literal=True),
    ]
    if cid := p.get("company_gmr_id"):
        company_iri = f"http://data.fontem.eu/id/Company/{cid}"
        out.append(Triple(iri, f"{FONTEM}filedBy", _iri(company_iri)))
    if dt := _lit(p.get("disclosure_type")):
        out.append(Triple(iri, f"{FONTEM}disclosureType", dt, is_literal=True))
    if fd := (p.get("filed_date") or "").strip()[:10]:
        out.append(Triple(iri, f"{FONTEM}filedDate",
                          f'"{fd}"^^<{XSD_DATE}>', is_literal=True))
    if (yr := p.get("year")) is not None:
        out.append(Triple(iri, f"{FONTEM}year",
                          f'"{int(yr)}"^^<{XSD_GYEAR}>', is_literal=True))
    if title := _lit(p.get("title"), lang="en"):
        out.append(Triple(iri, RDFS_LABEL, title, is_literal=True))
    if url := _lit(p.get("url")):
        out.append(Triple(iri, f"{FONTEM}url", url, is_literal=True))
    # `details` carries system-specific keys — we flatten one level
    # under fontem:detail{Key} so SPARQL can filter without parsing
    # JSON literals. Two-level nesting would need a richer mapping;
    # producers keep `details` flat by convention.
    for k, v in (p.get("details") or {}).items():
        if v is None:
            continue
        if isinstance(v, (int, float)):
            lit = _decimal(v)
            if lit is None:
                continue
            out.append(Triple(
                iri, f"{FONTEM}detail_{k}", lit, is_literal=True,
            ))
        elif isinstance(v, str):
            # Must escape via _lit — Virtuoso's SPARQL parser closes
            # the literal on the first un-escaped quote and 400s on
            # the rest. Real example: eu-cohesion description with
            # an embedded "multi-hazard flooding" jammed virtuoso_sink
            # at seq 4,806,634 from 2026-06-09 until this fix.
            if lit := _lit(v):
                out.append(Triple(
                    iri, f"{FONTEM}detail_{k}", lit, is_literal=True,
                ))
    return out


def render_upsert_exchange_rate(p: dict) -> list[Triple]:
    """ExchangeRate IRI is per-(base, target, date) — easy to dedup,
    easy to query."""
    iri = (
        f"http://data.fontem.eu/id/ExchangeRate/"
        f"{p['base']}-{p['target']}-{p['date']}"
    )
    out: list[Triple] = [
        Triple(iri, RDF_TYPE, _iri(f"{FONTEM}ExchangeRate")),
        Triple(iri, f"{FONTEM}base",
               _lit(p["base"]) or '""', is_literal=True),
        Triple(iri, f"{FONTEM}target",
               _lit(p["target"]) or '""', is_literal=True),
        Triple(iri, f"{FONTEM}date",
               f'"{p["date"]}"^^<{XSD_DATE}>', is_literal=True),
        Triple(iri, f"{FONTEM}rate",
               _decimal(p["rate"]) or '"0"', is_literal=True),
    ]
    if src := _lit(p.get("source")):
        out.append(Triple(iri, f"{FONTEM}rateSource", src, is_literal=True))
    return out


def render_upsert_investment_fund(p: dict) -> list[Triple]:
    """Pooled investment vehicle — a first-class entity, distinct from
    fontem:Company. Same gmr_id namespace as companies; the sink layer
    drops the entity's pre-fund .../id/Company/<gmr_id> subject in the
    same SPARQL update (see _sparql_update) so replays converge."""
    iri = f"http://data.fontem.eu/id/InvestmentFund/{p['gmr_id']}"
    out: list[Triple] = [
        Triple(iri, RDF_TYPE, _iri(f"{FONTEM}InvestmentFund"),
               is_literal=False),
    ]
    if name := _lit(p.get("name"), lang="en"):
        out.append(Triple(iri, RDFS_LABEL, name, is_literal=True))
    if lei := _lit(p.get("lei")):
        out.append(Triple(iri, f"{FONTEM}lei", lei, is_literal=True))
    if country := _lit(p.get("country")):
        out.append(Triple(iri, WDT_P17, country, is_literal=True))
    if (active := p.get("active")) is not None:
        out.append(Triple(iri, f"{FONTEM}active",
                          "true" if active else "false", is_literal=True))
    if lf := _lit(p.get("legal_form")):
        out.append(Triple(iri, f"{FONTEM}legalForm", lf, is_literal=True))
    if ft := _lit(p.get("fund_type")):
        out.append(Triple(iri, f"{FONTEM}fundType", ft, is_literal=True))
    return out


def render_upsert_listing(p: dict) -> list[Triple]:
    iri = f"http://data.fontem.eu/id/Listing/{p['ticker']}"
    # Fund units belong to an InvestmentFund entity, company equity to
    # a Company — same gmr_id either way; the granular security_type
    # decides which subject the listingOf edge points at.
    owner = ("InvestmentFund"
             if is_fund_security_type(p.get("security_type"))
             else "Company")
    company_iri = f"http://data.fontem.eu/id/{owner}/{p['company_gmr_id']}"
    out: list[Triple] = [
        Triple(iri, RDF_TYPE, _iri(f"{FONTEM}Listing")),
        # The Listing → owner linkage. Using fontem:listingOf so the
        # inverse fontem:hasListing falls out via OWL2-RL closure.
        Triple(iri, f"{FONTEM}listingOf", _iri(company_iri)),
        Triple(iri, f"{FONTEM}ticker",
               _lit(p["ticker"]) or '""', is_literal=True),
    ]
    if exch := _lit(p.get("exchange")):
        out.append(Triple(iri, f"{FONTEM}exchange", exch, is_literal=True))
    if cur := _lit(p.get("currency")):
        out.append(Triple(iri, f"{FONTEM}currency", cur, is_literal=True))
    if (active := p.get("active")) is not None:
        out.append(Triple(iri, f"{FONTEM}active",
                          "true" if active else "false", is_literal=True))
    if isin := _lit(p.get("isin")):
        out.append(Triple(iri, f"{FONTEM}isin", isin, is_literal=True))
    if mic := _lit(p.get("mic")):
        out.append(Triple(iri, f"{FONTEM}mic", mic, is_literal=True))
    if st := _lit(p.get("security_type")):
        out.append(Triple(iri, f"{FONTEM}securityType", st,
                          is_literal=True))
    return out


def render_upsert_authority(p: dict) -> list[Triple]:
    iri = f"http://data.fontem.eu/id/Authority/{p['authority_id']}"
    out: list[Triple] = [
        Triple(iri, RDF_TYPE, _iri(f"{FONTEM}Authority")),
    ]
    if name := _lit(p.get("name"), lang="en"):
        out.append(Triple(iri, RDFS_LABEL, name, is_literal=True))
    if country := _lit(p.get("country")):
        out.append(Triple(iri, WDT_P17, country, is_literal=True))
    if at := _lit(p.get("authority_type")):
        out.append(Triple(iri, f"{FONTEM}authorityType", at, is_literal=True))
    if nid := _lit(p.get("national_id")):
        out.append(Triple(iri, f"{FONTEM}nationalId", nid, is_literal=True))
    if url := _lit(p.get("url")):
        out.append(Triple(iri, f"{FONTEM}url", url, is_literal=True))
    if pc := _lit(p.get("postal_code")):
        out.append(Triple(iri, f"{FONTEM}postalCode", pc, is_literal=True))
    if city := _lit(p.get("city")):
        out.append(Triple(iri, f"{FONTEM}city", city, is_literal=True))
    if nuts := _lit(p.get("nuts")):
        out.append(Triple(iri, f"{FONTEM}nuts", nuts, is_literal=True))
    return out


def _contract_value_triples(iri: str, p: dict) -> list[Triple]:
    """Monetary-value triples (eur / currency / original), split out of
    render_upsert_contract to keep its cognitive complexity in check."""
    out: list[Triple] = []
    if (v_eur := p.get("value_eur")) is not None:
        v = _decimal(v_eur)
        if v is not None:
            out.append(Triple(iri, f"{FONTEM}valueEur", v, is_literal=True))
    if cur := _lit(p.get("value_currency")):
        out.append(Triple(iri, f"{FONTEM}valueCurrency", cur, is_literal=True))
    if (v_orig := p.get("value_original")) is not None:
        v = _decimal(v_orig)
        if v is not None:
            out.append(Triple(iri, f"{FONTEM}valueOriginal", v, is_literal=True))
    # Pre-modification totals (legacy F20 self-contains before+after); the
    # before->after delta is the value-change corruption signal.
    if (v_before := p.get("value_before_eur")) is not None:
        v = _decimal(v_before)
        if v is not None:
            out.append(
                Triple(iri, f"{FONTEM}valueBeforeEur", v, is_literal=True)
            )
    if (v_before_orig := p.get("value_before_original")) is not None:
        v = _decimal(v_before_orig)
        if v is not None:
            out.append(
                Triple(iri, f"{FONTEM}valueBeforeOriginal", v, is_literal=True)
            )
    return out


def render_upsert_contract(p: dict) -> list[Triple]:  # pylint: disable=too-many-locals
    # The contract has ~15 schema-defined optional fields (value,
    # currency, dates, authority/company IRIs, CPV, NUTS, lot info)
    # that map 1:1 to local vars used to conditionally emit triples.
    # Splitting just shuffles the same locals across helpers.
    iri = f"http://data.fontem.eu/id/Contract/{p['ted_notice_id']}"
    out: list[Triple] = [
        Triple(iri, RDF_TYPE, _iri(f"{FONTEM}Contract")),
        Triple(iri, f"{FONTEM}tedNoticeId",
               _lit(p["ted_notice_id"]) or '""', is_literal=True),
    ]
    if title := _lit(p.get("title"), lang="en"):
        out.append(Triple(iri, RDFS_LABEL, title, is_literal=True))
    if aid := p.get("authority_id"):
        a_iri = f"http://data.fontem.eu/id/Authority/{aid}"
        out.append(Triple(iri, f"{FONTEM}awardedBy", _iri(a_iri)))
    if cid := p.get("company_gmr_id"):
        c_iri = f"http://data.fontem.eu/id/Company/{cid}"
        out.append(Triple(iri, f"{FONTEM}awardedTo", _iri(c_iri)))
    if pd := (p.get("publication_date") or "").strip()[:10]:
        out.append(Triple(iri, f"{FONTEM}publicationDate",
                          f'"{pd}"^^<{XSD_DATE}>', is_literal=True))
    out.extend(_contract_value_triples(iri, p))
    if cpv := _lit(p.get("cpv")):
        out.append(Triple(iri, f"{FONTEM}cpv", cpv, is_literal=True))
    if nuts := _lit(p.get("nuts")):
        out.append(Triple(iri, f"{FONTEM}nuts", nuts, is_literal=True))
    if lang := _lit(p.get("language")):
        out.append(Triple(iri, f"{FONTEM}language", lang, is_literal=True))
    out.extend(_contract_integrity_triples(iri, p))
    return out


_INTEGRITY_FLAGS = (
    ("is_framework", "isFramework"), ("eu_funded", "euFunded"),
    ("is_single_bidder", "isSingleBidder"), ("is_non_open", "isNonOpen"),
    ("is_no_call", "isNoCall"), ("is_price_only", "isPriceOnly"),
)


def _contract_integrity_triples(iri: str, p: dict) -> list[Triple]:  # pylint: disable=too-many-locals
    """Tender-integrity fields + the shared keystone's red flags, so SPARQL
    carries the same single-bidder / CRI signals as Neo4j. Split out of
    render_upsert_contract to keep that function's branch count in check."""
    out: list[Triple] = []
    if proc := _lit(p.get("procedure_type")):
        out.append(Triple(iri, f"{FONTEM}procedureType", proc, is_literal=True))
    if (tr := p.get("tenders_received")) is not None:
        out.append(Triple(iri, f"{FONTEM}tendersReceived",
                          f'"{int(tr)}"^^<{XSD_INTEGER}>', is_literal=True))
    if crit := _lit(p.get("award_criterion_type")):
        out.append(Triple(iri, f"{FONTEM}awardCriterionType", crit, is_literal=True))
    if dl := (p.get("submission_deadline") or "").strip()[:10]:
        out.append(Triple(iri, f"{FONTEM}submissionDeadline",
                          f'"{dl}"^^<{XSD_DATE}>', is_literal=True))
    if fp := _lit(p.get("funding_programme")):
        out.append(Triple(iri, f"{FONTEM}fundingProgramme", fp, is_literal=True))
    # eForms procedure id + notice type, and (on modifications) the original
    # award's publication-number — the join keys the MODIFIES linking pass uses.
    if proc_id := _lit(p.get("procedure_id")):
        out.append(Triple(iri, f"{FONTEM}procedureId", proc_id, is_literal=True))
    if nt := _lit(p.get("notice_type")):
        out.append(Triple(iri, f"{FONTEM}noticeType", nt, is_literal=True))
    if mpn := _lit(p.get("modifies_publication_number")):
        out.append(Triple(iri, f"{FONTEM}modifiesPublicationNumber", mpn,
                          is_literal=True))
    flags = dict(contract_red_flags(p))
    for fld, pred in _INTEGRITY_FLAGS:
        b = p.get(fld) if fld in ("is_framework", "eu_funded") else flags.get(fld)
        if b is not None:
            out.append(Triple(iri, f"{FONTEM}{pred}",
                              f'"{str(bool(b)).lower()}"^^<{XSD_BOOLEAN}>', is_literal=True))
    if (rf := flags.get("integrity_red_flags")) is not None:
        out.append(Triple(iri, f"{FONTEM}integrityRedFlags",
                          f'"{int(rf)}"^^<{XSD_INTEGER}>', is_literal=True))
    return out


# Renderer registry. None = control event; sink doesn't render
# triples but uses the event for structural decisions.
RENDERERS: dict[str, Callable[[dict], list[Triple]] | None] = {
    "BeginGraphReplace": None,
    "EndGraphReplace": None,
    "UpsertCompany": render_upsert_company,
    "UpsertInvestmentFund": render_upsert_investment_fund,
    "UpsertListing": render_upsert_listing,
    "UpsertSanctionedEntity": render_upsert_sanctioned_entity,
    "UpsertFiling": render_upsert_filing,
    "UpsertAuthority": render_upsert_authority,
    "UpsertContract": render_upsert_contract,
    "UpsertTaxonomyCode": render_upsert_taxonomy_code,
    "UpsertRelationship": render_upsert_relationship,
    "UpsertDisclosure": render_upsert_disclosure,
    "UpsertExchangeRate": render_upsert_exchange_rate,
    "AssertSameAs": render_assert_same_as,
}


def to_turtle(triples: list[Triple]) -> str:
    """Render a triple list as a Turtle document. No prefixes —
    we use full IRIs throughout for unambiguous parsing on the
    Virtuoso side."""
    lines = []
    for t in triples:
        s = _iri(t.s) if not t.s.startswith("<") else t.s
        p = _iri(t.p) if not t.p.startswith("<") else t.p
        o = t.o
        lines.append(f"{s} {p} {o} .")
    return "\n".join(lines) + ("\n" if lines else "")
