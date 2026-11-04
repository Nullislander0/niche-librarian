from engine.rank import cross


class FakeEncoder:
    def rerank(self, query, texts):
        return [10.0 if "certificate" in t else 0.0 for t in texts]


def test_rescore_lifts_what_the_cross_encoder_prefers(monkeypatch):
    r = cross.CrossReranker("fake", candidates=2, alpha=0.7)
    monkeypatch.setattr(r, "_encoder", lambda: FakeEncoder())
    cands = [{"rowid": 1, "title": "a", "text": "unrelated"},
             {"rowid": 2, "title": "b", "text": "import the certificate"},
             {"rowid": 3, "title": "c", "text": "low fused score"}]
    out = r.rescore("certificate error", cands, {1: 1.0, 2: 0.6, 3: 0.1})
    assert out[2] > out[1] > out[3]   # reranked pair reordered; the rest only scaled
    assert abs(out[3] - 0.3 * 0.1) < 1e-9
