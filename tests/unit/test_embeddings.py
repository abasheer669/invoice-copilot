import math

import pytest

from ap_agent.config import Settings
from ap_agent.embeddings import FakeEmbedder, make_embedder


def cosine(a, b):
    return sum(x * y for x, y in zip(a, b, strict=True))


def test_fake_embedder_is_deterministic_and_normalised():
    embedder = FakeEmbedder()
    first = embedder.embed_query("duplicate invoice already paid")
    assert first == embedder.embed_documents(["duplicate invoice already paid"])[0]
    assert len(first) == embedder.dim
    assert math.isclose(math.sqrt(sum(v * v for v in first)), 1.0)


def test_fake_embedder_scores_shared_words_higher():
    embedder = FakeEmbedder()
    query = embedder.embed_query("duplicate invoices")
    related = embedder.embed_query("checking for a duplicate invoice")
    unrelated = embedder.embed_query("travel meal limits for dinner")
    assert cosine(query, related) > cosine(query, unrelated)


def test_provider_comes_from_config():
    embedder = make_embedder(Settings(_env_file=None, embed_provider="fake"))
    assert embedder.model_id == "fake-bow-256"


def test_gemini_needs_an_api_key():
    with pytest.raises(ValueError, match="LLM_API_KEY is not set"):
        make_embedder(Settings(_env_file=None, embed_provider="gemini"))
