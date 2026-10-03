"""Resume + cover-letter pipeline (stdlib only; shells out to pandoc + Chrome).

HARD PROVENANCE RULE (inherited from the resume-assistant skill):
never invent a number, a title, a date, an employer, or a technology that is
not in master.md. Variants REORDER and SUBSET master bullets and reweight the
skills block; cover letters fill fixed slots with facts already in master.md.
No sentence about the user is model-generated at run time.

PERSONALIZE (setup does this with you): PLANS, ARTIFACTS, LETTERS and ESSAY_STORIES below
ship tuned to the fictional docs/master.example.md (Alex Rivera). Rewrite them against YOUR
docs/master.md, then `python3 docs.py build && python3 docs.py demo`. Until ARTIFACTS match
your master.md, cover_letter() refuses to write anything.

CLI:  python3 docs.py import <resume.docx> | build | demo
"""
import json, os, re, shutil, subprocess, sys, time, zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
MASTER = DOCS / "master.md"
VARIANTS = DOCS / "variants"
# Optional: the resume-assistant skill supplies an ATS-safe stylesheet and the AI-flavor audit
# used by slop_ok(). Without it, pandoc's default style is used and the audit is skipped.
SKILL = Path(os.environ.get("RESUME_ASSISTANT_DIR",
                            "~/.resume-assistant")).expanduser()
