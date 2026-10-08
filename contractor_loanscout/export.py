"""Incremental CSV output.

Rows are appended and flushed as each contractor finishes, so a run killed
halfway still leaves a usable file. A sibling seen.txt of USDOT numbers lets a
re-run skip contractors already done — which is what lets each run continue
down the ranked list instead of spending credits on the same ones twice.
"""

from __future__ import annotations

import csv
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from .config import Rules
from .contacts import _format_phone
from .leads import Lead

COLUMNS = [
    # Who to call, and the business.
    "first_name",
    "last_name",
    "title",
    "decision_maker",
    "email",
    "phone",
    "company_name",
    "trade",
    # The weight: 0-100, from US DOT facts, Google (Serper) facts and the website.
    "score",
    "growth_score",
    "distress_score",
    "fit_score",
    "google_rating",
    "google_reviews",
    # More ways to reach them.
    "cell_phone",
    "second_officer",
    "email_belongs_to",
    "email_source",
    "filed_email",
    "website_email",
    "phone_source",
    "website",
    # The business.
    "legal_name",
    "address",
    "city",
    "state",
    "zip",
    "trucks",
    "drivers",
    "hauls",
    "usdot_number",
    "dot_registered",
    "years_registered",
    "dot_filing_updated",
    "business_type",
    "safety_rating",
    # Evidence and diagnostics.
    "disqualified",
    "disqualify_reason",
    "top_signals",
    "evidence_1",
    "evidence_2",
    "evidence_3",
    "google_listing",
    "google_category",
    "google_match",
    "other_emails",
    "other_phones",
    "pages_crawled",
    "thin_content",
    "notes",
    "processed_at",
    "key",
]

# Whose inbox the email is, so a message can be addressed right: "Hi Gary"
# only reaches Gary when the address is his.
EMAIL_BELONGS_TO = {
    "officer": "the officer named",
    "company inbox": "company inbox (address the company, not the officer)",
    "someone else": "someone else at the company (often who filed the form)",
    "unclear": "unclear (a personal mailbox; may or may not be the officer's)",
}

DECISION_MAKER = {
    "owner": "owner (surname in business name)",
    "officer": "company officer",
    "unknown": "unknown",
}


def _tidy_phone(phone: str) -> str:
    """Google writes '(215) 555-0134' or '+1 215-555-0134'; match the census
    and site contacts' format so the column reads the same either way."""
    return _format_phone(re.sub(r"\D", "", phone)) or phone


