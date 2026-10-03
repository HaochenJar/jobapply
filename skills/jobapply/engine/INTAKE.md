# INTAKE — things only you can supply

Edit `~/.config/jobapply/profile.json` **by hand** (no agent writes to it), then run
`python3 ~/jobapply/profile_check.py`.

**Not legal advice.** Confirm anything immigration-related with your school's international
student office (your DSO). Take anything touching a specific offer to an immigration attorney.

### 1. Identity + home address — `identity.*`
Workday and iCIMS require a street address; blank = those submits stall in the human queue.

### 2. Salary — `answers["what are your salary expectations"]`
`null` on purpose so no model picks a number. Set one figure (or "Negotiable"), once.
If wrong, you anchor low on every application and cannot walk it back.

### 3. Start date — `answers["earliest start date"]`  ⚠️ the trap
The start date printed on your **OPT EAD**, not your graduation date and not "immediately".
You may not work one day before it. Also budget the 90-day OPT unemployment limit, which
counts from the EAD start date — a long gap between EAD start and a fall class start can eat it.

### 4. Confirm the frozen work-auth answers (F-1 / OPT)

| Question | Answer |
|---|---|
| Legally authorized to work in the US? | **Yes** |
| Require sponsorship now or in the future? | **Yes** |
| Authorized to work *without* sponsorship? | **No** |
| US citizen or permanent resident? | **No** |
| Unrestricted work authorization? | **No** |
| Visa type | **F-1 student visa (post-completion OPT)** |

Yes/Yes is the standard pair for F-1 on OPT. Answering "No" to sponsorship is the most common
misrepresentation and surfaces at I-9 time. Answering "Yes" to citizenship is a federal crime
(18 U.S.C. § 911). `profile_check.py` asserts the pair as a unit so an edit cannot flip one half.
Not on F-1? Then this tool's work-auth checks are wrong for you — edit `profile_check.py` first.

### 5. Tier-1 firms — never auto-submit
`companies.json` marks firms `tier: 1`. Those are always drafted and held for your review.
Most have a 6–12 month reapply bar, so one bad auto-submit burns the firm. Edit the list.

### 6. Your resume — `docs/master.md`
The single source of truth for every fact on every resume and cover letter. Start from
`docs/master.example.md`'s format. Then personalize the templates in `docs.py` (PLANS,
ARTIFACTS, LETTERS, ESSAY_STORIES) and the targeting in `score.py` (search `PERSONALIZE`).

### 7. Fraud tripwire — nothing to do, just know it
No SSN, DOB, passport number, or bank details are stored, ever. A form asking for any of them
pre-offer aborts and gets flagged. Fake recruiters target international students specifically.
