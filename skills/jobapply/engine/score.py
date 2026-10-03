"""Deterministic role classifier + fit scorer.  stdlib only, no LLM, no network.

TWO TIERS, DIFFERENT JOBS -- this is the whole point of this file:

  company_tier  = jobs.tier  (INTEGER, set by discover.py from companies.json)
      A property of the FIRM: prestige / how much care an application deserves.
      It drives the draft-review gate only -- db.py claim() refuses to submit a
      company_tier-1 job (Jane Street, Point72, Jump, IMC, Optiver...) without an
      explicit human --approved.  score.py never writes it.
      NOTE: the column is still named `tier` because db.py's queue()/claim() read
      it by that name.  Renaming it is a db.py change, not a scorer change.

  role_tier     = jobs.role_tier  (INTEGER, set here)
      A property of the ROLE, which is what the user is actually applying to:
        1  quant research / quant trading / hedge fund / asset mgmt / IB / equity research
        2  quant dev / data science / ML eng / risk & strats / fintech
        3  broad analyst -- business, data, finance, strategy
        4  AI / agentic engineering
        0  none of the four.  Not scored, status='skipped'.
      "Jane Street | Accounting Coordinator" is company_tier 1, role_tier 0.
      "Affirm | Machine Learning Engineer" is company_tier 2, role_tier 2.

Scores are honest.  A new grad is a bad fit for "VP, 10+ years"
however well the keywords match, and this file says so.  Nothing here bends a
score toward the 100/day target; a thin day is a real signal about discovery.

  python3 score.py --bulk [--limit N] [--rescore]   score unscored jobs -> db.py score
  python3 score.py --dry-run [--limit N]            same, JSON to stdout, no writes
  python3 score.py --report [--limit N]             tier/score distribution + eyeball sample
  python3 score.py --title "..." [--desc "..."]     score one posting
  python3 score.py selftest
"""
import re, json, sqlite3, pathlib, argparse, subprocess, sys, collections
from db import defang   # Jane Street writes real titles in Lisu lookalikes

DB = pathlib.Path(__file__).resolve().parent / "jobapply.db"
MASTER = DB.parent / "docs/master.md"


def _rx(phrases):
    """One alternation, longest-first so 'quantitative researcher' beats 'research'."""
    return re.compile(r"(?<![a-z0-9])(?:%s)(?![a-z0-9])"
                      % "|".join(re.escape(p) for p in sorted(phrases, key=len, reverse=True)))


def _rx_cs(phrases):
    """Case-sensitive sibling of _rx() for matching against un-lowered text. _rx()'s boundary
    class is [a-z0-9] only, so run against mixed-case text it fails to exclude an uppercase
    neighbor -- "PA" false-matched inside "APAC" (preceding "A" is not in [a-z0-9], so the
    lookbehind was satisfied). Needed for _US_ABBR, which must stay case-sensitive (see there)."""
    return re.compile(r"(?<![A-Za-z0-9])(?:%s)(?![A-Za-z0-9])"
                      % "|".join(re.escape(p) for p in sorted(phrases, key=len, reverse=True)))


# ---------------------------------------------------------------- vocabulary
# The finance/quant vocabulary research flagged as missing: keyword-taxonomy.json
# only knows tech/biz/design/ops and would file "Equity Research Associate" under
# nothing at all.  Phrases, not words -- bare "strategist"/"research"/"financial"
# collide with Content Strategist / User Research / Financial Systems Admin.
FAMILY = {
    1: ["quantitative research", "quantitative researcher", "quant research", "quant researcher",
        "quantitative analyst", "quantitative trading", "quantitative trader", "quant trader",
        "quantitative strategist", "quantitative strategies", "quant strategist",
        "trader", "trading intern", "systematic trading", "systematic trader", "prop trading",
        "proprietary trading", "market maker", "market making", "delta one", "delta-one",
        "execution trader", "options trader", "volatility trader", "commodities trader",
        "portfolio manager", "portfolio management", "investment analyst", "research analyst",
        "equity research", "credit research", "fundamental research", "fundamental analyst",
        "buy side", "buy-side", "sell side", "sell-side", "hedge fund", "asset management",
        "investment management", "investment banking", "investment banker", "coverage banker",
        "mergers and acquisitions", "m&a", "leveraged finance", "equity capital markets",
        "debt capital markets", "capital markets", "restructuring", "private equity",
        "venture capital", "growth equity", "macro strategist", "macro research",
        "alpha research", "signal research", "systematic strategies", "long short",
        "long/short", "multi-strategy", "investment associate", "investment team",
        "macro analyst", "sales and trading", "sales & trading", "trading analyst",
        "trading associate", "equity analyst", "credit analyst", "investment research",
        # "member of research staff" -- Voleon Group's (systematic hedge fund) title for
        # quant researchers, 16 postings in the 2026-09-04 li backlog all role_tier 0.
        # Scoped to the 3-word phrase, not bare "research staff", so it doesn't catch
        # Deepgram's "Research Staff, Voice AI Foundations" (AI lab, not finance).
        "member of research staff"],
    2: ["quantitative developer", "quant developer", "quant dev", "quantitative engineer",
        "quantitative development", "quantitative technology", "trading systems",
        "trading technology", "trading infrastructure", "algorithmic trading", "low latency",
        "low-latency", "execution algo", "financial engineer", "financial engineering",
        "pricing quant", "derivatives quant", "model validation", "model risk",
        "quantitative risk", "market risk", "credit risk", "risk analyst", "risk management",
        "risk strat", "strats", "structuring", "structurer", "xva",
        # "risk strat"/"strats" don't match "Risk Strategist" -- the word-boundary regex
        # requires "strat" to end the word. Found live 2026-09-03 in the gh backlog: "Risk
        # Strategist", "Fraud Strategist" and "Fraud Operations Manager" all fell through to
        # role_tier 0 at Stripe, Affirm, SoFi, Mercury, Column, Binance, Imprint, Twilio,
        # Plaid, Adyen, Monzo -- fraud/risk strategy is squarely the "risk & strats" family
        # this tier already claims, it just never had its own vocabulary.
        "risk strategist", "risk strategy", "fraud strategist", "fraud strategy",
        # Same shape, found live 2026-09-04 in the third finance-roster batch: "credit
        # strategist"/"credit strategy" is nowhere in vocabulary, so "Senior Quantitative
        # Credit Strategist" (Vanguard -- "quantitative strategist" doesn't match with
        # "credit" inserted between the two words), "Credit Strategy Manager" (SoFi), and
        # "Credit Strategy Principal" (Wayflyer) all fell to role_tier 0. Real risk & strats
        # seats, same family as "risk strategist" above.
        "credit strategist", "credit strategy",
        "fraud operations", "fraud ops", "fraud investigator", "fraud investigations",
        "fraud prevention", "fraud specialist", "fraud risk", "fraud analysis",
        "fraud intelligence", "financial crimes",
        # Singular form of the phrase directly above -- "financial crimes" (plural) is a
        # substring match, not a stem, so it doesn't catch "financial crime" (singular).
        # Found live 2026-09-04, fourth finance-roster batch (KYC/AML-tech companies):
        # Monzo "Senior Financial Crime Investigator", Nium "Global Head of Financial Crime
        # Strategy", Vanguard "Head of Financial Crime Operations Execution" / "Financial
        # Crime Operations Specialist" all fell to role_tier 0 on this alone.
        "financial crime",
        # Second finance-roster expansion, 2026-09-04 (OKX, Bybit, Nubank, Payoneer, Thunes,
        # Forter, EBANX, Feedzai, Riskified, Pathstone, Bitpanda, Jumio, Qualia, ComplyAdvantage,
        # Slide Insurance...): "Risk Operations Lead"/"Risk Control Engineer"/"Conduct Risk
        # Specialist" fell through to role_tier 0 at Bybit, Nubank, Adyen, Airwallex, Binance,
        # Citi, Column, Kalshi, Melio, Stripe -- real risk & strats seats, just phrased with
        # "operations"/"control"/"conduct" instead of "management"/"analyst". Checked against
        # OpenAI's "User Safety & Risk Operations" postings (the one non-finance collision found
        # DB-wide): those already carry 5-8+ yr requirements that hold fit_score under 15, so the
        # role_tier reclass doesn't make them eligible.
        "risk operations", "risk control", "conduct risk",
        # Same risk-title-shape gap, found live 2026-09-04 in the fifth finance-roster batch:
        # bare "Risk Officer"/"Head of Risk" (no "credit"/"model"/"market" qualifier already in
        # vocabulary) fell to role_tier 0 at Adyen, Airwallex, AlphaSense, Binance, Citi (x3),
        # FalconX, Gusto, Imprint, Modern Treasury, Morgan Stanley (x2), Step, iCapital -- 15
        # rows, all finance/fintech risk leadership seats. Checked DB-wide for collisions: every
        # "risk officer"/"head of risk" title in the DB is at one of those firms; the one
        # non-finance-sounding one, Applied Intuition's "Head of Risk and Compliance", already
        # matched via bare "compliance" in FAMILY[3] before this change.
        "risk officer", "head of risk",
        "data scientist", "data science",
        "machine learning engineer", "machine learning scientist", "machine learning research",
        "ml engineer", "ml scientist", "applied scientist", "research engineer",
        "research scientist", "decision scientist", "quantitative modeler",
        "fintech", "payments engineer", "crypto", "digital assets", "blockchain",
        "defi", "treasury technology", "kdb", "machine learning", "ml researcher",
        "data engineer", "derivatives", "quantitative modeling", "risk analytics",
        "financial risk",
        # General software engineering. Added when targeting widened to any industry: SWE
        # titles were the single largest role_tier 0 bucket. Tier 2, not 4 -- tier 4 is the AI-eng family and drives
        # resume-variant selection; a plain backend seat should not get the AI variant.
        "software engineer", "software developer", "backend engineer", "frontend engineer",
        "full stack", "fullstack", "full-stack", "platform engineer", "systems engineer",
        "analytics engineer",
        # Language-prefixed dev/eng titles are the same SWE family, just with the
        # language in place of "software" -- same 2026-08-31 gap. Found live 2026-09-04
        # in the WorldQuant/iCapital backlog: "Python Developer", "Quantitative Python
        # Developer", "C++ Developer" fell to role_tier 0 at Tower Research, Citi,
        # WorldQuant, iCapital, Man Group, Qube, Optiver, Susquehanna -- real quant/trading
        # infra roles, just not phrased "software engineer". Same GENERIC_RX cap applies.
        "python developer", "python engineer", "c++ developer", "c++ engineer",
        "java developer", "java engineer"],
    3: ["business analyst", "data analyst", "financial analyst", "finance analyst",
        "strategy analyst", "strategic finance", "corporate finance", "corporate development",
        "corporate strategy", "fp&a", "financial planning and analysis",
        "financial planning & analysis", "business intelligence",
        "analytics", "analyst program", "rotational analyst", "consultant", "consulting",
        "strategy and operations", "strategy & operations", "business operations",
        "treasury analyst", "pricing analyst", "valuation",
        "commercial analyst", "reporting analyst", "insights analyst", "operations analyst",
        "product analyst", "growth analyst", "revenue analyst", "staff analyst",
        # Bare "analyst"/"associate" catch-all, same widening. Safe only because OUT_RX has
        # already removed the retail/sales/HR/legal/clinical long tail before we get here --
        # without that guard this would sweep in "Loss Prevention Analyst". Tier 1 still wins
        # ties (loop is strict >, ascending), so "Investment Banking Analyst" stays tier 1.
        "analyst", "associate", "underwriter", "underwriting", "actuarial", "actuary",
        "compliance analyst", "audit", "auditor", "accountant", "accounting",
        # Bare "compliance", 2026-09-04: "compliance analyst" alone missed "Compliance
        # Officer"/"Compliance Manager"/"Chief Compliance Officer"/"MLRO & Compliance Officer" --
        # role_tier 0 at Jane Street, Point72, IMC, Schonfeld, Flow Traders, Stripe, Coinbase,
        # Affirm, Robinhood, Betterment, Brex and the new OKX/Bybit/Nubank roster (335 titles
        # DB-wide). Safe as a bare word here (not FAMILY[1]/[2]) because OUT_RX's veto bypass
        # only checks tiers 1/2, so "Product Manager, Risk & Compliance" still correctly vetoes.
        "compliance",
        # Bare AML/KYC, same shape as bare "compliance" above. Found live 2026-09-04, fourth
        # finance-roster batch (Socure/TRM Labs/Elliptic/Quantexa/Trulioo -- KYC/AML-tech
        # companies): Robinhood "AML Investigator", Mercury "KYC Investigator" / "Senior KYC
        # Investigator", Strike "Senior AML Investigator", Quantexa "Head of AML & KYC --
        # APAC" all fell to role_tier 0 -- real financial-crimes-compliance titles, just
        # neither word alone was in vocabulary anywhere. Checked DB-wide for collisions first:
        # every "AML"/"KYC" title in the DB is finance/compliance work (no "AML" = acute
        # myeloid leukemia false hit from a healthcare posting, the obvious risk for a bare
        # 3-letter acronym) and both are word-bounded so they can't match inside "OCaml" etc.
        "aml", "kyc",
        # Bare "reconciliation", found live 2026-09-04 auditing the fifth finance-roster batch:
        # "Reconciliation Manager"/"Reconciliation Specialist"/"Reconciliations ... VP" fell to
        # role_tier 0 at Jump Trading, Stripe, Airwallex, Trust Sourcing Solutions, Mercury,
        # BlackRock, BNY -- 8 rows DB-wide. "Reconciliation Analyst"/"...Associate" shapes were
        # already caught by the bare analyst/associate catch-all above; this is the same
        # finance-ops family, just with Manager/Specialist/VP in the noun slot instead. Checked
        # DB-wide: every "reconcil*" title in the DB is finance/accounting-ops work.
        "reconciliation"],
    4: ["ai engineer", "ai engineering", "applied ai", "agentic", "agent engineer", "ai agent",
        "llm", "large language model", "generative ai", "genai", "gen ai", "foundation model",
        "prompt engineer", "ai research", "ai researcher", "ai scientist", "ai solutions",
        "ai product engineer", "forward deployed engineer", "nlp engineer", "rag",
        "retrieval augmented", "ai platform", "ai infrastructure", "model training",
        "post-training", "fine-tuning", "inference engineer", "ai safety", "alignment"],
}
FAM_RX = {t: _rx(p) for t, p in FAMILY.items()}

