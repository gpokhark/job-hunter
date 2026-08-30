import pytest

from job_hunter.location import evaluate_location
from job_hunter.models import WorkArrangement


@pytest.mark.parametrize(
    "text",
    [
        "Ann Arbor, Michigan, United States",
        "Raymond, OH",
        "Palo Alto, CA",
        "Remote - US",
        "United States (Remote)",
        "Hybrid - Plano, TX",
        "Plano, TX / Toronto, ON",
        "Germany or United States",
        "Remote, California",
    ],
)
def test_eligible_locations(text):
    assert evaluate_location(text).us_eligible


@pytest.mark.parametrize(
    "text",
    [
        "Remote",
        "North America Remote",
        "Global Remote",
        "Toronto, Ontario, Canada",
        "Berlin, Germany",
        "Monterrey, Mexico",
        "Berlin, BE, DE, 10178",
        "Osnabrück, DE +1 more…",
        "Pickering, ON, CA, L1V 0C4",
        "Wolfsburg, DE, 38436",
    ],
)
def test_ineligible_locations(text):
    assert not evaluate_location(text).us_eligible


@pytest.mark.parametrize(
    "text",
    [
        "Reston, VA, US, 20190",
        "Auburn Hills, MI, US, 48326",
    ],
)
def test_multinational_feed_us_state_still_matches(text):
    assert evaluate_location(text).us_eligible


def test_structured_country_wins():
    decision = evaluate_location("Anywhere", country="US", arrangement=WorkArrangement.REMOTE)
    assert decision.us_eligible and decision.country == "US"


def test_description_can_resolve_ambiguous_remote():
    assert evaluate_location(
        "Remote", description="This role is remote within the United States"
    ).us_eligible


def test_does_not_match_us_substring():
    assert not evaluate_location("Must be based remotely").us_eligible
