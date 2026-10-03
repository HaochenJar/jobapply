#!/usr/bin/env python3
"""ATS adapters. The recipes are DATA (adapters/*.json); this only routes and resolves.

plan() returns {steps: [...], submit: {...}} where every value is already substituted, so the
filling agent executes steps mechanically through the Chrome MCP tools instead of reasoning
about the form. submit is NEVER inside steps: it is irreversible and needs a human go-ahead.
"""
import json, re, pathlib

DIR = pathlib.Path(__file__).parent / "adapters"
VARIANTS = pathlib.Path(__file__).parent / "docs" / "variants"
# score.py's role_tier -> the resume docs.py builds for that family. The resume is a
# per-application artifact, so profile.json's attachments.resume_pdf is null BY DESIGN;
# without this resolution every Greenhouse/Ashby/Lever plan was human_queue on a missing
# required file, which is the whole pipeline.
VARIANT_BY_ROLE = {1: "quant", 2: "quantdev", 3: "analyst", 4: "aieng"}
PROFILE = pathlib.Path.home() / ".config/jobapply/profile.json"
COMPANIES = pathlib.Path(__file__).parent / "companies.json"


def _no_fill_form_companies():
    """Companies confirmed (live, this pipeline) to have no fillable form: a cross-origin
    iframe embed with no reachable canonical ATS URL, or a bespoke wizard the adapters don't
    model. Discovered the hard way, one wasted agent round-trip per company, before this was
    written down anywhere -- so every future run pays the cost once, not every time."""
    if not COMPANIES.exists():
        return {}
    reg = json.loads(COMPANIES.read_text())
    return {e["name"].strip().lower(): e["no_fill_form"]
            for entries in reg.values() if isinstance(entries, list)
            for e in entries if isinstance(e, dict) and e.get("no_fill_form")}


NO_FILL_FORM = _no_fill_form_companies()
NEVER_STORE = ("ssn", "date_of_birth", "passport_number", "bank_account", "routing_number",
               "drivers_license_number")
# Master switch, same source as db.claim()'s. False (the shipped state) means plan() never
# emits a submit block at all, so no batch can contain one.
AUTO_SUBMIT = bool(json.loads(PROFILE.read_text())["limits"].get("auto_submit", False)) \
    if PROFILE.exists() else False

# Canonical key -> where it lives in the frozen profile.json. This table is the ONLY
# path from the file to a form value: nothing here is inferred, defaulted, or generated,
# and a key with no entry is simply absent (-> human_queue), never guessed.
# The work-auth block is copied verbatim from `answers` and asserted as a unit in plan().
_MAP = {
    "first_name": ("identity", "legal_first_name"), "last_name": ("identity", "legal_last_name"),
    "preferred_name": ("identity", "preferred_name"),
    "full_name": ("identity", "full_name"), "email": ("identity", "email"),
    "phone": ("identity", "phone"), "linkedin": ("identity", "linkedin"),
    "current_company": ("identity", "current_company"),
    "current_title": ("identity", "current_title"),
    "github": ("identity", "github"), "website": ("identity", "website"),
    "city": ("identity", "city"), "state": ("identity", "state"),
    "address_line1": ("identity", "address_line1"),
    "address_line2": ("identity", "address_line2"),
    "postal_code": ("identity", "postal_code"),
    "additional_information": ("answers", "additional information"),
    "data_processing_consent": ("answers", "data_processing_consent"),
    "languages_fluent": ("answers", "what languages do you speak fluently"),
    "years_experience_default": ("answers", "years of professional experience"),
    "non_compete": ("answers", "do you have a non-compete agreement"),
    "pronouns": ("answers", "preferred pronouns"),
    "country": ("identity", "country"), "resume_pdf": ("attachments", "resume_pdf"),
    "authorized_to_work":
        ("answers", "are you legally authorized to work in the united states"),
    "requires_sponsorship":
        ("answers", "will you now or in the future require sponsorship"),
    "authorized_without_sponsorship":
        ("answers", "are you authorized to work without sponsorship"),
    "us_citizen_or_pr": ("answers", "are you a us citizen or permanent resident"),
    "visa_status": ("answers", "what is your visa type"),
    "needs_cpt_opt": ("answers", "will you require cpt or opt"),
    "earliest_start_date": ("answers", "earliest start date"),
    "salary_expectation": ("answers", "what are your salary expectations"),
    "willing_to_relocate": ("answers", "are you willing to relocate"),
    "willing_onsite_relocate": ("answers", "willing_onsite_relocate"),
    "previously_employed": ("answers", "have you previously been employed by this company"),
    "relatives_employed": ("answers", "do you have any relatives employed by this company"),
    "over_18": ("answers", "are you at least 18 years of age"),
    "source_of_referral": ("answers", "how did you hear about us"),
    "eeoc_gender": ("answers", "what is your gender"),
    "eeoc_race": ("answers", "race or ethnicity"),
    # A distinct question from race, with a DIFFERENT answer -- see eeoc_hispanic_latino's
    # entry in adapters/_common.json for why it must be matched before eeoc_race.
    "eeoc_hispanic_latino": ("answers", "are you hispanic or latino"),
    "eeoc_veteran": ("answers", "veteran status"),
    "eeoc_disability": ("answers", "disability status"),
    # sex_assigned_at_birth is deliberately its OWN key rather than an alias of eeoc_gender:
    # they are different questions and for many people different answers, and conflating them
    # would put an answer the user did not give into a field they did not see.
    "eeoc_sexual_orientation": ("answers", "sexual orientation"),
    "sex_assigned_at_birth": ("answers", "sex assigned at birth"),
    "notice_period": ("answers", "notice period"),
    # Blanket "agree to terms / arbitration / certification" checkbox. null in profile.json (the
    # default) = every such box goes to the user; "Yes" = the user accepts all of them.
    "acknowledge": ("answers", "acknowledge"),
    "ai_screening_consent": ("answers", "ai_screening_consent"),
}