def build_row(lead: Lead, rules: Rules) -> dict:
    c, listing, crawl, score = lead.contractor, lead.listing, lead.crawl, lead.score
    contacts = crawl.contacts if crawl is not None else None

    # The number filed with US DOT reaches the office that runs the trucks; the
    # Google listing's is next, the website's last.
    if c.phone:
        phone, phone_source = c.phone, "US DOT filing"
    elif listing.phone:
        phone, phone_source = _tidy_phone(listing.phone), "Google listing"
    elif contacts and contacts.phone:
        phone, phone_source = contacts.phone, contacts.phone_page
    else:
        phone, phone_source = "", ""

    email = lead.email

    evidence = [h.describe() for h in score.top_hits(3)]
    evidence += [""] * (3 - len(evidence))

    notes = [listing.note] if listing.note else []
    if c.safety_rating in ("Conditional", "Unsatisfactory"):
        notes.append(f"US DOT safety rating: {c.safety_rating}")
    if crawl is not None:
        notes += crawl.errors[:2]
    elif listing.website:
        notes.append("website not crawled (social media or directory page)")

    other_emails = [e for e in ((contacts.email,) + contacts.other_emails if contacts else ())
                    if e and e not in (email, lead.website_email, lead.filed_email)]

    return {
        "first_name": lead.contact_first,
        "last_name": lead.contact_last,
        "title": "Company officer" if lead.contact_first else "",
        "decision_maker": DECISION_MAKER.get(lead.contact_level, lead.contact_level),
        "email": email,
        "phone": phone,
        "company_name": c.company_name,
        "trade": lead.trade,
        "score": score.total,
        "growth_score": score.categories.get("growth", 0.0),
        "distress_score": score.categories.get("distress", 0.0),
        "fit_score": score.categories.get("fit", 0.0),
        "google_rating": f"{listing.rating:.1f}" if listing.rating else "",
        "google_reviews": listing.reviews if listing.matched_on else "",
        "cell_phone": c.cell_phone,
        "second_officer": lead.other_officer,
        "email_belongs_to": EMAIL_BELONGS_TO.get(lead.email_belongs_to, lead.email_belongs_to),
        "email_source": lead.email_source,
        "filed_email": lead.filed_email,
        "website_email": (lead.website_email + (" (officer's own)" if lead.website_email_is_officer else "")
                          if lead.website_email and lead.website_email != email else ""),
        "phone_source": phone_source,
        "website": listing.website,
        "legal_name": c.legal_name,
        "address": c.address,
        "city": c.city,
        "state": c.state,
        "zip": c.zip,
        "trucks": c.power_units,
        "drivers": c.drivers,
        "hauls": c.hauls,
        "usdot_number": c.dot_number,
        "dot_registered": c.registered,
        "years_registered": c.years_registered(),
        "dot_filing_updated": c.updated,
        "business_type": c.business_type,
        "safety_rating": c.safety_rating,
        "disqualified": "yes" if score.disqualified else "no",
        "disqualify_reason": score.disqualify_reason,
        "top_signals": " | ".join(score.signal_ids),
        "evidence_1": evidence[0],
        "evidence_2": evidence[1],
        "evidence_3": evidence[2],
        "google_listing": " — ".join(p for p in (listing.name, listing.address) if p),
        "google_category": listing.category,
        "google_match": listing.matched_on,
        "other_emails": " | ".join(other_emails[:3]),
        "other_phones": " | ".join(contacts.other_phones) if contacts else "",
        "pages_crawled": crawl.pages_crawled if crawl is not None else 0,
        "thin_content": (
            "yes" if crawl is not None and crawl.pages and crawl.thin_content(rules) else ""
        ),
        "notes": " | ".join(notes),
        "processed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "key": c.key,
    }


class CsvWriter:
    """Thread-safe append-as-you-go writer with a resumable seen-list."""

    def __init__(self, path: Path | str, resume: bool = True, track_seen: bool = True):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.seen_path = self.path.with_name(f"{self.path.stem}.seen.txt")
        self.track_seen = track_seen

        self._lock = threading.Lock()
        self.seen: set[str] = set()

        if resume and track_seen and self.seen_path.exists():
            self.seen = {
                line.strip()
                for line in self.seen_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            }

        if resume and self.path.exists():
            _upgrade_columns(self.path)

        write_header = not self.path.exists() or not resume
        mode = "w" if write_header else "a"

        self._file = self.path.open(mode, newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._file, fieldnames=COLUMNS)
        if write_header:
            self._writer.writeheader()
            self._file.flush()

        self._seen_file = (
            self.seen_path.open(mode, encoding="utf-8") if track_seen else None
        )

    def write(self, row: dict) -> None:
        with self._lock:
            self._writer.writerow(row)
            self._file.flush()
            if self._seen_file is not None:
                self._seen_file.write(f"{row['key']}\n")
                self._seen_file.flush()
            self.seen.add(row["key"])

    def mark_done(self, key: str) -> None:
        """Record a lead as finished without writing a row — one left out for
        having no working email — so later runs don't spend a credit on it again."""
        with self._lock:
            if self._seen_file is not None:
                self._seen_file.write(f"{key}\n")
                self._seen_file.flush()
            self.seen.add(key)

    def already_done(self, key: str) -> bool:
        return key in self.seen

    def close(self) -> None:
        self._file.close()
        if self._seen_file is not None:
            self._seen_file.close()

    def __enter__(self) -> "CsvWriter":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def _upgrade_columns(path: Path) -> None:
    """Rewrite a CSV from an older version with the current columns, so
    appended rows don't land under the wrong headings."""
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or reader.fieldnames == COLUMNS:
            return
        rows = list(reader)

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def sort_csv_by_score(path: Path | str) -> None:
    """Rewrite the finished CSV highest-score-first, for review."""
    path = Path(path)
    if not path.exists():
        return

    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    rows.sort(key=lambda r: float(r.get("score") or 0), reverse=True)

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
