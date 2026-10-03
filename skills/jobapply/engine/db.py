"""SQLite state store for the job auto-apply system.

DB file: jobapply.db next to this file (~/jobapply/jobapply.db)

uid = '<source>:<native_id>'  ->  'gh:8130725', 'ashby:<uuid>', 'lever:<uuid>', 'wd:<id>'

applications.status lifecycle:
    new -> scored -> tailored -> filling -> submitting -> submitted -> confirmed

side / terminal states:
    needs_human  captcha, unknown question, or fill-verify mismatch -> human queue
    failed       automation error, nothing submitted
    skipped      prefiltered out (clearance / no-sponsorship / cooldown / burst cap)
    filtered     scorer rejected it (off-role / too senior); never reaches a form
    manual       Workday / iCIMS / OCR portal: recorded, the user applies by hand
    rejected     employer said no
    interview    employer replied positively
    fraud_flag   scam signal (fee/SSN/bank ask) -> blocks that whole COMPANY, forever

TERMINAL = failed, skipped, filtered, manual, rejected.  Everything else counts as an
open application at that company (company_open_count -> cap 3 per gap #12).
"""
import sqlite3, hashlib, re, json, pathlib, datetime, argparse, sys, unicodedata

DB = pathlib.Path(__file__).resolve().parent / "jobapply.db"
TERMINAL = ("failed", "skipped", "filtered", "manual", "rejected")
READY = ("new", "scored", "tailored")  # queue() hands these back out
# fraud_flag is a signal about the EMPLOYER, not a pending application. Deliberate choice:
# it blocks every job at that company (company_blocked() -> queue() and claim()) instead of
# quietly eating one open slot forever, which is what it did by being in neither set.
BLOCKING = ("fraud_flag",)
# "open at this company" = past the ready stage, not yet terminal. A queued row must not
# count against its own cap, so READY is excluded on both sides. That only covers rows
# queue() hands out; a needs_human row coming back off the human queue is already "open",
# so claim() drops the row being claimed by uid -- see company_open_count(exclude_uid=).
OPEN_EXCL = TERMINAL + READY + BLOCKING
STATUSES = READY + TERMINAL + BLOCKING + ("filling", "submitting", "submitted", "confirmed",
                                          "needs_human", "interview")

# Caps live in the frozen profile, not here. Greenhouse's spam flag is permanent, org-wide
# and silent, so these are enforced in queue()'s SQL -- not left to an agent to remember.
_LIM = json.loads((pathlib.Path.home() / ".config/jobapply/profile.json").read_text())["limits"]
PER_COMPANY_PER_WEEK = int(_LIM["per_company_per_week"])
MAX_OPEN_PER_COMPANY = int(_LIM["max_open_per_company"])
TIER1_REQUIRES_APPROVAL = bool(_LIM["tier1_requires_approval"])
AUTO_SUBMIT = bool(_LIM.get("auto_submit", False))   # default false: draft mode until flipped
DAILY_TARGET = int(_LIM.get("daily_target", 100))   # enforced in claim(), the last gate

# queue() mix. tier-1 is draft-only (claim() refuses it without --approved), so ORDER BY tier ASC
# handed back 100 rows nothing could submit. Tier-1 now gets a small quota that feeds the human
# draft queue; the bulk comes from the auto-submittable tiers. Freshness still rules inside a tier.
SUBMITTABLE = (2, 3, 4)
TIER_MIX = ((1, 0.10), (2, 0.45), (3, 0.15), (4, 0.30))

JOB_COLS = ["uid", "source", "company_slug", "company_name", "title", "loc", "url",
            "apply_url", "first_seen_at", "posted_at", "desc_text", "dedup_hash",
            "tier", "score", "ats"]   # score_reason is scorer-only: set_score() writes it
APP_COLS = ["uid", "status", "attempt", "submitted_at", "resume_path", "resume_sha1",
            "cl_path", "cl_variant", "answers_json", "fail_reason", "screenshot_path",
            "confirmed_email_at"]


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


