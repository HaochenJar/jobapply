#!/usr/bin/env python3
"""Multi-channel job discovery. Pure HTTP via urllib, stdlib only, no browser.

Boards/slugs/queries live in companies.json (verified live; dead slugs kept
there as status=dead so nobody re-guesses them). Every channel returns dicts
shaped like db.JOB_COLS; main() prefilters, dedups via db.seen_dedup and upserts.

    python3 discover.py [--channels gh,ashby,lever,wd,li,efc] [--limit N] [--dry-run]
"""
import argparse, json, pathlib, re, sys, time, urllib.error, urllib.parse, urllib.request
from html import unescape

import adapters
import db

ROOT = pathlib.Path(__file__).resolve().parent
REGISTRY = ROOT / "companies.json"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# gaps #10: drop before storing. Free, and it protects throughput.
# Matched on a leading word boundary -- plain substrings blanked whole boards
# ("itar" is inside "military", which every EEO footer contains).
# ponytail: still substring-ish, so "U.S. citizenship is not required" false-drops.
# Rare enough to cost less than the postings it saves; tighten if it bites.
BLOCK = ["security clearance", "itar", "us citizen", "u.s. citizen", "citizenship required",
         "unable to sponsor", "no sponsorship", "not able to sponsor",
         "does not sponsor", "do not sponsor", "will not sponsor", "no visa sponsorship",
         # "export control"/"export controls" deliberately excluded: Cloudflare puts hedged
         # boilerplate ("this position MAY require access to... export control laws") on every
         # single posting regardless of role -- confirmed live, it over-blocked ~6% of all jobs,
         # including "Accounting Intern". Only the definite-requirement phrasing is a real signal.
         "must be a us person", "must be a u.s. person", "be a us person", "be a u.s. person"]
BLOCK_RE = [(b, re.compile(r"\b" + re.escape(b).replace("\\ ", r"\s+"), re.I)) for b in BLOCK]


def _get(url, data=None, tries=2, pause=3):
    """Bytes, or None on any failure. One dead channel must not kill the run."""
    h = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    if data is not None:
        h["Content-Type"] = h["Accept"] = "application/json"
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h),
                                        timeout=30) as r:
                return r.read()
        except Exception as e:
            if i + 1 == tries:
                print("  ! %s -> %s" % (url[:80], e), file=sys.stderr)
                return None
            time.sleep(pause)


def _json(url, data=None, tries=2):
    b = _get(url, data, tries)
    try:
        return json.loads(b) if b else None
    except ValueError:
        print("  ! non-JSON from %s" % url[:80], file=sys.stderr)
        return None


def _text(s):
    """HTML (possibly entity-encoded twice, as Greenhouse serves it) -> plain text."""
    s = unescape(s or "")
    s = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", unescape(s)).strip()


def _slug(name):
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower()) or "unknown"


_T1 = None


def tier1_slugs():
    """Every company_slug companies.json calls tier 1, by board slug AND by _slug(name).

    companies.json is the authority on COMPANY tier, but the keyword channels (li, efc)
    are handed the QUERY's tier -- so AQR surfacing on the tier-3 "research analyst" query
    was stored tier=3. jobs.tier is exactly what db.claim()'s draft-only gate reads, so that
    downgrade would have auto-submitted to a tier-1 firm. This set can only tighten a tier.
    """
    global _T1
    if _T1 is None:
        reg = json.loads(REGISTRY.read_text())
        _T1 = set()
        for v in reg.values():
            for e in v if isinstance(v, list) else []:
                if isinstance(e, dict) and e.get("tier") == 1:
                    _T1 |= {str(e.get("slug") or "").lower(), str(e.get("tenant") or "").lower(),
                            _slug(e.get("name"))}
        _T1 -= {"", "unknown"}
    return _T1


def job(uid, source, ats, company_slug, company_name, title, loc, url,
        apply_url=None, posted_at=None, desc_text="", tier=None):
    if (company_slug or "").lower() in tier1_slugs():
        tier = 1                       # see tier1_slugs(): a channel may never downgrade one
    elif tier == 1:
        # ...and a keyword channel may never PROMOTE a non-finance employer to tier 1 either: the
        # query's tier names the role family it searched for, not who answered. Found live:
        # healthcare employers came back on a tier-1 "quantitative" query and sat draft-only
        # in the non-finance queue. Name-only here
        # (li/efc descriptions arrive after job()), so a finance-sounding name stays tier 1.
        import score
        if not score.is_finance({"company_name": company_name, "desc_text": desc_text}):
            tier = 3
    return dict(uid=uid, source=source, ats=ats, company_slug=company_slug,
                company_name=company_name, title=(title or "").strip(), loc=loc or "",
                url=url, apply_url=apply_url or url, first_seen_at=db.now(),
                posted_at=posted_at, desc_text=desc_text, tier=tier, score=None,
                dedup_hash=db.dedup_hash(company_slug, title, loc))