def load_profile(path=PROFILE):
    """The frozen profile.json, flattened to canonical keys. No agent builds this dict.

    Everything is a verbatim copy; anything blank or absent stays absent so the
    required-field check routes the application to a human instead of inventing a value.
    """
    raw = json.loads(pathlib.Path(path).read_text())
    out = {}
    for k, (sect, src) in _MAP.items():
        v = (raw.get(sect) or {}).get(src)
        if v not in (None, ""):
            out[k] = v
    ed = (raw.get("education") or [{}])[0]          # [0] = the degree in progress
    ident = raw.get("identity") or {}
    derived = {"school": ed.get("school"), "degree": ed.get("degree"), "major": ed.get("field"),
               "grad_year": (ed.get("end") or "")[:4], "grad_month": (ed.get("end") or "")[5:7],
               "start_year": (ed.get("start") or "")[:4], "start_month": (ed.get("start") or "")[5:7],
               "gpa": (raw.get("education") or [{}])[-1].get("gpa"),
               "location": ", ".join(x for x in (ident.get("city"), ident.get("state")) if x),
               "phone_national": re.sub(r"\D", "", ident.get("phone") or ""),
               "workday_accounts": (raw.get("accounts") or {}).get("workday") or []}
    out.update({k: v for k, v in derived.items() if v})
    return out


def workday_tenant(url_or_tenant):
    """The per-employer account boundary: <tenant>.wd<N>.myworkdayjobs.com -> "<tenant>".

    Anything that is not a Workday host (vanity domain, bare slug) comes back lowercased
    as-is, so an unrecognised host can never accidentally match a known tenant.
    """
    m = re.search(r"//([^./]+)\.wd\d+\.myworkday(?:jobs|site)\.com", url_or_tenant, re.I)
    return (m.group(1) if m else url_or_tenant).lower()


def workday_account(url_or_tenant, prof=None):
    """True iff the user already has a candidate account at this Workday tenant.

    Workday has no guest apply path and the account is per-EMPLOYER: one at tenant A does
    not exist at tenant B. Creating one is a prohibited action (password entry) and costs a
    human 5-12 min plus an emailed verification, so a tenant with no login is not
    automatable. profile.json's accounts.workday is the single source of truth, read by both
    plan() and discover.store() so the adapter and the pipeline agree on the same rows.
    """
    p = prof if prof is not None else load_profile()
    return workday_tenant(url_or_tenant) in {str(t).lower() for t in p.get("workday_accounts") or []}


def load(name):
    """Adapter dict with the shared vocabulary (methods, answer_rules, helpers) merged in."""
    a = json.loads((DIR / (name + ".json")).read_text())
    a.update({k: v for k, v in json.loads((DIR / "_common.json").read_text()).items()
              if k not in a})
    return a


def _all():
    return sorted((load(p.stem) for p in DIR.glob("*.json") if not p.stem.startswith("_")),
                  key=lambda a: a["priority"])


def detect(url):
    """Platform name from URL, most specific first. None if unknown."""
    for a in _all():
        if any(re.search(p, url, re.I) for p in a["match"]["url"]):
            return a["platform"]
    return None


def dom_sniff_js():
    """One JS snippet naming the platform from the DOM, for vanity domains that hide the ATS."""
    pairs = [[s, a["platform"]] for a in _all() for s in a["match"].get("dom", [])]
    return ("(async()=>{for(const p of %s){try{if(document.querySelector(p[0]))return p[1]}"
            "catch(_){}}return null})()" % json.dumps(pairs))


def apply_url(name, url):
    """Posting URL -> application-form URL."""
    for pat, repl in load(name)["apply_url"]["rules"]:
        if re.search(pat, url, re.I):
            return re.sub(pat, repl, url, flags=re.I)
    return url


def _js(template, **sub):
    for k, v in sub.items():
        template = template.replace("{{%s}}" % k, v)
    return template


def _verify_js(filled):
    spec = json.dumps([[f["key"], f["selector"], f["read"]] for f in filled])
    return (
        "(async()=>{const spec=" + spec + ";const out={};for(const it of spec){"
        "const k=it[0],s=it[1],r=it[2];"
        "if(r==='group'){const g=[...document.querySelectorAll(s)];"
        "out[k]=g.length?(g.filter(x=>x.checked).map(x=>x.value).join(',')||''):'MISSING';continue}"
        "const e=document.querySelector(s);if(!e){out[k]='MISSING';continue}"
        "out[k]=r==='checked'?e.checked:r==='file'?((e.files&&e.files[0])?e.files[0].name:'')"
        ":r==='text'?__txt(e.closest('[class*=select__control]')||e.parentElement||e)"
        ":r==='select_text'?(e.options&&e.options[e.selectedIndex]?e.options[e.selectedIndex].text:e.value)"
        ":e.value}return out})()")


