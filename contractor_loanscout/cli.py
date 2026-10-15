"""Command-line entry point and run orchestration.

    run  search the US DOT carrier census for contractors in an area (free),
         rank them, look the best up on Google via Serper (1 credit each),
         crawl their websites, score, and append to the CSV

A run takes an optional threading.Event so the window's Stop button can end
it the same way Ctrl+C does: finished work is kept, the rest resumes on the
next run.
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from collections import Counter
from dataclasses import dataclass

from .config import ConfigError, load_settings
from .cleanup import LeftOutWriter, leave_out_chains, left_out_path, never_a_chain
from .crawl import DEFAULT_MAX_PAGES
from .dot import (CensusClient, DotUnavailable, SearchStopped, check_zip_prefixes,
                  find_contractors, load_contractor_rules)
from .emailcheck import EmailChecker, EmailCheckUnavailable, domain_of
from .contacts import is_wrong_department
from .export import COLUMNS, CsvWriter, build_row, remembered_emails, sort_csv_by_score
from .fetch import PoliteFetcher
from .anymailfinder import AnymailfinderClient
from .emailfinder import EmailFinderUnavailable
from .leads import build_lead
from .places import PlacesClient, SerperUnavailable

log = logging.getLogger("contractor_loanscout")

EXIT_OK = 0
EXIT_NO_MATCHES = 1  # the census has no contractor leads for this area
EXIT_CONFIG_ERROR = 2  # missing key, bad settings, data site unreachable
EXIT_DEPLETED = 3  # every matching contractor already processed
EXIT_LOOKUPS_FAILED = 4  # contractors were attempted and none could be looked up
EXIT_SERPER_UNAVAILABLE = 5  # Serper or the email finder: bad key or out of credits; the run stopped early
EXIT_INTERRUPTED = 130

DEFAULT_OUT = "out/contractor_leads.csv"


@dataclass
class RunOptions:
    """What a run should do. None means 'use the contractor.yaml setting'."""

    state: str | None = None
    zip_prefixes: tuple[str, ...] | None = None
    limit: int | None = None
    dry_run: bool = False
    out: str = DEFAULT_OUT
    max_pages: int = DEFAULT_MAX_PAGES
    workers: int = 8
    delay: float = 1.0
    no_resume: bool = False


@dataclass
class RunSummary:
    """Where every contractor ended up. Printed at the end of a run."""

    matching: int = 0
    no_email: int = 0
    shared_email: int = 0
    disqualified: int = 0
    wrong_department: int = 0
    chains: int = 0
    already_done: int = 0
    attempted: int = 0
    written: int = 0
    retry_later: int = 0
    with_name: int = 0
    with_owner: int = 0
    with_phone: int = 0
    with_email: int = 0
    with_officer_email: int = 0
    with_website: int = 0
    not_found: int = 0
    credits_used: int = 0
    finder_name: str = ""
    finder_lookups: int = 0
    finder_found: int = 0
    serper_unavailable: bool = False

    def outcome(self) -> int:
        if self.serper_unavailable:
            return EXIT_SERPER_UNAVAILABLE
        if self.matching == 0:
            return EXIT_NO_MATCHES
        if self.attempted == 0:
            return EXIT_DEPLETED
        if self.written == 0 and self.retry_later:
            return EXIT_LOOKUPS_FAILED
        return EXIT_OK

    def report(self, out_path: str) -> None:
        line = "-" * 58
        log.info(line)
        log.info("%-32s %d", "contractors matching", self.matching)
        if self.no_email:
            log.info("%-32s %d", "  left out: no working email", self.no_email)
        if self.shared_email:
            log.info("%-32s %d", "  left out: email already used", self.shared_email)
        if self.disqualified:
            log.info("%-32s %d", "  left out: disqualified", self.disqualified)
        if self.wrong_department:
            log.info("%-32s %d", "  left out: department inbox", self.wrong_department)
        if self.already_done:
            log.info("%-32s %d", "skipped (already done)", self.already_done)
        log.info("%-32s %d", "processed this run", self.attempted)
        if self.retry_later:
            log.info("%-32s %d", "  failed (will retry)", self.retry_later)
        log.info("%-32s %d", "  with a first and last name", self.with_name)
        log.info("%-32s %d", "    surname in business name", self.with_owner)
        log.info("%-32s %d", "  with a phone number", self.with_phone)
        log.info("%-32s %d", "  with an email", self.with_email)
        log.info("%-32s %d", "    the officer's own address", self.with_officer_email)
        log.info("%-32s %d", "  with a website", self.with_website)
        if self.not_found:
            log.info("%-32s %d", "  no Google listing", self.not_found)
        log.info("%-32s %d", "Serper credits used", self.credits_used)
        if self.finder_lookups:
            log.info("%-32s %d  (%d verified emails found)", f"{self.finder_name} searches",
                     self.finder_lookups, self.finder_found)
        log.info("%-32s %d  ->  %s", "rows written", self.written, out_path)
        if self.chains:
            log.info("%-32s %d", "moved out: chains or groups", self.chains)
        if self.disqualified or self.chains or self.wrong_department:
            log.info("%-32s %s", "left-out rows (with reasons)", left_out_path(out_path))
        log.info(line)


def preview_path(out: str) -> str:
    return out[:-4] + "_preview.csv" if out.endswith(".csv") else out + "_preview.csv"


def run_leads(options: RunOptions, stop: threading.Event | None = None,
              census: CensusClient | None = None, places=None, email_finder=None) -> int:
    """census, places and email_finder can be passed in (tests); otherwise real
    ones are made — the email finder only when its key is in .env."""
    stop = stop or threading.Event()
    try:
        settings = load_settings()
        crules = load_contractor_rules()
        state = (options.state or crules.state).upper()
        prefixes = crules.zip_prefixes if options.zip_prefixes is None else options.zip_prefixes
        check_zip_prefixes(prefixes)
        api_key = None if options.dry_run or places is not None else settings.require_serper_key()
    except ConfigError as exc:
        log.error("%s", exc)
        return EXIT_CONFIG_ERROR

    rules = settings.rules
    limit = crules.default_limit if options.limit is None else options.limit
    area = f"{state} ZIP {', '.join(p + '*' for p in prefixes)}" if prefixes else f"all of {state}"
    log.info("searching the US DOT carrier census: %s (free)...", area)

    client = census or CensusClient(app_token=settings.socrata_app_token, stop=stop)
    try:
        contractors = find_contractors(client, crules, state, prefixes)
    except SearchStopped:
        log.warning("stopped during the US DOT search — nothing was written")
        return EXIT_INTERRUPTED
    except DotUnavailable as exc:
        log.error("%s", exc)
        return EXIT_CONFIG_ERROR
    finally:
        if census is None:
            client.close()

    summary = RunSummary(matching=len(contractors))
    log.info("%d contractors are leads after removing suppliers, public bodies, big fleets "
             "and stale filings", len(contractors))
    if not contractors:
        return EXIT_NO_MATCHES

    # Rank on census facts alone (free), so credits go to the strongest first.
    ranked = sorted(
        contractors,
        key=lambda c: (build_lead(c, None, None, rules, crules, options.max_pages).score.total,
                       c.power_units, c.company_name),
        reverse=True,
    )

    checker = EmailChecker()
    if crules.require_email:
        try:
            already = {} if options.dry_run or options.no_resume else emails_in(options.out)
            ranked = keep_working_emails(ranked, checker, summary, stop, already)
        except SearchStopped:
            log.warning("stopped while checking emails — nothing was written")
            return EXIT_INTERRUPTED
        if not ranked:
            log.warning("no contractor here has a working email on file")
            return EXIT_NO_MATCHES

    if options.dry_run:
        return _preview(ranked, options, limit, rules, crules, summary)

    used_emails = {} if options.no_resume else emails_in(options.out)
    left_out = LeftOutWriter(options.out, COLUMNS)

    places = places or PlacesClient(api_key)
    fetcher = PoliteFetcher(settings.user_agent, default_delay=options.delay)
    if email_finder is None and crules.email_finder_when != "never":
        email_finder = make_email_finder(settings)
    if email_finder is not None:
        log.info("%s is on: when the filed email isn't the officer's own, it looks for theirs "
                 "at the company's domain", email_finder.name)

    try:
        with CsvWriter(options.out, resume=not options.no_resume) as writer:
            pending = [c for c in ranked if not writer.already_done(c.key)]
            summary.already_done = len(ranked) - len(pending)
            if limit > 0:
                pending = pending[:limit]

            if not pending:
                log.info("nothing new — every matching contractor is already done")
                summary.report(options.out)
                return EXIT_DEPLETED

            log.info("looking up the top %d on Google (about %d Serper credits)",
                     len(pending), len(pending))

            with ThreadPoolExecutor(max_workers=options.workers) as pool:
                futures = {
                    pool.submit(build_lead, c, places, fetcher, rules, crules, options.max_pages,
                                email_checker=checker, email_finder=email_finder): c
                    for c in pending
                }
                not_done = set(futures)
                try:
                    while not_done:
                        if stop.is_set():
                            raise KeyboardInterrupt
                        # Polled once a second so Stop and Ctrl+C are noticed promptly.
                        done, not_done = wait(not_done, timeout=1.0, return_when=FIRST_COMPLETED)
                        for future in done:
                            contractor = futures[future]
                            summary.attempted += 1
                            try:
                                lead = future.result()
                            except (SerperUnavailable, EmailFinderUnavailable) as exc:
                                log.error("%s — stopping; finished rows are saved", exc)
                                summary.serper_unavailable = True
                                raise
                            except Exception as exc:  # one bad contractor must not kill the run
                                summary.retry_later += 1
                                log.warning("%s: failed, will retry next run (%s)",
                                            contractor.company_name, exc)
                                continue

                            if lead.score.disqualified:
                                # Not a small-business borrower: kept for review, not as a lead.
                                writer.mark_done(contractor.key)
                                left_out.write(build_row(lead, rules), f"disqualified: {lead.score.disqualify_reason}")
                                summary.disqualified += 1
                                log.info("%-36.36s left out: disqualified (%s)", contractor.company_name,
                                         lead.score.disqualify_reason)
                                continue

                            if is_wrong_department(lead.email):
                                # hr@, billing@, compliance@: no officer's own address
                                # was found to use instead, and the owner won't read it.
                                writer.mark_done(contractor.key)
                                left_out.write(build_row(lead, rules), f"department inbox: {lead.email}")
                                summary.wrong_department += 1
                                log.info("%-36.36s left out: %s is a department's inbox",
                                         contractor.company_name, lead.email)
                                continue

                            # The email finder may have swapped in an address an
                            # earlier run (or another company) already uses.
                            if used_emails.get(lead.email.lower(), contractor.key) != contractor.key:
                                writer.mark_done(contractor.key)
                                summary.shared_email += 1
                                log.info("%-36.36s left out: %s already used for another lead",
                                         contractor.company_name, lead.email)
                                continue

                            row = build_row(lead, rules)
                            writer.write(row)
                            used_emails[lead.email.lower()] = contractor.key
                            _tally(summary, row, lead)
                            log.info("%-36.36s %-22.22s score=%-5s %s",
                                     contractor.company_name,
                                     f"{row['first_name']} {row['last_name']}".strip() or "-",
                                     row["score"], row["email"] or row["phone"] or "-")
                except (KeyboardInterrupt, SerperUnavailable, EmailFinderUnavailable):
                    fetcher.stop()
                    pool.shutdown(wait=False, cancel_futures=True)
                    raise
    except KeyboardInterrupt:
        log.warning("stopped — %d rows already written to %s (run again to continue)",
                    summary.written, options.out)
        return EXIT_INTERRUPTED
    except (SerperUnavailable, EmailFinderUnavailable):
        pass
    finally:
        summary.credits_used = getattr(places, "lookups", 0)
        summary.finder_name = getattr(email_finder, "name", "")
        summary.finder_lookups = getattr(email_finder, "lookups", 0)
        summary.finder_found = getattr(email_finder, "found", 0)
        fetcher.close()
        places.close()
        if email_finder is not None:
            email_finder.close()

    summary.chains = _leave_out_chains(options.out, crules, rules)
    sort_csv_by_score(options.out)
    summary.report(options.out)
    return summary.outcome()


def _leave_out_chains(out: str, crules, rules) -> int:
    """Move corporate groups' rows out of the results; how many moved."""
    moved = leave_out_chains(out, COLUMNS, crules.chain_min_companies,
                             never_a_chain(crules.franchise_domains, rules.domain_blocklist))
    for domain, count in sorted(moved.items(), key=lambda x: -x[1]):
        log.info("left out %d rows sharing %s (a corporate group or chain; add it to franchise_domains "
                 "in contractor.yaml to keep them)", count, domain)
    return sum(moved.values())


