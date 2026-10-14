# Contractor LoanScout

> **Prototype, branded edition.** Capital Sense Funding's colours and logo are
> in the window. The logo in `assets\` is the company's — keep this project
> private and check before sharing it outside the company.

Finds **contractors** — excavation, paving, concrete, roofing, hauling and
general contractors — in the US Department of Transportation's carrier
census, with a **company officer's first and last name, phone and email**. It
then looks each one up on Google to confirm it's operating and get its rating
and website, reads the website for signs of growth, and writes a ranked CSV of
leads with the evidence behind every score.

```
US DOT census ──▶ officer name, ──▶ Google listing ──▶ website crawl ──▶ contractor_leads.csv
(official, free,   phone, email,     (via Serper:       (growth
 nationwide)       fleet, rank       rating, website)    signals)
```

Every business that runs commercial trucks registers with US DOT and renews a
form (MCS-150) every two years naming up to two company officers, with the
business phone and email and what it hauls. Contractors with trucks are in
it, in every state, for free.

## The easy way: the window

Double-click **`Contractor LoanScout GUI.bat`**. The first time, it sets
itself up (about a minute). Then:

1. **Where** — pick a state and type the start of the ZIP codes (`191` is
   Philadelphia; blank = the whole state).
2. **Preview (free)** — searches the census and ranks every matching
   contractor from census facts alone. Writes
   `out\contractor_leads_preview.csv`. No credits used.
3. **Run** — looks up the top contractors on Google (about 1 Serper credit
   each), crawls their websites, and writes `out\contractor_leads.csv`. Run
   again to continue down the list; contractors already done are skipped.
4. **Open results** opens the newest spreadsheet. **Stop** ends a run early;
   finished rows are kept.

*Run* needs a Serper key in `.env` (below). *Preview* doesn't.

The US DOT data site slows down repeated searches from people without an app
token — the first search of an area can take a minute while it waits. Results
are then cached for a week in `data\dot_cache`. A free `SOCRATA_APP_TOKEN` in
`.env` removes the wait (see `.env.example`).

## What's in the spreadsheet

| Column | Meaning |
|---|---|
| `first_name`, `last_name` | The first company officer named on the US DOT filing — or the second, when the email is theirs |
| `decision_maker` | *owner* when the officer's surname is in the business name (Mike **Spano** & Sons), otherwise *company officer* |
| `email` | Filed with US DOT — or the officer's own address from the website or Anymailfinder, when the filed one isn't theirs |
| `email_belongs_to` | Whose inbox it is: *the officer named*, *company inbox*, *someone else at the company*, or *unclear* — see below |
| `phone` | Filed with US DOT; `cell_phone` too when given |
| `company_name` | The name the public knows it by (its DBA when it has one) |
| `trade` | Read from the name (*Paving & asphalt*, *Roofing & siding*…), or from Google's category after a Run |
| `score` | 0–100 weight; see below |
| `google_rating`, `google_reviews` | From the Google listing found through Serper |
| `trucks`, `drivers` | Fleet size from the filing — a sign of business size |
| `hauls` | *own work* (a contractor with its own trucks), *for hire*, or both |
| `second_officer` | The other officer named, when there is one |
| `email_source`, `filed_email` | Where the email came from; the email filed with US DOT when another replaced it |

Then the website, address, USDOT number, registration and filing dates,
safety rating, the top three pieces of evidence, and notes.

## Who is left out

- **Suppliers, manufacturers and public bodies** — lumber yards, material
  suppliers, rental companies, utilities, cities and counties (by name;
  edit `exclude_name_words` in `config\contractor.yaml`).
- **Freight companies.** They tick "construction" among many cargo types. A
  carrier hauling only for hire stays a lead only when its name shows a trade
  and no freight wording (Express, Logistics, Transport…).
- **National fleets** — over 150 trucks.
- **Stale filings** — not updated in 4 years: the contacts are likely out of
  date.
- **Disqualified companies** — nonprofits, lenders, public and very large
  companies, found on their websites during a Run. They go to
  `out\contractor_leads_left_out.csv` with the reason, not the results, and
  no email-finder credit is spent on them.
- **Corporate groups** — at the end of every Run, a website or email domain
  shared by 3 or more different companies in the results (Installed Building
  Products' insulation companies, each filing with US DOT on its own) moves
  them to the left-out file. Email providers (gmail, cox…), website builders,
  government sites and the franchise brands in `franchise_domains` in
  `config\contractor.yaml` never count — a franchise location is a small
  business. `chain_min_companies: 0` turns this off.

Anything in the left-out file can be copied back into the results by hand.

In Philadelphia this takes 1,011 census carriers hauling construction cargo
down to about 390 contractor leads.

## Only leads with a working email

Every row has an email that passed these checks, run on the census before any
Google lookup, so no credits go to the rest (set `require_email: false` in
`config\contractor.yaml` to turn this off):

- it is a real address — not a placeholder (none@, test@) or no-reply box,
  and not a misspelled domain (gmial.com, comcast.com);
- its domain exists and accepts mail (DNS lists a mail server for it);
- it isn't a filing service's — an address filed for 5 or more carriers, or
  a domain giving 5 or more carriers mailboxes named after each one
  (`peninsula.paving@transervices.com`);
- no higher-ranked contractor uses it, in this Run or an earlier one (another
  state's, say) — sister companies often share an inbox, and each person
  should get one email.

In Philadelphia, 372 of 386 contractor leads pass.

These checks can't tell whether one mailbox still exists (whether jsmith@ left
the company). A paid verifier — NeverBounce, ZeroBounce — checks that for
about a cent an address; worth it before a large send.

## Whose email it is

The US DOT form asks for the officers and *a* contact email, and the email is
often whoever filled in the form. Northstar Contracting names Gary Bowman as
its officer and filed `jryan@northstar.com`: "Hi Gary" would reach J. Ryan.
`email_belongs_to` says which it is, so a message can be addressed right:

| Value | Example | Address the message to |
|---|---|---|
| the officer named | `claudio@carusoneconstruction.com` | the officer |
| company inbox | `info@…`, `lavalandscaping@gmail.com` | the company |
| someone else at the company | `jryan@northstar.com` | the company, or that person |
| unclear | `murf716@yahoo.com` | the company — it may or may not be the officer's |

When the email is the *second* officer's, the row names them instead. In
Philadelphia: 138 of 372 are the officer's, 113 a company inbox, 77 unclear,
44 someone else.

**Email finder: Anymailfinder (optional).** With `ANYMAILFINDER_API_KEY` in
`.env` — the same key as Healthcare LoanScout — a Run asks Anymailfinder for
the officer's own address whenever the email isn't theirs and the company has
its own domain: from the filed email (`northstar.com`) or its website. For
Northstar, it verified `gbowman@northstar.com`. Only verified addresses are used; the filed
email moves to `filed_email`. Email finders can't search gmail-style
addresses, so this helps the ~54 Philadelphia leads with a company domain, not
the rest. Anymailfinder charges only for verified emails found. Set
`email_finder_when: never` in `config\contractor.yaml` to turn it off.

## The score (weight)

Every point comes from a piece of evidence, shown in the `evidence_` columns:

- **US DOT census** (free): registered 3+ years; filing updated in the last 2
  years; the officer is the owner (14) or a company officer (10); 10+ trucks
  (14) or 3–9 (8); an email on file.
- **Google, via Serper** (the Run step): a listing matched at the census
  address (10); 50+ ratings (14) or 10–49 (7); 4.0 stars or better (5).
- **Website**: growth signals — hiring crews or CDL drivers, new equipment,
  new locations, "licensed, bonded and insured". Lenders and public companies
  are disqualified.

All weights are in `config\contractor.yaml` (census and Google) and
`config\signals.yaml` (website). Edit and re-run.

## Command line

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
notepad .env

.venv\Scripts\python -m contractor_loanscout run --dry-run                 # free preview
.venv\Scripts\python -m contractor_loanscout run --limit 50                # ~50 credits
.venv\Scripts\python -m contractor_loanscout run --state NJ --zip 080,081
```

| `run` option | Default | Meaning |
|---|---|---|
| `--state` | `PA` | two-letter state |
| `--zip` | `191` | ZIP code starts, comma-separated; `all` for the whole state |
| `--limit` | `50` | contractors looked up this run; `0` = no cap |
| `--dry-run` | off | free preview, no Google lookups |
| `--no-resume` | off | start the CSV over |

## What it can't do

- **Contractors without registered trucks aren't in the census** — small
  trades working from pickups and vans usually aren't. It covers excavation,
  paving, concrete, roofing and hauling best.
- **The email is often the person who filed the form** (an office or fleet
  manager), not the officer named — `email_belongs_to` says so; see above.
- **"Officer" has no title in the census.** The surname check marks likely
  owners; otherwise it's a named officer of the company.

## Tests

```powershell
.venv\Scripts\python -m pytest
```