def plan(url, prof=None, job=None):
    """Ordered, fully-resolved action list for one application.

    prof defaults to the frozen profile.json on disk -- pass one only in tests.
    """
    profile = prof if prof is not None else load_profile()
    leaked = [k for k in NEVER_STORE if profile.get(k)]
    if leaked:
        raise ValueError("profile contains prohibited data, refusing to build a plan: %s" % leaked)
    # FROZEN work-authorization constants, asserted together, never inferred.
    pair = (profile.get("authorized_to_work"), profile.get("requires_sponsorship"))
    if pair != ("Yes", "Yes"):
        raise ValueError("work-auth constants must be ('Yes','Yes') read verbatim from "
                         "profile.json; got %r" % (pair,))
    # A false claim to US citizenship on an employment form is 18 U.S.C. 911 and a permanent
    # inadmissibility bar. There is no code path that may answer these anything but "No".
    for k in ("us_citizen_or_pr", "authorized_without_sponsorship"):
        if profile.get(k) not in (None, "No"):
            raise ValueError("%s must be 'No' (or absent); got %r" % (k, profile.get(k)))

    # Companies with no fillable form at all (confirmed live, cross-origin iframe with no
    # reachable canonical ATS URL, or a bespoke wizard) -- route straight to human_queue
    # before spending a single browser round-trip re-discovering what's already known.
    company = ((job or {}).get("company_name") or "").strip().lower()
    # The tag describes the COMPANY'S OWN wrapper page. A direct
    # job-boards.greenhouse.io/embed/job_app?for=<board>&token=<id> URL skips that wrapper
    # entirely (docs/learnings.md, "SOLVED: cross-origin Greenhouse iframe"), so it is not a
    # dead end -- short-circuit only while the URL is still the wrapper.
    if company in NO_FILL_FORM and "job-boards.greenhouse.io" not in url.lower():
        return {"platform": None, "route": "human_queue",
                "reason": "confirmed no-fill-form company: " + NO_FILL_FORM[company],
                "steps": [], "submit": None}

    # job['platform'] is the escape hatch for vanity domains (careers.<co>.com), where the URL
    # says nothing: run dom_sniff_js() in the tab first, then re-plan with what it named.
    name = (job or {}).get("platform") or detect(url)
    if not name:
        return {"platform": None, "route": "human_queue", "reason": "unknown ATS: " + url,
                "next": "run this in the tab; if it names a platform, re-call plan with "
                        "job={'platform': <name>}",
                "dom_sniff_js": dom_sniff_js(), "steps": [], "submit": None}
    a = load(name)
    out = {"platform": name, "url": url, "apply_url": apply_url(name, url),
           "difficulty": a["difficulty"], "js_helpers": a["js_helpers"],
           "methods": a["methods"], "do_not_touch": a.get("do_not_touch", []),
           "blockers": a["blockers"], "rate": a.get("rate"), "flow": a.get("flow"),
           "route": "auto", "steps": [], "submit": None}
    if not a.get("enabled", True):
        out.update(route=a.get("route", "human_queue"), reason=a["why_disabled"])
        return out
    # Workday's wall is per-employer. A tenant the user is already signed up at is automatable;
    # one that would need a new account is not, and gets the same terminal status
    # discover.store() writes for it, so the two never disagree about the same row.
    if name == "workday" and not workday_account(url, profile):
        out.update(route="manual", reason="no Workday account at tenant %r: applying there needs "
                   "a new per-employer account (password + emailed verification), which the "
                   "agent must never create. The user signs up by hand, adds the tenant to "
                   "profile.json accounts.workday, and this becomes route=auto."
                   % workday_tenant(url))
        return out

    vals = dict(profile)
    vals.update((job or {}).get("overrides", {}))
    # also fires when the tailored path does not exist: app-filler passes
    # applications/<uid>/resume.pdf, and emitting an upload of a missing file fails at
    # runtime with no readback. Falling back to the provenance-checked family variant is
    # the honest degrade.
    if not (vals.get("resume_pdf") and pathlib.Path(str(vals["resume_pdf"])).exists()):
        v = (job or {}).get("variant") or VARIANT_BY_ROLE.get((job or {}).get("role_tier"))
        f = VARIANTS / ("%s.pdf" % v) if v else None
        if f and f.exists():                # absent -> stays missing -> human_queue
            vals["resume_pdf"] = str(f)
    steps, filled, missing = [], [], []

    def js_step(why, code, **kw):
        steps.append(dict(op="js", why=why, js=a["js_helpers"] + code, **kw))

    # --- preflight
    steps.append({"op": "navigate", "url": out["apply_url"]})
    if a.get("preflight_js"):
        js_step(a["preflight_js"]["why"], a["preflight_js"]["js"], on=a["preflight_js"]["on"])
    steps.append({"op": "read_page", "filter": "interactive",
                  "why": "refs for file_upload and for anything the JS path cannot reach"})
    if a.get("gate"):
        steps.append({"op": "abort_if", "why": a["gate"]["why"],
                      "js": a["js_helpers"] + a["gate"]["detect_js"],
                      "abort_when": "result != 'ok'", "on_abort": a["gate"]["route"],
                      "hard_rule": a["gate"]["hard_rule"]})
    for k in ("fraud_scan", "captcha_scan"):
        p = a["preflight"][k]
        steps.append({"op": "abort_if", "why": p["why"], "js": a["js_helpers"] + p["js"],
                      "abort_when": p["abort_when"],
                      "on_abort": p.get("on_abort", "abort_and_flag")})

    # --- fields, ordered by the platform's phase list
    files = [f for f in a["fields"] if f["method"] == "file"]
    text = [f for f in a["fields"] if f["method"] != "file"]

    def emit(group):
        for f in group:
            v = vals.get(f["key"])
            if v in (None, ""):
                (missing.append(f["key"]) if f.get("required") else None)
                continue
            m = a["methods"][f["method"]]
            rec = {"key": f["key"], "selector": f["selector"], "read": m["read"]}
            if f["method"] == "file":
                steps.append({"op": "file_upload", "selector": f["selector"], "path": str(v),
                              "why": f.get("note", ""), "how": m["use"]})
            else:
                js_step("set %s" % f["key"],
                        _js(m["js"], SEL=json.dumps(f["selector"]), VAL=json.dumps(str(v))),
                        expect=str(v), key=f["key"])
            filled.append(rec)

    for phase in a["order"]:
        if phase == "upload":
            emit(files)
        elif phase == "fields":
            emit(text)
        elif phase == "wait_parse":
            w = a["wait_parse"]
            js_step(w["why"], _js(w["js"], SEL=json.dumps(w["watch_selector"]),
                                  MS=str(w["timeout_ms"]), SETTLE=str(w["settle_ms"])),
                    on_timeout=w["on_timeout"])
        elif phase == "questions" and a.get("question_map"):
            rules = []
            for r in a["answer_rules"]["rules"]:
                rules.append({"pattern": r["pattern"], "kind": r["kind"], "key": r.get("key"),
                              "answer": vals.get(r["key"]) if r.get("key") else None,
                              "why": r.get("why")})
            steps.append({
                "op": "discover", "why": "custom question ids are per-job; never hardcode them",
                "js": a["js_helpers"] + a["question_map"]["discover_js"],
                "then": ("for each discovered question: first matching rule pattern (case-insensitive) "
                         "against its label wins -> build js from methods[methods_by_type[type]] with "
                         "SEL=question.selector, VAL=rule.answer. kind=fraud -> abort and flag. "
                         "kind=human or no match -> human_queue. Never invent an answer."),
                "methods_by_type": a["question_map"]["methods_by_type"],
                "rules": rules, "unmatched": a["answer_rules"]["unmatched"]})

    if missing:
        out.update(route="human_queue", reason="missing required profile keys: %s" % missing)

    steps.append({"op": "verify", "why": "fail-closed readback: proof the fill actually took",
                  "js": a["js_helpers"] + _verify_js(filled),
                  "expect": {f["key"]: (pathlib.Path(str(vals[f["key"]])).name
                                        if f["read"] == "file" else str(vals[f["key"]]))
                             for f in filled},
                  "compare": "file keys: basename must match. others: trimmed case-insensitive equal. "
                             "any MISSING/empty/NO_OPTION -> human_queue, do NOT submit.",
                  "also": a["js_helpers"] + a["verify"]["extra_js"] if a.get("verify") else None})
    out["steps"] = steps
    # Tier-1 (named dream firms: quant / buyside / IB) never gets a submit block at all.
    # One shot per firm; a burned req there is unrecoverable, so the fill stops at a draft
    # and db.claim() refuses it too without --approved.
    if (job or {}).get("tier") == 1:
        out.update(route="draft_review", submit=None,
                   reason="tier-1 firm: fill and verify, then STOP. Human reviews the draft and "
                          "submits by hand (db.py claim <uid> --approved).")
        return out
    if not AUTO_SUBMIT:
        out.update(route="draft_review", submit=None,
                   reason="auto_submit is off in profile.json: fill, verify, stop at the draft.")
        return out
    # A submit is only reachable through db.claim(uid), and claim() raises on a uid with no jobs
    # row. Promising route='auto' for a job that was never discovered sends an agent through a
    # whole fill before it hits an unpassable gate — the Indeed run found exactly that. Say it up
    # front instead. (Indeed has no sourcing path at all: its HTTP endpoints 403, so nothing on
    # that channel is claimable today.)
    if not (job or {}).get("uid"):
        out.update(route="manual", submit=None, steps=[],
                   reason="no jobs-row uid: db.claim() would refuse, so this can never be "
                          "submitted by the pipeline. Source it into the DB first, or apply "
                          "by hand.")
        return out
    if a.get("submit") and out["route"] == "auto":
        out["submit"] = dict(a["submit"], op="submit",
                             requires="the verify step passed AND db.claim(uid) returned. claim() "
                                      "is the enforcement point: it re-checks tier 1, auto_submit "
                                      "and the per-company caps, and writes 'submitting' before "
                                      "the click so a crash cannot double-submit.")
    return out