def emails_in(path: str) -> dict[str, str]:
    """Emails already used, and whose they are (USDOT number) — in the results,
    or in an earlier batch whose results file has since been deleted — so a
    later run (another state, say) doesn't list the same inbox again."""
    used = remembered_emails(path)
    try:
        with open(path, newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if row.get("email"):
                    used.setdefault(row["email"].strip().lower(), row.get("key", ""))
    except (OSError, KeyError):
        pass
    return used


def make_email_finder(settings):
    """Anymailfinder when its key is in .env, else none."""
    if settings.anymailfinder_api_key:
        return AnymailfinderClient(settings.anymailfinder_api_key)
    return None


EMAIL_CHECK_WORKERS = 32
UNREACHABLE = "couldn't check the domain (try again later)"


def keep_working_emails(ranked: list, checker: EmailChecker, summary: RunSummary,
                        stop: threading.Event | None = None, already: dict[str, str] | None = None) -> list:
    """The contractors whose census email passes the checks, best first, with
    each address kept only once (sister companies often share one inbox).
    Free — done before any lookup, so no credits go to the rest.

    Each email domain is asked about once, many at a time. A whole state has
    thousands, and a slow one takes seconds, so Stop is checked twice a second
    and progress is reported while it runs. Raises SearchStopped on Stop."""
    stop = stop or threading.Event()
    emails = sorted({c.email for c in ranked if c.email})
    domains = sorted({domain_of(e) for e in emails if checker.needs_dns(e)})

    unreachable: set[str] = set()

    def ask(domain: str) -> None:
        try:
            checker.domain_problem(domain)
        except EmailCheckUnavailable:
            unreachable.add(domain)

    if domains:
        log.info("checking %d email domains...", len(domains))
    # Not a with-block: leaving one waits for every running check, which is
    # what made Stop slow. On Stop, checks not started are cancelled and the
    # few running ones finish in the background.
    pool = ThreadPoolExecutor(max_workers=EMAIL_CHECK_WORKERS)
    try:
        pending = {pool.submit(ask, d) for d in domains}
        last_report = time.monotonic()
        while pending:
            if stop.is_set():
                raise SearchStopped()
            _, pending = wait(pending, timeout=0.5)
            if pending and time.monotonic() - last_report >= 10:
                log.info("  checked %d of %d email domains", len(domains) - len(pending), len(domains))
                last_report = time.monotonic()
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    # Every domain's answer is now remembered, so these return at once.
    problems = {e: UNREACHABLE if domain_of(e) in unreachable else checker.problem(e) for e in emails}

    kept, used, why = [], set(), Counter()
    for c in ranked:
        problem = problems.get(c.email, "no email") if c.email else "no email on file"
        if problem:
            why[problem] += 1
        elif c.email in used or (already or {}).get(c.email, c.key) != c.key:
            summary.shared_email += 1
        else:
            used.add(c.email)
            kept.append(c)
    summary.no_email = sum(why.values())
    log.info("%d have a working email; left out: %s", len(kept),
             ", ".join(f"{n} {reason}" for reason, n in why.most_common()) or "none")
    if summary.shared_email:
        log.info("%d more share an email with a higher-ranked contractor (same owner) — "
                 "each inbox is listed once", summary.shared_email)
    return kept


def _tally(summary: RunSummary, row: dict, lead) -> None:
    summary.written += 1
    if row["first_name"] and row["last_name"]:
        summary.with_name += 1
        if lead.contact_level == "owner":
            summary.with_owner += 1
    if row["phone"]:
        summary.with_phone += 1
    if row["email"]:
        summary.with_email += 1
        if lead.email_belongs_to == "officer":
            summary.with_officer_email += 1
    if row["website"]:
        summary.with_website += 1
    if lead.listing.note and "may have moved" in lead.listing.note:
        summary.not_found += 1


def _preview(ranked, options, limit, rules, crules, summary) -> int:
    """Score from census facts alone, into a separate file. Nothing is marked
    done, so a real run afterwards still processes every contractor."""
    out = preview_path(options.out)
    with CsvWriter(out, resume=False, track_seen=False) as writer:
        for contractor in ranked:
            lead = build_lead(contractor, None, None, rules, crules, options.max_pages)
            row = build_row(lead, rules)
            writer.write(row)
            summary.attempted += 1
            _tally(summary, row, lead)

    # The preview starts over each time, and so does its left-out file.
    left_out_path(out).unlink(missing_ok=True)
    summary.chains = _leave_out_chains(out, crules, rules)
    sort_csv_by_score(out)
    next_run = min(limit, len(ranked)) if limit > 0 else len(ranked)
    log.info("preview only — no Google lookups made. The next real run would look up the "
             "top %d (about %d credits).", next_run, next_run)
    summary.report(out)
    return EXIT_OK


# --- command line ----------------------------------------------------------------------


def _zip_list(text: str) -> tuple[str, ...]:
    """'191,190' -> ('191', '190'); 'all' -> () meaning the whole state."""
    if text.strip().lower() in ("", "all"):
        return ()
    return tuple(z.strip() for z in text.split(",") if z.strip())


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="contractor_loanscout",
        description="Find contractors from the US DOT carrier census, with a company officer's "
        "name, phone, email and an evidence-backed score.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="find, look up and score leads")
    run.add_argument("--state", help="two-letter state code (default: contractor.yaml, PA)")
    run.add_argument("--zip", type=_zip_list,
                     help="ZIP code starts, comma-separated, e.g. 191 or 191,190; 'all' for the whole state")
    run.add_argument("--limit", type=int,
                     help="contractors looked up this run (default: contractor.yaml, 50; 0 = no cap)")
    run.add_argument("--dry-run", action="store_true",
                     help="free preview from the US DOT census only; no lookups, no credits")
    run.add_argument("--out", default=DEFAULT_OUT, help="output CSV path")
    run.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES, help="website pages per contractor")
    run.add_argument("--workers", type=int, default=8, help="contractors processed at once")
    run.add_argument("--delay", type=float, default=1.0, help="seconds between requests to one website")
    run.add_argument("--no-resume", action="store_true",
                     help="start the output over instead of skipping contractors already done")
    run.add_argument("--verbose", "-v", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return run_leads(
        RunOptions(
            state=args.state,
            zip_prefixes=args.zip,
            limit=args.limit,
            dry_run=args.dry_run,
            out=args.out,
            max_pages=args.max_pages,
            workers=args.workers,
            delay=args.delay,
            no_resume=args.no_resume,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