# TARGET="finance" mode: generic "AI Engineer" / "ML Engineer" / "Applied AI Engineer" / plain
# "Software Engineer" postings are NOT the finance vertical, even when they clear
# min_score. FAMILY[2]'s 2026-08-31 SWE/data-science widening and all of FAMILY[4] are
# both industry-agnostic: neither phrase says anything about the employer being a
# finance firm. Confirmed live -- big-tech
# "Software Engineer"/"Machine Learning Engineer"/"Data Scientist"/"Applied Scientist"
# postings were clearing min_score=40 on skill overlap alone (equity/python/risk/llm are
# common words, not finance evidence) with zero finance signal from either the company
# or the role. Gated in fit_score, not classify_role: role_tier stays honest (a plain
# Software Engineer req really is tier 2, an AI Engineer req really is tier 4) -- it's
# only the SCORE that should reflect "not the target vertical."
_GENERIC_2 = ["data scientist", "data science", "machine learning engineer",
              "machine learning scientist", "machine learning research", "ml engineer",
              "ml scientist", "applied scientist", "research engineer", "research scientist",
              "decision scientist", "machine learning", "ml researcher", "data engineer",
              "software engineer", "software developer", "backend engineer",
              "frontend engineer", "full stack", "fullstack", "full-stack",
              "platform engineer", "systems engineer", "analytics engineer",
              "python developer", "python engineer", "c++ developer", "c++ engineer",
              "java developer", "java engineer"]
GENERIC_RX = _rx(_GENERIC_2)
# Everything else in FAMILY[1]/FAMILY[2] is finance-specific by construction (quant,
# trading, risk, fraud, fintech...); a title hit there is corroboration enough on its own.
_STRONG_TITLE_RX = _rx(FAMILY[1] + [w for w in FAMILY[2] if w not in _GENERIC_2])

# ---------------------------------------------------- financial-industry priority
# In TARGET="finance" mode finance is the high-priority vertical. Two independent
# signals, weighted differently in fit_score() -- COMPANY sector matters more than
# ROLE function, because "FP&A at a tech company" is a worse fit there than "any role at
# a hedge fund". Company-name lookup reuses the already-verified quant-fund roster
# from companies.json (tier 1 there is 100% finance by construction); the boilerplate
# regex catches everyone else (banks, fintech, payments, insurance, asset managers)
# from their own "About us" sector language, the same technique discover.py's
# prefilter already relies on for negative signal.
_FINANCE_SECTOR_PHRASES = [
    "hedge fund", "asset management", "asset manager", "investment management",
    "investment bank", "investment banking", "broker-dealer", "broker dealer",
    "proprietary trading firm", "trading firm", "market maker", "market making firm",
    "private equity firm", "venture capital firm", "wealth management",
    "financial services company", "financial services firm", "fintech company",
    "fintech startup", "digital bank", "neobank", "payments company",
    "payments platform", "insurance company", "insurance carrier", "reinsurance",
    "credit union", "commercial bank", "retail bank", "investment firm",
    "quantitative trading firm", "quantitative investment", "capital markets firm",
    "securities firm", "brokerage firm", "family office",
]
FINANCE_SECTOR_RX = _rx(_FINANCE_SECTOR_PHRASES)
_FINANCE_ROLE_PHRASES = FAMILY[1] + [
    "financial analyst", "finance analyst", "strategic finance", "corporate finance",
    "fp&a", "financial planning and analysis", "treasury analyst", "treasury",
    "pricing analyst", "valuation", "investor relations",
]
FINANCE_ROLE_RX = _rx(sorted(set(_FINANCE_ROLE_PHRASES)))