# ---------------------------------------------------------------- channels

def greenhouse(slug, name=None, tier=None):
    d = _json("https://boards-api.greenhouse.io/v1/boards/%s/jobs?content=true" % slug)
    return [job("gh:%s" % j["id"], "gh", "greenhouse", slug,
                j.get("company_name") or name, j.get("title"),
                (j.get("location") or {}).get("name"), j.get("absolute_url"),
                posted_at=j.get("first_published") or j.get("updated_at"),
                desc_text=_text(j.get("content")), tier=tier)
            for j in (d or {}).get("jobs", [])]


def ashby(slug, name=None, tier=None):
    d = _json("https://api.ashbyhq.com/posting-api/job-board/%s" % slug)
    return [job("ashby:%s" % j["id"], "ashby", "ashby", slug, name or slug, j.get("title"),
                j.get("location"), j.get("jobUrl"), j.get("applyUrl"), j.get("publishedAt"),
                j.get("descriptionPlain") or _text(j.get("descriptionHtml")), tier)
            for j in (d or {}).get("jobs", []) if j.get("isListed", True)]


def lever(slug, name=None, tier=None):
    d = _json("https://api.lever.co/v0/postings/%s?mode=json" % slug)
    out = []
    for j in d if isinstance(d, list) else []:
        c = j.get("categories") or {}
        ts = j.get("createdAt")
        out.append(job("lever:%s" % j["id"], "lever", "lever", slug, name or slug, j.get("text"),
                       c.get("location"), j.get("hostedUrl"), j.get("applyUrl"),
                       time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts / 1000)) if ts else None,
                       "%s %s" % (j.get("descriptionPlain") or "", j.get("descriptionBodyPlain") or ""),
                       tier))
    return out


def workday(tenant, site, wd_num="wd5", query="", name=None, tier=None, pages=5):
    """CXS endpoint, 20/page. Descriptions need one extra GET per job, so they are
    left empty -- banks post thousands and the title filter carries the load.
    gaps #11: recorded as status='manual' unless profile.json accounts.workday already
    lists the tenant -- Workday has no guest apply path and the account is per-employer."""
    base = "https://%s.%s.myworkdayjobs.com" % (tenant, wd_num)
    out, offset = [], 0
    for _ in range(pages):
        d = _json("%s/wday/cxs/%s/%s/jobs" % (base, tenant, site),
                  json.dumps({"appliedFacets": {}, "limit": 20, "offset": offset,
                              "searchText": query}).encode())
        posts = (d or {}).get("jobPostings") or []
        for j in posts:
            path = j.get("externalPath") or ""
            out.append(job("wd:%s:%s" % (tenant, path.rsplit("/", 1)[-1]), "wd", "workday",
                           tenant, name or tenant, j.get("title"), j.get("locationsText"),
                           "%s/en-US/%s%s" % (base, site, path), posted_at=j.get("postedOn"),
                           tier=tier))
        offset += 20
        if len(posts) < 20 or offset >= (d or {}).get("total", 0):
            break
    return out


_LI_CARD = re.compile(r'data-entity-urn="urn:li:jobPosting:(\d+)"(.*?)(?=data-entity-urn="urn:li:'
                      r'jobPosting:|\Z)', re.S)


# [^>]*> consumes the rest of the opening tag: without it the capture starts mid-attribute
# and _text() has no tag to strip, leaking 'relative overflow-hidden">' into the description.
_LI_DESC = re.compile(r'show-more-less-html__markup[^>]*>(.*?)</section>', re.S)


def linkedin_desc(jid):
    """One extra GET per posting, against the same logged-out guest endpoint family the
    search above already uses. Unlike workday(), LinkedIn pays for it: the search card
    carries NO description at all, so without this every li row reaches score.py with
    desc_text='' and is judged on its title alone. That silently disabled three things
    that matter -- BLOCK_RE/NOSPONSOR_RX eligibility bars (postings that openly refuse
    sponsorship survived scoring and burned a live form fill), score.py's finance-employer
    +20 (sector language lives in the description, so Goldman Sachs read as non-finance),
    and every HAVE_RX/LACK_RX evidence hit. li volume is ~120/day, not Workday's thousands,
    so the extra GETs are affordable."""
    b = _get("https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/%s" % jid,
             tries=2, pause=5)
    m = _LI_DESC.search((b or b"").decode("utf-8", "replace"))
    return _text(m.group(1)) if m else ""


