from app.search.hybrid import keyword_query


def test_questions_keep_only_meaningful_words_joined_with_or():
    assert keyword_query("When is the design review for Project Atlas?") == (
        "design | review | project | atlas"
    )


def test_german_stopwords_are_dropped_too():
    assert keyword_query("Wann ist die Steuererklärung fällig?") == "steuererklärung | fällig"


def test_exact_tokens_survive():
    assert keyword_query("mTLS rollout") == "mtls | rollout"


def test_duplicates_and_punctuation():
    assert keyword_query("Rent, rent & RENT!") == "rent"


def test_only_stopwords_gives_empty_query():
    assert keyword_query("what is the") == ""


def test_operators_in_input_cannot_break_the_tsquery():
    assert keyword_query("a & (b | !c) <-> 'd'") == "b | c | d"