def _finance_company_slugs():
    """Company names already verified as finance firms in companies.json's tier-1 roster."""
    try:
        reg = json.loads((pathlib.Path(__file__).parent / "companies.json").read_text())
    except Exception:
        return set()
    names = set()
    for platform in ("greenhouse", "ashby", "lever", "workday"):
        for c in reg.get(platform, []):
            if c.get("tier") == 1 and c.get("status") != "dead":
                names.add(c["name"].lower())
    return names


FINANCE_COMPANY_NAMES = _finance_company_slugs()


# Unambiguous finance words in a COMPANY NAME. FINANCE_COMPANY_NAMES only covers the
# companies.json tier-1 roster (gh/ashby/lever/wd boards), so a firm that only ever surfaces
# through a keyword channel was invisible to it: "Janus Henderson Investors" -- a $300B asset
# manager -- read as non-finance because its posting body never says "asset management" either.
# Kept to nouns that are finance-specific as a trailing firm-name token; "capital" and
# "partners" are deliberately absent, they are far too common in ordinary startup names.
_FINANCE_NAME_RX = _rx(["hedge fund", "asset management", "investment management",
                        "investments", "investors", "securities", "capital management",
                        "capital markets", "asset manager", "wealth management",
                        "financial group", "bancorp", "brokerage"])


def is_finance_company(company_name, desc_text):
    name = (company_name or "").lower().strip()
    if name in FINANCE_COMPANY_NAMES:
        return True
    if _hits(_FINANCE_NAME_RX, " " + name + " "):
        return True
    return bool(_hits(FINANCE_SECTOR_RX, (desc_text or "").lower()[:3000]))


_TITLE_FINANCE_WORD_RX = re.compile(r"(?<![a-z0-9])(financ(e|ial))(?![a-z0-9])")


def is_finance_role(title, desc_text):
    # A bare "finance"/"financial" is too generic to trust in a full description (that's the
    # same over-broad-substring mistake "export control" was), but safe as a standalone word
    # inside a TITLE specifically -- titles are short, and "Finance" as a distinct word in one
    # ("Data Science, Finance & Strategy", "VP Finance") is reliably a real signal.
    if _TITLE_FINANCE_WORD_RX.search((title or "").lower()):
        return True
    # TITLE only, for the same reason classify_role() demands a title hit: scanning the
    # description made every posting whose boilerplate said "backed by venture capital" or
    # "serving capital markets" a finance role. That mislabelled General Matter's "Applied AI
    # Engineer" and Fieldguide's "AI Engineer" -- confirmed live, 2026-08-27. A genuinely
    # finance-flavored role says so in its own title; the description only ever added noise.
    return bool(_hits(FINANCE_ROLE_RX, " " + (title or "").lower() + " "))


# Titles that are never one of the four families, whatever the description says.
# Cheap and it kills the long tail: 17k rows include baristas and key holders.
OUT_RX = _rx([
    "recruiter", "recruiting", "talent acquisition", "barista", "key holder", "retail",
    # Not full-time seats, or not the candidate's craft. Found live in a non-finance queue:
    # half the rows were retail "Seasonal Operations Associate (Part-Time)" store shifts, plus apparel "Technical Designer"/"3D Visualization" seats,
    # all reaching role_tier 3 on the bare "associate" catch-all.
    "part-time", "part time", "seasonal", "designer", "3d visualization",
    # Sales seats under another name ("ProSales Associate (Early Career Sales Program)",
    # "Business Associate (Sales)").
    # Not bare "sales": a Sales Analyst is analytical work and stays in.
    "prosales", "sales program", "(sales)", "sales representative", "sales development",
    "sales associate", "account executive", "account manager", "customer success",
    "customer care", "customer service",     # 2026-09-10 LA batch: Trellint call-center seat
    "customer support", "solutions architect", "sales engineer",
    # Same pre-sales/sales-engineering family as "solutions architect"/"sales engineer"
    # above, just a different job-title convention (common at SaaS/fintech). Found live
    # 2026-09-04 auditing the fourth finance-roster batch: Socure's "Pre-Sales Solution
    # Consultant, Big Tech/AI" ("partner with Sales", "sales cycle", "closed won" in the
    # JD despite title-level "risk"/"fraud"/"compliance" language) scored 46, eligible,
    # via the bare "consultant" catch-all with nothing to veto it. DB-wide sweep found the
    # identical shape scoring >=40 at Navan, Similarweb, Notion, Five9, Synthesia, Ramp,
    # Figma, Braze, Iterable, Veeva Systems, Tennr, Kong (24 rows) -- real sales-engineering
    # seats, not analyst/compliance work, regardless of which vertical's problems they demo.
    "solution consultant", "solutions consultant",
    "counsel", "paralegal",
    "attorney", "nurse", "physician", "technician", "driver", "warehouse", "janitor",
    "facilities", "security guard", "executive assistant", "administrative assistant",
    "office manager", "receptionist", "product manager", "product marketing",
    "product designer", "product design", "brand", "communications", "public relations",
    "copywriter", "content", "social media", "event", "people partner", "people operations",
    "human resources", "benefits", "payroll", "compensation", "workplace", "real estate",
    "procurement", "supply chain", "logistics", "manufacturing", "hardware engineer",
    "mechanical engineer", "electrical engineer", "site reliability", "network engineer",
    "data center", "datacenter", "field engineer", "teacher", "instructor", "curriculum",
    "chef", "cook", "server", "bartender", "housekeep", "shift supervisor", "store manager",
    # Added 2026-08-31 alongside the bare "analyst"/"associate" catch-all: without these the
    # catch-all promoted warehouse and sales-development seats into the LA queue. Seen live:
    # "Fulfillment Associate - Torrance, CA" and "Business Development Rep Associate".
    "fulfillment", "business development rep", "sales development rep", "bdr", "sdr",
    # Not a requisition at all -- an evergreen resume drop. Nothing downstream can submit it.
    "talent community", "talent network", "talent pool", "general application",
    # Bare "success" -- the literal phrase "customer success" above only catches the one
    # spelling. Found live 2026-09-04 auditing the fourth finance-roster batch: Lendbuzz
    # "Bilingual Dealership Success Associate" scored 40 (eligible) via the bare
    # "associate" catch-all with nothing to veto it. DB-wide sweep of the other 444
    # "*success*" titles found the same shape at Oscar Health ("Associate, Provider
    # Success", 68), Vestwell ("Partner Success Associate", 57), ethic.com ("Client
    # Success Associate", 51) -- Partner/Client/Provider/Member/Employee/Founder/Borrower/
    # Technical/Developer/GTM Success are all the same account-management/CS family with
    # a different noun in front. Checked all 445 DB-wide "*success*" titles first: zero
    # are a genuine quant/finance/AI role this would wrongly veto.
    "success",
])

# Hard stops.  Score 0, status='skipped'.  Phrased so "we happily sponsor visas"
# does NOT match -- every entry contains its own negation.
BLOCK_RX = _rx([
    "will not sponsor", "will not provide sponsorship", "do not sponsor", "does not sponsor",
    "unable to sponsor", "not able to sponsor", "cannot sponsor", "can not sponsor",
    "no visa sponsorship", "without sponsorship", "not offer sponsorship",
    "does not offer visa sponsorship", "not provide visa sponsorship",
    "sponsorship is not available", "not eligible for sponsorship",
    "ineligible for sponsorship", "no sponsorship available",
    "security clearance", "active clearance", "ts/sci", "top secret", "public trust clearance",
    "polygraph", "itar", "export control",
    "must be a u.s. citizen", "must be a us citizen", "must be a united states citizen",
    "u.s. citizenship is required", "us citizenship is required", "us citizenship required",
    "u.s. citizenship required", "u.s. citizens only", "us citizens only",
    "citizenship requirement", "green card holder", "must be a permanent resident",
])

