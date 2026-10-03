#!/usr/bin/env python3
"""Regression check for adapters/_common.json answer_rules label->key mapping.

Every case here is modelled on a real label seen on a job form (company names replaced
with Acme). The `None` cases are over-broad-regex bugs that were writing wrong data into
submitted applications. Run: python3 test_answer_rules.py
"""
import json
import pathlib
import re
import sys

RULES_PATH = pathlib.Path(__file__).parent / "adapters" / "_common.json"


def _find_rules(obj):
    if isinstance(obj, list) and obj and isinstance(obj[0], dict) and "pattern" in obj[0]:
        return obj
    if isinstance(obj, dict):
        for v in obj.values():
            found = _find_rules(v)
            if found:
                return found
    return None


def match(label, rules):
    """First matching rule wins - same order semantics adapters.py relies on."""
    for r in rules:
        if re.search(r["pattern"], label, re.I):
            return r["key"]
    return None


# (label, expected_key). None = must NOT match anything; a wrong answer here is
# worse than no answer, because it puts a false statement on an application.
CASES = [
    # --- bugs: these previously mapped to a wrong key ---
    ("Do you opt-in to receive WhatsApp messages from Acme Recruiting?", None),
    ("Which best describes how you use AI tools today?", None),
    ("Have you worked at a startup?", None),
    ("Do you have a college degree?", None),
    ("Please select the country where you currently reside.", "country"),
    ("Will you require Acme to sponsor you for a work permit now or in the future?",
     "requires_sponsorship"),
    ("Who is your current or previous employer?", "current_company"),
    ("...technology or source code subject to the Export Administration Regulations, "
     "or hold permanent residency...", "us_citizen_or_pr"),
    # --- regressions: these must keep working ---
    ("What is your current visa status?", "visa_status"),
    # a bare "h-1b" in an EXAMPLE list must not hijack a visa-TYPE question
    ("If you are currently on a nonimmigrant visa, what visa do you hold currently "
     "(e.g., F-1, H-1B, L-1, O-1, etc.)?", "visa_status"),
    ("Do you require H-1B sponsorship?", "requires_sponsorship"),
    # year-in-school must not feed the school NAME into a year select
    ("If you're an active or incoming student, please select your current year in school:",
     "year_in_school"),
    ("Will you require CPT or OPT?", "visa_status"),
    ("Will you now or in the future require sponsorship for employment visa status?",
     "requires_sponsorship"),
    ("Are you a US citizen or permanent resident?", "us_citizen_or_pr"),
    ("Have you ever worked for Acme?", "previously_employed"),
    ("Have you previously worked here?", "previously_employed"),
    ("How did you hear about us?", "source_of_referral"),
    # a referrer NAME field must never receive the referral SOURCE value ("Website")
    ("If you were referred by an employee, what is their name?", "referrer_name"),
    ("Who referred you?", "referrer_name"),
    ("Where did you hear about this job?", "source_of_referral"),
    ("School", "school"),
    ("Please select your current school from the list below:", "school"),
    ("What university did you attend?", "school"),
    ("Degree", "degree"),
    ("What degree are you currently pursuing?", "degree"),
    ("Where are you currently located?", "location"),
    ("Current employer", "current_company"),
    ("I consent to the use of AI to screen my application", "ai_screening_consent"),
    # preferred_name must win over first_name, or the legal name lands in the wrong box
    ("Preferred First Name", "preferred_name"),
    ("Preferred Name", "preferred_name"),
    ("Legal First Name", "first_name"),
    ("First Name", "first_name"),
    # state must win over location, or "Los Angeles, CA" lands in a state dropdown
    ("What U.S State do you currently reside in?", "state"),
    ("Which state do you live in?", "state"),
    ("Which U.S. State or Canadian Province do you reside in?", "state"),
    ("Where are you currently located?", "location"),
    # phrasings that used to fall through entirely
    ("How did you first hear about us?", "source_of_referral"),
    ("Tell us how you heard about this role", "source_of_referral"),
    ("Current/Previous Employer", "current_company"),
    ("Will you require our assistance with work authorization now or in the future?",
     "requires_sponsorship"),
    # willingness rules must NOT fire on a present-tense residency FACT. For a candidate not
    # based there, auto-answering Yes to "are you currently based near..." is a false claim.
    ("Are you currently based near one of our hub offices (NYC, Chicago, Austin, SF, Lehi) "
     "and able to commute three days per week?", None),
    ("Are you willing to relocate?", "willing_to_relocate"),
    ("Are you able to commute to our SF office?", "willing_to_relocate"),
    ("This is a hybrid role requiring 3 days per week in office. Are you comfortable with that?",
     "willing_onsite_relocate"),
    # The PLAINEST phrasing of the onsite question used to match nothing: the pattern demanded a
    # day/week/policy word within 80 chars of "onsite", which cost false needs_human rows on
    # forms profile.json already answers.
    ("Are you comfortable working in an onsite setting?", "willing_onsite_relocate"),
    ("Are you comfortable with a hybrid schedule?", "willing_onsite_relocate"),
    ("This is an onsite position.", "willing_onsite_relocate"),
    # ...but a present-tense residency FACT must still not be read as willingness: for a
    # candidate not based there, a blanket Yes is a false location claim. (It currently maps to
    # `location`, not None; what matters here is only that it does NOT become
    # willing_onsite_relocate.)
    ("Are you currently located in the greater Seattle area?", "location"),
    # NEGATIVE-POLARITY work auth. F-1/OPT = restricted; profile answers this "No"
    # four ways. Mapping it to authorized_to_work ("Yes") is a materially false statement.
    ("Are you legally authorized to work in the United States for ANY employer without ANY "
     "restrictions?", "authorized_without_sponsorship"),
    ("Do you have unrestricted work authorization?", "authorized_without_sponsorship"),
    ("Are you authorized to work in the US without sponsorship?",
     "authorized_without_sponsorship"),
    # "without employer support or sponsorship"
    # slipped past `without (requiring |needing )?sponsorship` to authorized_to_work -> "Yes",
    # a false statement. The filler caught it before typing. Rule 2 now takes any "without ...
    # sponsor*" within 40 chars, or "without employer/company/visa/immigration support".
    ("Are you legally authorized to work in the United States without employer support or "
     "sponsorship?", "authorized_without_sponsorship"),
    ("Are you authorized to work in the US without the need for visa sponsorship?",
     "authorized_without_sponsorship"),
    ("Can you work in the US without company sponsorship now or in the future?",
     "authorized_without_sponsorship"),
    # previously unmatched (safe, but cost rows)
    ("Are you legally allowed to work in the US?", "authorized_to_work"),
    ("Are you legally allowed to work in the United States without sponsorship?",
     "authorized_without_sponsorship"),
    ("Are you subject to a non-competition agreement?", "non_compete"),
    # "authorized to lawfully work" missed rule 4's "authorized to work"
    ("Are you authorized to lawfully work for Acme in the United States?", "authorized_to_work"),
    ("Are you authorized to lawfully work in the US without sponsorship?",
     "authorized_without_sponsorship"),
    # gender/race rules: "gender" is a substring of "transgender", and a legal
    # paragraph's EEO clause ("race, sex, religion") hit eeoc_gender. Both rules are now
    # length-guarded (<=200 chars). Certifications with a penalty clause, or "I personally
    # completed this application", go to a person. A plain "true and accurate" certification
    # still reaches `acknowledge` (only answered if profile.json sets acknowledge).
    ("Do you identify as transgender?", None),
    ("What is your gender?", "eeoc_gender"),
    ("Race/Ethnicity", "eeoc_race"),
    ("I certify that my answers are true and complete. I understand that false statements or "
     "omissions are grounds for dismissal. We are an equal opportunity employer regardless of "
     "race, sex, religion or national origin.", None),
    ("I certify that I personally completed this application, and understand that omission or "
     "misstatement is grounds for discharge.", None),
    ("I certify that the information I have provided in this application is true, complete and "
     "accurate to the best of my knowledge. We are an equal opportunity employer and do not "
     "discriminate on the basis of race, sex, religion, or national origin.", "acknowledge"),
    # ...but the plain positive question must still resolve positively
    ("Are you legally authorized to work in the United States?", "authorized_to_work"),
    ("Are you eligible to work in the US?", "authorized_to_work"),
    # ADAPTER DEFECT, found live and fixed the same day. This label
    # resolved to `previously_employed` -> "No", i.e. the pipeline answered NO to a
    # work-AUTHORIZATION question. Two independent causes, both now closed:
    #   1. previously_employed's catch-all tail matched bare `employ`, which is inside "employER";
    #   2. authorized_to_work knew "authorized to work"/"work authorization" but not the
    #      inverted "authorization to work".
    # Highest-stakes class in the whole rule table -- a wrong answer here is a false statement
    # on an employment application, not a lost application.
    ("Do you have authorization to work in the United States?", "authorized_to_work"),
    # CORRECTED the same day it was first pinned. "for ANY/ALL employers" is the
    # UNRESTRICTED-authorization question, not the plain one: F-1/OPT ties work authorization to
    # a specific employer and needs sponsorship afterwards, so the truthful answer is NO. Pinning
    # these as authorized_to_work ("Yes") would have been the materially false statement this
    # whole rule family exists to prevent -- caught when a second form asked the same question
    # another way ("...for all employers...on a full-time basis") and it ALSO fell through to Yes.
    ("Do you have authorization to work for any employer in the US?",
     "authorized_without_sponsorship"),
    ("Are you currently authorized to work for all employers in the United States on a "
     "full-time basis?", "authorized_without_sponsorship"),
    ("Are you a former employee of this company?", "previously_employed"),
    ("Have you ever been employed by our firm?", "previously_employed"),
    # "permanent AUTHORIZATION" matched nothing at all (only "permanent basis" was covered) --
    # the highest-stakes question on one form was silently unanswered.
    ("Do you have permanent authorization to work for Acme in the U.S. or Canada?",
     "authorized_without_sponsorship"),
    ("Are you permanently authorized to work in the US?", "authorized_without_sponsorship"),
    # ADAPTER DEFECT found live. "legal first" matched the first_name rule,
    # so a field asking for the WHOLE name would have received only the first name. Same ordering
    # family as preferred_name-before-first_name: the more specific combined rule must win.
    ("Please list legal first and last name", "full_name"),
    ("First and Last Name", "full_name"),
    ("Last name and first name", "full_name"),
    ("Legal First & Last Name", "full_name"),
    # ...and the single-field forms must still resolve to the single-field keys.
    ("Legal First Name", "first_name"),
    ("Last Name", "last_name"),
    # The plainest able-and-willing onsite phrasing matched nothing. Same gap class as the
    # "comfortable working onsite" miss above.
    ("Are you able and willing to work onsite at our Los Angeles office?",
     "willing_onsite_relocate"),
    ("Are you willing to work onsite 3 days a week?", "willing_onsite_relocate"),
    # ...but a present-tense residency FACT must still not be read as willingness.
    ("Are you currently based near our Los Angeles office?", None),
    # This one pins the guard itself. While adding the two cases above I first appended them
    # OUTSIDE willing_onsite_relocate's leading ^(?!...currently based|located|living...) group
    # -- and onto the wrong rule -- which let a sentence that is BOTH a residency question and
    # an onsite question through as a willingness "Yes" -- a false location claim for anyone
    # not already based there. The alternatives now live inside the guard.
    ("Are you currently based near our LA office and able to work onsite 3 days a week?", None),

    # ADAPTER DEFECT: bare `itar` in the us_citizen_or_pr pattern matched
    # inside "mil-ITAR-y", so EVERY question containing "military" resolved to a citizenship /
    # export-control key. Third sighting of this exact substring family, after `employ` inside
    # "employER" and `retail` in score.py's OUT_RX. Now \bitar\b.
    ("Are you a Veteran or an active member of the military?", "eeoc_veteran"),
    ("Are you a protected veteran?", "eeoc_veteran"),
    # ...and the real export-control question must still resolve to us_citizen_or_pr.
    ("Is this technology subject to ITAR restrictions?", "us_citizen_or_pr"),

    # Demographic block. sex_assigned_at_birth is its own key, NOT an
    # alias of eeoc_gender (whose pattern is a bare \bsex\b) -- different questions, and for
    # many people different answers.
    ("Sex Assigned at Birth (required for health benefits)", "sex_assigned_at_birth"),
    ("Gender Identity", "eeoc_gender"),
    ("Sexual Orientation", "eeoc_sexual_orientation"),
    ("Do you identify as disabled?", "eeoc_disability"),

    # Yes/No notice-period question used to match earliest_start_date, i.e. it would have
    # written a DATE into a boolean field.
    ("Do you have a notice period with your current employer or other restrictions on "
     "your availability to start?", "notice_period"),
    ("When can you start?", "earliest_start_date"),

    # The blanket "agree to terms" key (profile.json answers.acknowledge)...
    ("I agree to the Terms and Conditions", "acknowledge"),
    ("I certify that the information provided is true and accurate", "acknowledge"),
    ("I acknowledge the Candidate Privacy Notice", "acknowledge"),
    # ...must NOT swallow an attestation that is a factual claim rather than terms. An
    # exclusivity/top-preference tick forecloses every other req at that firm for the season,
    # so it stays unanswered and routes to the user. It matches on "acknowledg" otherwise.
    ("I acknowledge that Acme is my top choice employer", "exclusivity_attestation"),
    ("Does this firm represent your top preference, and do you acknowledge you may not "
     "apply to other roles here?", "exclusivity_attestation"),

    # ADAPTER DEFECT found live: "hispanic|latino" was a bare alternative INSIDE eeoc_race's
    # pattern, so "Are you Hispanic/Latino?" resolved to eeoc_race and would have written the
    # race value into an ethnicity question profile.json answers separately. Same ordering
    # family as preferred_name-before-first_name.
    ("Are you Hispanic/Latino?", "eeoc_hispanic_latino"),
    ("Are you Hispanic or Latino?", "eeoc_hispanic_latino"),
    ("Race/Ethnicity", "eeoc_race"),
    ("What is your race?", "eeoc_race"),

    # "Do you require work authorization?" is a SPONSORSHIP question. It previously matched
    # authorized_to_work and was only right by luck, because both constants happen to be Yes.
    ("Do you require work authorization?", "requires_sponsorship"),
]


def main():
    rules = _find_rules(json.loads(RULES_PATH.read_text()))
    assert rules, f"no answer_rules list found in {RULES_PATH}"

    failures = []
    for label, want in CASES:
        got = match(label, rules)
        if got != want:
            failures.append((label, want, got))

    for label, want, got in failures:
        print(f"FAIL {label[:70]!r}\n     want={want} got={got}")

    print(f"\n{len(CASES) - len(failures)}/{len(CASES)} passed")
    assert not failures, f"{len(failures)} answer_rule regressions"
    print("all answer_rule mappings OK")


if __name__ == "__main__":
    main()
    sys.exit(0)
