# rag_engine.py

import math
import re
from collections import Counter
from typing import List, Dict, Tuple

# =========================
# SIMPLE RAG KNOWLEDGE BASE
# =========================

FINANCE_DOCS: List[Dict] = [
    {
        "id": "planning_1",
        "title": "Basic Financial Planning",
        "text": (
            "Good financial planning usually starts with tracking income and expenses, "
            "building an emergency fund of 3–6 months of essential expenses, paying high-interest "
            "debt first, and then investing for long-term goals such as retirement."
        ),
    },
    {
        "id": "investing_1",
        "title": "Simple Investment Strategy",
        "text": (
            "For most individuals, a diversified portfolio using low-cost index funds, "
            "regular contributions, and a long-term horizon tends to perform better than frequent trading. "
            "Asset allocation should reflect risk tolerance, time horizon, and goals."
        ),
    },
    {
        "id": "taxes_1",
        "title": "General Tax Considerations",
        "text": (
            "Tax rules differ by country. In many places, investment income can include dividends, "
            "capital gains, and interest. Using tax-advantaged accounts, keeping records of transactions, "
            "and consulting a qualified tax professional are important."
        ),
    },
    {
        "id": "budgeting_1",
        "title": "50/30/20 Budget Rule",
        "text": (
            "A popular budgeting rule is 50/30/20: 50% of net income for needs, 30% for wants, "
            "and 20% for savings and debt repayment. Adjust these percentages based on your situation."
        ),
    },
]

# Precompute token vectors for docs at import time

def _tokenize(text: str) -> List[str]:
    # Simple tokenizer: words of length >= 2, lowercase
    return [t for t in re.findall(r"\b\w+\b", text.lower()) if len(t) >= 2]


def _vectorize(tokens: List[str]) -> Counter:
    return Counter(tokens)


def _cosine_sim(v1: Counter, v2: Counter) -> float:
    # dot product
    dot = sum(v1[t] * v2.get(t, 0) for t in v1)
    # norms
    norm1 = math.sqrt(sum(c * c for c in v1.values()))
    norm2 = math.sqrt(sum(c * c for c in v2.values()))
    if norm1 == 0 or norm2 == 0:
        return 0.0
    return dot / (norm1 * norm2)


# Build doc vectors once
DOC_VECTORS: List[Tuple[Dict, Counter]] = []
for d in FINANCE_DOCS:
    tokens = _tokenize(d["text"])
    vec = _vectorize(tokens)
    DOC_VECTORS.append((d, vec))


def retrieve_docs(query: str, k: int = 3) -> List[Dict]:
    """Retrieve top-k docs by cosine similarity with a simple BoW model."""
    q_tokens = _tokenize(query)
    q_vec = _vectorize(q_tokens)

    scored: List[Tuple[float, Dict]] = []
    for doc, vec in DOC_VECTORS:
        score = _cosine_sim(q_vec, vec)
        scored.append((score, doc))

    scored.sort(key=lambda x: x[0], reverse=True)
    top_docs = [doc for score, doc in scored[:k] if score > 0]
    return top_docs or [FINANCE_DOCS[0]]  # fallback to something


def answer_with_rag(question: str) -> str:
    """
    Build a simple answer using only our local KB.
    No external AI calls, no APIs.
    """
    docs = retrieve_docs(question, k=3)

    parts = []
    parts.append("Here is some general guidance based on the internal knowledge base:\n")

    for doc in docs:
        parts.append(f"### {doc['title']}\n{doc['text']}\n")

    parts.append(
        "\nThis information is **general and educational only**. "
        "For detailed, personalized financial, legal or tax advice, "
        "you should consult a qualified professional in your country."
    )

    return "\n".join(parts)