# The sponsorship bar BLOCK_RX's bare phrases miss: a negation, then a gap, then the word
# "sponsorship" ("does not provide immigration-related sponsorship", "not eligible for
# employer-sponsored work authorization at any time").  Every branch carries its own
# negation, so "we are happy to sponsor visas" still cannot match.  Sampled 2026-08-25 over
# 36,200 described jobs: with BLOCK_RX it covers all 247 hits of the hand-checked sweep
# pattern and adds 3 the sweep missed -- zero false positives in either direction.
# Deliberately NOT in adapters.plan(): plan() routes on the URL and never reads desc_text.
NOSPONSOR_RX = re.compile(
    r"(?:un(?:able|willing)\s+to|do(?:es)?\s+not|will\s+not|cannot|can\s?not)\s+"
    r"(?:provide|offer|sponsor)\w*[^.;\n]{0,40}?sponsorship"
    r"|no\s+(?:visa|immigration|employment[- ]based)\s+sponsorships?"
    r"|not\s+eligible\s+for\s+employer[- ]sponsored"
    # BLOCK_RX already held the literal "not eligible for sponsorship", but employers almost
    # always slip a qualifier in the middle: "This position is not eligible for Visa
    # sponsorship." A literal phrase list cannot see that. Cost a full claim + tailor pass.
    r"|(?:not|no(?:t|n)?)\s+eligible\s+for\s+(?:\w+[- ]){0,3}sponsorship"
    r"|(?:position|role|opportunity|req\w*)\s+(?:is|are)\s+not\s+eligible[^.;\n]{0,40}?sponsor"
    r"|without\s+employer\s+sponsorship\s+now\s+and\s+in\s+the\s+future"
    # Cost a live form fill before the filler read the bar in the JD.
    r"|without\s+(?:current\s+or\s+future|present\s+or\s+future)\s+(?:employer\s+|visa\s+)?sponsorship"
    # The three commonest real-world phrasings, ALL missed at first -- each one cost a
    # full tailor pass. The old alternation only
    # caught "we cannot sponsor"-shaped sentences; employers far more often write the
    # requirement from the CANDIDATE's side ("you must be authorized without sponsorship"),
    # which never contains a verb the pattern above was looking for.
    r"|without\s+(?:the\s+)?need\s+for\s+(?:employer\s+|visa\s+)?sponsorship"
    r"|work\s+authorization\s+that\s+does\s+not[^.;\n]{0,60}?require\s+sponsorship"
    r"|(?:un(?:able|willing)|not\s+able)\s+to\s+consider[^.;\n]{0,60}?requiring[^.;\n]{0,30}?sponsorship"
    r"|authorized\s+to\s+work[^.;\n]{0,60}?on\s+a\s+permanent\s+basis"
    r"|require\s+sponsorship[^.;\n]{0,30}?(?:now\s+or|at\s+any\s+time)\s+in\s+the\s+future"
    # Two more, both found only because the TAILOR re-read the JD -- the scorer had passed
    # them and a claim + resume render was already spent on each.
    #   "Sponsorship for Work Authorization is not available for this posting."
    #     -- the bar is the SUBJECT of the sentence, so every verb-first alternation missed it.
    #   "does not intend to hire ... who will need, now or in the future, Acme sponsorship
    #     through the H-1B lottery" -- scored 46 and was even credited "new-grad: entry level",
    #     because the disqualifying clause contains the words that normally mean a good fit.
    r"|sponsorship\s+(?:for\s+[\w\s]{0,30})?is\s+not\s+(?:available|offered|provided)"
    r"|(?:do(?:es)?\s+not|will\s+not)\s+(?:intend\s+to\s+)?(?:hire|consider)[^.;\n]{0,80}?"
    r"(?:need|require)[^.;\n]{0,40}?sponsorship"
    r"|(?:do(?:es)?\s+not|will\s+not)\s+(?:provide|offer|support)[^.;\n]{0,60}?"
    r"(?:opt|stem\s+opt|cpt|f-?1)\b", re.I)


def _sentence(text, m, span=120):
    """The sentence around a match, so a rejection quotes its own evidence."""
    a = max(m.start() - span, text.rfind(".", 0, m.start()) + 1)
    b = text.find(".", m.end())
    b = m.end() + span if b < 0 else min(b + 1, m.end() + span)
    return re.sub(r"\s+", " ", text[a:b]).strip()


# Seniority.  PERSONALIZE: tuned for a new grad with ~2y of experience.
SENIOR_RX = _rx(["senior", "sr", "sr.", "staff", "principal", "lead", "head of", "director",
                 "vp", "svp", "evp", "avp", "vice president", "managing director", "chief",
                 "manager", "ii", "iii", "iv", "expert", "experienced"])
INTERN_RX = _rx(["intern", "internship", "co-op", "coop", "apprentice", "phd intern"])
NEWGRAD_RX = _rx(["new grad", "new graduate", "recent graduate", "recent grad", "entry level",
                  "entry-level", "campus", "university graduate", "graduate program",
                  "graduate programme", "rotational program", "analyst program", "class of 2027",
                  "class of 2026", "2027 start", "2027 full-time", "full-time 2027",
                  "december 2026", "master's degree", "masters degree", "mfe",
                  "financial engineering", "ms or phd", "master's or phd", "0-2 years",
                  "1-2 years", "1-3 years", "no prior experience", "early career",
                  "junior", "graduate", "associate program"])

# Skills with real evidence in docs/master.md.  selftest asserts every one of
# these literally appears there, so the list cannot silently drift from the resume.
# ("R" is deliberately absent: one letter, no boundary rule survives real JD text.)
# PERSONALIZE: skills on YOUR resume (HAVE) and ones you don't have (LACK).
HAVE_RX = _rx(["python", "sql", "matlab", "excel", "monte carlo", "valuation",
               "financial modeling", "machine learning", "regression", "statistics",
               "tableau", "llm", "rag", "multi-agent", "prompt engineering", "equity",
               "long/short", "arbitrage", "macro", "sentiment", "hedging", "clustering",
               "factor", "portfolio optimization", "dcf", "risk", "derivatives"])
# Demanded often, no evidence anywhere in the resume.  Each one is a real gap.
LACK_RX = _rx(["kdb", "q programming", "fpga", "verilog", "rust", "golang", "go lang",
               "scala", "java", "c#", ".net", "kubernetes", "terraform", "spark", "hadoop",
               "cuda", "pytorch", "tensorflow", "deep learning", "sas", "matlab",
               "distributed systems", "compiler", "kernel", "embedded", "react", "typescript",
               "salesforce", "sap", "workday", "tableau prep", "power bi", "cfa", "cpa",
               "series 7", "series 63", "actuarial", "solvency"])

_US = _rx(["united states", "usa", "u.s.", "us", "remote - us", "new york", "nyc",
           "san francisco", "boston", "chicago", "seattle", "austin", "dallas", "houston",
           "los angeles", "washington", "atlanta", "denver", "miami", "philadelphia",
           "san jose", "sunnyvale", "mountain view", "palo alto", "jersey city", "stamford",
           "charlotte", "minneapolis", "san diego", "portland", "phoenix", "nashville",
           "pittsburgh", "ann arbor", "salt lake city", "st. louis", "detroit", "baltimore"])
# Bare 2-letter state postal codes matched case-INsensitively collide with ordinary English
# words -- "or" (as in "London or remote"). Found live 2026-09-04, third finance-roster batch:
# "London or remote within the UK" (Griffin) got a false US signal from the word and scored 45,
# reaching the eligible bucket. A DB-wide sweep found 155 rows with this exact collision, 12
# already scoring >=40. Real postal codes in loc strings are written uppercase ("Austin, TX");
# matched case-sensitively against the untouched (non-lowered) text so "or" the word doesn't
# count but "OR" the state does (see _US_OR below, which narrows "OR" further still).
# Known remaining gap, NOT fixed here (low volume, ~10 rows DB-wide, needs per-country special
# casing rather than a vocabulary tweak): US postal codes that are ALSO another country's
# ISO code, uppercase in both -- "CA - Canada", "Bogota, CO" (Colombia), "Bogota D.C. / DC /
# Colombia" all still read as a US signal. Flagged for a dedicated sweep, not this pass.
_US_ABBR = _rx_cs(["CA", "NY", "TX", "MA", "IL", "WA", "NJ", "CT", "PA", "VA", "MD", "DC",
           "FL", "GA", "CO", "AZ", "NC", "OH", "MI", "MN", "UT", "WI", "MO", "TN"])