def linkedin_guest(keywords, location="United States", start=0, tier=None, fresh=True,
                   descs=True):
    """Guest (logged-out) HTML fragment endpoint. ~10 cards per call, paging dies
    past ~offset 400 -- use many narrow queries, not deep pagination."""
    q = {"keywords": keywords, "location": location, "start": start}
    if fresh:
        q["f_TPR"] = fresh if isinstance(fresh, str) else "r86400"  # default: last 24h
    b = _get("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?"
             + urllib.parse.urlencode(q), tries=3, pause=5)
    out = []
    for m in _LI_CARD.finditer((b or b"").decode("utf-8", "replace")):
        jid, card = m.group(1), m.group(2)
        def grab(p, d=""):
            m = re.search(p, card, re.S)
            return _text(m.group(1)) if m else d
        co = grab(r'base-search-card__subtitle"[^>]*>(.*?)</h4>') or "unknown"
        out.append(job("li:%s" % jid, "li", None, _slug(co), co,
                       grab(r'base-search-card__title"[^>]*>(.*?)</h3>'),
                       grab(r'job-search-card__location"[^>]*>(.*?)</span>'),
                       "https://www.linkedin.com/jobs/view/%s" % jid,
                       posted_at=grab(r'listdate[^"]*"[^>]*datetime="([^"]+)"', None), tier=tier))
    if descs:
        for j in out:
            j["desc_text"] = linkedin_desc(j["uid"].split(":", 1)[1])
            time.sleep(1)          # ponytail: fixed floor, no jitter -- politeness, not evasion
    return out


def efc_desc(jid):
    """The search endpoint's embedded JSON is metadata only -- no description field exists
    anywhere in it (confirmed by inspection, 2026-09-02: every efc:* row had desc_text=NULL
    since the channel's first commit, 190/190, silently disabling the sponsorship bar and
    experience-floor checks for the entire channel -- prefilter() and fit_score() both read
    desc_text and got nothing to read).
    The job PAGE is an Angular-Universal SPA: no server-rendered <title>, no JSON-LD JobPosting
    block (both checked, absent). But the page embeds its own hydration cache under
    <script id="ng-state">, keyed by the internal API URLs it prefetched -- among them
    job-branding-facade.efinancialcareers.com/job/<id>, which is directly and cheaply
    fetchable on its own without pulling the ~200KB SPA shell at all."""
    b = _get("https://job-branding-facade.efinancialcareers.com/job/%s" % jid, tries=2, pause=4)
    try:
        d = json.loads(b) if b else None
    except ValueError:
        return ""
    return _text(((d or {}).get("data") or {}).get("description", ""))


def efinancialcareers(keywords, location="United States", tier=None, descs=True):
    """Finance-only. Parse the embedded JSON records, not the rendered HTML."""
    # %20 not '+': the search endpoint 500s on plus-encoded spaces.
    b = _get("https://www.efinancialcareers.com/search?"
             + urllib.parse.urlencode({"q": keywords, "location": location},
                                      quote_via=urllib.parse.quote))
    out = []
    for chunk in re.split(r'(?=\{"job_id":")', (b or b"").decode("utf-8", "replace"))[1:]:
        def f(k):
            m = re.search(r'"%s":"((?:[^"\\]|\\.)*)"' % k, chunk)
            return json.loads('"%s"' % m.group(1)) if m else ""
        jid = f("job_id")
        if not jid:
            continue
        out.append(job("efc:%s" % jid, "efc", None, _slug(f("company_name")), f("company_name"),
                       f("job_title"), f("job_location"),
                       "https://www.efinancialcareers.com" + f("destination_url"), tier=tier))
    if descs:
        for j in out:
            j["desc_text"] = efc_desc(j["uid"].split(":", 1)[1])
            time.sleep(1)          # same politeness floor as linkedin_desc
    return out


# ---------------------------------------------------------------- pipeline

def prefilter(j):
    """Reason string if this job should be dropped, else None."""
    hay = "%s %s" % (j.get("title", ""), j.get("desc_text", ""))
    return next((b for b, r in BLOCK_RE if r.search(hay)), None)


