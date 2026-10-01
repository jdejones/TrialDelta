import pytest

from trialdelta.tad_parser import extract_target_action_dates, occurrences_with_context, to_plain_text


@pytest.mark.parametrize("expression,expected,precision", [
    ("November 14, 2026", "2026-11-14", "day"),
    ("Nov. 14th, 2026", "2026-11-14", "day"),
    ("14 November 2026", "2026-11-14", "day"),
    ("14th of Nov. 2026", "2026-11-14", "day"),
    ("2026 November 14", "2026-11-14", "day"),
    ("2026, November 14", "2026-11-14", "day"),
    ("2026 14 November", "2026-11-14", "day"),
    ("2026, 14th of November", "2026-11-14", "day"),
    ("2026-11-14", "2026-11-14", "day"),
    ("2026/11/14", "2026-11-14", "day"),
    ("11/14/2026", "2026-11-14", "day"),
    ("14/11/2026", "2026-11-14", "day"),
    ("14.11.2026", "2026-11-14", "day"),
    ("11-14-2026", "2026-11-14", "day"),
    ("11/11/2026", "2026-11-11", "day"),
    ("February 29, 2028", "2028-02-29", "day"),
    ("November 2026", "2026-11", "month"),
    ("Nov., 2026", "2026-11", "month"),
    ("Nov.,2026", "2026-11", "month"),
    ("2026 November", "2026-11", "month"),
    ("2026-11", "2026-11", "month"),
    ("11/2026", "2026-11", "month"),
])
def test_explicit_date_formats(expression, expected, precision):
    rows = extract_target_action_dates(f"FDA assigned a target action date of {expression}.")
    assert len(rows) == 1
    assert rows[0]["status"] == "identified"
    assert rows[0]["tad"] == expected
    assert rows[0]["precision"] == precision


@pytest.mark.parametrize("expression,status", [
    ("November 14", "unidentifiable"),
    ("14 November", "unidentifiable"),
    ("November", "unidentifiable"),
    ("next Monday", "unidentifiable"),
    ("February 29, 2026", "unidentifiable"),
    ("February 30, 2028", "unidentifiable"),
    ("April 31, 2026", "unidentifiable"),
    ("November 00, 2026", "unidentifiable"),
    ("2026-13-14", "unidentifiable"),
    ("13/14/2026", "unidentifiable"),
    ("00/14/2026", "unidentifiable"),
    ("11/14", "unidentifiable"),
    ("05/06/2026", "ambiguous"),
    ("11/14/26", "ambiguous"),
    ("November 14, 20261", "unidentifiable"),
    ("2026 November 140", "unidentifiable"),
    ("2026-11-140", "unidentifiable"),
    ("11/14/20261", "unidentifiable"),
])
def test_incomplete_invalid_and_ambiguous_dates_are_not_filled(expression, status):
    rows = extract_target_action_dates(f"The target action date is {expression}.")
    assert len(rows) == 1
    assert rows[0]["status"] == status
    assert rows[0]["tad"] is None
    assert rows[0]["reason"]


@pytest.mark.parametrize("text", [
    "On March 5, 2026, FDA assigned a target action date of November 14, 2026.",
    "FDA set November 14, 2026 as the target action date.",
    "November 14, 2026 is the target action date.",
    "The November 14, 2026 target action date remains unchanged.",
    "The target action date for Drug X is November 14, 2026.",
    "The target action date for ABC-123 is November 14, 2026.",
])
def test_explicit_relationships_in_both_directions(text):
    assert [row["tad"] for row in extract_target_action_dates(text)] == ["2026-11-14"]


@pytest.mark.parametrize("text", [
    "On March 5, 2026, FDA assigned a target action date that will be disclosed later.",
    "The target action date has not been assigned. The submission was on March 5, 2026.",
    "No target action date was assigned on March 5, 2026.",
    "Target action date pending. Submission accepted March 5, 2026.",
    "Target action date was announced on March 5, 2026.",
    "On October 15, 2027, the target action date was announced.",
])
def test_announcement_and_unassigned_dates_are_not_selected(text):
    rows = extract_target_action_dates(text)
    assert len(rows) == 1
    assert rows[0]["tad"] is None


@pytest.mark.parametrize("text,expected", [
    ("Target action dates are March 1 and April 2, 2027.", ["2027-03-01", "2027-04-02"]),
    ("Target action dates: November 14, 21, and 28, 2026.", ["2026-11-14", "2026-11-21", "2026-11-28"]),
    ("Target action dates of 14 and 21 November 2026.", ["2026-11-14", "2026-11-21"]),
    ("March 1 and April 2, 2027 are the target action dates.", ["2027-03-01", "2027-04-02"]),
    ("Target action dates: November 2026 and December 2026.", ["2026-11", "2026-12"]),
    ("Target action dates are December 1, 2026 and January 2, 2027.", ["2026-12-01", "2027-01-02"]),
])
def test_coordinated_lists(text, expected):
    assert [row["tad"] for row in extract_target_action_dates(text)] == expected