# "OR" is worse than its neighbors: it is also how postings join a plain city list with no US
# location anywhere ("Beijing OR Shanghai", "Geneva OR London OR Paris OR Zug" -- both live
# WorldQuant postings, 6 rows DB-wide, one scoring 69 and reaching the eligible bucket).
# Checked against every live example of each: the real Oregon abbreviation is always
# comma-prefixed ("Salem, OR", "Portland,OR"); the conjunction never is. So "OR" is matched
# only in that shape, not via the general _US_ABBR alternation above.
_US_OR = re.compile(r",\s*OR(?![A-Za-z0-9])")
_NONUS = _rx(["london", "uk", "united kingdom", "england", "scotland", "ireland", "dublin",
              "singapore", "hong kong", "tokyo", "japan", "sydney", "melbourne", "australia",
              "toronto", "vancouver", "montreal", "canada", "ontario", "paris", "france",
              "amsterdam", "netherlands", "india", "bengaluru", "bangalore", "mumbai", "pune",
              "hyderabad", "gurgaon", "shanghai", "beijing", "shenzhen", "china", "taipei",
              "taiwan", "seoul", "korea", "germany", "berlin", "munich", "frankfurt",
              "zurich", "geneva", "switzerland", "milan", "italy", "spain", "madrid",
              "barcelona", "poland", "warsaw", "krakow", "brazil", "sao paulo", "curitiba",
              "mexico",
              "israel", "tel aviv", "dubai", "abu dhabi", "uae", "emea", "apac", "latam",
              "europe", "asia", "stockholm", "sweden", "denmark", "copenhagen", "oslo",
              "norway", "finland", "helsinki", "belgium", "brussels", "vienna", "austria",
              "lisbon", "portugal", "bucharest", "romania", "prague", "czech", "budapest",
              "hungary", "manila", "philippines", "kuala lumpur", "malaysia", "bangkok",
              "thailand", "vietnam", "jakarta", "indonesia", "auckland", "new zealand",
              "south africa", "nigeria", "kenya", "cairo", "istanbul", "turkey", "turkiye",
              "latin america",  # found live 2026-09-02: Sezzle posts loc="Latin America" --
              # the list already had the abbreviation "latam" but not this spelled-out form,
              # so 3 LATAM-based roles scored as if they were US and reached the tailor queue.
              "buenos aires", "argentina", "chile", "bogota", "colombia", "lima",
              "costa rica", "san jose, costa rica",
              # Found live 2026-09-04 in the OKX/Bybit crypto-exchange batch: "Pakistan
              # (Remote)" and "El Salvador (Remote)" matched neither _US nor _NONUS, so the
              # gate never fired at all (silent pass-through, not a wrong verdict). Both
              # postings happened to score under 40 anyway on unrelated penalties, but the
              # gate itself was blind to them. _NONUS still has a long tail of un-listed
              # countries/cities (Qatar, Kazakhstan, Serbia, Malta, Luxembourg,
              # Bermuda, Vietnam cities, India cities beyond the metro-tech ones already
              # here...) -- out of scope for this pass, flagged for a dedicated sweep.
              "pakistan", "el salvador",
              # Found live 2026-09-04, third finance-roster batch (Tamara=UAE/Saudi, Zilch=UK,
              # GoCardless=UK): "Riyadh, Saudi Arabia" and "Zilch Lithuania" matched neither
              # _US nor _NONUS and slipped through at scores 54/48/49 (eligible); "Riga,
              # Latvia" (GoCardless) scored 43, also eligible.
              "saudi arabia", "riyadh", "lithuania", "latvia", "riga",
              # Found live 2026-09-04, fourth finance-roster batch (KYC/AML-tech companies:
              # Veriff is Estonia-HQ'd, BVNK lists Malta as an office): "Veriff | Tallinn"
              # (2 rows) matched neither _US nor _NONUS and scored 45, eligible; DB-wide sweep
              # found the same gap already live at Twilio ("Remote - Estonia", scored 57/25,
              # both eligible) -- a real vocabulary gap, not a one-off. Malta added alongside
              # it (BVNK's Cape Town/Malta office string) though no row is currently miscaught
              # by it alone -- every live example also carries another already-listed keyword.
              "estonia", "tallinn", "malta",
              # Found live 2026-09-04, fifth finance-roster batch (dLocal is Montevideo-HQ'd
              # with LatAm/Africa offices; WorldQuant already had 8 unrelated Montevideo rows
              # sitting in the DB, unrelated to this batch, 3 of them scoring 60-69 eligible):
              # bare "Montevideo" (loc="Montevideo (Hybrid)"), "Yaoundé" (Cameroon MLRO role,
              # title literally says "- Cameroon"), and "Dakar" (Senegal MLRO role, title says
              # "- Senegal") all matched neither _US nor _NONUS. 4 rows scored >=40 (eligible)
              # across dLocal + WorldQuant before this fix. Also added while sweeping the same
              # batch's loc strings: "Belfast" (DailyPay, scored 43 eligible), "Lagos" (Nigeria
              # is already listed but the bare city wasn't), "Cape Town" and "Milton Keynes"
              # (both already scored 0 on unrelated grounds, gate was blind regardless).
              "montevideo", "uruguay", "yaoundé", "yaounde", "cameroon", "dakar", "senegal",
              "lagos", "belfast", "cape town", "milton keynes"])

_YEARS = re.compile(r"(\d{1,2})\s*(?:\+|-|–|to)?\s*(?:\d{1,2})?\s*\+?\s*(?:years?|yrs?)"
                    r"[^.\n]{0,40}?experience")


def _hits(rx, text):
    return set(rx.findall(text))


def _min_years(text):
    """Lowest stated years-of-experience requirement, or None.  The lenient reading."""
    v = [int(m) for m in _YEARS.findall(text) if 0 < int(m) <= 20]
    return min(v) if v else None


def classify_role(title, desc=""):
    """-> (role_tier 0-4, reason).  A TITLE hit is required: a description that merely
    mentions trading does not make an Accounting Coordinator a quant role."""
    # gh:8596771002 is "\ua4dfachine \ua4e1earning \ua4e3esearcher": without defang() every
    # regex here misses it and a real Jane Street ML role scores 0 / role_tier 0.
    t = " " + defang(title or "").lower() + " "
    out = _hits(OUT_RX, t)
    if out and not (_hits(FAM_RX[1], t) or _hits(FAM_RX[2], t)):
        return 0, "not one of the four families: %s" % sorted(out)[0]
    d = (desc or "").lower()[:6000]
    best_t, best_w, best_why = 0, 0, ""
    for tier in (1, 2, 3, 4):          # strict > below, so a tie goes to the lower tier
        th, dh = _hits(FAM_RX[tier], t), _hits(FAM_RX[tier], d)
        if not th:
            continue
        w = 5 * len(th) + min(len(dh), 3)
        if w > best_w:
            best_t, best_w, best_why = tier, w, "title: " + ", ".join(sorted(th)[:3])
    if not best_t:
        return 0, "no quant/finance/AI role keyword in title"
    return best_t, best_why


# ------------------------------------------------------------------ targeting
# PERSONALIZE: TARGET picks the strategy. "la_general" = non-finance roles located in a
# LA_LOCAL_RX city (swap that list for YOUR metro); "finance" = finance-first, anywhere in the
# US. Run `python3 score.py selftest` after any change here.
# fit_score() is the only reader; "finance" re-enables the GENERIC_RX cap in fit_score.
TARGET = "la_general"
LA_LOCAL_RX = _rx(["los angeles", "santa monica", "culver city", "pasadena", "burbank",
                   "glendale", "long beach", "el segundo", "torrance", "playa vista", "venice",
                   "west hollywood", "beverly hills", "century city", "marina del rey",
                   "inglewood", "hawthorne", "manhattan beach", "redondo beach", "van nuys",
                   "sherman oaks", "encino", "woodland hills", "calabasas", "studio city",
                   "north hollywood", "downey", "carson", "gardena", "cerritos",
                   "santa clarita", "valencia"])
# is_finance_company()/is_finance_role() are tuned to AWARD a bonus, so they stay narrow on
# purpose. Excluding is the opposite trade: a bank slipping into a non-finance queue is the
# costlier miss. Found live 2026-09-10: City National Bank, BMO, DoubleLine, CIM Group,
# Enduring Ventures and Affirm all read as non-finance. "trust" stays out (Trust & Safety).
_FIN_EXTRA_RX = _rx(["bank", "banking", "bancorp", "bmo", "capital", "credit", "credit union",
                     "financial", "finance", "invest", "investment", "investments", "ventures",
                     "asset", "assets", "securities", "equity", "private equity", "fund", "funds",
                     "wealth", "insurance", "lending", "loan", "loans", "mortgage", "portfolio",
                     "foreign exchange", "fx", "trading", "trader", "underwriting", "underwriter",
                     "audit", "auditor", "actuarial", "actuary", "tax", "accounting",
                     "accountant", "controller", "treasury", "fp&a", "m&a", "doubleline",
                     "cim group", "affirm", "block", "wilshire", "royalty", "royalties",
                     "budget"])   # USC "Budget Business Analyst", 2026-09-10


# Description phrases that mark the EMPLOYER as finance/accounting, for exclusion only.
# 2026-09-10 LA LinkedIn batch: SentiLink ("Forbes Fintech 50"), Crowe ("our audit, tax, and
# consulting groups") both passed is_finance_company(); so did Walker & Dunlop ("one of the
# largest providers of capital to the commercial real estate industry"). The whole description is
# scanned, not the first 4000 chars: "about us" boilerplate often comes last in a long JD. Not
# "venture capital firm": startup boilerplate says "backed by a leading venture capital firm",
# and that would drop the LA startups this filter exists to find.
_FIN_DESC_RX = _rx(["fintech", "audit, tax", "financial institutions", "lenders", "credit unions",
                    "provider of capital", "providers of capital"])


def is_finance(job):
    """Any finance signal at all: la_general's exclusion, and docs.stage_one()'s variant pick."""
    name, title, desc = job.get("company_name") or "", job.get("title") or "", job.get("desc_text")
    return bool(is_finance_company(name, desc) or is_finance_role(title, desc)
                or _hits(_FIN_EXTRA_RX, " %s %s " % (defang(name).lower(), defang(title).lower()))
                or _hits(_FIN_DESC_RX, (desc or "").lower()))