def store(jobs, dry=False, seen=None):
    """-> (new, duplicate, prefiltered)."""
    new = dup = filt = 0
    for j in jobs:
        why = prefilter(j)
        if why:
            filt += 1
            continue
        if dry:
            if j["dedup_hash"] in seen:
                dup += 1
            else:
                seen.add(j["dedup_hash"])
                new += 1
            continue
        if db.seen_dedup(j["dedup_hash"]):
            dup += 1
            continue
        if db.upsert_job(j):
            new += 1
            # Workday's account wall is per-EMPLOYER tenant: a tenant the user already has a
            # login for is automatable and stays queueable; one that would need a NEW account
            # is the human worklist. Same call adapters.plan() makes, so the two agree.
            if j["source"] == "wd" and not adapters.workday_account(j["company_slug"]):
                db.mark(j["uid"], "manual")
        else:
            dup += 1
    return new, dup, filt


def run(channels, limit=None, dry=False):
    reg = json.loads(REGISTRY.read_text())
    live = lambda k: [e for e in reg[k] if e.get("status") != "dead"]
    tally = {}
    seen = set()

    def do(ch, label, jobs):
        jobs = jobs[:limit] if limit else jobs
        n, d, f = store(jobs, dry, seen)
        t = tally.setdefault(ch, [0, 0, 0, 0])
        t[0] += len(jobs); t[1] += n; t[2] += d; t[3] += f
        print("  %-28s fetched=%-5d new=%-5d dup=%-5d filtered=%d" % (label, len(jobs), n, d, f))

    for ch, fn, key in (("gh", greenhouse, "greenhouse"), ("ashby", ashby, "ashby"),
                        ("lever", lever, "lever")):
        if ch in channels:
            print("[%s]" % ch)
            for e in live(key):
                do(ch, e["slug"], fn(e["slug"], e.get("name"), e.get("tier")))
    if "wd" in channels:
        print("[wd]")   # searchText per tenant: an empty search drowns you in branch/ops reqs
        for e in live("workday"):
            for q in reg["queries"]["workday"]:
                do("wd", "%s/%s" % (e["tenant"], q),
                   workday(e["tenant"], e["site"], e.get("wd", "wd5"), q,
                           e.get("name"), e.get("tier"), pages=2))
    if "li" in channels:
        print("[li]")
        for q, tier in reg["queries"]["linkedin"].items():
            do("li", q, linkedin_guest(q, tier=tier))
            time.sleep(3)  # research: 25 rapid requests -> 36% failures. Throttle.
    if "li_la" in channels:
        # Wide-industry, non-finance, local search. Past week, 3 pages a query.
        # PERSONALIZE: the location string below, and companies.json queries.linkedin_la.
        print("[li_la]")
        for q, tier in reg["queries"]["linkedin_la"].items():
            for start in (0, 10, 20):
                do("li_la", "%s @%d" % (q, start),
                   linkedin_guest(q, "Los Angeles, California, United States", start, tier,
                                  fresh="r604800"))
                time.sleep(3)
    if "efc" in channels:
        print("[efc]")
        for q, tier in reg["queries"]["efc"].items():
            do("efc", q, efinancialcareers(q, tier=tier))
            time.sleep(2)

    print("\n%-8s %8s %8s %8s %8s" % ("channel", "fetched", "new", "dup", "filtered"))
    tot = [0, 0, 0, 0]
    for ch, t in tally.items():
        print("%-8s %8d %8d %8d %8d" % (ch, *t))
        tot = [a + b for a, b in zip(tot, t)]
    print("%-8s %8d %8d %8d %8d%s" % ("TOTAL", *tot, "   (dry run, nothing stored)" if dry else ""))
    return tally


