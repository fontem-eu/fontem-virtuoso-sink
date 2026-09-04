"""AssertSameAs must not wipe anything — not the subject, not itself.

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

Scoping it to owl:sameAs fixed that and introduced a quieter bug in its
place. A scoped DELETE clears every prior `<subject> owl:sameAs ?o`, so a
subject could only ever hold the LAST equivalence written. Measured in
prod 2026-09-04: 27,452 subjects carried owl:sameAs and every single one
had exactly 1 — not one had 2, out of 1.34M assertions across ~541k
nodes. An entity with three duplicates kept one at random.

Replace semantics only made sense while the stream was a continuously
recomputed set of guesses. An AssertSameAs is now a single approved
equivalence: a discrete fact that accumulates. Withdrawal is the job of
RetractSameAs, which removes one specific equivalence, not of the next
assertion silently clearing the others.
"""
from virtuoso_sink.sink import _delete_clause
from virtuoso_sink.sink import _PRESERVED_ON_REPLACE
from virtuoso_sink.triples import (
    ADDITIVE_EVENTS, OWL_SAME_AS, RETRACTION_EVENTS,
    SCOPED_REPLACE_PREDICATES, render_retract_same_as,
)

_G = "http://data.fontem.eu/graph/company"
_S = "http://data.fontem.eu/id/Company/abc"


def test_assert_same_as_is_additive():
    assert "AssertSameAs" in ADDITIVE_EVENTS
    assert "AssertSameAs" not in SCOPED_REPLACE_PREDICATES


def test_assert_same_as_deletes_nothing():
    """The whole point. An assertion states one fact and removes none.

    A bare `?p ?o` would take the entity's whole record (the 2026-09-02
    incident); a scoped delete on owl:sameAs would take the subject's
    other equivalences (the 2026-09-04 one). Neither is acceptable, so
    the clause must be empty.
    """
    clause = _delete_clause(_G, _S, "AssertSameAs")
    assert clause == "", (
        "AssertSameAs is deleting something — an assertion accumulates, "
        "it does not replace"
    )


def test_a_subject_can_hold_several_equivalences():
    """The regression that produced exactly-one-sameAs-per-subject.

    Two assertions about the same subject must both survive: a company
    with three duplicates has three equivalences, not the last one
    written.
    """
    assert _delete_clause(_G, _S, "AssertSameAs") == ""


def test_retraction_removes_both_directions():
    """owl:sameAs is symmetric and which direction was written depends on
    which side the consolidator treated as source. Removing one leaves
    the equivalence standing."""
    triples = render_retract_same_as({"a_iri": "urn:a", "b_iri": "urn:b"})
    assert len(triples) == 2
    assert all(t.p == OWL_SAME_AS for t in triples)
    subjects = {t.s for t in triples}
    assert subjects == {"urn:a", "urn:b"}


def test_retraction_is_registered_as_a_delete():
    assert "RetractSameAs" in RETRACTION_EVENTS
    assert "RetractSameAs" not in ADDITIVE_EVENTS


def test_upsert_still_wipes_the_whole_subject():
    """The default branch is correct for events that carry a full
    description — this fix must not weaken it."""
    clause = _delete_clause(_G, _S, "UpsertCompany")
    assert "?p ?o" in clause


def test_upsert_and_assert_no_longer_destroy_each_other():
    """The two streams must not overlap: the upsert owns the entity's
    attributes, the assertion owns its equivalences, and an assertion
    deletes nothing at all."""
    assert _delete_clause(_G, _S, "AssertSameAs") == ""
    upsert_clause = _delete_clause(_G, _S, "UpsertCompany")
    assert f"FILTER(?p != <{OWL_SAME_AS}>)" in upsert_clause


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
