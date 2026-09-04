"""Contract value fields the company page needs, and their caveats.

The Virtuoso projection carried valueEur but none of the quality
signals, so a reader could see a figure and had no way to tell a
trustworthy one from `implausible_magnitude`. The Neo4j read path never
had that problem — trusted_value_sum filters on value_low_confidence
before summing — which is exactly why moving the company page onto
Virtuoso needed these projected first.

Booleans are emitted only when true. `false` on 750k contracts is 750k
triples saying nothing, and absent already means "not flagged".
"""

from virtuoso_sink.triples import FONTEM, render_upsert_contract

_BASE = {"ted_notice_id": "2026-000123"}


def _preds(payload: dict) -> dict[str, str]:
    return {
        t.p.replace(FONTEM, ""): t.o
        for t in render_upsert_contract({**_BASE, **payload})
    }


def test_low_confidence_emitted_only_when_true():
    assert "valueLowConfidence" in _preds({"value_low_confidence": True})
    assert "valueLowConfidence" not in _preds({"value_low_confidence": False})
    assert "valueLowConfidence" not in _preds({})


def test_payable_discrepancy_emitted_only_when_true():
    assert "valuePayableDiscrepancy" in _preds({"value_payable_discrepancy": True})
    assert "valuePayableDiscrepancy" not in _preds({"value_payable_discrepancy": False})


def test_quality_flag_carries_the_actionable_values():
    """'ok' is the common case and tells a reader nothing; the other
    seven are the point of the field."""
    assert "valueQualityFlag" not in _preds({"value_quality_flag": "ok"})
    for flag in ("implausible_magnitude", "value_disagreement", "zero_value"):
        preds = _preds({"value_quality_flag": flag})
        assert preds.get("valueQualityFlag") == f'"{flag}"', flag


def test_confidence_and_estimate_are_numeric():
    preds = _preds({"value_confidence": 0.42, "estimated_value_eur": 1234.5})
    assert "valueConfidence" in preds
    assert "estimatedValueEur" in preds
    assert "0.42" in preds["valueConfidence"]


def test_zero_confidence_is_not_dropped():
    """0.0 is the strongest possible statement this value is untrusted;
    a falsy-check would silently discard exactly the rows that matter."""
    assert "valueConfidence" in _preds({"value_confidence": 0})


def test_ted_publication_number_is_projected():
    """The UI builds the TED link from it. Absent, every row on a
    50-contract page needs a runtime lookup round trip."""
    preds = _preds({"ted_publication_number": "295342-2026"})
    assert preds.get("tedPublicationNumber") == '"295342-2026"'


def test_quality_fields_reach_the_notice_grain_too():
    """Both grains share _contract_value_triples; a field that only
    landed on the legacy subject would be invisible for every contract
    ingested since the regrain."""
    triples = render_upsert_contract({
        **_BASE, "contract_key": "ck-1", "value_low_confidence": True,
        "ted_publication_number": "295342-2026",
    })
    preds = {t.p.replace(FONTEM, "") for t in triples}
    assert "valueLowConfidence" in preds
    assert "tedPublicationNumber" in preds