def test_no_year_is_borrowed_from_a_different_clause():
    rows = extract_target_action_dates("Target action date is November 14; announcement made March 5, 2026.")
    assert rows[0]["tad"] is None


def test_month_precision_in_relation():
    rows = extract_target_action_dates("The target action date is anticipated in October 2027.")
    assert rows[0]["tad"] == "2027-10"
    assert rows[0]["precision"] == "month"


def test_alternative_dates_are_ambiguous():
    rows = extract_target_action_dates("The target action date is October 15, 2027 or October 16, 2027.")
    assert len(rows) == 1
    assert rows[0]["tad"] is None
    assert rows[0]["status"] == "ambiguous"
    assert "October 16" in rows[0]["matched_text"]


def test_coordinated_announcement_clause_is_not_a_target_date_list():
    rows = extract_target_action_dates("Target action date is November 14, 2026 and March 5, 2026 was the announcement date.")
    assert [row["tad"] for row in rows] == ["2026-11-14"]


def test_explicit_extension_selects_destination():
    rows = extract_target_action_dates("FDA extended the target action date from May 1 to August 1, 2026.")
    assert [row["tad"] for row in rows] == ["2026-08-01"]
    assert "destination" in rows[0]["reason"]


def test_html_plain_text_offsets_and_exact_markers():
    markup = "<SEC-DOCUMENT><p>Target&nbsp;<b>Action</b> Dates: <span>Nov.</span> 14th, 2026.</p><script>target action date: Jan 1, 2026</script><style>target action date</style><noscript>target action date</noscript></SEC-DOCUMENT>"
    text = to_plain_text(markup)
    rows = extract_target_action_dates(markup)
    assert len(rows) == 1
    row = rows[0]
    assert row["tad"] == "2026-11-14"
    assert text[row["start"]:row["end"]] == "Target Action Dates"
    assert text[row["context_start"]:row["context_end"]] == row["context"]
    assert "script" not in text
    assert extract_target_action_dates("PDUFA date is November 14, 2026.") == []
    assert extract_target_action_dates("target action dateline November 14, 2026") == []


def test_each_unmatched_occurrence_is_preserved():
    rows = extract_target_action_dates("Target action date pending. Target action dates have not been assigned.")
    assert len(rows) == 2
    assert all(row["tad"] is None for row in rows)
    assert rows[0]["start"] != rows[1]["start"]


def test_original_helper_keeps_100_characters_on_each_side():
    text = "x" * 150 + " target action date " + "y" * 150
    occurrence = occurrences_with_context(text)[0]
    assert occurrence["start"] - occurrence["context_start"] == 100
    assert occurrence["context_end"] - occurrence["end"] == 100


def test_date_cut_by_context_boundary_is_completed_by_sentence_fallback():
    # Date begins inside the fixed window but the final year digits fall outside.
    # Normalized spaces collapse, so use allowed relation words as padding.
    text = "target action date " + "currently " * 8 + "of November 14, 2026"
    row = extract_target_action_dates(text)[0]
    assert row["tad"] == "2026-11-14"
    assert row["matched_text"] == "November 14, 2026"
    assert "Same-sentence fallback" in row["reason"]


@pytest.mark.parametrize("expression,expected,precision", [
    ("November 14, 2026", "2026-11-14", "day"),
    ("Nov. 14, 2026", "2026-11-14", "day"),
    ("14 Nov. 2026", "2026-11-14", "day"),
    ("14.11.2026", "2026-11-14", "day"),
    ("14. 11. 2026", "2026-11-14", "day"),
    ("November 2026", "2026-11", "month"),
    ("Nov., 2026", "2026-11", "month"),
    ("11/2026", "2026-11", "month"),
])
def test_date_outside_window_uses_sentence_and_preserves_evidence(expression, expected, precision):
    sentence = "The target action date for " + "a long application description " * 6 + f"is {expression}."
    markup = "<p>Prior announcement: March 5, 2026.</p><p>" + sentence + "</p><p>Other news.</p>"
    text = to_plain_text(markup)
    row = extract_target_action_dates(markup)[0]
    assert row["tad"] == expected
    assert row["precision"] == precision
    assert row["context"] == sentence
    assert text[row["context_start"]:row["context_end"]] == sentence
    assert text[row["start"]:row["end"]] == "target action date"
    assert expression in row["context"]
    assert row["context_end"] - row["end"] > 100
    assert "Same-sentence fallback" in row["reason"]


