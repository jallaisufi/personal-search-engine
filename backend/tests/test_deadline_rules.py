from datetime import date

import pytest

from app.deadline_rules import build_prompt, find_candidates, find_dates, parse_answer

REF = date(2026, 11, 7)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("due 2026-11-20.", date(2026, 11, 20)),
        ("design review by 20 November 2026", date(2026, 11, 20)),
        ("by the 3rd December 2026 at the latest", date(2026, 12, 3)),
        ("due November 20, 2026", date(2026, 11, 20)),
        ("due Nov 20th", date(2026, 11, 20)),
        ("Abgabe bis 31. Juli 2027", date(2027, 7, 31)),
        ("Frist: 31.07.2027", date(2027, 7, 31)),
        ("pay by 31/07/2027", date(2027, 7, 31)),
        ("ready by end of February 2027", date(2027, 2, 28)),
        ("Kündigung bis Ende März 2027", date(2027, 3, 31)),
    ],
)
def test_date_formats(text, expected):
    assert [m.day for m in find_dates(text, REF)] == [expected]


def test_dates_without_a_year_mean_the_next_occurrence():
    assert find_dates("by 15 December", REF)[0].day == date(2026, 12, 15)
    assert find_dates("by 15 January", REF)[0].day == date(2027, 1, 15)
    assert find_dates("by 1 November", REF)[0].day == date(2026, 11, 1)  # a week ago: this year


def test_things_that_are_not_dates():
    assert find_dates("In November 2026 we met 3 times. Version 2.11.4, room 12.", REF) == []
    assert find_dates("due 31 February 2027", REF) == []  # no such day


def test_each_date_is_found_once():
    mentions = find_dates("From 1 December 2026 until 2027-01-15.", REF)
    assert [m.day for m in mentions] == [date(2026, 12, 1), date(2027, 1, 15)]


def test_only_dates_with_a_deadline_cue_are_candidates():
    text = (
        "Train from Munich to Darmstadt on 14 April 2027, departing 07:28.\n\n"
        "Free cancellation until 31 March 2027. The hotel has a pool."
    )
    (candidate,) = find_candidates(text, REF)
    assert candidate.day == date(2027, 3, 31)
    assert candidate.quote == "Free cancellation until 31 March 2027."
    assert text[candidate.start : candidate.end] == candidate.quote


def test_list_items_are_separate_candidates_without_their_bullets():
    text = (
        "## Action Items\n\n"
        "- Priya: design review by 20 November 2026.\n"
        "- Sam: plan, due 4 December 2026."
    )
    candidates = find_candidates(text, REF)
    assert [c.quote for c in candidates] == [
        "Priya: design review by 20 November 2026.",
        "Sam: plan, due 4 December 2026.",
    ]


def test_german_ordinal_dates_stay_in_one_sentence():
    (candidate,) = find_candidates(
        "Die Steuererklärung ist bis 31. Juli 2027 einzureichen. Danke.", REF
    )
    assert candidate.quote == "Die Steuererklärung ist bis 31. Juli 2027 einzureichen."


def test_prompt_lists_the_parsed_dates():
    text = "Rent is due on 1 December 2026."
    prompt = build_prompt(text, find_candidates(text, REF))
    assert prompt.endswith('Dates found:\n- 2026-12-01 | "Rent is due on 1 December 2026."')


def test_answer_parsing_accepts_only_candidate_dates():
    candidates = find_candidates("Rent is due on 1 December 2026.", REF)
    answer = (
        "Sure! Here is the JSON:\n"
        '{"deadlines": [{"date": "2026-12-01", "task": "Pay  the first rent"},'
        ' {"date": "2026-12-24", "task": "Invented"}, {"date": "soon", "task": "x"},'
        ' {"date": "2026-12-01", "task": "Duplicate"}]}'
    )
    assert parse_answer(answer, candidates) == {date(2026, 12, 1): "Pay the first rent"}


@pytest.mark.parametrize(
    "answer", ["", "no json here", "{broken", '{"deadlines": "none"}', "[1, 2]"]
)
def test_unusable_answers_give_no_deadlines(answer):
    assert parse_answer(answer, find_candidates("Rent is due on 1 December 2026.", REF)) == {}
