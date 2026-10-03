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


def test_more_range_phrasings_seen_in_the_stored_pool():
    """Phrasings the original regex missed, each verbatim from a real stored posting
    (ABB, Uber, MBRDNA, Hatci, Toro, Autodesk, Zipline)."""
    examples = {
        # "between ... and ..." (ABB, Autodesk)
        "this position is expected to pay between $65,100 and $104,160 annually.": (65100.0, 104160.0),
        "we expect a starting base salary between $146,000 and $261,360. Offers": (146000.0, 261360.0),
        "pay between $40.05/hr and $60/hr. ABB Benefit": (40.05, 60.0),
        # a unit or currency between the numbers (Uber, MBRDNA)
        "is USD $216,000 per year - USD $240,000 per year. For": (216000.0, 240000.0),
        "is as follows: $145/hr - $180/hr #LI-ST1": (145.0, 180.0),
        # "K" and "~" (Hatci, Zipline)
        "Compensation Range : $115K~$165K": (115000.0, 165000.0),
        "Range of Position: $75,000 ~ $90,000/Year A Global": (75000.0, 90000.0),
        "$130k-180": (130000.0, 180000.0),
        # zero-width spaces around the numbers (Toro)
        "range is between ​$84,300 - ​$105,400​. Cash": (84300.0, 105400.0),
        "starting pay, $18.76/hr.-$23.00/hr., plus": (18.76, 23.0),
    }
    for text, (low, high) in examples.items():
        decision = evaluate_salary(text)
        assert decision.evidence, text
        assert (decision.min_value, decision.max_value) == (low, high), text


def test_company_size_and_spend_figures_are_not_a_salary():
    """Regression: real boilerplate that a looser '$' search would flag."""
    unrelated = [
        "The company generated sales of $98 billion in 2024.",
        "accounts ranging from ~$100K to $10M+ in annual spend",
        "between 810 suppliers with $300M $500M in annual spend",
        "Up to $1,000 per hired referral",
        "a $5 and $10 gift card",
        "between $5 and $10 million",
        "$50K to $30",
    ]
    for text in unrelated:
        assert evaluate_salary(text).evidence is None, text


def test_first_valid_range_wins_over_an_unfilled_placeholder():
    decision = evaluate_salary("Pay Range: $0.00 - $0.00 ... later: $20 - $25 per hour")
    assert (decision.min_value, decision.max_value) == (20.0, 25.0)


def test_typo_range_keeps_evidence_but_not_untrustworthy_numbers():
    """Live postings carry typos like '$130,000- $145,00': show the text, don't parse it."""
    decision = evaluate_salary("salary $130,000- $145,00 per year")
    assert decision.evidence
    assert decision.min_value is None and decision.max_value is None


def test_currency_code_ranges_without_a_dollar_sign():
    """NVIDIA (1,100+ postings) writes '184,000 USD - 287,500 USD' with no '$'. The first
    range is the one reported when several levels are listed."""
    text = (
        "The base salary range is 184,000 USD - 287,500 USD for Level 4, and "
        "224,000 USD - 356,500 USD for Level 5."
    )
    decision = evaluate_salary(text)
    assert (decision.min_value, decision.max_value) == (184000.0, 287500.0)
    assert evaluate_salary("USD 120,000 - 150,000").min_value == 120000.0
    # a bare number range, or a USD that isn't attached to the first number, is not pay
    for text in ("100,000 - 200,000 users", "we serve 5,000 USD customers - 10"):
        assert evaluate_salary(text).evidence is None, text