def fit_score(job, profile=None):
    """-> (score 0-100, reason).  Evidence in the posting only.  profile is accepted for
    call-site symmetry with the rest of the system; the resume evidence is HAVE_RX/LACK_RX,
    which selftest pins to docs/master.md."""
    title = " " + defang(job.get("title") or "").lower() + " "
    desc = job.get("desc_text") or ""
    text = title + "\n" + desc.lower()[:12000]

    b = _hits(BLOCK_RX, text)
    if b:
        return 0, "disqualified: %s" % sorted(b)[0]

    # Two eligibility bars that cost a full form fill each if they get past here.  Both are
    # cheap now and expensive live, and neither is visible to adapters.plan(), which routes on
    # the URL alone.  Un-truncated on purpose: this boilerplate is usually the last paragraph.
    m = NOSPONSOR_RX.search(desc)
    if m:
        return 0, 'bars sponsorship: "%s"' % _sentence(desc, m)

    # F-1/OPT is US-only, so a non-US posting is unapplyable however well it scores -- these
    # used to lose 30 points, survive min_score, and then park in needs_human as a fake
    # "work authorization" blocker.  jobs.loc ONLY: a region in the TITLE ("Analyst, EMEA")
    # is not evidence of where the desk is, and a false positive here discards a real job.
    # defang() strips diacritics too (not just the Lisu-confusable fold it was added for):
    # "Turkiye" only matches ascii "turkiye" -- the raw loc field is often "Türkiye, Remote".
    l_raw = defang(job.get("loc") or "")
    l = l_raw.lower()
    nonus = _hits(_NONUS, l)
    is_us = _hits(_US, l) or _hits(_US_ABBR, l_raw) or _US_OR.search(l_raw)
    if nonus and not is_us:
        return 0, "non-US location (%s): %r" % (sorted(nonus)[0], (job.get("loc") or "").strip()[:80])

    s, why = 40, []

    yrs = _min_years(text)
    senior = _hits(SENIOR_RX, title)
    if senior:
        s -= 35
        why.append("senior title (%s)" % sorted(senior)[0])
    if _hits(INTERN_RX, title):
        s -= 25
        why.append("internship, not a full-time seat")
    if yrs is not None:
        if yrs >= 8:
            s -= 40; why.append("%d+ yrs required" % yrs)
        elif yrs >= 5:
            s -= 30; why.append("%d+ yrs required" % yrs)
        elif yrs >= 3:
            s -= 15; why.append("%d+ yrs required" % yrs)
        else:
            s += 5; why.append("%d yrs ok" % yrs)

    ng = _hits(NEWGRAD_RX, text)
    if ng:
        s += min(len(ng), 4) * 6
        why.append("new-grad: " + ", ".join(sorted(ng)[:2]))

    have = _hits(HAVE_RX, text)
    s += min(len(have), 10) * 3
    if have:
        why.append("has " + ", ".join(sorted(have)[:3]))

    lack = _hits(LACK_RX, text)
    s -= min(len(lack), 5) * 3
    if lack:
        why.append("lacks " + ", ".join(sorted(lack)[:2]))

    if TARGET == "la_general":
        if is_finance(job):
            s = min(s, 25)
            why.append("finance employer/role -> capped (target: LA non-finance)")
        if not _hits(LA_LOCAL_RX, l) or "remote" in l or "remote" in title:  # BairesDev "... - Remote Work", loc LA
            s = min(s, 25)
            why.append("not LA-local -> capped (target: LA non-finance)")
        return max(0, min(100, int(round(s)))), "; ".join(why) or "no signal either way"

    # In TARGET="finance" mode the financial industry is the high-priority vertical.
    # Company-in-finance is the bigger signal; a finance-flavored role at a
    # non-finance company (e.g. "FP&A Analyst" at an AI lab) still counts, smaller.
    fin_co = is_finance_company(job.get("company_name"), desc)
    fin_role = is_finance_role(job.get("title"), desc)
    if fin_co:
        s += 20
        why.append("finance employer")
    elif fin_role:
        s += 8
        why.append("finance-flavored role")

    # Finance-only: a title that only matched via the generic SWE/data-science/AI-eng
    # vocabulary, with no finance signal anywhere (company, sector boilerplate, or a
    # finance-specific title word), is not the target vertical -- cap it below
    # min_score (40) however good the skill overlap looks. Does not touch role_tier: the
    # posting is still honestly tier 2/4, it just should not queue.
    if not fin_co and not fin_role and not _hits(_STRONG_TITLE_RX, title) and \
            (_hits(GENERIC_RX, title) or _hits(FAM_RX[4], title)):
        s = min(s, 25)
        why.append("generic SWE/AI/data title, no finance signal -> capped")

    return max(0, min(100, int(round(s)))), "; ".join(why) or "no signal either way"


def score_job(job):
    """-> the JSON row db.py score consumes.  role_tier 0 never gets a fit score."""
    rt, rwhy = classify_role(job.get("title"), job.get("desc_text"))
    if rt == 0:
        return {"uid": job["uid"], "role_tier": 0, "score": 0,
                "score_reason": rwhy[:120], "status": "skipped"}
    sc, swhy = fit_score(job)
    return {"uid": job["uid"], "role_tier": rt, "score": sc,
            "score_reason": ("t%d %s | %s" % (rt, rwhy, swhy))[:200],
            "status": "skipped" if sc == 0 else "scored"}


# ------------------------------------------------------------------ db plumbing
def _conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def _ensure_columns():
    """Additive only.  jobs.tier (the company tier) is left exactly as discover.py wrote it."""
    with _conn() as c:
        if "role_tier" not in {r["name"] for r in c.execute("PRAGMA table_info(jobs)")}:
            c.execute("ALTER TABLE jobs ADD COLUMN role_tier INTEGER")


def rows(limit=None, rescore=False):
    q = "SELECT uid,title,desc_text,loc,tier,company_name FROM jobs"
    if not rescore:
        q += " WHERE score IS NULL"
    if limit:
        q += " LIMIT %d" % int(limit)
    with _conn() as c:
        for r in c.execute(q):
            yield dict(r)


def bulk(limit=None, rescore=False, dry=False):
    """Score unscored jobs, then write in two places for one reason:

      score + score_reason -> `python3 db.py score` (JSON list on stdin), the owned write path
      role_tier            -> straight UPDATE here

    db.py's set_score(uid, score, tier, reason) has no role_tier slot, and its `tier`
    argument is the COMPANY tier -- the one claim()'s draft gate and queue()'s TIER_MIX
    read. Sending role_tier through it would silently reclassify Jane Street as a
    non-tier-1 firm and auto-submit to it. So role_tier gets its own column.
    """
    _ensure_columns()
    out = [score_job(j) for j in rows(limit, rescore)]
    n = collections.Counter()
    for r in out:
        n["role%d" % r["role_tier"]] += 1
        n[r["status"]] += 1
    payload = json.dumps([{"uid": r["uid"], "score": r["score"], "reason": r["score_reason"]}
                          for r in out])
    if dry:
        print(payload)
    else:
        with _conn() as c:
            c.executemany("UPDATE jobs SET role_tier=? WHERE uid=?",
                          [(r["role_tier"], r["uid"]) for r in out])
        if subprocess.run([sys.executable, str(DB.parent / "db.py"), "score"],
                          input=payload, text=True).returncode:
            print("db.py score failed -- score/score_reason NOT written", file=sys.stderr)
            return 1
    print(json.dumps(dict(n), sort_keys=True), file=sys.stderr)
    return 0


def report(limit=None):
    """Distribution + the eyeball check: does the top of the list look right?"""
    scored, buckets = [], collections.Counter()
    for j in rows(limit, rescore=True):
        r = score_job(j)
        buckets["role_tier %d" % r["role_tier"]] += 1
        if r["role_tier"]:
            buckets["score %s" % ("0" if r["score"] == 0 else
                                  "1-39" if r["score"] < 40 else
                                  "40-59" if r["score"] < 60 else
                                  "60-79" if r["score"] < 80 else "80+")] += 1
            scored.append((r["score"], r["role_tier"], j["tier"], j["title"], r["score_reason"]))
    tot = sum(v for k, v in buckets.items() if k.startswith("role_tier"))
    print("scored %d jobs\n" % tot)
    for k in sorted(buckets):
        print("  %-14s %6d  %5.1f%%" % (k, buckets[k], 100.0 * buckets[k] / tot))
    scored.sort(reverse=True)
    print("\nTOP 10")
    for s, rt, ct, t, w in scored[:10]:
        print("  %3d  role%d/co%s  %-52.52s  %s" % (s, rt, ct, t, w[:70]))
    print("\nBOTTOM 10 (in-scope roles only)")
    for s, rt, ct, t, w in [x for x in scored if x[0] > 0][-10:]:
        print("  %3d  role%d/co%s  %-52.52s  %s" % (s, rt, ct, t, w[:70]))
    return 0


def selftest():
    global TARGET
    saved, TARGET = TARGET, "finance"      # every pinned case in the body predates la_general
    try:
        _selftest_finance()
    finally:
        TARGET = saved
    # la_general (2026-09-10): LA-local non-finance clears min_score=40; finance or non-LA
    # cannot, and the finance-only generic-title cap no longer applies.
    la = {"title": "Data Analyst", "company_name": "Riot Games", "loc": "Los Angeles, CA",
          "desc_text": "Python, SQL, Tableau. 1 year of experience."}
    swe = {"title": "Software Engineer", "company_name": "Tubi", "loc": "Santa Monica, CA",
           "desc_text": "Python, statistics, machine learning. 2 years experience."}
    TARGET = "la_general"
    try:
        assert fit_score(la)[0] >= 40, fit_score(la)
        assert fit_score(swe)[0] >= 40, fit_score(swe)
        for bad in (dict(la, loc="New York, NY"), dict(la, loc="Los Angeles, CA (Remote)"),
                    dict(la, company_name="City National Bank"),
                    dict(la, company_name="BMO U.S."), dict(la, title="Credit Analyst")):
            assert fit_score(bad)[0] < 40, (bad, fit_score(bad))
        assert not is_finance(dict(la, title="Trust & Safety Analyst"))
        # the first live LA queue's leaks (2026-09-10)
        assert fit_score(dict(la, company_name="Wilshire", title="Analyst, Trade Operations"))[0] < 40
        assert classify_role("Seasonal Operations Associate (Part-Time) - Santa Monica")[0] == 0
        assert classify_role("Associate Technical Designer - Intimates & Swim")[0] == 0
        # the LA LinkedIn batch's leaks (same day)
        assert fit_score(dict(la, title="CRM Analyst - Reactivations - Remote Work"))[0] < 40
        assert classify_role("ProSales Associate (Early Career Sales Program)")[0] == 0
        assert classify_role("Corrugated Business Associate (Sales)")[0] == 0
        assert classify_role("Sales Analyst")[0] == 3          # analytical sales work stays in
        assert is_finance(dict(la, company_name="SentiLink",
                               desc_text="Named to the Forbes Fintech 50."))
    finally:
        TARGET = saved
    print("selftest ok (finance + la_general)")
    return 0


