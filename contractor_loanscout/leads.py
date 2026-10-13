"""Turn one US DOT contractor into a scored lead.

Census facts (who the officer is, how long it's been registered, fleet size,
how recently it filed) and Google facts found through Serper (a matching
listing, its rating and review count) become signal hits of their own, scored
by the same engine as website text. Every point of a score is traceable to
evidence: a row can say "fit 64 — 12 trucks, 15 drivers" next to the name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from .config import Rules
from .contacts import is_role_address
from .crawl import SiteCrawl, crawl_site, is_crawlable, registered_domain
from .emailcheck import FREE_MAIL, EmailChecker, domain_of
from .dot import Contractor, ContractorRules, split_person
from .fetch import PoliteFetcher
from .emailfinder import describe
from .places import Listing, PlacesClient
from .score import Score, score_site
from .signals import SignalHit, match_site

DOT_SOURCE = "US DOT census"
GOOGLE_SOURCE = "Google listing (Serper)"
DEFAULT_TRADE = "Construction (trade not in name)"


@dataclass(frozen=True)
class Lead:
    contractor: Contractor
    listing: Listing
    crawl: SiteCrawl | None
    score: Score
    trade: str
    website_email: str = ""
    website_email_page: str = ""
    website_email_is_officer: bool = False
    # Who the row addresses — the first officer, or the second when the email
    # is theirs — and the email to use, whose inbox it is, and where it came from.
    contact_first: str = ""
    contact_last: str = ""
    contact_level: str = "unknown"
    other_officer: str = ""
    email: str = ""
    email_belongs_to: str = ""  # officer, company inbox, someone else
    email_source: str = ""
    filed_email: str = ""  # the US DOT filing's email, when a better one replaced it


def _hit(signal_id: str, weight: float, snippet: str, source: str) -> SignalHit:
    return SignalHit(signal_id=signal_id, category="fit", weight=float(weight),
                     snippet=snippet, source_url=source, page_type="registry")


def dot_hits(c: Contractor, rules: ContractorRules, today: date | None = None) -> list[SignalHit]:
    """What the census record says about this contractor."""
    w = rules.weights
    hits = [_hit("dot_registered", w.get("dot_registered", 8),
                 f"Active US DOT carrier hauling construction cargo (USDOT {c.dot_number})", DOT_SOURCE)]

    years = c.years_registered(today)
    if years >= int(w.get("established_years", 3)):
        hits.append(_hit("dot_established", w.get("established", 10),
                         f"Registered with US DOT {c.registered} ({years} years)", DOT_SOURCE))
    if c.years_since_update(today) < 2:
        hits.append(_hit("dot_recently_updated", w.get("recently_updated", 6),
                         f"Filing updated {c.updated}: contacts are current", DOT_SOURCE))

    level_weight = (w.get("decision_maker") or {}).get(c.officer_level, 0)
    if level_weight:
        why = "surname in the business name" if c.officer_level == "owner" else "company officer"
        hits.append(_hit(f"officer_{c.officer_level}", level_weight,
                         f"Company officer: {c.officer_full} ({why})", DOT_SOURCE))

    fleet = f"{c.power_units} trucks, {c.drivers} drivers"
    if c.power_units >= 10:
        hits.append(_hit("fleet_large", w.get("fleet_large", 14), fleet, DOT_SOURCE))
    elif c.power_units >= 3:
        hits.append(_hit("fleet_medium", w.get("fleet_medium", 8), fleet, DOT_SOURCE))

    if c.email:
        hits.append(_hit("email_on_file", w.get("email_on_file", 6), f"Email filed with US DOT: {c.email}",
                         DOT_SOURCE))
    return hits


def google_hits(listing: Listing, rules: ContractorRules) -> list[SignalHit]:
    """What the matched Google listing says: open, and how busy."""
    if not listing.matched_on:
        return []
    w = rules.weights
    hits = [_hit("google_listing", w.get("google_listing", 10),
                 f"Google listing: {listing.name} — {listing.address} (matched on {listing.matched_on})",
                 GOOGLE_SOURCE)]
    stars = f"{listing.rating:.1f} stars from {listing.reviews} ratings"
    if listing.reviews >= 50:
        hits.append(_hit("google_reviews_many", w.get("reviews_many", 14), f"Google: {stars}", GOOGLE_SOURCE))
    elif listing.reviews >= 10:
        hits.append(_hit("google_reviews_some", w.get("reviews_some", 7), f"Google: {stars}", GOOGLE_SOURCE))
    if listing.reviews >= 10 and listing.rating >= 4.0:
        hits.append(_hit("google_good_rating", w.get("good_rating", 5), f"Google: {stars}", GOOGLE_SOURCE))
    return hits


def _letters(text: str) -> str:
    return re.sub(r"[^a-z]", "", (text or "").lower())


def email_belongs_to(email: str, first: str, last: str) -> bool:
    """True when the address's mailbox carries the officer's name:
    jane.doe@, jdoe@, doej@, jane@ — but never info@ or office@."""
    if not email or is_role_address(email):
        return False
    local = _letters(email.partition("@")[0])
    first, last = _letters(first), _letters(last)
    # A short surname must sit at an end, or "Lee" would match fleet@.
    if len(last) >= 4 and last in local:
        return True
    if len(last) == 3 and (local.startswith(last) or local.endswith(last)):
        return True
    # jane@ for Jane, rich@ for Richard.
    return len(first) >= 3 and (local.startswith(first) or (len(local) >= 3 and first.startswith(local)))


# Words in a company name that say nothing about which company it is.
_GENERIC_NAME_WORDS = {
    "inc", "llc", "corp", "co", "company", "the", "and", "of", "construction", "contracting",
    "contractor", "contractors", "services", "service", "group", "trucking", "enterprises",
    "associates", "sons", "son", "brothers", "bros", "general", "home", "improvement",
}


def email_owner(email: str, c: Contractor) -> str:
    """Whose inbox an address is: 'officer', 'second officer', 'company inbox'
    (info@, or one named after the company: spanoandsons@), 'someone else'
    (jryan@northstar.com when the filing names Gary Bowman — often whoever
    filed the form) or 'unclear' (a personal gmail-style box whose name says
    nothing: murf716@yahoo.com may well be Richard Murphy's own)."""
    if email_belongs_to(email, c.officer_first, c.officer_last):
        return "officer"
    second_first, second_last = split_person(c.second_officer)
    if second_first and email_belongs_to(email, second_first, second_last):
        return "second officer"
    if is_role_address(email):
        return "company inbox"
    local = _letters(email.partition("@")[0])
    words = {w for w in re.findall(r"[a-z0-9]+", f"{c.legal_name} {c.dba}".lower())
             if len(w) >= 3 and w not in _GENERIC_NAME_WORDS}
    if any(w in local for w in words):
        return "company inbox"
    return "unclear" if domain_of(email) in FREE_MAIL else "someone else"


def _level(last: str, c: Contractor) -> str:
    """owner when the surname is in the business name, as for the first officer."""
    names = f"{c.legal_name} {c.dba}".lower()
    return "owner" if last and re.search(rf"\b{re.escape(last.lower())}\b", names) else "officer"


def company_domain(c: Contractor, listing: Listing, rules: Rules) -> str:
    """The company's own email domain: from the filed email (northstar.com), or
    the Google listing's website. '' when it only uses gmail and the like."""
    if c.email and domain_of(c.email) not in FREE_MAIL:
        return domain_of(c.email)
    if listing.website and is_crawlable(listing.website, rules):
        return registered_domain(listing.website)
    return ""


def email_is_theirs(email: str, site_domain: str) -> bool:
    """An address on the business's own domain, or a gmail-style mailbox it
    lists on its own site — not a web designer's or a directory's."""
    domain = domain_of(email)
    if site_domain and (domain == site_domain or domain.endswith("." + site_domain)):
        return True
    return domain in FREE_MAIL


def website_email(crawl: SiteCrawl | None, c: Contractor,
                  checker: EmailChecker | None = None) -> tuple[str, str, bool]:
    """(email on the website, page it was on, whether it is the officer's own).
    The officer's own mailbox beats the site's general best pick. Only the
    business's own addresses count, and, given a checker, only ones that pass it."""
    if crawl is None or not crawl.pages:
        return "", "", False

    def usable(email: str) -> bool:
        return email_is_theirs(email, crawl.domain) and (checker is None or checker.ok(email))

    for page in crawl.pages:
        for email in page.contacts.emails:
            if email_belongs_to(email, c.officer_first, c.officer_last) and usable(email):
                return email, page.url, True
    contacts = crawl.contacts
    for email in (contacts.email,) + contacts.other_emails:
        if email and usable(email):
            page = next((p.url for p in crawl.pages if email in p.contacts.emails), contacts.email_page)
            return email, page, False
    return "", "", False


def build_lead(
    c: Contractor,
    places: PlacesClient | None,
    fetcher: PoliteFetcher | None,
    rules: Rules,
    contractor_rules: ContractorRules,
    max_pages: int,
    today: date | None = None,
    email_checker: EmailChecker | None = None,
    email_finder=None,
) -> Lead:
    """Look the contractor up on Google, crawl its website, and score it.

    Pass places=None for a free preview: census facts only, no lookups.
    Raises SerperUnavailable (stop the run) or ConnectionError (retry this
    contractor next run) from the lookup.
    """
    if places is None:
        listing = Listing(note="preview: not looked up")
    else:
        listing = places.find(c.search_name, c.address, c.city, c.state, c.zip)
        if not listing.matched_on:
            listing = Listing(note=f"{listing.note}; may have moved, or works from a yard not on Google")

    crawl = None
    if fetcher is not None and listing.website and is_crawlable(listing.website, rules):
        crawl = crawl_site(listing.website, fetcher, rules, max_pages)

    site_email, site_page, is_officer = website_email(crawl, c, email_checker)

    # The website's own signals. One that disqualifies the company (a
    # supplier, a lender, a public company...) means no paid email search.
    site_hits = match_site(crawl.pages, rules) if crawl is not None and crawl.pages else []
    disqualified = any(h.category == "disqualify" for h in site_hits)

    # Who the row addresses, and which email reaches them. The filed email is
    # often whoever filled in the form; when it names the second officer, the
    # row addresses them instead, and when it's nobody's in particular, the
    # officer's own address is looked for: on the website, then with the
    # email finder (Anymailfinder or Hunter).
    first, last, level, other = c.officer_first, c.officer_last, c.officer_level, c.second_officer
    email, source, filed = c.email, "US DOT filing", ""
    belongs = email_owner(c.email, c) if c.email else ""
    if belongs == "second officer":
        first, last = split_person(c.second_officer)
        level, other, belongs = _level(last, c), c.officer_full, "officer"
    if not email and site_email:
        email, source = site_email, f"website ({site_page})"
        belongs = "officer" if is_officer else email_owner(site_email, c)
    if belongs not in ("officer", "") and first:
        if site_email and is_officer:
            email, source, belongs, filed = site_email, f"website ({site_page})", "officer", c.email
        elif (email_finder is not None and contractor_rules.email_finder_when == "not_officer"
              and not disqualified):
            domain = company_domain(c, listing, rules)
            found = email_finder.find(domain, first, last) if domain else None
            if found and found.email and email_is_theirs(found.email, domain) and (
                    email_checker is None or email_checker.ok(found.email)):
                email, belongs, filed = found.email, "officer", c.email
                source = describe(email_finder, found)

    # The name says the trade most of the time; Google's category otherwise.
    trade = c.trade
    if trade == DEFAULT_TRADE and listing.category:
        trade = contractor_rules.trade_of(listing.category) or listing.category

    hits = dot_hits(c, contractor_rules, today) + google_hits(listing, contractor_rules) + site_hits

    return Lead(c, listing, crawl, score_site(hits, rules), trade, site_email, site_page, is_officer,
                contact_first=first, contact_last=last, contact_level=level, other_officer=other,
                email=email, email_belongs_to=belongs, email_source=source if email else "",
                filed_email=filed)