def init():
    with conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS jobs(
            uid TEXT PRIMARY KEY, source TEXT, company_slug TEXT, company_name TEXT,
            title TEXT, loc TEXT, url TEXT, apply_url TEXT, first_seen_at TEXT,
            posted_at TEXT, desc_text TEXT, dedup_hash TEXT, tier INTEGER,
            score REAL, ats TEXT, score_reason TEXT, role_tier INTEGER)""")
        c.execute("""CREATE TABLE IF NOT EXISTS applications(
            uid TEXT PRIMARY KEY REFERENCES jobs(uid), status TEXT, attempt INTEGER DEFAULT 0,
            submitted_at TEXT, resume_path TEXT, resume_sha1 TEXT, cl_path TEXT,
            cl_variant TEXT, answers_json TEXT, fail_reason TEXT, screenshot_path TEXT,
            confirmed_email_at TEXT)""")
        # existing DBs predate score_reason: add the column in place, never rebuild the table
        have = {r["name"] for r in c.execute("PRAGMA table_info(jobs)")}
        for col, decl in (("score_reason", "TEXT"), ("role_tier", "INTEGER")):
            if col not in have:
                c.execute(f"ALTER TABLE jobs ADD COLUMN {col} {decl}")
        c.execute("CREATE INDEX IF NOT EXISTS i_dedup ON jobs(dedup_hash)")
        c.execute("CREATE INDEX IF NOT EXISTS i_co ON jobs(company_slug)")
        c.execute("CREATE INDEX IF NOT EXISTS i_status ON applications(status)")


# " Analyst II" / " Analyst 2" / " Analyst, Senior" / " Analyst (Remote)" -> "analyst"
_LEVEL = re.compile(r"(?:[\s,\-–—]+(?:i{1,3}|iv|vi{0,3}|ix|x|\d{1,2}|senior|sr\.?|junior|jr\.?)"
                    r"|\s*[\(\[][^\)\]]*[\)\]])$")


# Jane Street posts titles in Lisu codepoints shaped like Latin capitals -- the real row
# gh:8596771002 is "\ua4dfachine \ua4e1earning \ua4e3esearcher". Those fork the dedup hash and hide the
# title from every keyword regex. Only the Lisu block appears in the live data, so map that
# block and nothing else: the DB's real Japanese/Chinese titles must survive untouched.
_CONFUSABLE = str.maketrans({
    "\ua4d0": "B", "\ua4d1": "P", "\ua4d2": "D", "\ua4d3": "D", "\ua4d4": "T", "\ua4d7": "K",
    "\ua4d9": "J", "\ua4da": "C", "\ua4dc": "Z", "\ua4dd": "F", "\ua4df": "M", "\ua4e0": "N",
    "\ua4e1": "L", "\ua4e2": "S", "\ua4e3": "R", "\ua4e6": "V", "\ua4e7": "H", "\ua4ea": "W",
    "\ua4eb": "X", "\ua4ec": "Y", "\ua4ee": "A", "\ua4f0": "E", "\ua4f2": "I", "\ua4f3": "O",
    "\ua4f4": "U"})


def defang(s):
    """NFKC, drop zero-width/format chars, de-accent Latin, fold Lisu lookalikes to ASCII."""
    out = []
    for ch in unicodedata.normalize("NFKC", s).translate(_CONFUSABLE):
        if unicodedata.category(ch) in ("Cf", "Mn"):      # ZWSP, variation selectors
            continue
        d = unicodedata.normalize("NFD", ch)
        out.append(d[0] if d[0].isascii() else ch)        # e-acute -> e, but katakana stays put
    return "".join(out)


def normalize_title(t):
    s = re.sub(r"\s+", " ", defang(t or "").strip().lower())
    while True:
        s2 = _LEVEL.sub("", s).strip(" ,-–—")
        if s2 == s or not s2:
            return s
        s = s2


def dedup_hash(company_slug, title, loc):
    city = (loc or "").split(",")[0].strip().lower()
    key = "%s|%s|%s" % ((company_slug or "").lower(), normalize_title(title), city)
    return hashlib.sha1(key.encode()).hexdigest()


def upsert_job(j):
    """True if newly inserted. Never overwrites first_seen_at."""
    j = dict(j)
    j.setdefault("first_seen_at", now())
    j.setdefault("dedup_hash", dedup_hash(j.get("company_slug"), j.get("title"), j.get("loc")))
    row = [j.get(k) for k in JOB_COLS]
    with conn() as c:
        cur = c.execute("INSERT OR IGNORE INTO jobs(%s) VALUES(%s)"
                        % (",".join(JOB_COLS), ",".join("?" * len(JOB_COLS))), row)
        if cur.rowcount:
            return True
        upd = [k for k in JOB_COLS if k not in ("uid", "first_seen_at")]
        # discovery re-runs pass score=None every night; COALESCE keeps what set_score() wrote
        # instead of nulling the whole scoring pass back out.
        keep = ("score", "tier")
        c.execute("UPDATE jobs SET %s WHERE uid=?"
                  % ",".join("%s=COALESCE(?,%s)" % (k, k) if k in keep else k + "=?"
                             for k in upd),
                  [j.get(k) for k in upd] + [j["uid"]])
        return False


def seen_dedup(h):
    """Existing uid for this dedup_hash (cross-channel duplicate), or None."""
    with conn() as c:
        r = c.execute("SELECT uid FROM jobs WHERE dedup_hash=? ORDER BY first_seen_at LIMIT 1",
                      (h,)).fetchone()
    return r["uid"] if r else None


def company_cooldown(company_slug, days):
    """Most recent submitted_at at this company within `days`, else None (None = clear to apply)."""
    cut = (datetime.datetime.now(datetime.timezone.utc)
           - datetime.timedelta(days=days)).isoformat(timespec="seconds")
    with conn() as c:
        r = c.execute("""SELECT max(a.submitted_at) m FROM applications a JOIN jobs j USING(uid)
                         WHERE j.company_slug=? AND a.submitted_at>=?""", (company_slug, cut)).fetchone()
    return r["m"]


def company_open_count(company_slug, exclude_uid=None):
    """Open applications at this company. exclude_uid drops one row: an application must not
    count against its own cap. `IS NOT` not `!=` -- `!=NULL` is NULL, which drops every row."""
    with conn() as c:
        return c.execute("""SELECT count(*) n FROM applications a JOIN jobs j USING(uid)
                            WHERE j.company_slug=? AND a.uid IS NOT ?
                              AND a.status NOT IN (%s)"""
                         % ",".join("?" * len(OPEN_EXCL)),
                         (company_slug, exclude_uid) + OPEN_EXCL).fetchone()["n"]


def company_blocked(company_slug):
    """True once anything at this company hit the fraud tripwire. A scam signal is about the
    employer, so it takes the whole company off the board rather than just that one req."""
    with conn() as c:
        return bool(c.execute("""SELECT 1 FROM applications a JOIN jobs j USING(uid)
                                 WHERE j.company_slug=? AND a.status IN (%s) LIMIT 1"""
                              % ",".join("?" * len(BLOCKING)),
                              (company_slug,) + BLOCKING).fetchone())


def submitted_today():
    """Submissions dated today (UTC) -- the daily_target counter, enforced in claim()."""
    with conn() as c:
        return c.execute("SELECT count(*) n FROM applications WHERE substr(submitted_at,1,10)=?",
                         (now()[:10],)).fetchone()["n"]


def set_score(uid, score, tier=None, reason=None):
    """The scorer's write path (mark() can only touch applications). None leaves a field alone.
    Returns 1 if the row existed, 0 if not."""
    with conn() as c:
        return c.execute("""UPDATE jobs SET score=COALESCE(?,score), tier=COALESCE(?,tier),
                            score_reason=COALESCE(?,score_reason) WHERE uid=?""",
                         (score, tier, reason, uid)).rowcount


def company_week_count(company_slug):
    """Submits at this company in the trailing 7 days."""
    cut = (datetime.datetime.now(datetime.timezone.utc)
           - datetime.timedelta(days=7)).isoformat(timespec="seconds")
    with conn() as c:
        return c.execute("""SELECT count(*) n FROM applications a JOIN jobs j USING(uid)
                            WHERE j.company_slug=? AND a.submitted_at>=?""",
                         (company_slug, cut)).fetchone()["n"]


def claim(uid, approved=False):
    """Write status='submitting' BEFORE the submit click. Raises rather than claiming
    if a cap or the tier-1 gate says this application must not be submitted.

    This ordering is the entire crash-duplicate defence: if the process dies
    between the click and the result, the row already says 'submitting', so the
    next run sees it as in-flight and routes it to a human instead of clicking
    submit a second time. Recording after the click would lose that window.

    The cap re-check here is deliberate duplication of queue()'s SQL: this is the
    last instruction before an irreversible click, and the queue read may be minutes old.
    """
    with conn() as c:
        r = c.execute("SELECT company_slug, tier FROM jobs WHERE uid=?", (uid,)).fetchone()
    if not r:
        raise ValueError("no such job: %s" % uid)
    if r["tier"] == 1 and TIER1_REQUIRES_APPROVAL and not approved:
        raise PermissionError("tier-1 firm (%s): submit requires explicit human approval "
                              "(--approved). Leave it at draft." % r["company_slug"])
    if not AUTO_SUBMIT and not approved:
        raise PermissionError("auto_submit is off in profile.json: leave this at a verified "
                              "draft. A human submits with --approved.")
    if company_blocked(r["company_slug"]):
        raise PermissionError("fraud_flag on %s: that employer is blocked entirely, no req "
                              "there is submittable" % r["company_slug"])
    n_today = submitted_today()
    if n_today >= DAILY_TARGET:
        raise PermissionError("daily cap: %d applications already submitted today (UTC), "
                              "daily_target=%d. Stop until tomorrow -- running apply twice "
                              "does not buy a second day." % (n_today, DAILY_TARGET))
    n_open = company_open_count(r["company_slug"], exclude_uid=uid)
    n_week = company_week_count(r["company_slug"])
    if n_open >= MAX_OPEN_PER_COMPANY:
        raise PermissionError("cap: %s already has %d open applications (max %d)"
                              % (r["company_slug"], n_open, MAX_OPEN_PER_COMPANY))
    if n_week >= PER_COMPANY_PER_WEEK:
        raise PermissionError("cap: %s already got %d submits this week (max %d) -- a burst is "
                              "what trips Greenhouse's permanent spam flag"
                              % (r["company_slug"], n_week, PER_COMPANY_PER_WEEK))
    with conn() as c:
        # CONFIRMED LIVE 2026-09-03: this INSERT/UPDATE is a SEPARATE write path from mark()
        # and had no guard of its own -- mark()'s 2026-09-02 DONE/PRE_SUBMIT guard only
        # protects calls that go through mark(). Two parallel app-tailor batches re-offered a
        # uid an app-filler had already resolved to 'manual'/'skipped' (queue() legitimately
        # re-hands-out anything not yet TERMINAL/submitted), and the tailor agent's claim()
        # call silently flipped it back to 'submitting', erasing the filler's terminal
        # classification. This is the exact crash-duplicate-defense field the docstring above
        # describes -- overwriting it on an already-DONE row is the failure mode claim() exists
        # to prevent, not one it should be able to cause.
        row = c.execute("SELECT status FROM applications WHERE uid=?", (uid,)).fetchone()
        if row and row["status"] in DONE:
            raise ValueError(
                "refusing to claim %r -- it is already %r, a done/irreversible state. "
                "Something else already resolved this application; re-claiming it would erase "
                "that outcome, not restart it." % (uid, row["status"]))
        c.execute("""INSERT INTO applications(uid,status,attempt) VALUES(?,'submitting',1)
                     ON CONFLICT(uid) DO UPDATE SET status='submitting',
                     attempt=applications.attempt+1""", (uid,))


# A submission is a real-world, irreversible event -- once the employer has the
# application, nothing in this DB can undo it. mark() used to allow regressing a row from
# any of these back to a PRE-submission stage with no guard at all: CONFIRMED LIVE
# once, a remediation script re-marked already-`submitted` rows as
# `tailored` and the trigger -- which only validates the
# status is a known ENUM value, not that the TRANSITION is legal -- let it through.
# Result: two rows sat as `tailored` with a stale `submitted_at` still populated, exactly the
# shape claim()/queue() would have handed straight back to a filler agent for a genuine
# double-submit to an employer who already has the application. Caught by chance, not by
# any check. This is the check.
# WIDENED same day, same incident: the resume-remediation script above tripped the
# submitted-only version of this guard; a few minutes later a REAL parallel-agent race (two
# app-tailor/app-filler agents working the same queue, one re-marking a row `tailored` after
# the other had already confirmed it `manual` -- an unsupported ATS with no adapter) hit the
# identical shape from the OTHER direction, 8 times in one run, caught only because the filler
# happened to re-verify. `manual`/`skipped`/`filtered`/`rejected`/`failed` are just as
# irreversible in intent as `submitted` -- each is a real-world or human-review conclusion
# about this specific application, not a step in an ongoing pipeline. TERMINAL is this
# module's own name for that set; DONE adds the post-submit states and fraud_flag (a
# permanent company-wide block, never meant to un-flag via mark()).
DONE = TERMINAL + BLOCKING + ("submitted", "confirmed", "interview")
PRE_SUBMIT = ("new", "scored", "tailored", "filling", "submitting")


def mark(uid, status, **fields):
    if status not in STATUSES:      # a typo used to create a silent dead bucket
        raise ValueError("unknown status %r; one of: %s" % (status, " ".join(sorted(STATUSES))))
    bad = set(fields) - set(APP_COLS)
    assert not bad, "unknown application columns: %s" % bad
    if status in PRE_SUBMIT:
        with conn() as c:
            row = c.execute("SELECT status FROM applications WHERE uid=?", (uid,)).fetchone()
        if row and row["status"] in DONE:
            raise ValueError(
                "refusing to mark %r %r -- it is already %r, a done/irreversible state. "
                "Re-marking it backward into the pipeline risks a genuine duplicate action "
                "(e.g. a second submit) next time this row is queued. To attach a corrected "
                "artifact (e.g. resume_sha1) to an already-resolved row, call mark(uid, "
                "'<same status>', ...) again -- status stays put, only the fields you pass "
                "change." % (uid, status, row["status"]))
    fields["status"] = status
    if status == "submitted":
        fields.setdefault("submitted_at", now())
    cols = ["uid"] + list(fields)
    with conn() as c:
        c.execute("INSERT INTO applications(%s) VALUES(%s) ON CONFLICT(uid) DO UPDATE SET %s"
                  % (",".join(cols), ",".join("?" * len(cols)),
                     ",".join(k + "=excluded." + k for k in fields)),
                  [uid] + list(fields.values()))


def _pool(limit, tiers, min_score, loc=None):
    """One tier slice of queue(), with the per-company caps ENFORCED here in SQL.

    The caps are the highest-consequence control in the system, so they are a
    property of the query rather than a rule an agent is asked to honour: a job
    at a capped or fraud-flagged company is never handed out, so nothing downstream
    can apply to it.

    Scored jobs first (score DESC), unscored still reachable at the tail so the
    pipeline can bootstrap; freshest first as a tiebreak -- 2027 FT reqs close when full.
    """
    week = (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(days=7)).isoformat(timespec="seconds")
    q = """SELECT j.* FROM jobs j LEFT JOIN applications a USING(uid)
           WHERE (a.uid IS NULL OR a.status IN (%s))
             AND j.tier IN (%s)
             AND NOT EXISTS (SELECT 1 FROM applications af JOIN jobs jf USING(uid)
                             WHERE jf.company_slug=j.company_slug AND af.status IN (%s))
             AND (SELECT count(*) FROM applications a2 JOIN jobs j2 USING(uid)
                  WHERE j2.company_slug=j.company_slug AND a2.status NOT IN (%s)) < ?
             AND (SELECT count(*) FROM applications a3 JOIN jobs j3 USING(uid)
                  WHERE j3.company_slug=j.company_slug AND a3.submitted_at>=?) < ?
        """ % (",".join("?" * len(READY)), ",".join("?" * len(tiers)),
               ",".join("?" * len(BLOCKING)), ",".join("?" * len(OPEN_EXCL)))
    p = (list(READY) + list(tiers) + list(BLOCKING) + list(OPEN_EXCL)
         + [MAX_OPEN_PER_COMPANY, week, PER_COMPANY_PER_WEEK])
    if min_score is not None:
        q += " AND j.score>=?"          # explicit filter: unscored rows drop out on purpose
        p.append(min_score)
    if loc:
        # Geography lives HERE, not in a hand-written SELECT at the call site: every
        # per-company cap above is enforced in this query, and a caller that filtered by
        # location itself would have to re-implement all of them to stay honest.
        q += " AND j.loc LIKE ?"
        p.append("%" + loc + "%")
    q += " ORDER BY (j.score IS NULL), j.score DESC, j.posted_at DESC LIMIT ?"
    p.append(limit)
    with conn() as c:
        return [dict(r) for r in c.execute(q, p)]


def queue(limit, tier=None, min_score=None, loc=None):
    """Jobs ready to act on. Tier-INTERLEAVED, not ORDER BY tier ASC.

    Tier-1 jobs are thousands of rows, and tier-1 is exactly what plan() will not submit and
    claim() refuses without --approved, so a tier-sorted page meant 0 auto-throughput.
    TIER_MIX gives tier-1 a small quota (it feeds the human draft queue) and the rest to
    tiers 2-4. If a tier runs dry the leftovers top up from the submittable tiers only --
    never from tier-1, which would put the unsubmittable page straight back.

    tier= still forces a single-tier page for targeted runs.
    """
    if tier is not None:
        return _pool(limit, (tier,), min_score, loc)
    rows, seen = [], set()
    for t, share in TIER_MIX:
        for r in _pool(max(1, round(limit * share)), (t,), min_score, loc):
            if len(rows) < limit and r["uid"] not in seen:
                seen.add(r["uid"]); rows.append(r)
    if len(rows) < limit:
        for r in _pool(limit + len(rows), SUBMITTABLE, min_score, loc):
            if len(rows) >= limit:
                break
            if r["uid"] not in seen:
                seen.add(r["uid"]); rows.append(r)
    return rows


def stats():
    week = (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(days=7)).isoformat(timespec="seconds")
    with conn() as c:
        by_status = {r["status"]: r["n"] for r in
                     c.execute("SELECT status, count(*) n FROM applications GROUP BY status")}
        jobs = c.execute("SELECT count(*) n FROM jobs").fetchone()["n"]
        per_company = {r["company_slug"]: r["n"] for r in
                       c.execute("""SELECT j.company_slug, count(*) n FROM applications a
                                    JOIN jobs j USING(uid) WHERE a.submitted_at>=?
                                    GROUP BY 1 ORDER BY 2 DESC""", (week,))}
    return {"jobs": jobs, "by_status": by_status, "applied_today": submitted_today(),
            "needs_human": by_status.get("needs_human", 0), "per_company_week": per_company,
            "daily_target": DAILY_TARGET}


def demo():
    global DB, DAILY_TARGET
    import tempfile, os
    real, DB = DB, pathlib.Path(tempfile.mkdtemp()) / "t.db"
    try:
        init()
        assert normalize_title("Quantitative Analyst II") == "quantitative analyst"
        assert normalize_title("Data Scientist, Senior") == "data scientist"
        assert normalize_title("ML Engineer (Remote)") == "ml engineer"
        assert normalize_title("Analyst 2") == "analyst"
        assert normalize_title("Trader Jr.") == "trader"

        # DEFECT 11: Jane Street's real row gh:8596771002 writes M/L/R as Lisu codepoints
        homoglyph = "\ua4dfachine \ua4e1earning \ua4e3esearcher"
        assert normalize_title(homoglyph) == "machine learning researcher"
        assert dedup_hash("janestreet", homoglyph, "New York, NY") == \
               dedup_hash("janestreet", "Machine Learning Researcher", "New York")
        assert normalize_title("Quant\u200b Analyst\u00a0II") == "quant analyst"
        assert normalize_title("\u30c7\u30fc\u30bf\u30b5\u30a4\u30a8\u30f3\u30c6\u30a3\u30b9\u30c8") == \
               "\u30c7\u30fc\u30bf\u30b5\u30a4\u30a8\u30f3\u30c6\u30a3\u30b9\u30c8"   # real Japanese titles survive

        j = dict(uid="gh:8130725", source="gh", company_slug="janestreet", company_name="Jane Street",
                 title="Quantitative Trader I", loc="New York, NY", url="u", apply_url="a",
                 posted_at="2026-08-20", desc_text="d", tier=1, score=0.9, ats="greenhouse")
        assert upsert_job(j) is True
        first = queue(9)[0]["first_seen_at"]

        # second upsert: not new, first_seen_at preserved, other fields refreshed
        j2 = dict(j, title="Quantitative Trader II", first_seen_at="1999-01-01T00:00:00+00:00")
        assert upsert_job(j2) is False
        row = queue(9)[0]
        assert row["first_seen_at"] == first, row["first_seen_at"]
        assert row["title"] == "Quantitative Trader II"

        # cross-channel duplicate collapses to the same hash
        assert dedup_hash("janestreet", "Quantitative Trader II", "New York, NY") == \
               dedup_hash("janestreet", "Quantitative Trader", "New York")
        assert seen_dedup(row["dedup_hash"]) == "gh:8130725"
        assert seen_dedup("nope") is None

        # tier-1 gets a QUOTA, not the whole page; freshest first inside a tier
        upsert_job(dict(j, uid="lever:x", company_slug="acme", title="Analyst", tier=2,
                        posted_at="2026-08-22"))
        upsert_job(dict(j, uid="ashby:y", company_slug="acme", title="Engineer", tier=1,
                        posted_at="2026-08-01"))
        assert [r["uid"] for r in queue(9)] == ["gh:8130725", "lever:x"], queue(9)
        assert [r["uid"] for r in queue(9, tier=2)] == ["lever:x"]
        assert [r["uid"] for r in queue(9, tier=1)] == ["gh:8130725", "ashby:y"]

        # nothing can be claimed without approval while auto_submit is off / tier-1
        try:
            claim("gh:8130725")
        except PermissionError:
            pass
        else:
            raise AssertionError("tier-1 claim went through without approval")

        # claim writes the row BEFORE the click, so a crash here cannot double-submit
        claim("gh:8130725", approved=True)
        assert stats()["by_status"] == {"submitting": 1}
        assert queue(9)[0]["uid"] != "gh:8130725"  # claimed -> out of the queue
        assert company_open_count("janestreet") == 1
        assert company_cooldown("janestreet", 30) is None  # claimed but not submitted

        mark("gh:8130725", "submitted", resume_sha1="abc", cl_variant="quant")
        assert company_cooldown("janestreet", 30)[:4] == now()[:4]
        assert company_cooldown("acme", 30) is None
        s = stats()
        assert s["applied_today"] == 1 and s["by_status"] == {"submitted": 1} and s["jobs"] == 3

        mark("gh:8130725", "rejected")
        assert company_open_count("janestreet") == 0  # terminal
        mark("lever:x", "needs_human", fail_reason="captcha")
        assert stats()["needs_human"] == 1
        assert stats()["per_company_week"] == {"janestreet": 1}

        # per-company caps are enforced in the query path, not left to an agent
        for i in range(4):
            upsert_job(dict(j, uid="cap:%d" % i, company_slug="capco", company_name="CapCo",
                            title="Analyst %d" % i, tier=2, posted_at="2026-08-1%d" % i))
        assert len([r for r in queue(99) if r["company_slug"] == "capco"]) == 4
        for i in range(MAX_OPEN_PER_COMPANY):
            mark("cap:%d" % i, "needs_human")          # open, not terminal
        assert company_open_count("capco") == MAX_OPEN_PER_COMPANY
        assert [r for r in queue(99) if r["company_slug"] == "capco"] == [], "max_open not enforced"
        for i in range(MAX_OPEN_PER_COMPANY):
            mark("cap:%d" % i, "rejected")             # terminal -> reopens the company
        for i in range(PER_COMPANY_PER_WEEK):
            mark("cap:%d" % i, "submitted")
        assert company_week_count("capco") == PER_COMPANY_PER_WEEK
        assert [r for r in queue(99) if r["company_slug"] == "capco"] == [], "weekly cap not enforced"
        try:
            claim("cap:3", approved=True)      # approval does NOT buy past a cap
        except PermissionError:
            pass
        else:
            raise AssertionError("claim() ignored the weekly cap")

        # DEFECT 13: a row must not count against its OWN cap. Live case: 2 genuinely-open submits + the needs_human row being claimed = 3 =
        # max_open, so claim() refused the very req it was asked to claim. READY covers this
        # for queued rows; a needs_human row comes back off the human queue already "open".
        for i in range(MAX_OPEN_PER_COMPANY + 1):
            upsert_job(dict(j, uid="self:%d" % i, company_slug="selfco", company_name="SelfCo",
                            title="Analyst %d" % i, tier=2, posted_at="2026-08-20"))
        for i in range(MAX_OPEN_PER_COMPANY):
            mark("self:%d" % i, "needs_human")
        assert company_open_count("selfco") == MAX_OPEN_PER_COMPANY
        try:
            claim("self:0", approved=True)         # human unblocked it -> must go through
        except PermissionError as e:
            raise AssertionError("row counted against its own cap: %s" % e)
        assert company_open_count("selfco", "self:0") == MAX_OPEN_PER_COMPANY - 1
        try:
            claim("self:%d" % MAX_OPEN_PER_COMPANY, approved=True)   # DIFFERENT req: still capped
        except PermissionError:
            pass
        else:
            raise AssertionError("a needs_human row stopped occupying its company's slot")

        # DEFECT 1: a plain queue() must be mostly submittable, not 100% tier-1
        for t in (1, 2, 3, 4):
            for i in range(30):
                upsert_job(dict(j, uid="mix:%d:%d" % (t, i), company_slug="mix%d" % t,
                                company_name="Mix", title="Analyst", tier=t, score=None,
                                posted_at="2026-08-%02d" % (i % 28 + 1)))
        q = queue(100)
        assert len(q) == 100 and len({r["tier"] for r in q}) > 1, len(q)
        assert sum(1 for r in q if r["tier"] in SUBMITTABLE) > len(q) / 2, "queue unsubmittable"
        assert sum(1 for r in q if r["tier"] == 1) <= 10, "tier-1 quota blew out"

        # DEFECT 5/6: scores are writable, steer the order, and survive a rediscovery pass
        assert set_score("mix:2:0", 91.0, tier=2, reason="quant fit") == 1
        assert set_score("nope:404", 50.0) == 0
        top = queue(9, tier=2)[0]
        assert (top["uid"], top["score"], top["score_reason"]) == ("mix:2:0", 91.0, "quant fit")
        assert [r["uid"] for r in queue(9, tier=2, min_score=90)] == ["mix:2:0"]
        assert any(r["score"] is None for r in queue(20)), "unscored must stay reachable"
        upsert_job(dict(j, uid="mix:2:0", company_slug="mix2", title="Analyst", tier=2,
                        score=None, posted_at="2026-08-01"))
        assert queue(9, tier=2)[0]["score"] == 91.0, "rediscovery nulled the scorer's write"

        # DEFECT 8: a typo status must not open a silent dead bucket
        try:
            mark("lever:x", "banana")
        except ValueError:
            pass
        else:
            raise AssertionError("mark() accepted a typo status")

        # DEFECT 9: a fraud flag blocks the whole employer, not just that one req
        for i in (1, 2):
            upsert_job(dict(j, uid="scam:%d" % i, company_slug="scamco", company_name="ScamCo",
                            title="Analyst %d" % i, tier=2, posted_at="2026-08-20"))
        mark("scam:1", "fraud_flag", fail_reason="asked for bank details")
        assert company_open_count("scamco") == 0                              # not "open" ...
        assert [r for r in queue(99) if r["company_slug"] == "scamco"] == []  # ... but blocked
        try:
            claim("scam:2", approved=True)
        except PermissionError:
            pass
        else:
            raise AssertionError("claim() ignored a company fraud_flag")

        # DEFECT 10: daily_target binds at the last gate, so a second run cannot double the day
        real_target, DAILY_TARGET = DAILY_TARGET, 1
        try:
            assert submitted_today() >= 1
            claim("mix:3:0", approved=True)
        except PermissionError as e:
            assert "daily" in str(e), e
        else:
            raise AssertionError("claim() ignored daily_target")
        finally:
            DAILY_TARGET = real_target

        try:
            mark("lever:x", "failed", ssn="123")
        except AssertionError:
            pass
        else:
            raise AssertionError("mark() accepted an unknown column")

        # FIXED 2026-09-03: claim() writes status='submitting' via its own raw INSERT/UPDATE,
        # a path mark()'s DONE/PRE_SUBMIT guard never covered. A parallel-agent race hit it
        # live: an app-filler marked a row 'manual' (terminal), a concurrent app-tailor's
        # queue() pull re-offered the same uid, and its claim() call silently flipped it back
        # to 'submitting', erasing the terminal outcome -- same failure shape as the mark()
        # incident above, just through the other door.
        upsert_job(dict(j, uid="guard:0", company_slug="guardco", company_name="GuardCo",
                        title="Analyst", tier=2, posted_at="2026-08-20"))
        mark("guard:0", "manual", fail_reason="no adapter")
        try:
            claim("guard:0", approved=True)
        except ValueError as e:
            assert "already" in str(e), e
        else:
            raise AssertionError("claim() overwrote an already-manual row")
        assert stats()["by_status"].get("manual", 0) >= 1  # unchanged, not silently reopened

        print("demo ok")
    finally:
        DB = real


CLI = """jobapply state store. Every status write goes through here.

  python3 db.py init
  python3 db.py queue [--limit N] [--tier T] [--min-score S]   clear to act on (caps applied)
  python3 db.py score < scores.json              bulk scorer write, JSON on stdin:
                                                 [{"uid":..,"score":88,"tier":2,"reason":".."}]
  python3 db.py claim <uid> [--approved]         status='submitting' BEFORE the submit click
  python3 db.py mark <uid> <status> [--reason R] [--screenshot P] [--sha1 S] [--variant V]
  python3 db.py stats
  python3 db.py selftest

