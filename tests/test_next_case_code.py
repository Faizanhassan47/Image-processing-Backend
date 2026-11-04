from routes.cases_api import next_case_code


def test_next_case_code_starts_at_one_when_empty():
    assert next_case_code([]) == "CASE-0001"


def test_next_case_code_increments_the_highest_existing_identifier():
    existing = ["CASE-0001", "demo", "CASE-12", "case-0007", None]
    assert next_case_code(existing) == "CASE-0013"


def test_next_case_code_ignores_non_matching_identifiers():
    assert next_case_code(["DEMO-001", "CASE-12A", " CASE-0004 "]) == "CASE-0005"