CSS = SKILL / "assets/themes/ats-safe.css"
AUDIT = SKILL / "scripts/audit_ai_flavor.py"
PANDOC = shutil.which("pandoc") or "pandoc"
CHROME = os.environ.get("CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
# density override on top of ats-safe.css — whitespace only, no font/layout change ATS cares about
TIGHTEN = ("<style>@page{margin:12.7mm 15mm}body{font-size:10pt;line-height:1.32}"
           "h2{margin:7pt 0 2pt}h3{margin:4pt 0 1pt}"
           "p{margin:2pt 0}h1+p{margin:0 0 8pt}ul{margin:2pt 0 3pt;padding-left:14pt}"
           "li{margin:1pt 0;line-height:1.34}</style>")
MONTH = r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
SECTIONS = ("Education", "Skills", "Professional Experience", "Additional Information")


# ---------------------------------------------------------------- 1. import
def import_master(src, out=MASTER):
    """docx -> normalized master.md. Strips <u>/link wrappers, adds headings.

    Expects the docx to open with the bold name, then section titles from SECTIONS in bold.
    Always eyeball the result against docs/master.example.md -- it is the format every
    other function here parses."""
    raw = subprocess.run([PANDOC, str(src), "-t", "gfm"], capture_output=True, text=True,
                         check=True).stdout
    raw = re.sub(r"\[<u>(.+?)</u>\]\(.+?\)", r"\1", raw)      # [<u>x</u>](url) -> x
    raw = re.sub(r"</?u>", "", raw)
    lines = []
    for b in re.split(r"\n\s*\n", raw.strip()):
        b = re.sub(r"\n(?!\s*-\s)\s*", " ", b.strip())         # unwrap soft-wrapped block
        if not lines and re.fullmatch(r"\*\*[^*]+\*\*", b):   # the docx opens with the bold name
            lines = [f"# {b.strip('*')}", ""]
            continue
        if b.strip("*") in SECTIONS and b.startswith("**"):
            lines += ["", "## " + b.strip("*"), ""]
        elif b.startswith("- "):
            lines.append(re.sub(r"^-\s+", "- ", b))
        elif (m := re.match(r"^\*\*([^:]+?)\*\*\s+([A-Z][\w .]*,\s*[A-Z][\w .]*)$", b)):
            lines += ["", f"### {m.group(1)} — {m.group(2)}"]
        elif b.startswith("*") and not b.startswith("**"):
            lines.append(re.sub(rf"\s+((?:Expected )?{MONTH} \d{{4}})", r" · \1", b, count=1))
        else:
            lines += ["", b] if b.startswith("**") else [b]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip() + "\n"
    # Re-importing OVERWRITES master.md, including any hand fixes made since the last import
    # (confirmed live: an import silently reverted a corrected skills line). Fix the .docx
    # itself, or re-apply the fix after import.
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    return out


def parse_master(md=MASTER):
    """master.md -> {'head': [str], 'Education': [(kind, text)], ...}"""
    doc, cur = {"head": []}, "head"
    for ln in md.read_text().splitlines():
        if ln.startswith("## "):
            cur = ln[3:].strip()
            doc[cur] = []
        elif ln.strip():
            kind = "h3" if ln.startswith("### ") else "li" if ln.startswith("- ") else "p"
            doc[cur].append((kind, ln[4:] if kind == "h3" else ln[2:] if kind == "li" else ln))
    return doc


# -------------------------------------------------------------- 2. variants
# bullet indices per employer, in the order they should appear. Subset+reorder only.
# Keys are the START of each "### Employer — City" heading in master.md; skills keys are the
# start of each **Skills** line. PERSONALIZE: these match docs/master.example.md.
PLANS = {
    "quant":    {"skills": ["Investment", "Technical", "AI", "Languages"],
                 "b": {"Northwind": [0, 1, 2], "Contoso": [1, 0, 2], "Fabrikam": [0],
                       "Tailspin": [0]}},
    "quantdev": {"skills": ["Technical", "AI", "Investment", "Languages"],
                 "b": {"Northwind": [3, 1, 0], "Contoso": [0, 1, 2], "Fabrikam": [0],
                       "Tailspin": [0]}},
    "analyst":  {"skills": ["Investment", "Technical", "AI", "Languages"],
                 "b": {"Northwind": [2, 0, 4], "Contoso": [3, 0], "Fabrikam": [1],
                       "Tailspin": [0]}},
    "aieng":    {"skills": ["AI", "Technical", "Investment", "Languages"],
                 "b": {"Northwind": [3, 4, 1], "Contoso": [0, 2], "Fabrikam": [1],
                       "Tailspin": [0]}},
    # Non-finance employers (score.is_finance() false): no Investment & Quant line,
    # analytics/automation/operations bullets. A hand-picked docs/variants_src/general.md,
    # if present and provenance-clean, overrides these indices.
    "general":  {"skills": ["Technical", "AI", "Languages"],
                 "b": {"Northwind": [3, 4, 0], "Contoso": [0, 2], "Fabrikam": [1],
                       "Tailspin": [0]}},
}


# -------------------------------------------------------- 2b. per-JD tailoring
# ATS keyword scanners and a skimming recruiter both key off the same surface: exact tool/
# skill terms and short phrases repeated in the JD. Unlike jd_tokens() above (which deliberately
# EXCLUDES generic skill words because a cover-letter hook must be true only of one employer),
# here the generic words ARE the signal -- "Python", "RAG", "Monte Carlo" are exactly what a
# keyword scanner and a recruiter's eye are both looking for. This never invents a bullet: it
# only picks and orders bullets ALREADY in master.md (or an approved variants_src override),
# so provenance_ok() passes by construction -- same lines, different subset/order.
JD_STOP = {
    "the","a","an","and","or","to","of","in","on","for","with","at","as","by","is","are","be",
    "will","this","that","you","your","we","our","us","who","what","how","why","team","role",
    "job","company","work","working","years","year","experience","strong","excellent","ability",
    "including","across","other","also","etc","from","into","over","per","new","all","any",
    "such","have","has","been","being","their","they","them","it","its","not","more","most",
}


def jd_bag(jd_text):
    """word/bigram frequency bag from JD text -- lowercased, stopwords dropped, bigrams weighted
    2x since a matched short phrase ("relative value", "risk management") is a stronger signal
    than either word alone."""
    words = [w for w in re.findall(r"[a-z][a-z0-9+#./-]{2,}", (jd_text or "").lower())
             if w not in JD_STOP]
    bag = {}
    for w in words:
        bag[w] = bag.get(w, 0) + 1
    for a, b in zip(words, words[1:]):
        if a not in JD_STOP and b not in JD_STOP:
            bag[f"{a} {b}"] = bag.get(f"{a} {b}", 0) + 2
    return bag


def bullet_score(text, bag):
    words = [w for w in re.findall(r"[a-z][a-z0-9+#./-]{2,}", text.lower()) if w not in JD_STOP]
    s = sum(bag.get(w, 0) for w in set(words))
    s += sum(bag.get(f"{a} {b}", 0) for a, b in zip(words, words[1:]))
    return s


def _bullets_by_employer(doc, keys):
    """{short_key: [(original_index, text), ...]} -- short_key matched the same way
    build_variant() matches an h3 heading to a PLANS['b'] key: text.startswith(key)."""
    out, role, n = {k: [] for k in keys}, None, 0
    for kind, text in doc["Professional Experience"]:
        if kind == "h3":
            role = next((k for k in keys if text.startswith(k)), None)
            n = 0
        elif kind == "li" and role:
            out[role].append((n, text))
            n += 1
    return out


def jd_plan(tier, jd_text, doc=None, base_plan=None):
    """PLANS[tier]-shaped plan, but bullets/skills chosen and ordered by relevance to jd_text
    instead of the fixed default. Same COUNT per employer/skills category as base_plan (or, for
    an override doc with no PLANS entry, the same count already in that doc) -- this never
    changes how much content ships, only which pieces and in what order."""
    doc = doc or parse_master()
    base_plan = base_plan or PLANS[tier]
    bag = jd_bag(jd_text)
    if not bag:                                    # no JD text -> nothing to rank against
        return base_plan
    skills_text = {t.lstrip("*").split(":", 1)[0].split()[0]: t
                   for _, t in doc.get("Skills", [])}
    order = sorted(base_plan["skills"],
                   key=lambda k: -bullet_score(skills_text.get(k, k), bag))
    by_emp = _bullets_by_employer(doc, base_plan["b"].keys())
    b = {}
    for emp, base_idxs in base_plan["b"].items():
        candidates = by_emp.get(emp) or []
        ranked = sorted(candidates, key=lambda nt: -bullet_score(nt[1], bag))
        keep = ranked[:len(base_idxs)] or [(i, "") for i in base_idxs]  # never fewer than before
        b[emp] = [n for n, _ in keep]
    return {"skills": order, "b": b}


def provenance_ok(md_path, master=MASTER):
    """Every non-blank line of a CV must appear verbatim in master.md ('## ' headings excepted).

    This is the whole anti-hallucination contract: to add a fact, edit master.md (which mirrors
    the real .docx), never the variant.
    """
    # Corpus = master.md + docs/sources/*.md. Sources hold facts that are real but not on the
    # .docx (e.g. a work log). Adding one is a deliberate act with a citation header —
    # the contract is still "a fact must exist in a source of record before it reaches a CV".
    m = {l.strip() for l in master.read_text().splitlines() if l.strip()}
    for src in sorted((master.parent / "sources").glob("*.md")):
        m |= {l.strip() for l in src.read_text().splitlines() if l.strip()}
    bad = [l.strip() for l in Path(md_path).read_text().splitlines()
           if l.strip() and l.strip() not in m and not l.startswith("## ")]
    return not bad, bad


def build_variant(tier, doc=None, outdir=VARIANTS, jd_text=None, out_name=None):
    """out_name lets a caller write a per-application file (e.g. f"{tier}_{uid}") without
    touching the shared docs/variants/{tier}.md that other applications fall back to. jd_text,
    when given, reorders/reselects bullets and skills by relevance to that specific posting —
    same bullets, never invented ones (jd_plan()'s whole contract)."""
    name = out_name or tier
    override = MASTER.parent / "variants_src" / f"{tier}.md"
    if override.exists():                       # hand-authored wins — if it passes provenance
        ok, bad = provenance_ok(override)
        if ok:
            outdir.mkdir(parents=True, exist_ok=True)
            dst = outdir / f"{name}.md"
            if not jd_text:
                dst.write_text(override.read_text())
                return dst
            # reorder the override's own bullets by JD relevance; never add/drop any
            odoc = parse_master(md=override)
            identity = {"skills": PLANS[tier]["skills"],
                       "b": {k: [n for n, _ in v]
                             for k, v in _bullets_by_employer(odoc, PLANS[tier]["b"].keys()).items()}}
            return _render_variant(odoc, jd_plan(tier, jd_text, odoc, identity), outdir, name)
        print(f"!!! OVERRIDE REJECTED {override}: {len(bad)} line(s) not in master.md, e.g. "
              f"{bad[0][:80]!r} — add the fact to master.md or drop it. Using generated {tier}.md.",
              file=sys.stderr)
    doc = doc or parse_master()
    plan = jd_plan(tier, jd_text, doc) if jd_text else PLANS[tier]
    return _render_variant(doc, plan, outdir, name)


def _render_variant(doc, plan, outdir, name):
    title = next((t for _, t in doc["head"] if t.startswith("# ")), "")
    head = [t for _, t in doc["head"] if t != title]
    out = [title, ""] + head + ["", "## Education", ""]
    for k, t in doc["Education"]:
        out.append(f"### {t}" if k == "h3" else t)
    out += ["", "## Skills", ""]
    skills = doc["Skills"]
    for key in plan["skills"]:
        out += [t for _, t in skills if t.lstrip("*").startswith(key)]
    out += ["", "## Professional Experience"]
    role = None
    for kind, text in doc["Professional Experience"]:
        if kind == "h3":
            role = next((k for k in plan["b"] if text.startswith(k)), None)
            n = 0
            out += ["", f"### {text}"]
        elif kind == "li":
            if role and n in plan["b"][role]:
                out.append((plan["b"][role].index(n), "- " + text))
            n += 1
        else:
            out.append(text)
    # bullets were tagged with their plan rank; sort each contiguous run by it
    final, run = [], []
    for item in out + [""]:
        if isinstance(item, tuple):
            run.append(item)
        else:
            final += [t for _, t in sorted(run)]; run = []; final.append(item)
    outdir.mkdir(parents=True, exist_ok=True)
    p = outdir / f"{name}.md"
    p.write_text(re.sub(r"\n{3,}", "\n\n", "\n".join(final)).strip() + "\n")
    return p


# --------------------------------------------------------------- 3. exports
def pdf_probe(pdf):
    """(page_count, text_operator_count) from raw PDF bytes. stdlib only -- no pypdf.

    Chrome embeds subset fonts with a private encoding, so the glyphs cannot be
    decoded back to words; the operator count is the honest proxy for "there is a
    real text layer here", which is all the check needs.
    """
    d = Path(pdf).read_bytes()
    blob = b""
    for m in re.finditer(rb"stream\r?\n(.*?)endstream", d, re.S):
        try:
            blob += zlib.decompress(m.group(1))
        except zlib.error:
            pass
    return len(re.findall(rb"/Type\s*/Page[^s]", d)) or 1, len(re.findall(rb"\bT[jJ]\b", blob))


def export_pdf(md_path, out_pdf):
    """Verified path: pandoc -> self-contained html5 -> headless Chrome. Returns (pdf, pages)."""
    # resolve(): Chrome needs an absolute file:// URL below — a relative path renders
    # a "site can't be reached" error page, which is 1 page and passes the count check.
    if not CSS.exists():           # no resume-assistant skill: use the self-contained Word layout
        return export_pdf_docx_style(md_path, out_pdf)
    md_path, out_pdf = Path(md_path).resolve(), Path(out_pdf).resolve()
    html = out_pdf.with_suffix(".html")
    subprocess.run([PANDOC, str(md_path), "--from", "gfm", "--to", "html5", "--css", str(CSS),
                    "--standalone", "--self-contained", "--metadata", "title=resume",
                    "-V", "header-includes=" + TIGHTEN, "-o", str(html)], check=True)
    # drop pandoc's title block, else "Resume" is the first token an ATS reads
    html.write_text(re.sub(r'<header id="title-block-header">.*?</header>', "",
                           html.read_text(), flags=re.S))
    return _chrome_pdf(html, out_pdf, md_path.stem)


def _chrome_pdf(html, out_pdf, stem=""):
    """html file -> pdf via headless Chrome, with the page-count gate. Returns (pdf, pages)."""
    # ponytail: exactly the flags research verified (2.6s). --user-data-dir=<fresh dir> and
    # --virtual-time-budget were tried and HANG this Chrome (151) forever — do not re-add them.
    # Never render two variants concurrently into the same file; page counts go wrong.
    for attempt in range(3):
        try:
            r = subprocess.run([CHROME, "--headless", "--disable-gpu", "--no-pdf-header-footer",
                                f"--print-to-pdf={out_pdf}", f"file://{html}"],
                               capture_output=True, timeout=60)
            if r.returncode == 0 and out_pdf.exists():
                break
        except subprocess.TimeoutExpired:
            pass
        time.sleep(1.5 * (attempt + 1))
    else:
        raise RuntimeError(f"Chrome failed to render {out_pdf.name} after 3 attempts")
    pages, _ = pdf_probe(out_pdf)
    if pages > 1:
        print(f"!!! PAGE OVERFLOW: {out_pdf.name} is {pages} pages — trim PLANS['{stem}'] "
              f"bullets before shipping this to a tier-1 employer.", file=sys.stderr)
    return out_pdf, pages


# ------------------------------------------------- 3b. docx-faithful renderer
# A classic Word-resume layout, not pandoc's default: Times New Roman 9.5pt on Letter, name
# centred with 1.2pt tracking over a ruled contact line, section headings ruled, and — the
# part markdown cannot express — employer/location and title/dates on ONE line with the right
# half flush to the right margin (a Word right-tab). Numbers were read out of a real resume's
# word/document.xml + styles.xml (twips/20 = pt). Content still comes from master.md.
DOCX_CSS = """
@page { size: letter; margin: 28pt 40pt 22.5pt 40pt; }
body { font-family: "Times New Roman", Times, serif; font-size: 9.5pt; line-height: 1.15;
       color: #000; margin: 0; }
h1 { font-size: 20pt; font-weight: bold; text-align: center; letter-spacing: 1.2pt;
     margin: 0 0 3.3pt; }
header { display: flex; align-items: center; gap: 14pt; }
.hd-text { flex: 1; min-width: 0; }
.contact { font-size: 9pt; text-align: center; border-bottom: 1pt solid #000;
           padding-bottom: 6pt; margin: 0 0 4pt; }
.contact u { text-decoration: underline; }
h2 { font-size: 10.5pt; font-weight: bold; border-bottom: 0.5pt solid #000;
     padding-bottom: 3pt; margin: 6.2pt 0 2.5pt; page-break-after: avoid; }
.row { display: flex; justify-content: space-between; align-items: baseline; gap: 12pt;
       page-break-after: avoid; }
.org { font-size: 10.5pt; font-weight: bold; margin: 3.9pt 0 0; }
.role { font-size: 10pt; font-style: italic; margin: 0.2pt 0 0.8pt; }
.role .right { font-size: 9pt; }
.right { white-space: nowrap; }
p { margin: 0 0 1pt; }
ul { margin: 0 0 1pt; padding-left: 12.4pt; }
li { margin: 0 0 1pt; page-break-inside: avoid; }
"""
_DATE = (rf"((?:Expected\s+)?{MONTH}\s+\d{{4}}(?:\s*[–-]\s*(?:Present|{MONTH}\s+\d{{4}}))?"
         r"|(?:预计\s*)?\d{4}\s*年\s*\d{1,2}\s*月(?:\s*[–-]\s*(?:至今|\d{4}\s*年\s*\d{1,2}\s*月))?)"
         r"\**\s*$")


def _inline(s):
    """master.md inline markdown -> html. Escapes first, so ** / * cannot come from content."""
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    return re.sub(r"\*(.+?)\*", r"<em>\1</em>", s)


def _split_role(line):
    """'*Research Analyst · June 2025 – Present*' -> ('<em>Research Analyst</em>', 'June 2025 – Present').

    Two shapes occur in master.md: wholly-italic (title · date), and the undergrad line where
    only parts are italic ('*Bachelor of Science in Mathematics* · GPA 3.86 / 4.0 *May 2025*').
    """
    body = line.strip()
    whole_italic = body.startswith("*") and body.endswith("*") and "*" not in body[1:-1]
    if whole_italic:
        body = body[1:-1]
    if (m := re.search(_DATE, body)):
        left, right = re.sub(r"[\s·*]+$", "", body[: m.start()]), m.group(1)
    else:                                   # no recognisable date: fall back to the separator
        left, _, right = body.rpartition(" · ")
        left, right = (left, right) if left else (right, "")
    left = f"<em>{_inline(left)}</em>" if whole_italic else _inline(left)
    return left, _inline(right)


# Chinese skin for the same renderer. Three things make a CJK resume read as typeset rather
# than translated, and all three are wrong if you just swap the font into DOCX_CSS:
#   1. no italics — Chinese has no true italic, so font-style:italic is a synthetic slant.
#      Role lines carry hierarchy by weight and colour instead.
#   2. line-height ~1.6, not 1.15 — Han glyphs are full-width and need the leading.
#   3. text-align: justify — CJK has no inter-word spaces, so justification is clean here
#      (it is not in English, where it opens rivers).
ZH_CSS = """
@page { size: A4; margin: 12mm 15mm 11mm; }
body { font-family: "Helvetica Neue", "PingFang SC", "Hiragino Sans GB", "Heiti SC", sans-serif;
       font-size: 9.2pt; line-height: 1.42; color: #2b2b2b; margin: 0;
       -webkit-font-smoothing: antialiased; }
h1 { font-size: 19pt; font-weight: 600; letter-spacing: 1.6pt; color: #12263a; margin: 0 0 4pt; }
.contact { font-size: 7.8pt; color: #6b7684; letter-spacing: .2pt; margin: 0 0 2pt; }
.photo { width: 21mm; flex: none; border: 0.6pt solid #dce1e6; border-radius: 2pt;
         box-sizing: border-box; }
header { display: flex; align-items: center; justify-content: space-between; gap: 16pt; }
.hd-text { flex: 1; min-width: 0; }
h2 { font-size: 10.4pt; font-weight: 600; color: #12263a; letter-spacing: 1.2pt;
     display: flex; align-items: center; gap: 8pt; margin: 6.5pt 0 3.5pt;
     page-break-after: avoid; }
h2::after { content: ""; flex: 1; height: 0.6pt; background: #d9dee4; }
header + h2 { margin-top: 9pt; }
.row { display: flex; justify-content: space-between; align-items: baseline; gap: 12pt;
       page-break-after: avoid; }
.org { font-size: 10.2pt; font-weight: 600; color: #12263a; margin: 5.5pt 0 0; }
.org .right { font-size: 8.8pt; font-weight: 400; color: #6b7684; letter-spacing: .3pt; }
.role { font-style: normal; font-size: 9pt; color: #55606c; margin: 0.5pt 0 2pt; }
.role em { font-style: normal; }
.role .right { font-size: 8.5pt; color: #8a929c; font-variant-numeric: tabular-nums; }
.right { white-space: nowrap; }
p { margin: 0 0 2.5pt; text-align: justify; }
strong { font-weight: 600; color: #12263a; }
ul { margin: 0 0 2pt; padding-left: 0; list-style: none; }
li { position: relative; padding-left: 11pt; margin: 0 0 1.3pt; text-align: justify;
     page-break-inside: avoid; }
li::before { content: "•"; position: absolute; left: 2pt; color: #2f6da8; }
"""


def export_pdf_docx_style(md_path, out_pdf, css=None):
    """master.md (or any variant) -> PDF laid out like a classic Word resume (DOCX_CSS).

    css=ZH_CSS renders the same markdown with the Chinese skin instead.
    """
    css = DOCX_CSS if css is None else css
    md_path, out_pdf = Path(md_path).resolve(), Path(out_pdf).resolve()
    lines = [l.rstrip() for l in md_path.read_text().splitlines()]
    out, ul = [], False
    head = {"photo": "", "h1": "", "contact": ""}

    def close_ul():
        nonlocal ul
        if ul:
            out.append("</ul>")
            ul = False

    for line in lines:
        if not line.strip():
            continue
        if line.startswith("<img"):
            head["photo"] = line                  # headshot (Chinese skin)
        elif line.startswith("# "):
            head["h1"] = f"<h1>{_inline(line[2:])}</h1>"
        elif line.startswith("## "):
            close_ul()
            out.append(f"<h2>{_inline(line[3:])}</h2>")
        elif line.startswith("### "):
            close_ul()
            org, _, loc = line[4:].partition(" — ")
            out.append(f'<div class="row org"><span>{_inline(org)}</span>'
                       f'<span class="right">{_inline(loc)}</span></div>')
        elif line.startswith("- "):
            if not ul:
                out.append("<ul>")
                ul = True
            out.append(f"<li>{_inline(line[2:])}</li>")
        elif head["h1"] and not head["contact"]:
            # the one line under the name: phone · email · linkedin · site, links underlined
            parts = [p.strip() for p in line.split("·")]
            cells = [p if i == 0 else f"<u>{p}</u>" for i, p in enumerate(parts)]
            head["contact"] = ('<p class="contact">'
                               + "&nbsp; &middot; &nbsp;".join(cells) + "</p>")
        elif line.startswith("*") and not line.startswith("**"):
            # single leading * = a role/degree line. ** = a bold-label paragraph (Skills etc).
            close_ul()
            left, right = _split_role(line)
            out.append(f'<div class="row role"><span>{left}</span>'
                       f'<span class="right">{right}</span></div>')
        else:
            close_ul()
            out.append(f"<p>{_inline(line)}</p>")
    close_ul()
    # The headshot must NOT float: a float only narrows the lines it overlaps, so the
    # right-aligned locations/dates beside it landed short of the ones below it. A flex
    # header keeps the photo out of the body flow and every right cell on one margin.
    out.insert(0, f'<header><div class="hd-text">{head["h1"]}{head["contact"]}</div>'
                  f'{head["photo"]}</header>')

    html = out_pdf.with_suffix(".html")
    html.write_text(f"<!doctype html><html><head><meta charset='utf-8'><style>{css}"
                    f"</style></head><body>{''.join(out)}</body></html>")
    return _chrome_pdf(html, out_pdf, md_path.stem)


def export_docx(md_path, out):
    subprocess.run([PANDOC, str(md_path), "--from", "gfm", "--to", "docx", "-o", str(out)],
                   check=True)
    return Path(out)


# --------------------------------------------------- 4. cover letters + gates
# PERSONALIZE. Every artifact below is a verbatim fragment of master.md and carries a number.
# These match docs/master.example.md; cover_letter() refuses to run until they match YOURS.
ARTIFACTS = {
    "quant": "returned 18% over 12 months against an 8% maximum drawdown",
    "quantdev": "summarizes filings and earnings calls for 40+ covered companies",
    "analyst": "returned 18% over 12 months against an 8% maximum drawdown",
    "aieng": "summarizes filings and earnings calls for 40+ covered companies",
}
SIGNOFF = "\n\nSincerely,\n{name}\n{phone} · {email}\n"
# PERSONALIZE. Hand-written once per variant, every fact and number taken from master.md
# (demo() checks the numbers). Slots: {company} {role} {hook1} {hook2} {artifact}.
LETTERS = {
"quant": """Dear {company} Hiring Team,

I am writing to apply for the {role} role. What draws me to the seat is specific: {hook1} and {hook2} are where my research hours already go.

At Northwind Capital I work as a research analyst on a systematic equity process. I built a screening model covering 300+ US mid-cap names and ran a paper portfolio of 20 long/short positions that {artifact}. The same discipline runs through my research notes on consumer retail and logistics: a thesis is a sized position with a catalyst, not an opinion. At Contoso Analytics I ran Monte Carlo simulations to stress-test a $2M budget allocation, which is where I learned to treat tail risk as a number rather than a footnote.

I complete a Master of Quantitative Finance in May 2027, after a BS in Mathematics. The work I do on {hook1}, from factor exposure to drawdown control, is the same work this role asks for.

I would welcome the chance to walk through the portfolio and the screening model in detail.""",

"quantdev": """Dear {company} Hiring Team,

I am writing to apply for the {role} position. {hook1} and {hook2} are the parts of the posting I want to work on, and they line up with what I build daily.

At Northwind Capital I designed a Python research-agent pipeline with RAG retrieval that {artifact}. I also automated the daily risk report in SQL and Python, replacing 2 hours of manual spreadsheet work each morning, and built a screening model covering 300+ US mid-cap names. At Contoso Analytics I built a churn-prediction model that lifted retention-campaign precision by 15%, and joined 10M+ rows of transaction data in SQL to feed weekly KPI dashboards.

I finish a Master of Quantitative Finance in May 2027; my BS in Mathematics included coursework in time series analysis and machine learning.

I would be glad to walk your team through the pipeline code, including where {hook1} would slot in.""",

"analyst": """Dear {company} Hiring Team,

I am writing to apply for the {role} role. My reason is narrow: {hook1} and {hook2} describe the work I already do.

At Northwind Capital I write catalyst-driven research notes on consumer retail and logistics for the investment committee, and I ran a paper portfolio of 20 long/short positions that {artifact}. Behind the notes sits a screening model covering 300+ US mid-cap names that cut idea-generation time by 40%. At Fabrikam Partners I built DCF and trading-comps models for 2 sell-side pitches and drafted 15 pages of a confidential information memorandum for a $120M acquisition.

I finish a Master of Quantitative Finance in May 2027, after a BS in Mathematics.

I would welcome a conversation about how that research process applies to {hook1}.""",

"aieng": """Dear {company} Hiring Team,

I am writing to apply for the {role} position. {hook1} and {hook2} are the specific problems I want to work on.

At Northwind Capital I designed a Python research-agent pipeline with RAG retrieval that {artifact}. Building it meant multi-agent workflows, prompt engineering, RAG retrieval and LLM evaluation in a domain where a wrong answer costs money, so it is built around evidence retrieval rather than free-form generation. In the same role I automated the daily risk report in SQL and Python, replacing 2 hours of manual work each morning. At Contoso Analytics I built a churn-prediction model that lifted campaign precision by 15%.

I work in Python, SQL, and R, and finish a Master of Quantitative Finance in May 2027, after a BS in Mathematics.

I would be glad to walk your team through the agent pipeline, {hook1} included.""",
}
GENERIC = {w.lower() for w in """A An The We Our Your You I In On At For And Or But As Is Are Be To Of With By From
About Role Position Job Jobs Team Teams Company Firm Group Department About Us Who What Why How When Where
Responsibilities Requirements Qualifications Preferred Required Basic Minimum Skills Experience Education
Bachelor Master PhD Degree Years Year Strong Excellent Good Great Ability Able Work Working Working Environment
Equal Opportunity Employer Benefits Salary Compensation Range Location Remote Hybrid Onsite Full Time Part
Please Apply Applicants Candidate Candidates Successful Ideal Note Description Overview Summary Mission Values
Monday Friday New York San Francisco Los Angeles London United States US USA English Yes No
Join Build Help Lead Manage Develop Support Drive Own Design Create Ensure Collaborate Partner Deliver
Looking Seeking Hiring This These Their They It If As While Here There Must Should Will Can May""".split()}


def jd_tokens(jd_text, company):
    """Proper nouns from the JD that could only be said about THIS company.

    Sentence-initial capitals and JD boilerplate ("Who We Are", "What You'll Do") are noise,
    not facts — an earlier version fed them straight into a letter and produced
    "the catalyst-driven work I do on Who We Are Founded". Two filters do the work:
    a token must appear MID-SENTENCE (real proper nouns do) and must repeat (a named desk,
    product or market gets mentioned more than once; a stray capital does not).

    Returns [] when nothing survives — the caller then skips the letter rather than
    shipping a generic one, which is the correct trade.
    """
    STOP = {"who","we","are","our","you","your","the","a","an","what","how","why","when",
            "join","about","description","overview","summary","role","team","company","group",
            "position","job","apply","requirements","qualifications","responsibilities",
            "benefits","equal","opportunity","employer","compensation","salary","location",
            "remote","hybrid","onsite","full","time","part","this","that","is","in","of","and",
            "or","to","for","with","at","on","as","by","it","us","will","do","have","be",
            "must","should","may","can","please","note","new","york","san","francisco","london",
            "chicago","boston","seattle","austin","llc","inc","ltd","lp"}
    cw = set(re.findall(r"[a-z]+", company.lower()))

    # kill markdown/HTML-ish headings and list bullets before scanning
    text = re.sub(r"(?m)^\s*[#*\-\u2022]+\s*", "", jd_text)
    text = re.sub(r"\s+", " ", text)

    counts, midsentence = {}, set()
    for m in re.finditer(r"\b[A-Z][A-Za-z0-9+#.&/-]*(?:[ -][A-Z][A-Za-z0-9+#.&/-]*){0,3}", text):
        t = m.group(0).strip(" -.")
        words = re.findall(r"[a-z]+", t.lower())
        if len(t) < 4 or not words:
            continue
        if all(w in STOP for w in words) or any(w in STOP for w in words):
            continue                                    # any function word => not a proper noun
        if set(words) & cw:
            continue                                    # the company's own name
        counts[t] = counts.get(t, 0) + 1
        prev = text[max(0, m.start() - 2):m.start()]
        if m.start() > 0 and not re.search(r"[.!?:;]\s$|^$", prev):
            midsentence.add(t)                          # a capital that is not sentence-initial

    # A hook must be true of THIS firm. "Python", "Mathematics", "Computer Science" are true of
    # every quant shop on the list, so they fail the name-swap test by construction — an earlier
    # version slotted them in and produced letters indistinguishable across employers.
    INDUSTRY_GENERIC = {
        "python","java","javascript","c","c++","sql","r","matlab","excel","vba","git","linux",
        "mathematics","statistics","physics","computer science","engineering","economics",
        "finance","bachelor","bachelors","master","masters","phd","degree","gpa",
        "develop","design","build","support","assist","manage","operations","proficiency",
        "experience","skills","knowledge","ability","strong","excellent","preferred","required",
        "machine learning","data science","research","analysis","analytics","quantitative",
        "communication","teamwork","leadership","upcoming graduates","december","january"}
    good = [t for t, n in counts.items()
            if t in midsentence and n >= 2
            and t.lower() not in INDUSTRY_GENERIC
            and not all(w in INDUSTRY_GENERIC for w in t.lower().split())]
    return sorted(good, key=lambda t: (-counts[t], -len(t.split())))


def name_swap_ok(draft, job, hooks=None):
    """Delete the company name; what's left must still be true only of THIS company.

    Checks the RESEARCHED hooks actually survived into the prose. Re-scraping the JD here was
    the bug that let "Arbitration" and "Work/Life" pass as company-specific facts.
    """
    # CONFIRMED LIVE, repeatedly: splitting the company name into individual words and
    # stripping each one GLOBALLY corrupts unrelated text that happens to share an ordinary
    # word with it -- "Summit Asset Management" strips "Asset" and "Management" out of the
    # candidate's own employer line, "Acme Home" strips "homes" out of a real numeric fact.
    # Agent sessions kept working around it by hand instead of trusting the gate. Strip
    # the company name as the PHRASE it actually is, not its constituent words everywhere.
    stripped = re.sub(re.escape(job["company"]), "", draft, flags=re.I)
    toks = [t for t in (hooks or []) if t in stripped]
    arts = [a for a in ARTIFACTS.values() if a in stripped and re.search(r"\d", a)]
    ok = len(toks) >= 2 and len(arts) >= 1
    return ok, f"company-specific tokens={toks[:4]} artifacts={len(arts)}"


MAX_SLOP_HITS = 3


def slop_ok(draft):
    if "passionate" in draft.lower():
        return False, "banned word: passionate"
    for s in re.split(r"(?<=[.!?])\s+", draft):
        if "your mission" in s.lower():
            return False, "banned phrase: your mission"
    if not AUDIT.exists():                 # optional resume-assistant skill not installed
        return True, "ai_flavor audit skipped (resume-assistant not installed)"
    report = json.loads(subprocess.run([sys.executable, str(AUDIT), "--text", draft],
                                       capture_output=True, text=True).stdout)
    # CONFIRMED LIVE: summary.total_hits counts DISTINCT flagged terms, not total
    # occurrences (3 em-dashes + 1 "architected" reports total_hits=2, not 4), so it
    # undercounted. The gate should count what it claims to count. Sum the raw per-term counts instead.
    hits = sum(h["count"] for h in report.get("hits", []))
    return hits <= MAX_SLOP_HITS, f"ai_flavor total_hits={hits}"


def cover_letter(job, profile, variant, hooks=None):
    """-> markdown, or None when the letter is skipped.

    `hooks` must be RESEARCHED facts about this employer — a named desk, product, paper, market
    they make — supplied by the caller (the app-tailor agent, which has get_company_data and the
    web). They are never scraped from the JD: three attempts at that produced "the catalyst-driven
    work I do on Who We Are Founded", then "Python", then "Arbitration" and "Work/Life". JD prose
    is boilerplate plus industry-generic requirements, and no regex separates a fact about THIS
    firm from either. Without real hooks the letter is skipped, which is the correct trade — a
    visibly templated letter is worse than no letter.

    job: {company, title, jd_text, cover_letter: required|optional|none}
    """
    need = job.get("cover_letter", "optional")
    if need == "none" or (need == "optional" and variant != "quant"):
        return None
    if variant not in LETTERS:     # no template (e.g. general): a required letter -> human queue
        return None
    if ARTIFACTS[variant] not in MASTER.read_text():
        print("cover_letter refused: ARTIFACTS/LETTERS in docs.py are not personalized to "
              "docs/master.md yet", file=sys.stderr)
        return None
    if not profile:
        import adapters                                 # lazy: only letters need the profile
        profile = adapters.load_profile()
    hooks = hooks or []
    if len(hooks) < 2:
        return None                                    # nothing specific to say -> human queue
    draft = (LETTERS[variant] + SIGNOFF).format(
        company=job["company"], role=job["title"], hook1=hooks[0], hook2=hooks[1],
        artifact=ARTIFACTS[variant], name=profile.get("full_name", ""),
        phone=profile.get("phone", ""), email=profile.get("email", ""))
    for gate in (name_swap_ok(draft, job, hooks), slop_ok(draft)):
        if not gate[0]:
            print(f"cover_letter rejected ({job['company']}): {gate[1]}", file=sys.stderr)
            return None
    words = len(draft.split())
    if not 150 <= words <= 250:
        print(f"cover_letter rejected ({job['company']}): {words} words", file=sys.stderr)
        return None
    return draft


# PERSONALIZE. Fixed, hand-vetted story fragments for the essay-question categories that
# recur across applications, 25-100 words each. They are NOT generated per application (same
# house rule as LETTERS/ARTIFACTS above: no sentence about the user is model-generated at run
# time), so each must be a true story you wrote, ideally cited to a file in docs/sources/.
# Self-narrative categories (proud_project, biggest_challenge, time_wrong, ambiguous_tools)
# need no employer-specific hook. why_role is the exception -- it's the one category that's
# actually about THIS employer, so it needs a real researched hook, prepended at run time.
# Ships EMPTY on purpose: a missing category returns None, i.e. the human queue. Example:
#   "proud_project": "One project I'm proud of: <what you built, the number it moved, what
#                     you learned when it didn't go to plan> ...",
#   "why_role": "At <employer> I <what you do daily that this role needs> ...",
ESSAY_STORIES = {}
# question text -> category, first pattern to match wins. Same first-match-wins discipline as
# adapters/_common.json's answer_rules -- an unrecognized question is not this function's to
# guess, it stays needs_human.
ESSAY_CATEGORY_RULES = (
    ("time_wrong", r"time you (were wrong|failed|changed your mind)|mistake you|"
                   r"learned from a failure|caught your own mistake"),
    ("biggest_challenge", r"biggest (technical )?challenge|hardest problem|most difficult"),
    ("proud_project", r"proud of|favorite project|project you\b|best work|accomplishment|"
                      r"exceptional work|describe.*(project|system) you|"
                      r"(system|something) you (built|worked on)|link to.*(project|work)"),
    ("ambiguous_tools", r"ambiguous|approach to (building|solving)|how (do |would )?you build|"
                        r"tools? you'?ve built|tool you (built|made|wrote)|"
                        r"(internal )?tool you built or"),
    # Under-broad matching here is expensive: a why-role question that fails to classify
    # exits 2 and becomes a needs_human row EVEN WHEN good researched hooks were supplied.
    # "What made you apply to this role at Acme?" was one such live miss.
    ("why_role", r"why (do you want|are you interested|.*join)|excites you|"
                r"what draws you|what is your interest in|excited to work on|"
                r"tell us why you|what (made|makes) you (want to )?(apply|join)|"
                r"why did you apply|why (this|our) (company|team|role)|"
                r"what (attracted|appeals to) you|motivation (to|for) join|"
                r"(describing|describe) your interest|your interest in (us|this|working|the )|"
                r"interested in (this role|working)|"
                r"why (is|are) \w+ the right (place|fit|team|company)|"
                r"the right place for you|why (us|here)\b"),
)


def classify_essay_question(question):
    ql = question.lower()
    for category, pattern in ESSAY_CATEGORY_RULES:
        if re.search(pattern, ql):
            return category
    return None


def essay_answer(question, job, hooks=None):
    """-> plain text answer, or None (needs_human -- the safe default).

    `question` is the exact on-page question text. `job` is {company, title}. `hooks` are
    researched, employer-specific facts (same contract as cover_letter's hooks) -- required
    only for why_role, since that's the one category that's actually about the employer.
    """
    category = classify_essay_question(question)
    # Bare "Why <Company>?" is common shorthand and always means why_role, but a plain regex
    # can't safely recognize an arbitrary company name -- check it against the one we were
    # actually told, not a fixed word list.
    if category is None and job.get("company") and re.match(
            r"^\s*why\s+" + re.escape(job["company"].split()[0]) + r"\b", question, re.I):
        category = "why_role"
    if category is None:
        return None
    story = ESSAY_STORIES.get(category)
    if story is None:
        return None                                    # not personalized yet -> human queue
    if category == "why_role":
        hooks = hooks or []
        if not hooks:
            return None                                # nothing specific to say -> human queue
        hook = hooks[0][:1].upper() + hooks[0][1:]     # sentence-initial, whatever case the
                                                        # researched hook string came in as
        draft = f"{hook} is exactly the kind of problem I want to work on. {story}"
    else:
        draft = story
    if not slop_ok(draft)[0]:
        print(f"essay_answer rejected ({job.get('company')}, {category}): "
              f"{slop_ok(draft)[1]}", file=sys.stderr)
        return None
    words = len(draft.split())
    if not 25 <= words <= 100:
        print(f"essay_answer rejected ({job.get('company')}, {category}): {words} words",
              file=sys.stderr)
        return None
    return draft


# ------------------------------------------------------------------ 5. demo
def demo():
    m = MASTER.read_text()
    for a in ARTIFACTS.values():
        assert a in m, f"artifact not in master.md: {a}"          # provenance
    # Every figure in a letter template must exist in master.md. The templates are static
    # and hand-authored, so this is what stops an edit from quietly inventing a number.
    for tmpl in LETTERS.values():
        for n in re.findall(r"\$?\d[\d,]*(?:\.\d+)?\s*(?:%|[KMB]\b|\+)?", tmpl):
            n = n.strip().rstrip(",")
            assert n in m, f"letter cites {n!r}, which is not in master.md"
    for tier in PLANS:
        ok, bad = provenance_ok(VARIANTS / f"{tier}.md")
        assert ok, f"{tier}.md invented: {bad[:2]}"

    fake = VARIANTS / "_provenance_probe.md"                       # a fabricated line is caught
    fake.write_text("# Alex Rivera\n\n- Managed a $40B book at a macro fund.\n")
    assert not provenance_ok(fake)[0]
    fake.unlink()

    # per-JD tailoring: never invents, always same bullet COUNT per employer, actually reorders
    doc = parse_master()
    jd_quant = ("Quantitative Researcher role focused on options and volatility. Monte Carlo "
                "simulation, relative-value arbitrage, sentiment analysis, macro views, risk "
                "and drawdown management required. Options, volatility, options, volatility.")
    p1 = jd_plan("quant", jd_quant, doc)
    assert p1 != PLANS["quant"], "a strongly-matching JD must actually change the plan"
    for emp, idxs in PLANS["quant"]["b"].items():
        assert len(p1["b"][emp]) == len(idxs), f"{emp}: bullet count must not change"
        assert set(p1["b"][emp]) <= set(range(10)), "indices must stay real bullet indices"
    assert sorted(p1["skills"]) == sorted(PLANS["quant"]["skills"]), "must not invent a category"
    assert jd_plan("quant", "", doc) == PLANS["quant"], "empty JD -> unchanged default plan"
    tailored_md = build_variant("quant", doc, jd_text=jd_quant, out_name="_jd_probe")
    ok, bad = provenance_ok(tailored_md)
    assert ok, f"JD-tailored variant invented content: {bad[:2]}"
    tailored_md.unlink()

    # override doc (aieng) + JD: reorders its own bullets, never adds/drops/invents
    if (MASTER.parent / "variants_src" / "aieng.md").exists():
        jd_ai = "LLM evaluation harnesses and multi-agent orchestration experience required, plus RAG pipelines."
        tailored_override = build_variant("aieng", jd_text=jd_ai, out_name="_jd_probe_aieng")
        ok, bad = provenance_ok(tailored_override)
        assert ok, f"JD-tailored override invented content: {bad[:2]}"
        base_lines = {l.strip() for l in (MASTER.parent / "variants_src" / "aieng.md")
                     .read_text().splitlines() if l.strip().startswith("- ")}
        tailored_lines = {l.strip() for l in tailored_override.read_text().splitlines()
                          if l.strip().startswith("- ")}
        assert base_lines == tailored_lines, "JD reorder must not add or drop a bullet from the override"
        tailored_override.unlink()

    job = {"company": "Citadel Securities", "title": "Quantitative Researcher",
           "jd_text": "You will join the Options Market Making desk in Chicago, working on "
                      "volatility surface calibration in C++ and Python alongside the Fixed Income "
                      "team. Citadel Securities is an equal opportunity employer.",
           "cover_letter": "required"}
    HOOKS = ["Options Market Making", "volatility surface calibration"]
    good = cover_letter(job, {}, "quant", hooks=HOOKS)
    assert good and name_swap_ok(good, job, HOOKS)[0] and slop_ok(good)[0]
    assert cover_letter(job, {}, "quant") is None, "must refuse without researched hooks"
    assert "Options Market Making" in good and 150 <= len(good.split()) <= 250

    generic = ("Dear Citadel Securities Hiring Team,\n\nI am excited to apply. I am a strong "
               "team player who thrives in fast-paced environments and would love to bring my "
               "skills to your firm. I am confident I would be a great fit.\n\nSincerely,\nAlex")
    assert not name_swap_ok(generic, job, HOOKS)[0], "name-swap test failed to reject slop"
    assert not slop_ok("I am passionate about markets.")[0]

    # FIXED: splitting the company name into words and stripping each one GLOBALLY used to
    # corrupt unrelated text sharing an ordinary word with the company name.
    swap_job = {"company": "Summit Asset Management"}
    swap_draft = ("I ran research at Northwind Asset Management, where a paper portfolio "
                  f"{ARTIFACTS['quant']}. Summit Asset Management runs "
                  "the Credit Opportunities desk and manages 20 billion in AUM.")
    assert "Northwind Asset Management" in re.sub(re.escape(swap_job["company"]), "",
                                                     swap_draft, flags=re.I), \
        "stripping the company name must not also strip unrelated text sharing a common word"
    assert name_swap_ok(swap_draft, swap_job,
                        ["Credit Opportunities desk", "20 billion in AUM"])[0]

    # FIXED: slop_ok() read summary.total_hits, which counts DISTINCT flagged TERMS, not total
    # occurrences (3 em-dashes + "architected" reported total_hits=2, not 4). Now sums the raw
    # per-term counts instead.
    dashy = ("I built the pipeline — end to end — and scaled it — carefully — "
            "architected for growth.")
    ok, why = slop_ok(dashy)
    assert ok if not AUDIT.exists() else not ok, \
        f"4 spaced em-dashes + 1 verb-flag must exceed MAX_SLOP_HITS=3: {why}"

    pdf, pages = export_pdf(VARIANTS / "quant.md", VARIANTS / "quant.pdf")
    assert pdf_probe(pdf)[1] > 500, "PDF has no text layer (image-only or blank)"
    assert pages == 1, f"quant.pdf is {pages} pages"

    ej = {"company": "Citadel Securities", "title": "Quantitative Researcher"}
    assert classify_essay_question("Tell us about a time you were wrong") == "time_wrong"
    assert classify_essay_question("What is your biggest technical challenge?") == "biggest_challenge"
    assert classify_essay_question("Describe a project you're proud of") == "proud_project"
    assert classify_essay_question("How do you approach ambiguous problems?") == "ambiguous_tools"
    assert classify_essay_question("Why do you want to join Citadel Securities?") == "why_role"
    assert classify_essay_question("What's your favorite color?") is None, \
        "must not force-match an unrelated question"
    for cat in set(ESSAY_STORIES) - {"why_role"}:
        # self-narrative categories need no hook -- check the fixed story clears its own gates
        a = essay_answer({"proud_project": "project you're proud of",
                          "biggest_challenge": "biggest technical challenge",
                          "time_wrong": "time you were wrong",
                          "ambiguous_tools": "how do you approach ambiguous problems"}[cat], ej)
        assert a and slop_ok(a)[0] and 25 <= len(a.split()) <= 100, (cat, a)
    assert essay_answer("Why do you want to join Citadel Securities?", ej) is None, \
        "why_role must refuse without a researched hook"
    withhook = essay_answer("Why do you want to join Citadel Securities?", ej,
                            hooks=["Options Market Making"])
    assert ("why_role" not in ESSAY_STORIES and withhook is None) or \
        (withhook and "Options Market Making" in withhook)
    assert essay_answer("What's your favorite color?", ej) is None, \
        "unrecognized question must stay needs_human, never guessed"

    # stage_one end-to-end with real JD text: must actually render a tailored, provenance-clean,
    # single-page PDF distinct from the shared quant.pdf -- not just build the intermediate .md.
    probe_job = {"uid": "test:jd_stage_probe", "role_tier": 1,
                "company_name": "Citadel Securities", "title": "Quantitative Researcher",
                "desc_text": jd_quant, "cover_letter": "none"}
    res = stage_one(probe_job)
    try:
        assert res.get("resume_tailored_to_jd") is True, res
        assert pdf_probe(res["resume"])[1] > 500, "tailored PDF has no real text layer"
        assert Path(res["resume"]).read_bytes() != (VARIANTS / "quant.pdf").read_bytes(), \
            "tailored PDF must not be byte-identical to the generic shared variant"
    finally:
        import shutil as _shutil
        _shutil.rmtree(APPS / "test_jd_stage_probe", ignore_errors=True)

    # FIXED 2026-09-03: the already-staged guard used to live only in the CLI `stage` command --
    # a library caller (every app-tailor agent) re-running stage_one() on a queue()-returned row
    # that happened to already be tailored silently overwrote a good resume.pdf/cover_letter.md
    # with no way to recover the old bytes. Confirmed live: a tailor agent did exactly this to 4
    # rows. Guard now lives in stage_one() itself, so no caller can bypass it by accident.
    import sqlite3 as _sqlite3
    guard_uid = "test:stage_guard_probe"
    with _sqlite3.connect(str(ROOT / "jobapply.db")) as _c:
        _c.execute("insert or replace into applications(uid, status) values (?, 'tailored')",
                   (guard_uid,))
    try:
        blocked = stage_one({"uid": guard_uid, "role_tier": 1, "company_name": "Citadel",
                             "title": "Quant", "desc_text": ""})
        assert blocked.get("error") == "already_staged", \
            "stage_one() must refuse to silently re-stage an already-tailored uid"
        assert not (APPS / "test_stage_guard_probe" / "resume.pdf").exists(), \
            "a refused stage must not write a resume.pdf"
        forced = stage_one({"uid": guard_uid, "role_tier": 1, "company_name": "Citadel",
                            "title": "Quant", "desc_text": ""}, force=True)
        assert forced.get("error") is None, f"force=True must bypass the guard: {forced}"
    finally:
        import shutil as _shutil2
        _shutil2.rmtree(APPS / "test_stage_guard_probe", ignore_errors=True)
        with _sqlite3.connect(str(ROOT / "jobapply.db")) as _c:
            _c.execute("delete from applications where uid=?", (guard_uid,))

    print("demo ok")


# ---------------------------------------------------------------- 5. staging
APPS = ROOT / "applications" if (ROOT := Path(__file__).resolve().parent) else None


def stage_one(job, profile=None, force=False):
    """Write applications/<uid>/ for one job: the exact resume sent, its sha1, the letter.

    The PDF is COPIED, not referenced. Six weeks from now at interview prep, a mutable path
    tells you nothing about what that company actually received.

    When the job carries real JD text, the resume is TAILORED to it (bullets/skills reordered
    and reselected by keyword relevance -- jd_plan()/build_variant(jd_text=...)), not just a
    copy of one of the 4 shared tier PDFs. Every posting gets its own rendered PDF; nothing is
    invented (provenance_ok() gates the intermediate .md before it's ever exported). No JD
    text -> falls back to the static tier PDF, same as before -- a safe default, not a failure.

    CONFIRMED LIVE 2026-09-03: this is the ONE function every staging path routes through
    (both `docs.py stage --uid` and every app-tailor agent's library call), but the
    already-staged guard used to live only in the CLI wrapper -- a library caller re-tailoring
    a `queue()`-returned row that happened to already be `tailored` (queue() legitimately
    re-offers those) silently overwrote a good resume.pdf/cover_letter.md with no way to
    recover the old bytes. Root-caused: check here, not in each caller.
    """
    import hashlib, shutil, json as _json, sqlite3 as _sqlite3
    with _sqlite3.connect(str(ROOT / "jobapply.db")) as _c:
        _row = _c.execute("select status from applications where uid=?", (job["uid"],)).fetchone()
    if _row and _row[0] not in (None, "new", "scored") and not force:
        return {"uid": job["uid"], "error": "already_staged", "status": _row[0],
                "message": f"{job['uid']!r} is status={_row[0]!r}; re-staging would overwrite "
                           f"its resume.pdf/cover letter. Pass force=True if you really mean "
                           f"to redo it from scratch."}
    # A non-finance employer gets the industry-neutral "general" resume (score.is_finance).
    import score   # lazy: score imports db; docs.py never needs it at import time
    variant = job.get("variant") or ("general" if not score.is_finance(job) else None) or {
        1: "quant", 2: "quantdev", 3: "analyst", 4: "aieng"}.get(job.get("role_tier")) or "analyst"
    d = APPS / job["uid"].replace(":", "_"); d.mkdir(parents=True, exist_ok=True)
    dst = d / "resume.pdf"
    jd_text = job.get("desc_text") or ""
    tailored = False
    if jd_text.strip():
        safe_uid = job["uid"].replace(":", "_")
        md = build_variant(variant, jd_text=jd_text, out_name=f"{variant}_{safe_uid}")
        ok, bad = provenance_ok(md)
        if ok:
            pdf, pages = export_pdf(md, d / "_tailored.pdf")
            if pages == 1:
                shutil.copyfile(pdf, dst)
                tailored = True
            else:
                print(f"stage_one({job['uid']}): tailored resume is {pages} pages, "
                      f"falling back to the static {variant}.pdf", file=sys.stderr)
            pdf.unlink(missing_ok=True)
            pdf.with_suffix(".html").unlink(missing_ok=True)
        else:
            print(f"stage_one({job['uid']}): tailored .md failed provenance ({bad[:1]}), "
                  f"falling back to the static {variant}.pdf", file=sys.stderr)
        md.unlink(missing_ok=True)
    if not tailored:
        src = VARIANTS / f"{variant}.pdf"
        if not src.exists():
            return {"uid": job["uid"], "error": f"no variant pdf: {src.name}"}
        shutil.copyfile(src, dst)
    sha1 = hashlib.sha1(dst.read_bytes()).hexdigest()
    letter = cover_letter({"company": job.get("company_name") or job.get("company", ""),
                           "title": job.get("title", ""),
                           "jd_text": job.get("desc_text") or "",
                           "cover_letter": job.get("cover_letter", "optional")},
                          profile or {}, variant)
    cl = None
    if letter:
        cl = d / "cover.md"; cl.write_text(letter)
    # A letter is REQUIRED but unwritten => the app-tailor agent must research this employer
    # (get_company_data / web) and call cover_letter(..., hooks=[...]) before this one can go.
    needs_letter = job.get("cover_letter") == "required" and not letter
    (d / "meta.json").write_text(_json.dumps(
        {"uid": job["uid"], "company": job.get("company_name"), "title": job.get("title"),
         "variant": variant, "resume_sha1": sha1, "resume_tailored_to_jd": tailored,
         "cover_letter": bool(letter), "needs_researched_letter": needs_letter}, indent=1))
    return {"uid": job["uid"], "variant": variant, "resume": str(dst), "resume_sha1": sha1,
            "resume_tailored_to_jd": tailored,
            "cover_letter": str(cl) if cl else None, "needs_researched_letter": needs_letter}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"
    if cmd == "import":
        if len(sys.argv) != 3:
            print("usage: docs.py import <resume.docx>", file=sys.stderr)
            sys.exit(2)
        print(import_master(sys.argv[2]))
    elif cmd == "build":
        # `build` takes no arguments -- it always parses argv[2:], so a stray `--help` (or
        # any other typo) was silently IGNORED and a full rebuild ran anyway. Confirmed live
        # 2026-09-02: an agent ran `docs.py build --help` expecting usage text and instead
        # rewrote docs/variants/*.pdf. Harmless that time only because master.md hadn't
        # changed since the last build. Fail loudly instead.
        if sys.argv[2:]:
            print("usage: docs.py build   (no arguments)", file=sys.stderr)
            sys.exit(2)
        doc = parse_master()
        for tier in PLANS:
            md = build_variant(tier, doc)
            pdf, pages = export_pdf(md, VARIANTS / f"{tier}.pdf")
            export_docx(md, VARIANTS / f"{tier}.docx")
            print(f"{tier}: {pdf} ({pages}p)")
    elif cmd == "essay":
        import argparse
        p = argparse.ArgumentParser()
        p.add_argument("--question", required=True, help="exact on-page question text")
        p.add_argument("--company", required=True)
        p.add_argument("--title", default="")
        p.add_argument("--hooks", default="[]",
                       help="JSON list of researched, employer-specific facts; only "
                            "needed for a why-this-role question")
        args = p.parse_args(sys.argv[2:])
        ans = essay_answer(args.question, {"company": args.company, "title": args.title},
                           hooks=json.loads(args.hooks))
        if ans:
            print(ans)
        else:
            print("NEEDS_HUMAN: unrecognized question, or a why-role question with no "
                  "researched hook -- do not guess, route to the human queue", file=sys.stderr)
            sys.exit(2)
    elif cmd == "stage":
        import argparse, sqlite3
        p = argparse.ArgumentParser()
        p.add_argument("--uid", required=True)
        p.add_argument("--force", action="store_true",
                       help="re-stage even though this uid is already tailored+; "
                            "OVERWRITES resume.pdf unconditionally and does not know about "
                            "any hand-authored cover_letter.md sitting next to it")
        args = p.parse_args(sys.argv[2:])
        c = sqlite3.connect(str(ROOT / "jobapply.db")).cursor()
        c.execute("select * from jobs where uid=?", (args.uid,))
        row = c.fetchone()
        if not row:
            print(f"NO_SUCH_JOB: {args.uid!r} is not in the jobs table", file=sys.stderr)
            sys.exit(2)
        # The already-staged guard now lives inside stage_one() itself (2026-09-03) -- it's the
        # one function every staging path routes through, library callers included. See its
        # docstring for the incident history (2026-09-02 CLI clobber, 2026-09-03 library clobber).
        job = dict(zip([d[0] for d in c.description], row))
        result = stage_one(job, force=args.force)
        if result.get("error") == "already_staged":
            print(f"ALREADY_STAGED: {result['message']}", file=sys.stderr)
            sys.exit(2)
        print(json.dumps(result, indent=1))
    elif cmd in ("demo", "selftest"):
        demo()
    else:
        # Anything unrecognized -- `--help` above all -- used to fall through to demo(), which
        # RE-RENDERS docs/variants/*.pdf as a side effect. A stray `docs.py --help` therefore
        # rewrote the shared variant PDFs mid-batch (twice on 2026-08-28) and then died on
        # demo()'s own text-layer assert, so the clobber looked like an unrelated failure.
        # Shipped artifacts were unaffected only because each applications/<uid>/ holds a byte
        # copy. Fail loudly instead of mutating shared state.
        print("usage: docs.py {import|build|essay|stage|demo}", file=sys.stderr)
        sys.exit(2)
