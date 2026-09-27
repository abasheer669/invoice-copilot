"""Text embeddings behind one small interface, chosen by EMBED_PROVIDER.

Documents and queries are embedded differently (Gemini task types), so the two calls
stay separate. `model_id` is stored with every index so a search can refuse to mix models.
"""

import hashlib
import math
import re
from typing import Protocol

from google import genai
from google.genai import errors, types

from ap_agent.config import Settings
from ap_agent.tools import TransientError


class Embedder(Protocol):
    model_id: str
    dim: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class GeminiEmbedder:
    BATCH = 100  # texts per request

    def __init__(self, api_key: str, model: str, dim: int):
        self.model_id = model
        self.dim = dim
        self._client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=30_000))

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for start in range(0, len(texts), self.BATCH):
            vectors += self._embed(texts[start : start + self.BATCH], "RETRIEVAL_DOCUMENT")
        return vectors

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text], "RETRIEVAL_QUERY")[0]

    def _embed(self, texts: list[str], task_type: str) -> list[list[float]]:
        config = types.EmbedContentConfig(task_type=task_type, output_dimensionality=self.dim)
        try:
            response = self._client.models.embed_content(
                model=self.model_id, contents=texts, config=config
            )
        except errors.ServerError as e:
            raise TransientError("Gemini embedding service unavailable") from e
        except errors.ClientError as e:
            if e.code == 429:
                raise TransientError("Gemini rate limit reached; try again in a minute") from e
            raise
        return [list(e.values) for e in response.embeddings]


class FakeEmbedder:
    """Offline and deterministic: a hashed bag of words. Texts that share words score
    higher, which is enough to test retrieval without a network or an API key."""

    STOPWORDS = frozenset(
        "a an and are as at be by for from has in is it its of on or that the this to "
        "was were will with must may not no any".split()
    )

    def __init__(self, dim: int = 256):
        self.model_id = f"fake-bow-{dim}"
        self.dim = dim

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        for word in re.findall(r"[a-z0-9]+", text.lower()):
            if word in self.STOPWORDS:
                continue
            word = word[:-1] if len(word) > 4 and word.endswith("s") else word
            digest = hashlib.blake2b(word.encode(), digest_size=8).digest()
            vector[int.from_bytes(digest) % self.dim] += 1.0
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return [v / norm for v in vector]


def make_embedder(settings: Settings) -> Embedder:
    if settings.embed_provider == "fake":
        return FakeEmbedder()
    if settings.llm_api_key is None or not settings.llm_api_key.get_secret_value():
        raise ValueError("LLM_API_KEY is not set; it is needed for Gemini embeddings")
    return GeminiEmbedder(
        settings.llm_api_key.get_secret_value(), settings.embed_model, settings.embed_dim
    )
