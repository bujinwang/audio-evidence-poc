"""Embedding providers.

`sentence-transformers` is the real semantic model. If it is not installed the
app falls back to a deterministic hashing embedder so the semantic path still
functions end-to-end — but that fallback is *lexical*, not semantic, and it says
so in its name (`hashing-lexical-v1`) and in the API/UI. We would rather show an
honest weak retriever than a broken button.
"""

from __future__ import annotations

import hashlib
import importlib.util
import math
from dataclasses import dataclass

from ..text_utils import tokenize


class Embedder:
    name = "none"
    dim = 0
    is_semantic = False

    def available(self) -> tuple[bool, str]:
        return False, "not configured"

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


class HashingEmbedder(Embedder):
    """Signed feature hashing over word unigrams/bigrams + character 4-grams.

    Stopwords are dropped and unigrams dominate, which makes this behave like a
    fuzzy bag-of-content-words rather than a semantic model. It is honest about
    that: `is_semantic` is False and the name says `lexical`.
    """

    name = "hashing-lexical-v2"
    dim = 512
    is_semantic = False
    CHAR_WEIGHT = 0.2
    BIGRAM_WEIGHT = 0.7

    def available(self) -> tuple[bool, str]:
        return True, "deterministic lexical fallback (no model download required)"

    def _features(self, text: str) -> list[tuple[str, float]]:
        words = tokenize(text)
        features: list[tuple[str, float]] = [(f"w:{w}", 1.0) for w in words]
        features += [
            (f"b:{words[i]}_{words[i + 1]}", self.BIGRAM_WEIGHT)
            for i in range(len(words) - 1)
        ]
        compact = " ".join(words)
        features += [
            (f"c:{compact[i:i + 4]}", self.CHAR_WEIGHT)
            for i in range(max(len(compact) - 3, 0))
        ]
        return features

    def _vector(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for feature, weight in self._features(text):
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "big") % self.dim
            sign = 1.0 if digest[4] & 1 else -1.0
            vec[bucket] += sign * weight
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]


class SentenceTransformerEmbedder(Embedder):
    name = "sentence-transformers"
    is_semantic = True

    def __init__(self, model_name: str):
        self.model_name = model_name
        self._model = None
        self.dim = 384

    def available(self) -> tuple[bool, str]:
        if importlib.util.find_spec("sentence_transformers") is None:
            return False, "sentence-transformers not installed (`pip install sentence-transformers`)"
        return True, f"sentence-transformers ({self.model_name})"

    def _load(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self.model_name)
            # `get_sentence_embedding_dimension` was renamed in sentence-transformers
            # 6.x; keep the old name as a fallback so older installs still work.
            getter = getattr(
                self._model,
                "get_embedding_dimension",
                self._model.get_sentence_embedding_dimension,
            )
            self.dim = int(getter())
        return self._model

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        model = self._load()
        vectors = model.encode(texts, normalize_embeddings=True, batch_size=32)
        return [list(map(float, v)) for v in vectors]


def get_embedder(preference: str = "auto", model_name: str = "") -> Embedder:
    preference = (preference or "auto").lower()
    if preference in {"hashing", "lexical", "none"}:
        return HashingEmbedder()
    st = SentenceTransformerEmbedder(model_name or "sentence-transformers/all-MiniLM-L6-v2")
    if preference in {"sentence-transformers", "sentence_transformers", "st"}:
        return st
    if st.available()[0]:
        return st
    return HashingEmbedder()


@dataclass(frozen=True)
class EmbeddingInfo:
    name: str
    dim: int
    is_semantic: bool
