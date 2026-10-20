"""Shared tokenisation used by keyword search, snippets, and the hashing embedder."""

from __future__ import annotations

import re

WORD_RE = re.compile(r"[a-z0-9']+")

STOPWORDS = frozenset(
    {
        "a", "about", "an", "and", "any", "are", "as", "at", "be", "been", "by",
        "did", "do", "does", "for", "from", "had", "has", "have", "he", "her",
        "him", "his", "i", "in", "is", "it", "its", "me", "my", "near", "of",
        "on", "or", "our", "she", "some", "that", "the", "their", "them", "then",
        "there", "they", "this", "to", "was", "we", "were", "what", "when",
        "where", "which", "who", "with", "you",
    }
)


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens, stopwords and single characters removed."""
    return [
        token
        for token in WORD_RE.findall((text or "").lower())
        if len(token) > 1 and token not in STOPWORDS
    ]


def extract_terms(query: str) -> list[str]:
    """Content terms of a query, used for highlighting and snippets."""
    return tokenize(query)
