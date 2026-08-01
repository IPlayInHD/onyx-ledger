from app.integrations.embeddings import LocalHashEmbedder


def test_embedder_is_deterministic_and_normalized():
    e = LocalHashEmbedder()
    v1 = e.embed("medical expense credit")
    v2 = e.embed("medical expense credit")
    assert v1 == v2
    assert len(v1) == e.dim == 1536
    norm = sum(x * x for x in v1) ** 0.5
    assert abs(norm - 1.0) < 1e-6


def test_similar_text_is_closer_than_unrelated():
    e = LocalHashEmbedder()

    def cos(a, b):
        return sum(x * y for x, y in zip(a, b, strict=True))  # both unit-normalized

    q = e.embed("can I claim medical expenses")
    medical = e.embed("Medical Expense Tax Credit for eligible medical expenses")
    rrsp = e.embed("RRSP contribution retirement savings deduction")
    assert cos(q, medical) > cos(q, rrsp)