def _selftest_finance():
    # the vocabulary is pinned to the real resume, so it cannot drift from it
    m = MASTER.read_text().lower()
    for skill in ["python", "sql", "matlab", "excel", "monte carlo", "valuation",
                  "financial modeling", "machine learning", "regression", "statistics",
                  "tableau", "llm", "rag", "multi-agent", "prompt engineering", "equity",
                  "long/short", "arbitrage", "macro", "sentiment", "hedging", "clustering",
                  "factor", "portfolio optimization", "dcf", "risk"]:
        assert skill in m, "HAVE_RX claims %r but it is not in docs/master.md" % skill

    # DEFECT 4: same company, same company_tier, different role families
    assert classify_role("Quantitative Researcher")[0] == 1
    # Was pinned to 0. Since the 2026-08-31 widening "accounting" is a tier-3 title word, so
    # this is now a legitimate broad-analyst seat. The invariant that actually mattered is
    # unchanged and still pinned: a description that merely says "trading floor" must NOT
    # make it a QUANT role.
    assert classify_role("Accounting Coordinator", "audit, credit card, trading floor")[0] == 3
    assert classify_role("Campus Recruiter")[0] == 0
    assert classify_role("Quantitative Developer | Trading Team")[0] == 2
    assert classify_role("Data Analyst II")[0] == 3
    assert classify_role("Applied AI Engineer, Startups")[0] == 4
    assert classify_role("Equity Research Associate")[0] == 1
    assert classify_role("Shift Supervisor - Lead Barista")[0] == 0
    # Also flipped by the widening: generic SWE is now tier 2. Tier 2 and not 4 on purpose --
    # tier 4 is the AI-eng family and picks the AI resume variant.
    assert classify_role("Senior Software Engineer- Networking")[0] == 2

    # OUT_RX must not veto a title carrying a tier-1/2 phrase: "Consumer & Retail" is the
    # coverage group, not the job. Found live in the LA pool, 2026-08-31.
    assert classify_role("Investment Banking - Consumer & Retail - Analyst")[0] == 1
    # ...but a title with no such phrase is still vetoed by the same OUT word.
    assert classify_role("Retail Sales Associate, East Pasadena")[0] == 0
    assert classify_role("Retail Key Holder, West Los Angeles")[0] == 0
    assert classify_role("Executive Assistant to the CFO")[0] == 0

    # "Financial Planning & Analysis" (ampersand) never matched -- only the spelled-out
    # "financial planning and analysis" was in FAMILY[3]. Found live 2026-09-04: 14 of 29
    # "Financial Planning & Analysis" titles in the gh/wd backlog fell to role_tier 0
    # whenever the title had no separate "analyst"/"associate" word to catch them, e.g.
    # "Senior Manager, Financial Planning & Analysis", "Director, Financial Planning &
    # Analysis" at MongoDB, Spotify, Cohere, United Talent Agency, Nielsen, Robert Half.
    assert classify_role("Senior Manager, Financial Planning & Analysis")[0] == 3

    # seniority beats keyword density: this is the honest-scoring rule
    jr = {"uid": "x", "title": "Quantitative Research Analyst, 2027 Campus Program",
          "desc_text": "New grad. Python, SQL, Monte Carlo, valuation. 0-2 years experience.",
          "loc": "New York, NY"}
    sr = dict(jr, title="Vice President, Quantitative Research",
              desc_text="10+ years of experience. Python, SQL, Monte Carlo, valuation.")
    assert fit_score(jr)[0] > 65, fit_score(jr)
    assert fit_score(sr)[0] < 30, fit_score(sr)
    assert fit_score(jr)[0] > fit_score(sr)[0]

    # hard stops are hard
    for bad in ["Requires an active security clearance.", "We will not sponsor visas.",
                "Applicants must be a US citizen."]:
        assert fit_score(dict(jr, desc_text=bad))[0] == 0, bad
    # ... and a firm that DOES sponsor is not caught by them
    assert fit_score(dict(jr, desc_text="We are happy to sponsor visas."))[0] > 0

    # ---- the two fill-stage leaks (docs/learnings.md 2026-08-25), caught here, not live ----
    # 1. a JD that bars sponsorship-requiring candidates, verbatim from AlixPartners
    alix = dict(jr, desc_text="IMPORTANT: This role is not eligible for employer-sponsored "
                "work authorization at any time. This includes candidates who are currently "
                "authorized to work on a temporary basis (e.g. H-1B visa, STEM OPT, CPT, TN, "
                "F-1, J-1, etc.) but will require sponsorship at a later date.")
    assert fit_score(alix)[0] == 0, fit_score(alix)
    assert "not eligible for employer-sponsored" in fit_score(alix)[1], fit_score(alix)[1]
    assert score_job(dict(alix, uid="a"))["status"] == "skipped"
    for bad in ["We are unable to provide visa sponsorship.",
                "Guidepoint does not provide employment-based visa sponsorship for this role",
                "JPMorganChase does not offer employment-based immigration sponsorship "
                "(including OPT/CPT) for this program.",
                "No visa sponsorships available for this position now or in the future."]:
        assert fit_score(dict(jr, desc_text=bad))[0] == 0, bad
    # ... and a firm that DOES sponsor still survives both the phrase list and the regex
    assert fit_score(dict(jr, desc_text="We are happy to provide visa sponsorship."))[0] > 0
    assert fit_score(dict(jr, desc_text="Sponsorship is available for this role."))[0] > 0

    # 2. non-US loc is a hard stop for an F-1/OPT candidate, not a score penalty
    # "Latin America" and "T\u00fcrkiye, Remote" added 2026-09-02: found live on 4 Sezzle
    # postings that scored as US roles and reached the tailor queue. The list already had the
    # abbreviation "latam" and the ascii "turkey", but not the spelled-out or accented forms.
    for bad in ["London", "Hong Kong", "S\u00e3o Paulo, Brazil", "Sydney, Australia",
                "Bochum, Germany", "Taipei, Taiwan", "Melbourne", "Latin America",
                "T\u00fcrkiye, Remote",
                # Found live 2026-09-04: "Pakistan (Remote)" and "El Salvador (Remote)" on
                # OKX compliance postings matched neither _US nor _NONUS -- the gate never
                # fired, it just silently let them through.
                "Pakistan (Remote)", "El Salvador (Remote)",
                # Found live 2026-09-04, third finance-roster batch: missing vocab (Tamara,
                # Zilch, GoCardless) and, separately, "London or remote within the UK"
                # (Griffin) which scored 45 and reached the eligible bucket because bare "or"
                # matched the Oregon abbreviation case-insensitively -- lowercase "or"/"ca"
                # collide with plain English ("remote or ...") and other countries' codes
                # ("CA-Toronto" meaning Canada). Both now require the real US postal code to
                # be uppercase in the original text.
                "Riyadh, Saudi Arabia", "Zilch Lithuania", "Riga, Latvia",
                "London or remote within the UK",
                # "OR" is worse than the rest: also how postings join a plain city list with
                # no US location anywhere. Real WorldQuant postings, live 2026-09-04.
                "Beijing OR Shanghai", "Geneva OR London OR Paris OR Zug"]:
        assert fit_score(dict(jr, loc=bad))[0] == 0, bad
        assert repr(bad)[1:-1] in fit_score(dict(jr, loc=bad))[1], fit_score(dict(jr, loc=bad))
    assert score_job(dict(jr, uid="b", loc="London"))["status"] == "skipped"
    # a location naming BOTH keeps the benefit of the doubt; a region in the TITLE is not a loc
    assert fit_score(dict(jr, loc="New York; London"))[0] > 0
    assert fit_score(dict(jr, title="Quantitative Analyst, EMEA Coverage", loc=""))[0] > 0
    # ...and the real Oregon/California abbreviations must still pass as US
    assert fit_score(dict(jr, loc="Portland, OR"))[0] == fit_score(jr)[0]
    assert fit_score(dict(jr, loc="San Francisco, CA"))[0] == fit_score(jr)[0]

    # 3. an ordinary US job with an ordinary description is untouched by both
    assert fit_score(jr)[0] > 65, fit_score(jr)
    assert fit_score(dict(jr, loc="Remote - US"))[0] == fit_score(jr)[0]
    assert fit_score(dict(jr, loc="Chicago, IL"))[0] == fit_score(jr)[0]
    assert score_job(dict(jr, uid="c"))["status"] == "scored"

    # role_tier 0 short-circuits: no fit score, straight to skipped
    r = score_job({"uid": "u", "title": "Retail Key Holder", "desc_text": "", "loc": ""})
    assert r == {"uid": "u", "role_tier": 0, "score": 0, "status": "skipped",
                 "score_reason": r["score_reason"]}, r
    assert score_job(dict(jr))["status"] == "scored"

    # In TARGET="finance" mode the financial industry is the priority. Company beats
    # role, role-at-a-non-finance-company still counts for something, neither is invented.
    fin_co = {"title": "Financial Analyst", "desc_text": "Jane Street is a proprietary "
             "trading firm.", "company_name": "Jane Street", "loc": "New York, NY"}
    fin_role_only = {"title": "Data Science, Finance & Strategy",
                     "desc_text": "Anthropic is an AI safety company.",
                     "company_name": "Anthropic", "loc": "San Francisco, CA"}
    no_fin = {"title": "Machine Learning Engineer", "desc_text": "Anthropic is an AI safety "
             "company.", "company_name": "Anthropic", "loc": "San Francisco, CA"}
    assert is_finance_company("Jane Street", "") is True
    assert is_finance_company("Anthropic", "Anthropic is an AI safety company.") is False
    assert is_finance_role("Data Science, Finance & Strategy", "") is True
    assert is_finance_role("Machine Learning Engineer", "") is False
    # a keyword-channel firm that never reaches companies.json's roster, and whose posting
    # body never states its sector -- caught on the NAME or not at all
    assert is_finance_company("Janus Henderson Investors", "") is True
    # description boilerplate must NOT make an engineering role a finance role
    assert is_finance_role("Applied AI Engineer", "We are backed by venture capital.") is False
    assert is_finance_role("AI Engineer", "Software for capital markets clients.") is False
    assert is_finance_role("Equity Research Associate", "") is True

    # Employers state the sponsorship bar from the CANDIDATE's side far more often than their
    # own ("you must be authorized without sponsorship", not "we cannot sponsor"). All three of
    # these were MISSED until 2026-08-28 and each cost a full tailor pass.
    for _t in ("Applicants must be legally authorized to work in the United States on a "
               "permanent basis without the need for employer sponsorship.",
               "Must be legally authorized to work in the United States without the need for "
               "employer sponsorship, now or at any time in the future.",
               "Applicants must have work authorization that does not now or in the future "
               "require sponsorship of a visa for employment authorization.",
               "Unfortunately, we are unable to consider candidates requiring visa sponsorship.",
               # Added 2026-08-31. Each of these ALSO cost a claim + resume render, and each was
               # caught by the tailor re-reading the JD rather than by this file.
               # the qualifier "Visa" splits a phrase BLOCK_RX only knew literally.
               "This position is not eligible for Visa sponsorship.",
               # the bar is the SUBJECT, so every verb-first alternation missed it.
               "Sponsorship for Work Authorization is not available for this posting.",
               "We do not provide training plans or support for F-1 OPT, STEM OPT extensions.",
               # scored 46 and was even CREDITED "new-grad: entry level", because the
               # disqualifying clause contains the words that normally signal a good fit.
               "Acme does not intend to hire experienced or entry level job seekers who will "
               "need, now or in the future, Acme sponsorship through the H-1B lottery.",
               # cost a live form fill.
               "Candidates must be authorized to work in the U.S. without current or future "
               "employer sponsorship."):
        assert NOSPONSOR_RX.search(_t), _t
    # ...and must stay silent on sponsorship-FRIENDLY or merely neutral language
    for _t in ("We sponsor H-1B and green card applications for qualified candidates.",
               "This role is eligible for visa sponsorship.",
               # The 2026-08-31 additions are the widest patterns in this regex; these pin that
               # they did not start eating sponsor-FRIENDLY sentences on the way past.
               "We are happy to support OPT and STEM OPT candidates.",
               "We provide full visa sponsorship including OPT support.",
               "Sponsorship for this role is available to qualified applicants.",
               "Applicants must be legally authorized to work in the United States."):
        assert not NOSPONSOR_RX.search(_t), _t
    s_co, s_role, s_none = fit_score(fin_co)[0], fit_score(fin_role_only)[0], fit_score(no_fin)[0]
    assert s_co > s_role > s_none, (s_co, s_role, s_none)

    # "risk strat"/"strats" don't match "Risk Strategist" (word-boundary regex needs "strat"
    # to end the word); found live 2026-09-03 across a dozen real fintechs -- pinned so the
    # phrase-list addition doesn't silently drift back out.
    assert classify_role("Risk Strategist")[0] == 2
    assert classify_role("Fraud Strategist")[0] == 2
    assert classify_role("Fraud Operations Manager")[0] == 2

    # Third finance-roster expansion, 2026-09-04: same "word inserted between the phrase" gap,
    # this time "credit". Real titles found live at Vanguard, SoFi, Wayflyer.
    assert classify_role("Senior Quantitative Credit Strategist")[0] == 2
    assert classify_role("Credit Strategy Manager")[0] == 2
    assert classify_role("Credit Strategy Principal")[0] == 2
    # A Product Manager title bypasses the OUT_RX veto when it also carries a FAMILY[1]/[2]
    # phrase -- true of "credit strategy" same as the pre-existing "risk strategy"/"fraud
    # strategy" (checked below, was already true before this change). Unlike bare
    # "compliance", which lives in FAMILY[3] specifically so it does NOT bypass.
    assert classify_role("Product Manager, Risk Strategy")[0] == 2
    assert classify_role("Staff Product Manager, ML Credit Strategy")[0] == 2
    # ...and the veto still wins for a PM title with no FAMILY[1]/[2] phrase at all.
    assert classify_role("Product Manager, Risk & Compliance")[0] == 0

    # Second finance-roster expansion, 2026-09-04: "Compliance Officer" and "Risk Operations
    # Lead" fell through role_tier 0 at real finance/fintech employers (Jane Street, Stripe,
    # Bybit, Nubank...) because only "compliance analyst" and "risk management" were in
    # vocabulary. Pinned so these don't silently drift back out.
    assert classify_role("Chief Compliance Officer")[0] == 3
    assert classify_role("Risk Operations Lead")[0] == 2
    assert classify_role("Risk Control Engineer")[0] == 2
    assert classify_role("Conduct Risk Specialist")[0] == 2
    # ...but the OUT_RX veto still wins when a title is otherwise a Product/Program Manager
    # seat that happens to mention compliance/risk -- "compliance" only lives in FAMILY[3],
    # and the veto bypass only checks FAMILY[1]/[2].
    assert classify_role("Product Manager, Risk & Compliance")[0] == 0

    # Finance-only gate (2026-09-03): a generic SWE/data-science/AI-eng title with no
    # finance signal anywhere must not clear min_score=40, however good the skill overlap.
    # role_tier itself is untouched -- these are still honestly tier 2/4.
    waymo_swe = {"title": "Software Engineer, Quantitative Evaluations", "company_name": "Waymo",
                 "desc_text": "Build ML systems for self-driving. Python, statistics, "
                 "machine learning, equity. 2 years experience.", "loc": "Mountain View, CA"}
    gitlab_ai = {"title": "Backend Engineer, AI Engineering: Duo Chat", "company_name": "GitLab",
                 "desc_text": "GitLab is a DevSecOps platform. Python, LLM, risk.",
                 "loc": "Remote - US"}
    assert classify_role(waymo_swe["title"], waymo_swe["desc_text"])[0] == 2
    assert fit_score(waymo_swe)[0] < 40, fit_score(waymo_swe)
    assert classify_role(gitlab_ai["title"], gitlab_ai["desc_text"])[0] == 4
    assert fit_score(gitlab_ai)[0] < 40, fit_score(gitlab_ai)
    # ...but the same generic title at a real finance employer, or with a finance-specific
    # tier-1/2 word alongside it, is NOT capped.
    jane_street_swe = dict(waymo_swe, title="Software Engineer", company_name="Jane Street",
                           desc_text="Jane Street is a proprietary trading firm. Python.")
    assert fit_score(jane_street_swe)[0] >= 40, fit_score(jane_street_swe)
    trading_swe = dict(waymo_swe, title="Software Engineer, Trading Systems",
                       company_name="Waymo")
    assert fit_score(trading_swe)[0] >= 40, fit_score(trading_swe)

    print("selftest ok")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bulk", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--rescore", action="store_true", help="include already-scored jobs")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--title")
    ap.add_argument("--desc", default="")
    ap.add_argument("--loc", default="")
    ap.add_argument("cmd", nargs="?")
    a = ap.parse_args(argv)
    if a.cmd == "selftest":
        return selftest()
    if a.title:
        print(json.dumps(score_job({"uid": "-", "title": a.title, "desc_text": a.desc,
                                    "loc": a.loc}), indent=2))
        return 0
    if a.report:
        return report(a.limit)
    if a.bulk or a.dry_run:
        return bulk(a.limit, a.rescore, dry=a.dry_run)
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
