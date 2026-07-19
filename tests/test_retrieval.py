from app.retrieval import rank_candidates


def test_rank_candidates_finds_the_closest_match():
    query_texts = [
        "Footing depth shall be at least 4 feet below grade per local frost line requirements.",
        "Concrete strength shall be 4000 psi at 28 days.",
    ]
    corpus_texts = [
        "1607.1 Minimum foundation footing depth shall be established based on local frost penetration data.",
        "1905.1 Concrete compressive strength requirements for structural elements.",
        "3001.1 Elevator car dimensions and clearances.",
    ]

    results = rank_candidates(query_texts, corpus_texts, top_n=2)

    assert results[0][0] == 0  # footing query -> footing corpus entry ranked first
    assert results[1][0] == 1  # concrete query -> concrete corpus entry ranked first
    assert 2 not in results[0]  # elevator clause has no overlap, excluded
    assert 2 not in results[1]


def test_rank_candidates_handles_empty_inputs():
    assert rank_candidates([], ["some text"]) == []
    assert rank_candidates(["some text"], []) == [[]]
