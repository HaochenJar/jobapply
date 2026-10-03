---
name: jobapply
description: Semi-automated job applications for F-1/OPT new grads. Use when the user wants to set up jobapply, run the daily application loop (discover, score, tailor, apply, inbox), check application status or the needs_human queue, or personalize their resume/targeting for it.
---

# jobapply

A daily pipeline: **discover** postings (Greenhouse/Lever/Ashby/Workday/LinkedIn guest APIs) →
**score** fit (deterministic, no LLM) → **tailor** a resume + cover letter (every fact traced to
`docs/master.md`) → **apply** in Chrome (verify every field, human gates on anything risky) →
**inbox** (classify ATS mail from Gmail, close the did-it-send loop).

Engine + data live in `~/jobapply/`. Profile lives in `~/.config/jobapply/profile.json` (chmod 600).
Five subagents ship with this plugin: job-scout, job-scorer, app-tailor, app-filler, inbox-watcher.

## First run — setup

Check `~/jobapply/docs/master.md` exists and `python3 ~/jobapply/profile_check.py` passes. If not,
walk the user through these steps **in order**, one at a time:

1. **Install the engine.** `bash <this skill's base directory>/setup.sh`. Safe to re-run; it never
   overwrites existing files. Fix anything it reports MISSING (pandoc: `brew install pandoc`;
   Google Chrome).
2. **Profile.** Tell the user to open `~/.config/jobapply/profile.json` and fill it in **by hand**
   (`open -e ~/.config/jobapply/profile.json`), guided by `~/jobapply/INTAKE.md`. You may explain
   fields, but never write work-authorization, salary, start-date or EEO answers yourself — those
   are the user's frozen statements on legal forms. Then `python3 ~/jobapply/profile_check.py`.
   The defaults assume F-1/OPT; a user on a different status must edit `profile_check.py` first.
3. **Resume → `~/jobapply/docs/master.md`.** From the user's .docx:
   `cd ~/jobapply && python3 docs.py import <resume.docx>`, or from a PDF/pasted text write it by
   hand. Either way it must follow `docs/master.example.md`'s exact format (`# Name`, contact line,
   `## Education` / `## Skills` / `## Professional Experience` / `## Additional Information`,
   `### Employer — City, ST`, `*Title · Month YYYY – Month YYYY*`, `- bullet`). Every fact comes
   from the user; never invent or embellish one. Show it to the user and get a yes.
4. **Personalize the templates** (they ship tuned to the fictional Alex Rivera). Search
   `PERSONALIZE` in `~/jobapply/docs.py` and `~/jobapply/score.py`:
   - `docs.py` `PLANS`: employer keys + bullet indices per variant, matching the new master.md.
   - `docs.py` `ARTIFACTS` + `LETTERS`: rewrite from master.md facts only, every number verbatim.
   - `docs.py` `ESSAY_STORIES`: leave empty unless the user dictates real stories.
   - `score.py` `TARGET`, `LA_LOCAL_RX` (their metro), `HAVE_RX`/`LACK_RX`, seniority comment;
     `discover.py` LinkedIn location; `companies.json` tier-1 list.
5. **Verify.** All must pass before any real application:
   ```bash
   cd ~/jobapply && python3 docs.py build && python3 docs.py demo && python3 score.py selftest \
     && python3 adapters.py && python3 test_answer_rules.py && python3 db.py selftest
   ```
   Show the user `docs/variants/*.pdf` — every variant must be 1 page and read right.
6. **Connect tools.** app-filler needs the Claude in Chrome extension (the user's logged-in Chrome);
   inbox-watcher needs a Gmail connector. Run Claude Code from `~/jobapply` for the daily loop.

## Daily loop

From `~/jobapply` (`./run.sh` wraps each step; inside a session, run the same thing directly):

| step | command | notes |
|---|---|---|
| discover | `./run.sh discover` | or dispatch the **job-scout** subagent |
| score | `./run.sh score` | or **job-scorer** (also audits misclassification) |
| tailor | `./run.sh tailor` | **app-tailor**, one uid per subagent |
| apply | `./run.sh apply` | **app-filler**, one at a time, ≥60s apart |
| inbox | `./run.sh inbox` | **inbox-watcher**, 2–3×/day |
| status | `./run.sh status` | `needs_human` is the only number that needs action |
| dashboard | `./run.sh dashboard` | writes `dashboard.html` |

`python3 db.py --help` is the state contract; every status write goes through `db.py`.

## Gates — where it stops on purpose (never work around these)

- `limits.auto_submit: false` (the default) → every application stops at a verified draft;
  `db.py claim <uid> --approved` only on the user's explicit go-ahead for that uid.
- Tier-1 firms (`companies.json` tier 1) always stop at a draft, whatever auto_submit says.
- CAPTCHA, unknown question, or any field mismatch → `needs_human`. Never solved, never guessed.
- SSN / DOB / passport / bank details asked pre-offer → abort, `fraud_flag`. Probable scam.
- Workday/iCIMS/Handshake without an existing account → `manual`. Never create accounts or
  type passwords.
- Email: drafts only. Nothing is ever auto-sent.
- Caps: ≤2 submits/company/week (a Greenhouse spam flag is per-employer and permanent).
