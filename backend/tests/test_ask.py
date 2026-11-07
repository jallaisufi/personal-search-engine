from app.ask import build_user_prompt, cited_numbers
from app.llm import FakeLLM

SOURCES = [
    {
        "title": "lease-agreement.md",
        "heading_path": "Lease > Security Deposit",
        "page": None,
        "text": "The deposit is three months' rent. It is returned within six months.",
    },
    {
        "title": "trip.pdf",
        "heading_path": None,
        "page": 2,
        "text": "Submit receipts in four weeks.",
    },
]


def test_prompt_numbers_sources_with_their_location():
    prompt = build_user_prompt("How much is the deposit?", SOURCES)
    assert "[1] lease-agreement.md / Lease > Security Deposit\nThe deposit" in prompt
    assert "[2] trip.pdf / page 2\nSubmit receipts" in prompt
    assert prompt.endswith("Question: How much is the deposit?")


def test_cited_numbers_keeps_valid_citations_in_order():
    answer = "Three months [2]. Returned later [1][2]. Also [7] and [0]."
    assert cited_numbers(answer, source_count=2) == [2, 1]


async def test_fake_llm_answers_from_the_first_source_with_a_citation():
    prompt = build_user_prompt("How much is the deposit?", SOURCES)
    answer = "".join([piece async for piece in FakeLLM().stream("system", prompt)]).strip()
    assert answer == "The deposit is three months' rent. [1]"


async def test_fake_llm_without_sources():
    answer = "".join([p async for p in FakeLLM().stream("system", "Question: anything?")])
    assert answer == "I couldn't find this in your documents."
