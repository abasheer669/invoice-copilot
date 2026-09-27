from collections import Counter
from pathlib import Path

import pytest

from ap_agent.ingest import (
    IngestError,
    chunk_document,
    load_corpus,
    load_golden,
    parse_document,
    validate_corpus,
)
from ap_agent.rules_config import load_rules

DATA = Path(__file__).parents[2] / "data"
DOCS = load_corpus(DATA / "corpus")
CHUNKS = {c.chunk_id: c for doc in DOCS for c in chunk_document(doc)}

FRONT_MATTER = """---
document_id: FIN-POL-099
title: Test Policy
version: 2.10
effective_date: 2026-01-01
status: current
classification: internal
---

# Test Policy

## 1. Scope

Applies to tests.
"""


def test_all_fifteen_documents_are_ingested_with_their_type():
    assert len(DOCS) == 15
    types = Counter((d.doc_type, d.meta.status) for d in DOCS)
    assert types == {
        ("policy", "current"): 12,
        ("policy", "superseded"): 1,
        ("supplier_document", "untrusted"): 1,
        ("reference_extract", "untrusted"): 1,
    }


def test_one_chunk_per_section_and_one_for_documents_without_sections():
    assert len(CHUNKS) == 58
    tolerances = CHUNKS["FIN-POL-002#tolerances"]
    assert (tolerances.section, tolerances.citation) == ("§2 Tolerances", "FIN-POL-002 v2.4 §2")
    assert tolerances.text.startswith("For goods, a line is within tolerance")


@pytest.mark.parametrize(
    "chunk_id, citation",
    [
        ("FIN-POL-003-OLD#body", "FIN-POL-003-OLD v1.0 (SUPERSEDED)"),
        ("ADV-001#body", "ADV-001 v1.0 (UNTRUSTED)"),
        ("ADV-002#body", "ADV-002 v7.0 (UNTRUSTED)"),
    ],
)
def test_citations_label_documents_that_are_not_current_policy(chunk_id, citation):
    assert CHUNKS[chunk_id].citation == citation


def test_embedded_text_carries_a_context_header():
    header = CHUNKS["FIN-POL-002#tolerances"].embed_text.splitlines()[0]
    assert (
        header == "[FIN-POL-002 v2.4 · Three-Way Matching and Tolerances · §2 Tolerances · current]"
    )


def test_front_matter_values_stay_strings(tmp_path):
    path = tmp_path / "policy.md"
    path.write_text(FRONT_MATTER)
    assert parse_document(path).meta.version == "2.10"  # not the float 2.1


def test_incomplete_front_matter_is_rejected(tmp_path):
    path = tmp_path / "policy.md"
    path.write_text(FRONT_MATTER.replace("status: current\n", ""))
    with pytest.raises(IngestError, match="policy.md: invalid front-matter"):
        parse_document(path)


def test_the_supplied_corpus_matches_the_rule_values():
    validate_corpus(DOCS, load_rules())


def test_two_current_versions_of_one_policy_are_rejected(tmp_path):
    path = tmp_path / "policy.md"
    path.write_text(FRONT_MATTER.replace("FIN-POL-099", "FIN-POL-002"))
    with pytest.raises(IngestError, match="more than one current version of FIN-POL-002"):
        validate_corpus([*DOCS, parse_document(path)], load_rules())


def test_rule_values_copied_from_an_older_policy_version_are_rejected():
    rules = load_rules().model_copy(deep=True)
    rules.sources["FIN-POL-002"] = "2.3"
    with pytest.raises(IngestError, match="copied from FIN-POL-002 v2.3 but the corpus has v2.4"):
        validate_corpus(DOCS, rules)


def test_golden_queries_load():
    assert {q.expect for q in load_golden(DATA / "golden_queries.yaml")} >= {
        "FIN-POL-002",
        "FIN-POL-003",
        "FIN-POL-005",
    }