def demo():
    # Regression guard, live 2026-08-24: current_title and education start_year/start_month
    # must resolve to real facts and never collide with earliest_start_date (a frozen,
    # legally-significant employment answer keyed off the OPT EAD -- a bare "Start date"
    # must always mean the job, never the degree).
    import json as _json4, re as _re4
    _rules4 = _json4.loads((pathlib.Path(__file__).parent / "adapters" / "_common.json")
                           .read_text())["answer_rules"]["rules"]
    def _first4(q):
        for r in _rules4:
            if _re4.search(r["pattern"], q, _re4.I):
                return r["key"]
        return None
    assert _first4("What is your current or previous job title?") == "current_title"
    assert _first4("Education Start Date") == "start_year"
    assert _first4("Start date") == "earliest_start_date", \
        "bare 'Start date' must default to employment, never education"
    assert _first4("When can you start?") == "earliest_start_date"

    # Regression guard, live 2026-08-24: data_processing_consent must stay narrowly scoped to
    # generic privacy/data-processing boilerplate and must NEVER bleed into arbitration/waiver
    # consent, which is a different, higher-stakes category that stays on 'acknowledge'
    # (deliberately unanswered). Both keys route through the same "consent"/"acknowledg" word
    # roots, so this is the one guard that actually matters here.
    import json as _json3, re as _re3
    _rules3 = _json3.loads((pathlib.Path(__file__).parent / "adapters" / "_common.json")
                           .read_text())["answer_rules"]["rules"]
    def _first3(q):
        for r in _rules3:
            if _re3.search(r["pattern"], q, _re3.I):
                return r["key"]
        return None
    assert _first3("Do you consent to Acme processing your personal information for the purpose "
                   "of assessing your candidacy in accordance with Acme's Applicant Privacy "
                   "Policy?") == "data_processing_consent"
    assert _first3("I agree to resolve any disputes through binding arbitration and waive my "
                   "right to a jury trial or class action.") == "acknowledge"
    assert _first3("I acknowledge and agree to the Arbitration Agreement as a condition of "
                   "employment.") == "acknowledge"
    assert _first3("I certify that the information provided in this application is true and "
                   "correct.") == "acknowledge"

    # CRITICAL regression guard, live 2026-08-24: "Are you legally authorised to work
    # full-time..." (British spelling) fell through authorized_to_work (which only recognized
    # American "authorized") to previously_employed's broad "are you...work" pattern -- which
    # would have written the WRONG answer ("No") into a work-authorization question where the
    # correct frozen answer is "Yes". Caught only by the filling agent's own semantic check
    # before it reached a real form, not by this suite. This is the single highest-stakes bug
    # found this session; guard both halves of the fix permanently.
    import json as _json2, re as _re2
    _rules2 = _json2.loads((pathlib.Path(__file__).parent / "adapters" / "_common.json")
                           .read_text())["answer_rules"]["rules"]
    def _first(q):
        for r in _rules2:
            if _re2.search(r["pattern"], q, _re2.I):
                return r["key"]
        return None
    assert _first("Are you legally authorised to work full-time in the country where this "
                  "job is based?") == "authorized_to_work", \
        "British spelling regressed: 'authorised' no longer reaches authorized_to_work"
    assert _first("Are you legally authorized to work in the United States?") == "authorized_to_work"
    assert _first("Are you willing to work remotely?") != "previously_employed", \
        "previously_employed regressed: matching bare 'to work' phrasing again"
    assert _first("Have you worked at or been a consultant for Acme in the past?") \
        == "previously_employed", "previously_employed regressed: lost a real confirmed case"

    # Regression guard, live 2026-08-24: a semantic-shape widening of the previously_employed
    # answer_rule (adapters/_common.json) matched bare noun-phrase labels like
    # "Current or Previous Employer" -- a FREE-TEXT field asking to name an employer -- as if
    # they were yes/no boilerplate. It would have typed the frozen "No" answer into a text
    # field asking for a company name. Caught by app-filler's own verify-before-submit step,
    # not by this suite -- add the check here so it can never regress silently again.
    import json as _json, re as _re
    _rules = _json.loads((pathlib.Path(__file__).parent / "adapters" / "_common.json")
                         .read_text())["answer_rules"]["rules"]
    _pe = next(r for r in _rules if r.get("key") == "previously_employed")
    for _label in ("Current or Previous Employer", "Current Employer", "Previous Employer"):
        assert not _re.search(_pe["pattern"], _label, _re.I), \
            f"previously_employed regressed: now matches free-text label {_label!r}"
    for _q in ("Are you a current or former employee of Acme?",
              "Have you previously been employed by this company?",
              "Did you work here before?"):
        assert _re.search(_pe["pattern"], _q, _re.I), \
            f"previously_employed regressed: no longer matches real question {_q!r}"

    urls = {
        "https://job-boards.greenhouse.io/anthropic/jobs/4020567008": "greenhouse",
        "https://boards.greenhouse.io/stripe/jobs/1234567?gh_src=x": "greenhouse",
        "https://jobs.lever.co/veeva/1a2b3c4d-5e6f-4071-8899-aabbccddeeff": "lever",
        "https://jobs.ashbyhq.com/openai/6f7e8d9c-1234-4abc-9def-0123456789ab": "ashby",
        "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/US-CA-Santa-Clara/Engineer_JR2001098": "workday",
        "https://www.linkedin.com/jobs/view/3912345678/": "linkedin_easyapply",
        "https://www.indeed.com/viewjob?jk=abc123def456": "indeed",
        # deliberately unmatched: no adapter was written for them, so they queue for a human
        "https://www.glassdoor.com/job-listing/x": None,
        "https://www.ziprecruiter.com/c/x/Job/y": None,
        "https://example.com/careers/123": None,
    }
    for u, want in urls.items():
        got = detect(u)
        assert got == want, (u, got, want)

    assert apply_url("lever", "https://jobs.lever.co/veeva/1a2b3c4d-5e6f-4071-8899-aabbccddeeff") \
        .endswith("/apply")
    assert apply_url("lever", "https://jobs.lever.co/veeva/1a2b3c4d-5e6f-4071-8899-aabbccddeeff/apply") \
        .count("/apply") == 1
    assert apply_url("ashby", "https://jobs.ashbyhq.com/openai/6f7e8d9c-1234-4abc-9def-0123456789ab") \
        .endswith("/application")

    p = dict(first_name="Alex", last_name="Rivera", full_name="Alex Rivera",
             email="alex.rivera@example.com", phone="+15550100199",
             phone_national="5550100199", linkedin="https://linkedin.com/in/alex-rivera-example",
             website="https://alexrivera.dev", location="Springfield, Illinois",
             resume_pdf="/tmp/Alex_Rivera_Resume.pdf",
             current_company="Northwind Capital",
             authorized_to_work="Yes", requires_sponsorship="Yes",
             authorized_without_sponsorship="No", us_citizen_or_pr="No",
             visa_status="F-1 student visa", source_of_referral="Company Website")

    JOB = {"uid": "lever:1a2b3c4d-5e6f-4071-8899-aabbccddeeff", "tier": 2}
    lv = plan("https://jobs.lever.co/veeva/1a2b3c4d-5e6f-4071-8899-aabbccddeeff", p, JOB)
    ops = [s["op"] for s in lv["steps"]]
    up = ops.index("file_upload")
    first_field = next(i for i, s in enumerate(lv["steps"])
                       if s["op"] == "js" and s.get("key") == "full_name")
    assert up < first_field, "Lever resume upload must precede its text fields"
    assert any(s["op"] == "js" and "PARSE_TIMEOUT" in s["js"]
               for s in lv["steps"][up:first_field]), "Lever must wait for the resume parse"
    assert lv["steps"][first_field]["js"].rstrip().endswith(")()")
    assert "e.value=" in lv["steps"][first_field]["js"], "Lever is plain DOM"

    gh = plan("https://job-boards.greenhouse.io/anthropic/jobs/4020567008", p, {"uid":"gh:4020567008","tier":2})
    fn = next(s for s in gh["steps"] if s.get("key") == "first_name")
    assert "HTMLInputElement.prototype" in fn["js"] and "HTMLTextAreaElement.prototype" in fn["js"]
    assert "Object.getOwnPropertyDescriptor(p,'value').set.call(e,v)" in fn["js"], \
        "Greenhouse is React: the native value setter is mandatory"
    assert "new Event('input',{bubbles:true})" in fn["js"]
    assert "Alex" in fn["js"] and fn["expect"] == "Alex"
    assert [s["op"] for s in gh["steps"]][-1] == "verify"
    assert "submit" not in [s["op"] for s in gh["steps"]], "submit must never be inside steps"
    if AUTO_SUBMIT:
        assert gh["submit"]["op"] == "submit" and gh["submit"]["requires"]
    else:
        assert gh["submit"] is None and gh["route"] == "draft_review"
    assert any(s["op"] == "abort_if" and "ssn" in s["js"] for s in gh["steps"])
    assert any(s["op"] == "abort_if" and "CAPTCHA_CHALLENGE" in s["js"]
               and s["on_abort"] == "human_queue" for s in gh["steps"])
    assert any(s["op"] == "discover" for s in gh["steps"])
    auth = next(r for r in next(s for s in gh["steps"] if s["op"] == "discover")["rules"]
                if r["key"] == "requires_sponsorship")
    assert auth["answer"] == "Yes"

    # Workday: the wall is per-EMPLOYER tenant, so plan() and discover.store() must agree on
    # which rows are automatable. No account -> route='manual', the same terminal status
    # discover writes; an account the user already holds -> route='auto'.
    wd_url = "https://nvidia.wd5.myworkdayjobs.com/en-US/Site/job/US-CA/Eng_JR1"
    assert workday_tenant(wd_url) == "nvidia" and workday_tenant("NVIDIA") == "nvidia"
    assert workday_tenant("https://careers.acme.com/en-US/Acme/job/LA/A_JR9") != "acme"
    assert not workday_account(wd_url, p)
    wd = plan(wd_url, p, {"uid": "wd:JR1", "tier": 2})
    assert wd["route"] == "manual" and wd["submit"] is None and wd["steps"] == [], wd["route"]
    assert "accounts.workday" in wd["reason"] and "nvidia" in wd["reason"]
    known = dict(p, workday_accounts=["NVIDIA"])            # matching is case-insensitive
    assert workday_account(wd_url, known) and workday_account("nvidia", known)
    wd = plan(wd_url, known, {"uid": "wd:JR1", "tier": 2})
    assert wd["route"] == "auto", wd["route"]
    gate = next(i for i, s in enumerate(wd["steps"]) if s.get("on_abort") == "human_queue")
    assert gate < next(i for i, s in enumerate(wd["steps"]) if s["op"] == "file_upload")
    assert wd["apply_url"].endswith("/apply")

    # LinkedIn and Indeed ship ENABLED; both platforms' terms prohibit automated applying, so
    # disable them in their adapter json unless you accept that risk. Honest means: the user's
    # own logged-in browser, a declared rate ceiling the orchestrator can honour,
    # and a session/bot-wall gate that hands off to a human instead of pushing through.
    for n, u in (("linkedin_easyapply", "https://www.linkedin.com/jobs/view/1/"),
                 ("indeed", "https://www.indeed.com/viewjob?jk=abc123")):
        r = plan(u, p, {"uid": "%s:1" % n[:2], "tier": 2})
        assert r["platform"] == n and r["route"] == "auto", (n, r["route"])
        assert 0 < r["rate"]["per_day"] <= 30 and r["rate"]["min_seconds_between"] >= 60, n
        assert r["flow"]["next"], n
        assert "submit" not in [s["op"] for s in r["steps"]], n
        assert r["submit"] and r["submit"]["op"] == "submit", n
        # a gate step routes an un-logged-in session / challenge page to a human, before any fill
        assert any(s["op"] == "abort_if" and s.get("on_abort") == "human_queue"
                   for s in r["steps"]), n
        assert r["steps"][0]["op"] == "navigate" and r["steps"][1]["op"] == "js", n
    vanity = plan("https://careers.acme.com/en-US/Acme/job/LA/Analyst_JR9", p)
    assert vanity["route"] == "human_queue" and "dom_sniff_js" in vanity
    assert plan("https://careers.acme.com/en-US/Acme/job/LA/Analyst_JR9", p,
                {"platform": "workday"})["platform"] == "workday"

    # --- the master switch, both paths. ON is the user's choice; tier 1 ignores it, always.
    t1 = plan("https://job-boards.greenhouse.io/janestreet/jobs/1", p, {"uid":"gh:1","tier": 1})
    assert t1["submit"] is None and t1["route"] == "draft_review", t1["route"]
    t2 = plan("https://job-boards.greenhouse.io/x/jobs/1", p, {"uid":"gh:1","tier": 2})
    assert t2["submit"]["op"] == "submit" and t2["route"] == "auto"
    try:                                   # and flipping it back still puts everything in draft
        globals()["AUTO_SUBMIT"] = False
        off = plan("https://job-boards.greenhouse.io/x/jobs/1", p, {"uid":"gh:1","tier": 2})
        assert off["submit"] is None and off["route"] == "draft_review"
        assert plan("https://job-boards.greenhouse.io/janestreet/jobs/1", p,
                    {"uid": "gh:1", "tier": 1})["submit"] is None
    finally:
        globals()["AUTO_SUBMIT"] = True

    # the real profile.json flattens to the frozen pair, with no model in the path
    real = load_profile()
    assert (real["authorized_to_work"], real["requires_sponsorship"]) == ("Yes", "Yes")
    assert real["us_citizen_or_pr"] == "No" and real["authorized_without_sponsorship"] == "No"
    assert all(v not in (None, "") for v in real.values()), "blanks must be dropped on load"
    assert "ssn" not in real and "date_of_birth" not in real
    assert plan("https://jobs.lever.co/x/1a2b3c4d-5e6f-4071-8899-aabbccddeeff")["route"] \
        in ("auto", "human_queue", "draft_review", "manual")   # no uid -> unclaimable -> manual

    # attachments.resume_pdf is null in the real profile BY DESIGN (built per-application), so
    # a plan for a real queue row must still resolve one from role_tier -- otherwise every
    # Greenhouse/Ashby/Lever job is human_queue on a missing required file and throughput is 0.
    assert real.get("resume_pdf") is None, "if this fires, the fallback below is untested"
    for rt, want in VARIANT_BY_ROLE.items():
        g = plan("https://job-boards.greenhouse.io/x/jobs/1", real,
                 {"uid": "gh:1", "tier": 2, "role_tier": rt})
        up = next(s for s in g["steps"] if s["op"] == "file_upload")
        assert up["path"].endswith("/%s.pdf" % want), (rt, up["path"])
        assert g["route"] == "auto" and g["submit"], (rt, g["route"], g.get("reason"))
    # no role_tier and no variant -> no invented resume, and it fails closed to a human
    assert plan("https://job-boards.greenhouse.io/x/jobs/1", real,
                {"uid": "gh:1", "tier": 2})["route"] == "human_queue"
    # a tailored path that was never built must NOT become an upload of a missing file
    g = plan("https://job-boards.greenhouse.io/x/jobs/1", real,
             {"uid": "gh:1", "tier": 2, "role_tier": 1,
              "overrides": {"resume_pdf": "applications/nope/r.pdf"}})
    up = next(s for s in g["steps"] if s["op"] == "file_upload")
    assert pathlib.Path(up["path"]).exists() and up["path"].endswith("/quant.pdf"), up["path"]

    for bad in ({"authorized_to_work": "Yes", "requires_sponsorship": "No"},
                {"authorized_to_work": "No", "requires_sponsorship": "Yes"},
                {"us_citizen_or_pr": "Yes"},
                {"authorized_without_sponsorship": "Yes"},
                {"ssn": "123-45-6789"}):
        try:
            plan("https://jobs.lever.co/x/1a2b3c4d-5e6f-4071-8899-aabbccddeeff",
                 dict(p, **bad), {"uid": "lever:1", "tier": 2})
            raise AssertionError("should have refused: %s" % bad)
        except ValueError:
            pass

    # Blanks must never reach a form (they route to human_queue instead). Test the BEHAVIOUR on a
    # fixture — asserting the live profile is blank breaks the moment the user fills it in, which is
    # exactly what happened.
    import tempfile as _tf
    _fix = json.loads(PROFILE.read_text())
    _fix["identity"]["city"] = ""
    _fix["answers"]["are you willing to relocate"] = None
    _t = pathlib.Path(_tf.mkdtemp()) / "p.json"; _t.write_text(json.dumps(_fix))
    _blank = load_profile(_t)
    assert "city" not in _blank, "blank identity.city must be dropped, not sent as ''"
    assert "willing_to_relocate" not in _blank, "null answer must be dropped, not sent"

    for a in _all():                       # every declared method exists and is substitutable
        for f in a.get("fields", []):
            assert f["method"] in a["methods"], (a["platform"], f["key"])
        for t, m in (a.get("question_map") or {}).get("methods_by_type", {}).items():
            assert m in a["methods"], (a["platform"], t, m)
        for m in a["methods"].values():
            assert m["js"] is None or "{{" not in _js(m["js"], SEL='"x"', VAL='"y"')
        # deny-lists are real: nothing the adapter fills or clicks may sit on one
        deny = [d["selector"] for d in a.get("do_not_touch", [])]
        assert deny or not a.get("enabled", True), a["platform"]
        for sel in [f["selector"] for f in a.get("fields", [])] \
                + ([a["submit"]["selector"]] if a.get("submit") else []):
            assert not any(d in sel for d in deny), (a["platform"], sel)
        # both preflight scans survive, on every adapter
        assert "ssn" in a["preflight"]["fraud_scan"]["js"]
        assert a["preflight"]["captcha_scan"]["on_abort"] == "human_queue"
    assert "greenhouse" in dom_sniff_js() and "beecatcher" not in dom_sniff_js()
    # Confirmed no-fill-form companies (companies.json) short-circuit before any browser
    # step is built, and never swallow an unrelated company by accident.
    # NO_FILL_FORM is legitimately EMPTY as of 2026-08-25: all 15 entries were re-tested and
    # every one was stale (the direct Greenhouse embed / canonical Ashby URL serves a real
    # form). So do not assert it is populated -- assert the ROUTING still works, using a
    # synthetic tag if the real list is empty.
    tagged = sorted(NO_FILL_FORM)[0] if NO_FILL_FORM else "synthetic no-fill co"
    _restore = None
    if not NO_FILL_FORM:
        NO_FILL_FORM[tagged] = "synthetic entry, demo() only"
        _restore = tagged
    nofill_plan = plan("https://%s.com/jobs/listing/x" % tagged.replace(" ", ""),
                       job={"uid": "test:nofill", "company_name": tagged})
    assert nofill_plan["route"] == "human_queue" and nofill_plan["steps"] == [] \
        and "no-fill-form" in nofill_plan["reason"]
    if _restore:
        NO_FILL_FORM.pop(_restore)
    clean_plan = plan("https://job-boards.greenhouse.io/anthropic/jobs/1",
                      job={"uid": "test:clean", "company_name": "Anthropic"})
    assert "no-fill-form" not in (clean_plan.get("reason") or "")
    # a tagged company reached through the direct embed URL is NOT the wrapper -> fillable
    embed_plan = plan("https://job-boards.greenhouse.io/embed/job_app?for=brex&token=1",
                      job={"uid": "test:embed", "company_name": "Brex"})
    assert "no-fill-form" not in (embed_plan.get("reason") or "") and embed_plan["steps"]
    print("ok: %d adapters (%s), auto_submit=%s, tier-1 draft-only, "
          "%d steps for lever, %d for greenhouse"
          % (len(_all()), ", ".join("%s%s" % (a["platform"],
                                              "" if a.get("enabled", True) else ":off")
                                    for a in _all()),
             AUTO_SUBMIT, len(lv["steps"]), len(gh["steps"])))


if __name__ == "__main__":
    AUTO_SUBMIT = True    # demo() tests the ON path, then flips it off itself to test draft-only
    demo()
