"""Renders dashboard.html (next to this file) from jobapply.db. Stdlib only, no JS.

PII rule: this file prints employer-side facts only -- uid / company / title / job
location / tier / ats / status. Never phone, home address, or answers_json --
those columns are not read here on purpose.
"""
import html, datetime, pathlib
import db

OUT = pathlib.Path(__file__).resolve().parent / "dashboard.html"
FUNNEL = ["new", "scored", "tailored", "filling", "submitting", "submitted", "confirmed",
          "interview", "rejected", "needs_human", "fraud_flag", "failed", "skipped", "manual"]
MIN_CHART = 20  # below this many data points a table beats a chart


def e(x):
    return html.escape("" if x is None else str(x))


def table(headers, rows, cls=""):
    if not rows:
        return "<p class=empty>nothing here</p>"
    h = "".join("<th>%s</th>" % e(c) for c in headers)
    b = "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % c for c in r) for r in rows)
    return "<table class='%s'><thead><tr>%s</tr></thead><tbody>%s</tbody></table>" % (cls, h, b)


def bars(pairs):
    m = max(v for _, v in pairs) or 1
    return "<div class=bars>" + "".join(
        "<div class=bar><span class=lab>%s</span>"
        "<span class=fill style='width:%.1f%%'></span><span class=val>%d</span></div>"
        % (e(k), 100.0 * v / m, v) for k, v in pairs) + "</div>"