def test_sentence_fallback_handles_long_relation_before_marker():
    # A coordinated list can cross the left boundary.
    text = ("November 14, 2026 and December 14, 2026 and January 14, 2027 and February 14, 2027 and "
            "March 14, 2027 and April 14, 2027 are the target action dates.")
    rows = extract_target_action_dates(text)
    # The initial pass notices the clipped first expression instead of silently
    # treating it as a different date; the sentence retry retains the full list.
    assert [row["tad"] for row in rows] == [
        "2026-11-14", "2026-12-14", "2027-01-14", "2027-02-14", "2027-03-14", "2027-04-14",
    ]


@pytest.mark.parametrize("abbreviation", ["U.S.", "Example Inc.", "Example Corp.", "Example Ltd."])
def test_sentence_fallback_keeps_filing_abbreviations(abbreviation):
    text = "The target action date for " + abbreviation + " and its " + "new application " * 8 + "is November 14, 2026."
    assert extract_target_action_dates(text)[0]["tad"] == "2026-11-14"


@pytest.mark.parametrize("ending", [".", "!", "?", '."', ".)", "…."])
def test_sentence_fallback_does_not_take_date_from_next_sentence(ending):
    text = "The target action date for " + "a long application description " * 6 + ending + " is November 14, 2026."
    row = extract_target_action_dates(text)[0]
    assert row["tad"] is None
    assert "November" not in row["context"]


@pytest.mark.parametrize("ending", [".", "!", "?", '."', ".)"])
def test_sentence_fallback_does_not_take_date_from_previous_sentence(ending):
    text = "November 14, 2026" + ending + " The target action date for " + "a long application description " * 6 + "is pending."
    row = extract_target_action_dates(text)[0]
    assert row["tad"] is None
    assert "November" not in row["context"]


@pytest.mark.parametrize("expression,status", [
    ("November 14", "unidentifiable"),
    ("February 30, 2026", "unidentifiable"),
    ("05/06/2026", "ambiguous"),
    ("November 14, 2026 or November 15, 2026", "ambiguous"),
])
def test_sentence_fallback_preserves_validation_and_ambiguity(expression, status):
    text = "The target action date for " + "a long application description " * 6 + "is " + expression + "."
    row = extract_target_action_dates(text)[0]
    assert row["tad"] is None
    assert row["status"] == status
    assert expression in row["context"]


def test_sentence_fallback_keeps_shared_year_lists():
    text = "The target action dates are " + "currently " * 10 + "March 1 and April 2, 2027."
    assert [row["tad"] for row in extract_target_action_dates(text)] == ["2027-03-01", "2027-04-02"]


def test_terminal_month_abbreviation_before_alternative_does_not_end_sentence():
    text = "Target action dates " + "currently " * 10 + "of 2026 14 Nov. or 2026 15 Dec."
    row = extract_target_action_dates(text)[0]
    assert row["status"] == "ambiguous"
    assert row["tad"] is None


@pytest.mark.parametrize("separator", [" & ", " , and ", " AND "])
def test_terminal_month_abbreviation_before_coordinated_date_continues_sentence(separator):
    text = "Target action dates " + "currently " * 10 + "of 2026 14 Nov." + separator + "2026 15 Dec."
    assert [row["tad"] for row in extract_target_action_dates(text)] == ["2026-11-14", "2026-12-15"]


@pytest.mark.parametrize("next_sentence", [
    "The next conference is November 14, 2026.",
    "Management announced that its next conference is November 14, 2026.",
])
def test_initialism_at_sentence_end_does_not_borrow_conference_date(next_sentence):
    text = ("The target action date for the investigational treatment of adult patients "
            "has not yet been announced in the U.S. " + next_sentence)
    row = extract_target_action_dates(text)[0]
    assert row["tad"] is None
    assert "conference" not in row["context"]


def test_initialism_before_capitalized_agency_name_continues_sentence():
    text = ("The target action date for the U.S. Food and Drug Administration review of "
            "our investigational treatment of adult patients is November 14, 2026.")
    assert extract_target_action_dates(text)[0]["tad"] == "2026-11-14"


def test_month_abbreviation_ending_date_does_not_borrow_next_sentence_year():
    text = "Target action date " + "currently " * 10 + "of 14 Nov. The year 2026 was mentioned elsewhere."
    row = extract_target_action_dates(text)[0]
    assert row["tad"] is None
    assert "2026" not in row["context"]


def test_initial_identified_match_retains_100_character_context():
    text = "Previous information. " + "x" * 120 + ". Target action date is November 14, 2026. " + "Other information. " * 15
    row = extract_target_action_dates(text)[0]
    assert row["tad"] == "2026-11-14"
    assert row["context_end"] - row["end"] == 100
    assert row["start"] - row["context_start"] == 100
    assert "fallback" not in row["reason"]


def test_initial_ambiguous_match_is_not_replaced_by_distant_date():
    text = "Target action date is 05/06/2026; " + "additional information " * 8 + "November 14, 2026."
    row = extract_target_action_dates(text)[0]
    assert row["status"] == "ambiguous"
    assert "fallback" not in row["reason"]
