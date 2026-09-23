from job_hunter.models import SponsorshipStatus
from job_hunter.sponsorship import evaluate_sponsorship


def test_no_description_is_unmentioned():
    assert evaluate_sponsorship(None).status == SponsorshipStatus.UNMENTIONED
    assert evaluate_sponsorship("").status == SponsorshipStatus.UNMENTIONED


def test_unrelated_sponsor_mentions_stay_unmentioned():
    """Regression: these are real false-positive risks found directly in live postings —
    'sponsor' with no visa-related meaning at all."""
    unrelated = [
        "Ability to sponsor Key-Op program participants with support from the team.",
        "Acts as liaison between stakeholders and sponsors for training initiatives.",
        "Outdoor activities, regular sport activities and access to our sponsored sports hall.",
        "Sponsorowane prywatne ubezpieczenie zdrowotne oraz bezpłatną opiekę medyczną.",
    ]
    for text in unrelated:
        decision = evaluate_sponsorship(text)
        assert decision.status == SponsorshipStatus.UNMENTIONED, text


def test_real_not_available_phrasings():
    """Each of these is verbatim (or near-verbatim) boilerplate observed in live postings."""
    examples = [
        "Applicants and employees for this position will not be sponsored for work authorization, including H-1B visas.",
        "Toyota does not offer support or sponsorship of job applicants for employment-based visas or any other work authorization.",
        "Please note that this position is not eligible for visa sponsorship. Applicants must be authorized to work in the US.",
        "Sponsorship for employment visa status for these positions is unavailable. Applicants require current authorization.",
        "This position is not eligible for work visa sponsorship.",
        "Legally authorized to work in the U.S. without sponsorship",
        "Education Requirement: Bachelor's degree</p><p>Sponsorship: No</p>",
    ]
    for text in examples:
        decision = evaluate_sponsorship(text)
        assert decision.status == SponsorshipStatus.NOT_AVAILABLE, text
        assert decision.evidence


def test_available_phrasings_including_the_users_examples():
    examples = [
        "Visa Sponsorship may be available.",
        "Visa sponsorship available for well-qualified candidates.",
        "This role is eligible for visa sponsorship.",
        "The company offers visa sponsorship for this role.",
        # Confirmed live on Anthropic's Greenhouse postings: "We do sponsor visas!"
        "Visa sponsorship: We do sponsor visas! However, we aren't able to sponsor every role.",
        # Singular phrasing gap found comparing against career-ops' visa_filter.
        "For the right candidate, we will sponsor a visa.",
    ]
    for text in examples:
        decision = evaluate_sponsorship(text)
        assert decision.status == SponsorshipStatus.AVAILABLE, text
        assert decision.evidence


def test_users_not_available_example_is_not_available():
    decision = evaluate_sponsorship("Visa sponsorship is not available for this position.")
    assert decision.status == SponsorshipStatus.NOT_AVAILABLE


def test_inline_tag_splitting_a_phrase_does_not_defeat_a_match():
    """Real live miss: Ford's actual wording bolds just the word 'not' — stripping that
    tag alone (without also collapsing the resulting double space) leaves 'is  not
    available', which the literal single-space pattern 'is not available' never matches."""
    decision = evaluate_sponsorship(
        "Visa sponsorship is <strong>not</strong> available for this position."
    )
    assert decision.status == SponsorshipStatus.NOT_AVAILABLE


def test_gm_and_google_boilerplate_variants():
    """GM's most common boilerplate (hundreds of live postings) and Google's phrasing —
    both initially missed because the negation and the target word weren't adjacent."""
    examples = [
        "GM DOES NOT PROVIDE IMMIGRATION-RELATED SPONSORSHIP FOR THIS ROLE. DO NOT APPLY IF YOU WILL NEED SPONSORSHIP.",
        "This role is not eligible for U.S. immigration sponsorship.",
        "This position will not be eligible for immigration sponsorship.",
        "Visa sponsorship for this position is not available at this time.",
    ]
    for text in examples:
        decision = evaluate_sponsorship(text)
        assert decision.status == SponsorshipStatus.NOT_AVAILABLE, text


def test_html_tag_between_words_does_not_defeat_a_match():
    """Real case: Nissan's field is literally '<b>Sponsorship:</b> No'."""
    decision = evaluate_sponsorship("<p><b>Sponsorship:</b> No</p>")
    assert decision.status == SponsorshipStatus.NOT_AVAILABLE


def test_not_available_checked_before_available_to_avoid_double_match():
    """'does not offer... sponsorship' must never fall through to the looser 'offers
    sponsorship' positive pattern."""
    decision = evaluate_sponsorship("The company does not offer support or sponsorship for this role.")
    assert decision.status == SponsorshipStatus.NOT_AVAILABLE


def test_export_control_licensing_implies_not_available():
    """Real live case: MBRDNA's Lever posting never uses the word 'sponsorship' at all —
    the ITAR/EAR export-control clause is the only signal, but it's the industry-standard
    way of saying the role is restricted to U.S. persons absent extra licensing."""
    decision = evaluate_sponsorship(
        "This position may involve access to export-controlled technology, hardware, or "
        "software under the Export Administration Regulations (EAR), 15 C.F.R. Parts "
        "730-774 and possibly technical data, defense articles and defense services "
        "subject to the International Traffic in Arms Regulations (ITAR), 22 C.F.R. Parts "
        "120-130. As such, the Company may be required to obtain an export license or "
        "authorization in accordance with United States law."
    )
    assert decision.status == SponsorshipStatus.NOT_AVAILABLE
    assert decision.evidence


def test_export_control_mention_alone_stays_unmentioned():
    """A bare ITAR/EAR disclosure with no export-license-required clause doesn't by itself
    mean sponsorship is unavailable — some postings disclose export-control exposure
    without restricting hiring on it. Only the narrower combined phrase should match."""
    decision = evaluate_sponsorship(
        "This role may involve access to technology subject to the International Traffic "
        "in Arms Regulations (ITAR) and the Export Administration Regulations (EAR)."
    )
    assert decision.status == SponsorshipStatus.UNMENTIONED


def test_sponsorship_restricted_to_existing_employees_implies_not_available():
    """Real live case: Daimler Truck North America's Workday posting reads as offering
    sponsorship at a glance, but it's only a same-employer visa transfer for people already
    sponsored, not available to a new external applicant."""
    examples = [
        "Visa sponsorship will only be open to current Daimler Truck North America "
        "employees working under an existing U.S. Daimler Truck North America Visa.",
        "Sponsorship is restricted to existing Acme Corp employees.",
    ]
    for text in examples:
        decision = evaluate_sponsorship(text)
        assert decision.status == SponsorshipStatus.NOT_AVAILABLE, text
        assert decision.evidence


def test_not_open_for_sponsorship_phrasing():
    """Real live case: Daimler Truck North America's Workday postings (a different
    boilerplate than the 'existing employees' one above) use 'not open for' rather than
    'not available'/'does not offer' — confirmed verbatim via the CXS API's jobDescription
    field: 'This position is not open for Visa sponsorship or to existing Visa holders.'"""
    decision = evaluate_sponsorship(
        "This position is not open for Visa sponsorship or to existing Visa holders."
    )
    assert decision.status == SponsorshipStatus.NOT_AVAILABLE
    assert decision.evidence
