"""AssertSameAs must not wipe the subject it is describing.

_delete_clause has two branches: a scoped replace that clears only the
event's own predicates, and a default that clears the whole subject. The
default is right for an Upsert, which carries a full description and owns
the entity's attributes. It is catastrophic for AssertSameAs, which
carries ONE triple about a subject somebody else describes.

Measured in prod on 2026-09-02, hours after the consolidator's emit was
enabled: every subject carrying owl:sameAs had exactly 1 property where an
untouched company has 7 (type, label, lei, country, active, legalForm,
postalCode), and 27,696 companies had been stripped. It also made the two
event types fight — an UpsertCompany landing after an AssertSameAs removed
the equivalence again, so only ~15% of emitted events survived as triples.
"""
from virtuoso_sink.sink import _delete_clause
from virtuoso_sink.sink import _PRESERVED_ON_REPLACE
from virtuoso_sink.triples import OWL_SAME_AS, SCOPED_REPLACE_PREDICATES

_G = "http://data.fontem.eu/graph/company"
_S = "http://data.fontem.eu/id/Company/abc"


def test_assert_same_as_is_scoped():
    assert SCOPED_REPLACE_PREDICATES.get("AssertSameAs") == (OWL_SAME_AS,)


def test_assert_same_as_clause_touches_only_same_as():
    """The regression: a bare ?p would take the entity's whole record."""
    clause = _delete_clause(_G, _S, "AssertSameAs")
    assert OWL_SAME_AS in clause
    assert "?p ?o" not in clause, (
        "AssertSameAs is deleting every predicate of its subject — this is "
        "what stripped 27,696 companies in prod"
    )


def test_assert_same_as_still_replaces_rather_than_accumulates():
    """Scoped, not skipped: re-asserting for a subject whose matches
    changed must not leave the stale equivalences behind."""
    clause = _delete_clause(_G, _S, "AssertSameAs")
    assert clause.strip().startswith("DELETE WHERE")
    assert _S in clause


def test_upsert_still_wipes_the_whole_subject():
    """The default branch is correct for events that carry a full
    description — this fix must not weaken it."""
    clause = _delete_clause(_G, _S, "UpsertCompany")
    assert "?p ?o" in clause


def test_upsert_and_assert_no_longer_destroy_each_other():
    """The two clauses must not overlap beyond owl:sameAs, or whichever
    event lands last wins and the other's work disappears."""
    assert_clause = _delete_clause(_G, _S, "AssertSameAs")
    # The assert clause must be narrow enough that an UpsertCompany's
    # attributes (label, lei, country...) are not in its blast radius.
    for pred in ("rdf-schema#label", "ontology#lei", "ontology#legalForm"):
        assert pred not in assert_clause


def test_upsert_replace_preserves_same_as():
    """The other half of the incident.

    Scoping AssertSameAs stopped it destroying company attributes, but
    Upserts flow continuously from the ETL and kept destroying the
    equivalences — so only ~15% of emitted AssertSameAs survived as
    triples. A whole-subject replace must leave predicates another
    producer owns alone, or the two streams still overwrite each other.

    Verified against prod Virtuoso: seeding a subject with an lei and a
    sameAs then applying this clause leaves 1 triple — the sameAs kept,
    the lei cleared.
    """
    clause = _delete_clause(_G, _S, "UpsertCompany")
    assert f"FILTER(?p != <{OWL_SAME_AS}>)" in clause
    # It must still be a DELETE/WHERE that clears everything else.
    assert clause.strip().startswith("DELETE {")
    assert "?p ?o" in clause


def test_upsert_replace_still_clears_ordinary_predicates():
    """Preserving sameAs must not turn the replace into an append —
    the upsert still owns every attribute it writes."""
    clause = _delete_clause(_G, _S, "UpsertCompany")
    # Only sameAs is exempt; nothing else is filtered out of the delete.
    assert clause.count("FILTER(") == 1


def test_preserved_set_is_minimal():
    """Anything added here stops being cleaned up by the producer that
    writes the rest of the subject, so the list must stay deliberate."""
    assert _PRESERVED_ON_REPLACE == (OWL_SAME_AS,)
