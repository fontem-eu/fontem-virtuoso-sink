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

from dataclasses import dataclass
from typing import Callable

FONTEM = "http://data.fontem.eu/ontology#"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
SKOS_ALT_LABEL = "http://www.w3.org/2004/02/skos/core#altLabel"
WDT_P17 = "http://www.wikidata.org/prop/direct/P17"  # country
XSD_DATE = "http://www.w3.org/2001/XMLSchema#date"
XSD_DECIMAL = "http://www.w3.org/2001/XMLSchema#decimal"
XSD_GYEAR = "http://www.w3.org/2001/XMLSchema#gYear"

OWL_SAME_AS = "http://www.w3.org/2002/07/owl#sameAs"


@dataclass(frozen=True)
class Triple:
    s: str  # subject IRI
    p: str  # predicate IRI
    o: str  # object: full IRI or already-formatted Turtle literal
    is_literal: bool = False


def _iri(s: str) -> str:
    return f"<{s}>"


def _lit(value, *, lang: str | None = None,
         datatype: str | None = None) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    s = str(value).replace("\\", "\\\\").replace('"', '\\"')
    s = s.replace("\n", "\\n").replace("\r", "\\r")
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
    import uuid
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


# Renderer registry. None = control event; sink doesn't render
# triples but uses the event for structural decisions.
RENDERERS: dict[str, Callable[[dict], list[Triple]] | None] = {
    "BeginGraphReplace": None,
    "EndGraphReplace": None,
    "UpsertCompany": render_upsert_company,
    "UpsertSanctionedEntity": render_upsert_sanctioned_entity,
    "UpsertFiling": render_upsert_filing,
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