def render():
    c = db.conn()
    q = lambda s, p=(): [dict(r) for r in c.execute(s, p)]

    human = q("""SELECT a.uid, a.status, j.company_name, j.title, j.apply_url,
                        a.fail_reason, a.attempt
                 FROM applications a JOIN jobs j USING(uid)
                 WHERE a.status IN ('needs_human','fraud_flag','manual')
                 ORDER BY a.status='fraud_flag' DESC, j.tier, j.posted_at DESC""")
    daily = q("""SELECT substr(submitted_at,1,10) d, count(*) n FROM applications
                 WHERE submitted_at IS NOT NULL GROUP BY 1 ORDER BY 1 DESC LIMIT 30""")[::-1]
    counts = {r["status"]: r["n"] for r in q("SELECT status, count(*) n FROM applications GROUP BY 1")}
    week = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=7)).isoformat()[:19]
    comp = q("""SELECT j.company_slug s, count(*) tot,
                sum(a.submitted_at>=?) wk,
                sum(a.status NOT IN ('failed','skipped','manual','rejected','new','scored','tailored')) opn,
                max(a.submitted_at) last FROM applications a JOIN jobs j USING(uid)
                GROUP BY 1 ORDER BY wk DESC, tot DESC""", (week,))
    variants = q("""SELECT coalesce(cl_variant,'(none)') k, count(*) sent,
                    sum(status IN ('rejected','interview')) resp, sum(status='interview') iv
                    FROM applications WHERE submitted_at IS NOT NULL GROUP BY 1""")
    resumes = q("""SELECT coalesce(resume_sha1,'(none)') k, count(*) sent,
                   sum(status IN ('rejected','interview')) resp, sum(status='interview') iv
                   FROM applications WHERE submitted_at IS NOT NULL GROUP BY 1""")
    applied = q("""SELECT a.uid, a.status, substr(a.submitted_at,1,10) d,
                          cast(julianday('now')-julianday(a.submitted_at) AS INT) age,
                          j.company_name, j.title, j.tier, j.ats, j.loc, j.apply_url
                   FROM applications a JOIN jobs j USING(uid)
                   WHERE a.submitted_at IS NOT NULL ORDER BY a.submitted_at DESC""")
    jobs_n = c.execute("SELECT count(*) n FROM jobs").fetchone()["n"]
    c.close()

    today = db.now()[:10]
    sent = sum(r["n"] for r in daily)
    hp = table(["uid", "company", "title", "state", "why", "try", "link"],
               [[e(r["uid"]), e(r["company_name"]), e(r["title"]),
                 ("<b class=warn>FRAUD</b>" if r["status"] == "fraud_flag" else e(r["status"])),
                 e(r["fail_reason"]),
                 e(r["attempt"]), "<a href='%s'>open</a>" % e(r["apply_url"] or "#")] for r in human],
               "wide")
    dp = (bars([(r["d"], r["n"]) for r in daily]) if len(daily) >= MIN_CHART
          else table(["day", "applied"], [[e(r["d"]), r["n"]] for r in daily]))
    fp = table(["status", "n"], [[e(s), counts[s]] for s in FUNNEL if s in counts])
    cp = table(["company", "week", "open", "total", "last submit", "flag"],
               [[e(r["s"]), r["wk"] or 0, r["opn"] or 0, r["tot"], e(r["last"]),
                 ("<b class=warn>burst</b>" if (r["wk"] or 0) >= 2 else "") +
                 ("<b class=warn>3 open</b>" if (r["opn"] or 0) >= 3 else "")] for r in comp])

    def rr(rows, label):
        return table([label, "sent", "replied", "interview", "resp %"],
                     [[e(r["k"]), r["sent"], r["resp"] or 0, r["iv"] or 0,
                       "%.0f%%" % (100.0 * (r["resp"] or 0) / r["sent"])] for r in rows])

    # The tracker. status IS the outcome -- submitted means nobody has replied yet,
    # so age since submit is the only signal until inbox-watcher writes one back.
    OUTCOME = {"submitted": "waiting", "confirmed": "confirmed",
               "interview": "interview", "rejected": "rejected"}
    silent = sorted(r["age"] for r in applied if r["status"] in ("submitted", "confirmed"))
    heard = [r for r in applied if r["status"] in ("interview", "rejected")]
    ivs = sum(r["status"] == "interview" for r in applied)
    med = silent[len(silent) // 2] if silent else 0

    def cell(r):
        o = OUTCOME.get(r["status"], r["status"])
        cls = {"interview": "ok", "rejected": "warn"}.get(o, "mut")
        return "<b class=%s>%s</b>" % (cls, e(o))

    ap = table(["sent", "d", "company", "title", "t", "ats", "where", "outcome", ""],
               [[e(r["d"]), r["age"], e(r["company_name"]), e(r["title"]), r["tier"],
                 e(r["ats"]), e(r["loc"]), cell(r),
                 "<a href='%s'>open</a>" % e(r["apply_url"] or "#")] for r in applied], "wide")

    # ATS rejections land inside two weeks; past three the req is filled or you were dropped.
    CLOCK = [(3, "0-3d  too early"), (7, "4-7d  normal"), (14, "8-14d  watch"),
             (21, "15-21d  follow up"), (10 ** 9, "22d+  assume dead")]
    clock = [0] * len(CLOCK)
    for a in silent:
        clock[next(i for i, (b, _) in enumerate(CLOCK) if a <= b)] += 1
    kp = table(["silence", "n"], [[e(l), n] for (_, l), n in zip(CLOCK, clock) if n])

    doc = """<title>Job apply dashboard</title><meta name=viewport content="width=device-width,initial-scale=1">
<style>
:root{--bg:#fff;--fg:#111;--mut:#666;--line:#ddd;--card:#fafafa;--acc:#0a58ca;--warn:#b3261e}
@media(prefers-color-scheme:dark){:root{--bg:#111417;--fg:#e8e8e8;--mut:#9aa;--line:#2c3238;--card:#171b1f;--acc:#7aa7ff;--warn:#ff8a80}}
*{box-sizing:border-box}body{margin:0;padding:24px;background:var(--bg);color:var(--fg);
font:15px/1.5 ui-sans-serif,-apple-system,Segoe UI,sans-serif}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:0 0 10px;letter-spacing:.04em;text-transform:uppercase;color:var(--mut)}
.sub{color:var(--mut);font-size:13px;margin-bottom:20px}
section{border:1px solid var(--line);background:var(--card);border-radius:10px;padding:16px;margin-bottom:16px}
section.action{border:2px solid var(--warn)}section.action h2{color:var(--warn)}
.grid{display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(300px,1fr))}
.grid section{margin:0}
table{border-collapse:collapse;width:100%%;font-size:13px}
th{text-align:left;color:var(--mut);font-weight:600;border-bottom:1px solid var(--line);padding:5px 8px 5px 0}
td{padding:5px 8px 5px 0;border-bottom:1px solid var(--line);vertical-align:top}
table.wide td{max-width:280px;overflow-wrap:anywhere}
a{color:var(--acc)}.empty{color:var(--mut);font-size:13px;margin:0}
.big{font-size:30px;font-weight:600}.warn{color:var(--warn);margin-right:6px}
.ok{color:#0a7d33}.mut{color:var(--mut);font-weight:400}
@media(prefers-color-scheme:dark){.ok{color:#5fd07f}}
.bars{display:flex;flex-direction:column;gap:3px}
.bar{display:grid;grid-template-columns:78px 1fr 34px;align-items:center;gap:8px;font-size:12px}
.lab{color:var(--mut)}.val{text-align:right}
.fill{background:var(--acc);height:11px;border-radius:3px;display:block;min-width:2px}
</style>
<h1>Job apply</h1>
<div class=sub>%(gen)s &middot; %(jobs)d jobs tracked &middot; %(sent)d submitted &middot; %(today)d today &middot; no PII rendered</div>
<section class=action><h2>Needs human &mdash; %(hn)d</h2>%(hp)s</section>
<section><h2>Applied &mdash; %(sent)d sent, %(heard)d replied, %(ivs)d interview</h2>
<div class=sub style="margin:0 0 12px">median %(med)d d silent &middot; a reply is the only
result that exists; every row below still reading <b class=mut>waiting</b> has had none</div>
%(kp)s<div style="height:12px"></div>%(ap)s</section>
<div class=grid>
<section><h2>Applied per day</h2>%(dp)s</section>
<section><h2>Funnel</h2>%(fp)s</section>
<section><h2>Per company (7d)</h2>%(cp)s</section>
<section><h2>Response by cover-letter variant</h2>%(vp)s
<h2 style="margin-top:16px">Response by resume sha1</h2>%(rp)s
<p class=empty>callback rates run 2-5%%; nothing here is significant under n=200</p></section>
</div>""" % dict(gen=e(db.now()), jobs=jobs_n, sent=sent,
                 today=sum(r["n"] for r in daily if r["d"] == today),
                 hn=len(human), hp=hp, dp=dp, fp=fp, cp=cp,
                 ap=ap, kp=kp, heard=len(heard), ivs=ivs, med=med,
                 vp=rr(variants, "variant"), rp=rr(resumes, "resume"))
    OUT.write_text(doc)
    return OUT


if __name__ == "__main__":
    print(render())
