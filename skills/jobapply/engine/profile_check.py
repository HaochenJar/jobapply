#!/usr/bin/env python3
"""Validate the frozen profile store. Run after every hand-edit."""
import json, re, sys
from pathlib import Path

PROFILE = Path.home() / ".config/jobapply/profile.json"
# Same list as adapters.NEVER_STORE and adapters/_common.json's fraud_scan regex. Keep in sync.
BANNED = re.compile(r"\b(ssn|social security|dob|date of birth|birthdate|passport|bank account|"
                    r"routing number|iban|credit card|driver'?s? licen[cs]e)\b")

def load(path=PROFILE):
    return json.loads(Path(path).read_text())

def _keys(o, prefix=""):
    """Yield every (dotted_path, key) in the tree."""
    if isinstance(o, dict):
        for k, v in o.items():
            yield f"{prefix}{k}", k
            yield from _keys(v, f"{prefix}{k}.")
    elif isinstance(o, list):
        for i, v in enumerate(o):
            yield from _keys(v, f"{prefix}{i}.")

def validate(p=None):
    p = p if p is not None else load()
    a = p["answers"]

    # The pair is asserted as a UNIT so no future edit can flip one half.
    pair = (a.get("are you legally authorized to work in the united states"),
            a.get("will you now or in the future require sponsorship"))
    assert pair == ("Yes", "Yes"), f"WORK-AUTH PAIR BROKEN: {pair} (must be ('Yes','Yes'))"
    assert a.get("are you authorized to work without sponsorship") == "No"
    assert a.get("are you a us citizen or permanent resident") == "No"
    assert a.get("do you have unrestricted work authorization") == "No"
    assert a.get("what is your visa type", "").startswith("F-1"), "visa type must be F-1"

    # Fraud tripwire: none of these may ever be a key in the file.
    # (fraud_tripwire itself is the abort list, not stored data -- skip it.)
    bad = [d for d, k in _keys(p) if not d.startswith("fraud_tripwire.")
           and BANNED.search(re.sub(r"[^a-z0-9 ]", " ", k.lower()))]
    assert not bad, f"PII KEY PRESENT (never store these): {bad}"

    # Every non-null attachment must exist on disk.
    gone = [f"{k} -> {v}" for k, v in p["attachments"].items()
            if not k.startswith("_") and v and not Path(v).exists()]
    assert not gone, f"ATTACHMENT PATH MISSING: {gone}"
    return True

def report(p=None):
    p = p if p is not None else load()
    print(f"profile: {PROFILE}")
    try:
        validate(p)
        print("VALID: work-auth pair frozen, no PII keys, all attachment paths exist.\n")
    except AssertionError as e:
        print(f"INVALID: {e}\n"); return 1

    ident = p["identity"]
    blocking = []
    for k in ("address_line1", "city", "postal_code"):
        if not ident.get(k):
            blocking.append(f"identity.{k} -- home address (required by Workday/iCIMS; blocks those submits)")
    # Salary may be a DELIBERATE blank: no anchor, and a required salary field
    # routes to the human queue per employer. Not a gap -- do not list it as blocking.
    if p["answers"].get("earliest start date") is None:
        blocking.append("answers['earliest start date'] -- must be the OPT EAD start date, "
                        "NOT graduation")
    for k, note in p["attachments"]["_MISSING"].items():
        if "Blocking" in note:
            blocking.append(f"attachments.{k} -- {note}")

    print("BLOCKING -- you must supply (see INTAKE.md next to this file):")
    for b in blocking:
        print(f"  [ ] {b}")
    print("\nNON-BLOCKING (generated per-application):")
    for k, note in p["attachments"]["_MISSING"].items():
        if "Not blocking" in note:
            print(f"  - attachments.{k}")
    print(f"\nlimits: {p['limits']['daily_target']}/day, "
          f"{p['limits']['per_company_per_week']}/company/week, "
          f"tier1_requires_approval={p['limits']['tier1_requires_approval']}")
    return 0

if __name__ == "__main__":
    # self-check: a flipped half of the pair must fail
    good = load()
    assert validate(good)
    import copy
    for k, v in [("will you now or in the future require sponsorship", "No"),
                 ("are you a us citizen or permanent resident", "Yes")]:
        bad = copy.deepcopy(good); bad["answers"][k] = v
        try: validate(bad); raise SystemExit(f"SELF-CHECK FAILED: flipping {k!r} was accepted")
        except AssertionError: pass
    bad = copy.deepcopy(good); bad["identity"]["ssn"] = "x"
    try: validate(bad); raise SystemExit("SELF-CHECK FAILED: ssn key was accepted")
    except AssertionError: pass
    bad = copy.deepcopy(good); bad["attachments"]["headshot"] = "/nope.jpg"
    try: validate(bad); raise SystemExit("SELF-CHECK FAILED: missing attachment was accepted")
    except AssertionError: pass
    print("self-check: OK\n")
    sys.exit(report(good))
