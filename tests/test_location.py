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


@pytest.mark.parametrize(
    "text",
    [
        # Real live misses, confirmed in production data: an ambiguous U.S. state
        # abbreviation (CA/TN/etc.) matched as a state even though the very same string
        # also spells out the real non-U.S. country/region name right next to it.
        "Woodbridge, Ontario, CA",
        "Chennai, TN, India",
        "St. Thomas – Formet, Ontario, CA",
    ],
)
def test_ambiguous_code_loses_to_an_explicit_non_us_name_in_the_same_text(text):
    assert not evaluate_location(text).us_eligible


def test_structured_country_disambiguates_a_bare_ambiguous_code():
    """Real live miss: Magna's Workday tenant renders every Indian posting's location as
    just 'Maharashtra, IN' with no other text to go on — textually identical to a bare
    U.S. state abbreviation. Workday's own structured country field ('India') is decisive."""
    decision = evaluate_location("Maharashtra, IN", country="India")
    assert not decision.us_eligible


def test_foreign_location_does_not_veto_a_genuine_us_location_listed_alongside_it():
    """Real live case: Anthropic's Greenhouse posting lists both a London and a San
    Francisco option as 'London, UK; San Francisco, CA'. A naive whole-string non-US-name
    check would let 'UK' disqualify 'CA' as California even though a real U.S. location is
    plainly offered too — the check must stay scoped to the single location entry
    containing the ambiguous code."""
    decision = evaluate_location("London, UK; San Francisco, CA")
    assert decision.us_eligible


def test_bare_ambiguous_code_without_structured_country_stays_a_state():
    """Known, accepted residual limitation: with no structured country and no other text
    evidence, 'Maharashtra, IN' is textually indistinguishable from a real 'City, Indiana'
    U.S. posting (see test_eligible_locations' 'Raymond, OH' / 'Palo Alto, CA') — resolving
    this case requires the adapter to supply a structured country, not a text heuristic."""
    decision = evaluate_location("Maharashtra, IN")
    assert decision.us_eligible
