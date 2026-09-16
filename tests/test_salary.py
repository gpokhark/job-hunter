from job_hunter.salary import evaluate_salary


def test_no_description_has_no_evidence():
    assert evaluate_salary(None).evidence is None
    assert evaluate_salary("").evidence is None


def test_real_range_phrasings_from_live_postings():
    """Each of these is verbatim (or near-verbatim) text observed in live GM, Honda,
    Ford, and Torc Robotics postings."""
    examples = {
        "The salary range for this role is $76,100.00 to $114,300.00.": (76100.0, 114300.0),
        "The expected base compensation for this role is: $170,600- $261,300.": (170600.0, 261300.0),
        "<span>Pay Rate: $37.13 - $42.43/hour </span>": (37.13, 42.43),
        "<strong>Salary Range:</strong> $83,000.00 - $124,500.00": (83000.0, 124500.0),
        # Ford: the second number carries no "$" of its own at all.
        "ranges from $72,480-121,440. This position": (72480.0, 121440.0),
        # Torc/Greenhouse: numbers sit in separate <span> tags either side of an em-dash.
        '<span>$177,300</span><span class="divider">&mdash;</span><span>$212,800 USD</span>': (
            177300.0,
            212800.0,
        ),
    }
    for text, (low, high) in examples.items():
        decision = evaluate_salary(text)
        assert decision.evidence, text
        assert decision.min_value == low, text
        assert decision.max_value == high, text


def test_bare_single_dollar_amount_is_not_a_salary():
    """Regression: real false-positive risk found directly in a live Ford posting —
    single dollar figures in benefits boilerplate, not compensation."""
    unrelated = [
        "Basic Life Insurance of $3,000 and Accidental Death and Dismemberment of $1,500.",
        "Starting wage rate at $21.00 per hour plus applicable shift premiums.",
    ]
    for text in unrelated:
        assert evaluate_salary(text).evidence is None, text


def test_zero_placeholder_range_is_not_a_salary():
    """Regression: confirmed live on dozens of Caterpillar/Nissan Workday postings for
    hourly/union roles — an unfilled compensation-template field renders as a literal
    "$0.00 - $0.00", not a real range."""
    decision = evaluate_salary("<div class='pay-range'>$0.00 - $0.00</div>")
    assert decision.evidence is None
    assert decision.min_value is None
    assert decision.max_value is None