def demo():
    assert _text("&lt;p&gt;Hi &amp;amp; bye&lt;/p&gt;<script>x()</script>") == "Hi & bye"
    assert prefilter({"title": "Quant", "desc_text": "Must hold an active Security Clearance."}) \
        == "security clearance"
    assert prefilter({"title": "Quant Dev", "desc_text": "We are unable to sponsor visas"}) \
        == "unable to sponsor"
    assert prefilter({"title": "Quant", "desc_text": "We sponsor H-1B."}) is None
    assert prefilter({"title": "Quant", "desc_text": "US Citizens only"}) == "us citizen"
    assert prefilter({"title": "Quant", "desc_text": "protected veteran or military status"}) is None
    assert prefilter({"title": "Quant", "desc_text": "subject to  ITAR  rules"}) == "itar"

    card = ('<div class="base-card" data-entity-urn="urn:li:jobPosting:4000000001">'
            '<h3 class="base-search-card__title"> Quant Analyst </h3>'
            '<h4 class="base-search-card__subtitle"><a href="#">Jane Street</a></h4>'
            '<span class="job-search-card__location"> New York, NY </span>'
            '<time class="job-search-card__listdate" datetime="2026-08-22"></time>')
    (jid, blk), = _LI_CARD.findall(card)
    assert jid == "4000000001"
    assert "Jane Street" in blk

    # linkedin_desc(): the search card carries no description, so the detail page's markup
    # block is the ONLY source of the text every eligibility bar and finance signal reads.
    page = ('<section><div class="show-more-less-html__markup relative">'
            ' We are <b>unable to sponsor</b> visas.</div></section>')
    assert _text(_LI_DESC.search(page).group(1)) == "We are unable to sponsor visas."
    assert _LI_DESC.search("<section>no markup here</section>") is None

    class _R:  # efc: records carry nested objects, so parse fields, not the whole dict
        text = ('junk{"job_id":"10000001","destination_url":"/j.id1","company_name":"Acme",'
                '"work":{"a":1},"job_location":"New York","job_title":"Quant \\"Trader\\""}'
                '{"job_id":"999","job_title":"Other"}')
    chunks = re.split(r'(?=\{"job_id":")', _R.text)[1:]
    assert len(chunks) == 2

    # efc_desc(): every efc:* row had desc_text=NULL, 190/190, since the channel's first
    # commit -- the search response is metadata only, and the job PAGE is a client-rendered
    # Angular SPA with no server-rendered <title> and no JSON-LD JobPosting block (both
    # checked, absent). The description lives in the page's OWN hydration cache
    # (<script id="ng-state">), keyed by the internal facade API it prefetched -- which is
    # directly fetchable on its own, so efc_desc() never needs the ~200KB SPA shell at all.
    _facade = json.dumps({"data": {"description": "<p>Muni desk, <b>Python</b> only.</p>"}})
    assert _text(json.loads(_facade)["data"]["description"]) == "Muni desk, Python only."
    assert _text(json.loads('{"data":{}}').get("data", {}).get("description", "")) == ""
    g = lambda c, k: json.loads('"%s"' % re.search(r'"%s":"((?:[^"\\]|\\.)*)"' % k, c).group(1))
    assert g(chunks[0], "job_title") == 'Quant "Trader"'
    assert g(chunks[0], "company_name") == "Acme" and g(chunks[1], "job_id") == "999"

    # a keyword channel carrying a lower tier must NOT downgrade a tier-1 employer:
    # jobs.tier is what claim()'s draft-only gate reads.
    assert "janestreet" in tier1_slugs() and "aqr" in tier1_slugs()
    assert job("efc:1", "efc", None, "aqr", "AQR", "Research Analyst", "NY", "u",
               tier=3)["tier"] == 1
    assert job("li:1", "li", None, "someshop", "Some Shop", "Analyst", "NY", "u",
               tier=3)["tier"] == 3           # only tier-1 firms are pinned
    # ...and a tier-1 QUERY can't promote a non-finance employer (a healthcare employer leaked through once),
    # while a finance-sounding name keeps the query's tier 1.
    assert job("li:2", "li", None, "kaiserpermanente", "Kaiser Permanente", "Data Scientist",
               "Pasadena, CA", "u", tier=1)["tier"] == 3
    assert job("li:3", "li", None, "janushendersoninvestors", "Janus Henderson Investors",
               "Analyst", "NY", "u", tier=1)["tier"] == 1

    j = job("gh:1", "gh", "greenhouse", "janestreet", "Jane Street", "Quantitative Trader I",
            "New York, NY", "http://u")
    assert j["dedup_hash"] == db.dedup_hash("janestreet", "Quantitative Trader", "New York")
    assert j["apply_url"] == "http://u" and set(j) == set(db.JOB_COLS)

    reg = json.loads(REGISTRY.read_text())
    assert all(e.get("name") for k in ("greenhouse", "ashby", "lever") for e in reg[k]
               if e.get("status") != "dead")
    print("demo ok")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--channels", default="gh,ashby,lever")
    p.add_argument("--limit", type=int, help="max jobs kept per board/query")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--selftest", action="store_true")
    a = p.parse_args()
    if a.selftest:
        demo()
    else:
        db.init()
        run(set(a.channels.split(",")), a.limit, a.dry_run)
