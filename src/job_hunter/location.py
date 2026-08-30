from __future__ import annotations

import re
from dataclasses import dataclass

from .models import LocationConfidence, WorkArrangement

STATES = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "FL": "Florida",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
    "DC": "District of Columbia",
}
NON_US = re.compile(
    r"\b(canada|ontario|quebec|mexico|germany|india|japan|united kingdom|uk|england|"
    r"france|china|australia|singapore|brazil)\b",
    re.I,
)
US_COUNTRY = re.compile(r"\b(united states(?: of america)?|u\.?s\.?a?\.?|usa)\b", re.I)
REMOTE_US = re.compile(
    r"(?:remote\s*[-,/()]?\s*(?:in|within)?\s*(?:the\s*)?(?:united states|u\.?s\.?a?\.?))|"
    r"(?:(?:united states|u\.?s\.?a?\.?)\s*[-,/()]?\s*remote)|(?:us-based\s+remote)",
    re.I,
)


@dataclass(frozen=True)
class LocationDecision:
    us_eligible: bool
    confidence: LocationConfidence
    evidence: str
    city: str | None = None
    state: str | None = None
    country: str | None = None
    arrangement: WorkArrangement = WorkArrangement.UNKNOWN


def detect_arrangement(text: str | None) -> WorkArrangement:
    value = (text or "").lower()
    if "hybrid" in value:
        return WorkArrangement.HYBRID
    if "remote" in value or "telecommute" in value:
        return WorkArrangement.REMOTE
    if "on-site" in value or "onsite" in value:
        return WorkArrangement.ONSITE
    return WorkArrangement.UNKNOWN


# Several U.S. state abbreviations collide with ISO-3166 country/region codes that appear in
# multinational ATS feeds formatted as "City, Region, COUNTRY, Zip" (e.g. Volkswagen/Audi's
# SuccessFactors listings: "Berlin, BE, DE, 10178", "Pickering, ON, CA, L1V 0C4"). Bare-abbreviation
# matches for these codes require the code to not be sitting in that feed's country position.
_AMBIGUOUS_STATE_COUNTRY_CODES = {
    "AL",  # Albania
    "CA",  # Canada
    "DE",  # Germany
    "GA",  # Georgia (country)
    "IN",  # India
    "LA",  # Laos
    "MA",  # Morocco
    "MD",  # Moldova
    "ME",  # Montenegro
    "MT",  # Malta
    "PA",  # Panama
    "SC",  # Seychelles
    "TN",  # Tunisia
    "VA",  # Vatican City
}
_TRAILING_MORE = re.compile(r"\s*\+\s*\d+\s*more[….]*\s*$", re.I)


def _ambiguous_code_is_country_marker(abbreviation: str, text: str) -> bool:
    """True if an ambiguous code's position/context marks it as a country code, not a state."""
    segments = [segment.strip() for segment in text.split(",")]
    if len(segments) < 2:
        return False
    # "City, Region, COUNTRY, Zip": the code immediately preceding a trailing zip/postal
    # segment is that feed's country field. If it isn't literally "US", it's not a state.
    if len(segments) >= 3 and any(char.isdigit() for char in segments[-1]):
        country_position = segments[-2].strip().upper()
        return country_position == abbreviation and country_position not in {"US", "USA"}
    # "City, XX +N more…": the same feeds abbreviate a job's first location to a bare country
    # code when it has additional posting locations, with no other U.S. evidence anywhere.
    last = _TRAILING_MORE.sub("", segments[-1]).strip()
    return (
        last.upper() == abbreviation
        and bool(_TRAILING_MORE.search(segments[-1]))
        and not US_COUNTRY.search(text)
    )


def _state_match(text: str) -> tuple[str | None, str | None]:
    for abbreviation, name in STATES.items():
        if re.search(rf"(?<![A-Za-z]){re.escape(name)}(?![A-Za-z])", text, re.I):
            return abbreviation, name
        # Require punctuation before two-letter codes to avoid words such as "Remote US" or "IN".
        if re.search(rf"(?:,|/|\||\s-\s)\s*{abbreviation}(?:\s|,|/|\||$)", text, re.I):
            if abbreviation in _AMBIGUOUS_STATE_COUNTRY_CODES and _ambiguous_code_is_country_marker(
                abbreviation, text
            ):
                continue
            return abbreviation, name
    return None, None


def evaluate_location(
    location_raw: str | None,
    *,
    country: str | None = None,
    state: str | None = None,
    description: str | None = None,
    arrangement: WorkArrangement | None = None,
) -> LocationDecision:
    location = " ".join(filter(None, [location_raw, state, country])).strip()
    combined = " ".join(filter(None, [location, description]))
    work = arrangement or detect_arrangement(location_raw)
    structured_country = (country or "").strip().upper()
    structured_state = (state or "").strip()

    if structured_country in {"US", "USA", "UNITED STATES", "UNITED STATES OF AMERICA"}:
        abbr, _ = _state_match(structured_state or location)
        return LocationDecision(
            True,
            LocationConfidence.HIGH,
            "structured U.S. country",
            state=abbr or state,
            country="US",
            arrangement=work,
        )
    # Multi-location text can override an exclusively non-US structured primary location.
    if (
        structured_country
        and structured_country not in {"US", "USA"}
        and not _state_match(location_raw or "")[0]
        and not US_COUNTRY.search(location_raw or "")
    ):
        return LocationDecision(
            False,
            LocationConfidence.HIGH,
            f"structured non-U.S. country: {country}",
            country=country,
            arrangement=work,
        )
    if REMOTE_US.search(combined):
        return LocationDecision(
            True,
            LocationConfidence.HIGH,
            "explicit U.S.-remote evidence",
            country="US",
            arrangement=WorkArrangement.REMOTE,
        )
    abbr, _ = _state_match(location)
    if abbr:
        return LocationDecision(
            True,
            LocationConfidence.HIGH,
            f"recognized U.S. state: {abbr}",
            state=abbr,
            country="US",
            arrangement=work,
        )
    if US_COUNTRY.search(location):
        return LocationDecision(
            True,
            LocationConfidence.HIGH,
            "explicit United States location",
            country="US",
            arrangement=work,
        )
    if NON_US.search(location) and not US_COUNTRY.search(location):
        return LocationDecision(
            False,
            LocationConfidence.HIGH,
            "recognized non-U.S. location",
            country=country,
            arrangement=work,
        )
    if work == WorkArrangement.REMOTE or re.search(r"\bremote\b", location, re.I):
        return LocationDecision(
            False,
            LocationConfidence.LOW,
            "remote location lacks U.S. evidence",
            arrangement=WorkArrangement.REMOTE,
        )
    return LocationDecision(
        False,
        LocationConfidence.LOW,
        "location does not establish U.S. eligibility",
        arrangement=work,
    )
