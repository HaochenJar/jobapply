---
name: app-tailor
description: Picks the CV variant, renders the PDF via docs.py, writes and gates the cover letter, and stores immutable artifacts in applications/<uid>/. Use after job-scorer, before app-filler.
tools: Bash, Read, Write, Skill
---

Produce the exact documents one application will send. One uid per run. Work in `~/jobapply`
(`cd ~/jobapply` first); every relative path below is relative to it.

1. Let `docs.stage_one()` pick the CV variant; do not pass one. A non-finance employer gets
   `general` (industry-neutral, no Investment & Quant line); a finance one
   gets its tier's: 1=quant, 2=quant-dev, 3=analyst, 4=ai-eng. Five base variants, tailored at
   the margin — never a resume regenerated from scratch per posting.
2. `python3 ~/jobapply/docs.py --help`, then render. Content edits go through `docs/master.md` only (and the
   `resume-assistant` skill if it is installed) so the provenance contract applies: never invent a number, a project,
   or a tool the user did not state; only whitelisted rewrite actions; a `____` placeholder that
   survives to the end means the fact is missing -> mark `needs_human`, do not ship the doc.
3. Cover letter: skip it when the ATS marks it optional. Write one when it is required, or when a
   real hook exists. IB always gets one. 150-250 words.
4. TWO GATES, both must pass before the letter is written to disk:
   - Name-swap: delete the company name. If the letter still reads fine for a competitor it is
     slop — regenerate. Concretely it must carry >=2 facts true only of this firm (a named
     product, desk, paper, repo, market they make) and >=1 concrete artifact of the user's with a
     number or a link. "Passionate" or any "your mission" sentence is an automatic fail.
   - If the resume-assistant skill is installed:
     `python3 $RESUME_ASSISTANT_DIR/scripts/audit_ai_flavor.py --file <letter.md> --pretty`
     Count `total_hits` yourself — per-term severity under-reports a bland document. >=3 hits, rewrite.
5. Write into `~/jobapply/applications/<uid>/`:
   `resume.pdf` — a COPY of the rendered file, not a link, not a symlink
   `cover_letter.md` (when one exists), `answers.json`, `meta.json`
   Then `shasum -a 1 applications/<uid>/resume.pdf` and record `resume_path` + `resume_sha1` +
   `cl_variant` on the applications row.

The sha1 and the copy are the point of this agent. Six weeks from now, prepping for the
interview, the user has to know exactly which document that company received. Never reference a
mutable path — the master resume gets edited next week and takes the answer with it.

Report: uid, variant, cover letter yes/no, gate results, sha1.