statuses: %s
queue mix (tier, share): %s -- tier-1 is draft-only, so it gets a quota and the rest of the
  page comes from tiers 2-4. Scored rows first, unscored still reachable.
caps (from ~/.config/jobapply/profile.json, enforced in queue() and claim()):
  <= %d submits/company/week, <= %d open/company, <= %d submits/day, tier-1 needs --approved
  a fraud_flag blocks that company entirely; auto_submit=%s (false = every claim needs --approved)
""" % (" ".join(sorted(STATUSES)), TIER_MIX, PER_COMPANY_PER_WEEK, MAX_OPEN_PER_COMPANY,
       DAILY_TARGET, AUTO_SUBMIT)


def main(argv=None):
    ap = argparse.ArgumentParser(description=CLI,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init"); sub.add_parser("stats"); sub.add_parser("selftest")
    q = sub.add_parser("queue"); q.add_argument("--limit", type=int, default=20)
    q.add_argument("--tier", type=int, choices=(1, 2, 3, 4))
    q.add_argument("--min-score", type=float, dest="min_score")
    q.add_argument("--loc", help="substring match on jobs.loc, e.g. --loc 'Los Angeles'")
    sub.add_parser("score", help="bulk scorer write; JSON object or list on stdin")
    c = sub.add_parser("claim"); c.add_argument("uid")
    c.add_argument("--approved", action="store_true",
                   help="human go-ahead for THIS application; required for tier-1")
    m = sub.add_parser("mark"); m.add_argument("uid")
    m.add_argument("status", choices=sorted(STATUSES))
    for f, col in (("--reason", "fail_reason"), ("--screenshot", "screenshot_path"),
                   ("--sha1", "resume_sha1"), ("--variant", "cl_variant"),
                   ("--resume", "resume_path"), ("--cl", "cl_path")):
        m.add_argument(f, dest=col)
    a = ap.parse_args(argv)
    if a.cmd == "selftest":
        return demo()
    init()
    if a.cmd == "init":
        print("ok", DB)
    elif a.cmd == "stats":
        print(json.dumps(stats(), indent=2))
    elif a.cmd == "queue":
        for r in queue(a.limit, a.tier, a.min_score, a.loc):
            print("%-22s t%-2s %5s %-26.26s %-38.38s %s"
                  % (r["uid"], r["tier"], "-" if r["score"] is None else "%.0f" % r["score"],
                     r["company_name"], r["title"], r["apply_url"]))
    elif a.cmd == "score":
        rows = json.loads(sys.stdin.read())
        rows = [rows] if isinstance(rows, dict) else rows
        n = sum(set_score(d["uid"], d.get("score"), d.get("tier"), d.get("reason"))
                for d in rows)
        print("scored %d/%d (%d uids not in jobs)" % (n, len(rows), len(rows) - n))
    elif a.cmd == "claim":
        try:
            claim(a.uid, a.approved)
        except (PermissionError, ValueError) as e:
            print("REFUSED: %s" % e, file=sys.stderr); return 2
        print("claimed %s -> submitting" % a.uid)
    elif a.cmd == "mark":
        mark(a.uid, a.status, **{k: v for k, v in vars(a).items()
                                 if k in APP_COLS and k not in ("uid", "status")
                                 and v is not None})
        print("%s -> %s" % (a.uid, a.status))
    return 0


if __name__ == "__main__":
    sys.exit(main())
