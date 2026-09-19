"""
Shared semantic-similarity utilities (sentence-transformers embeddings) used
across the KAG pipeline for exemplar retrieval, table retrieval, and
schema-fixation (repair), as a complement to the existing lexical
(token-overlap / difflib character-overlap) signals.

Why lexical alone isn't enough: it mis-ranks genuine paraphrases with low
word overlap ("how many sars were filed per month" vs. the exemplar bank's
wording), and worse, it can't tell a plausible-but-wrong exemplar match from
a real one when both share plenty of surrounding vocabulary -- e.g.
"minimum and maximum risk score" vs. the stored "average and 90th percentile
risk score" scored 0.64 Jaccard (past the old 0.6 threshold) despite asking
for a different statistic entirely, while their embeddings score only 0.58,
correctly well below genuine-paraphrase pairs (~0.93+).

Lazy-loaded singleton: the model (~80MB, all-MiniLM-L6-v2) only loads the
first time it's actually needed, so callers that never touch semantic search
(e.g. schema_validator running standalone, or a pure exact-repeat exemplar
hit) don't pay the load cost.
"""

_MODEL = None
MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"


def get_embedder():
    global _MODEL
    if _MODEL is None:
        from sentence_transformers import SentenceTransformer
        _MODEL = SentenceTransformer(MODEL_NAME)
    return _MODEL


def embed(texts):
    """texts: a str, or a list of str. Returns a single normalized vector for
    a str input, or a 2D array (one row per text) for a list input. Vectors
    are L2-normalized, so cosine_sim() is just a dot product."""
    single = isinstance(texts, str)
    vecs = get_embedder().encode([texts] if single else list(texts), normalize_embeddings=True)
    return vecs[0] if single else vecs


def cosine_sim(a, b):
    """a, b: normalized embedding vectors from embed()."""
    return float(a @ b)
