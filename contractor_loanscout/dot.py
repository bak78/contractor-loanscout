"""Contractors from the US DOT carrier census (FMCSA Company Census).

Every business that runs commercial trucks registers with the US Department of
Transportation and files an MCS-150 form, renewed every two years. The form
names up to two company officers and gives the business phone and email, and
says what the carrier hauls — so contractors with trucks (excavation, paving,
roofing, concrete, hauling) can be found with a person to call, nationwide,
for free.

The census is published on the US DOT's Socrata data site. Without an app
token that site slows down repeated searches ("Too many requests"), so a busy
search waits and retries, and results are cached for a week.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
import yaml

from .config import DATA_DIR, DEFAULT_CONTRACTOR_PATH, ConfigError
from .cleanup import SHARED_MAIL
from .contacts import _format_phone
from .emailcheck import FREE_MAIL

log = logging.getLogger(__name__)

CENSUS_URL = "https://datahub.transportation.gov/resource/az4n-8mr2.json"
PAGE_SIZE = 50_000
TIMEOUT = 120
RETRIES = 6
CACHE_DIR = DATA_DIR / "dot_cache"
CACHE_DAYS = 7

# Only these columns are fetched.
COLUMNS = [
    "dot_number", "legal_name", "dba_name", "company_officer_1", "company_officer_2",
    "phone", "cell_phone", "email_address", "phy_street", "phy_city", "phy_state", "phy_zip",
    "power_units", "total_drivers", "add_date", "mcs150_date", "business_org_desc",
    "carrier_operation", "safety_rating", "classdef",
]

CARGO_COLUMNS = {"crgo_construct", "crgo_bldgmat", "crgo_machlrg", "crgo_drivetow",
                 "crgo_logpole", "crgo_metalsheet", "crgo_utility", "crgo_waterwell",
                 "crgo_garbage", "crgo_drybulk"}

SAFETY_RATINGS = {"S": "Satisfactory", "C": "Conditional", "U": "Unsatisfactory"}


class DotUnavailable(ConnectionError):
    """The US DOT data site couldn't be reached or refused a search."""


class SearchStopped(Exception):
    """The search was cancelled with Stop or Ctrl+C."""


# --- settings ----------------------------------------------------------------------


@dataclass(frozen=True)
class Trade:
    name: str
    words: tuple[str, ...]


@dataclass(frozen=True)
class ContractorRules:
    """Everything loaded from contractor.yaml."""

    state: str
    zip_prefixes: tuple[str, ...]
    default_limit: int
    cargo: tuple[str, ...]
    min_power_units: int
    max_power_units: int
    max_years_since_update: int
    exclude_name_words: tuple[str, ...]
    trades: tuple[Trade, ...]
    weights: dict = field(default_factory=dict)
    freight_words: tuple[str, ...] = ()
    require_email: bool = True
    agent_email_carriers: int = 5
    email_finder_when: str = "not_officer"
    chain_min_companies: int = 3
    franchise_domains: tuple[str, ...] = ()
    national_chains: tuple[str, ...] = ()

    def excluded(self, *names: str) -> str:
        """Why a carrier is never a lead, by its names, or ''."""
        for name in names:
            lowered = f" {(name or '').lower()} "
            for words in self.exclude_name_words:
                if re.search(rf"(?<![a-z0-9]){re.escape(words.lower())}(?![a-z0-9])", lowered):
                    return f"name contains '{words}'"
        return ""

    def freight(self, *names: str) -> bool:
        """Whether a name reads like a freight trucking company."""
        lowered = " ".join(f" {(n or '').lower()} " for n in names)
        return any(re.search(rf"(?<![a-z]){re.escape(w.lower())}(?![a-z])", lowered)
                   for w in self.freight_words)

    def trade_of(self, *texts: str) -> str:
        """The first trade whose words appear in any of the texts, or ''."""
        lowered = " ".join(f" {(t or '').lower()} " for t in texts)
        for trade in self.trades:
            if any(word.lower() in lowered for word in trade.words):
                return trade.name
        return ""


def load_contractor_rules(path: Path | str = DEFAULT_CONTRACTOR_PATH) -> ContractorRules:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"settings file not found: {path}")
    with path.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    state = str(raw.get("state", "PA")).upper()
    if not re.fullmatch(r"[A-Z]{2}", state):
        raise ConfigError(f"contractor.yaml state must be a two-letter code, got {state!r}")
    prefixes = tuple(str(z) for z in raw.get("zip_prefixes") or [])
    check_zip_prefixes(prefixes)

    cargo = tuple(str(c) for c in raw.get("cargo") or [])
    unknown = [c for c in cargo if c not in CARGO_COLUMNS]
    if not cargo or unknown:
        raise ConfigError(f"contractor.yaml cargo must list census cargo columns, e.g. crgo_construct; "
                          f"unknown: {unknown or 'none listed'}")

    trades = []
    for entry in raw.get("trades") or []:
        name = str(entry.get("name") or "").strip()
        words = tuple(str(w) for w in entry.get("words") or [])
        if not name or not words:
            raise ConfigError(f"each contractor.yaml trade needs a name and words, got {entry!r}")
        trades.append(Trade(name, words))

    low, high = int(raw.get("min_power_units", 1)), int(raw.get("max_power_units", 150))
    if low > high:
        raise ConfigError("contractor.yaml min_power_units is larger than max_power_units")

    return ContractorRules(
        state=state,
        zip_prefixes=prefixes,
        default_limit=int(raw.get("default_limit", 50)),
        cargo=cargo,
        min_power_units=low,
        max_power_units=high,
        max_years_since_update=int(raw.get("max_years_since_update", 4)),
        exclude_name_words=tuple(str(w) for w in raw.get("exclude_name_words") or []),
        trades=tuple(trades),
        weights=dict(raw.get("weights") or {}),
        freight_words=tuple(str(w) for w in raw.get("freight_words") or []),
        require_email=bool(raw.get("require_email", True)),
        agent_email_carriers=int(raw.get("agent_email_carriers", 5)),
        email_finder_when=_email_finder_when(raw.get("email_finder_when", "not_officer")),
        chain_min_companies=int(raw.get("chain_min_companies", 3)),
        franchise_domains=tuple(str(d).strip().lower() for d in raw.get("franchise_domains") or []),
        national_chains=tuple(str(d).strip().lower() for d in raw.get("national_chains") or []),
    )


def _email_finder_when(value) -> str:
    value = str(value).strip().lower()
    if value not in ("not_officer", "never"):
        raise ConfigError(f"contractor.yaml email_finder_when must be 'not_officer' or 'never', got {value!r}")
    return value


def check_zip_prefixes(prefixes) -> None:
    for prefix in prefixes:
        if not re.fullmatch(r"\d{1,5}", prefix):
            raise ConfigError(f"ZIP code start {prefix!r} must be 1 to 5 digits")


# --- names ----------------------------------------------------------------------------


_KEEP_UPPER = {"LLC", "PC", "PLLC", "LLP", "LP", "II", "III", "IV", "USA", "HVAC", "JR", "SR"}
_SUFFIXES = {"JR", "SR", "II", "III", "IV", "V"}
_COMPANY_WORDS = re.compile(
    r"\b(llc|inc|corp|co|company|ltd|lp|llp|pc|trust|estate|enterprises|group|holdings|"
    r"construction|contracting|services|trucking|incorporated|corporation)\b", re.I)


def tidy_name(name: str) -> str:
    """'MIKE SPANO & SONS INC' -> 'Mike Spano & Sons Inc'; "O'BRIEN" -> "O'Brien".
    Names already in mixed case are kept as typed."""
    name = " ".join((name or "").split())
    if not name or not (name.isupper() or name.islower()):
        return name
    words = []
    for word in name.split():
        bare = re.sub(r"[^A-Za-z]", "", word).upper()
        if bare in _KEEP_UPPER and word.upper().strip(".,") == bare:
            words.append(word.upper())
            continue
        word = re.sub(r"(?<![0-9a-z])[a-z]+", lambda m: m.group().capitalize(), word.lower())
        words.append(re.sub(r"'S\b", "'s", word))
    return " ".join(words)


def split_person(full: str) -> tuple[str, str]:
    """('Anthony', 'Luceri') from 'ANTHONY V. LUCERI III' or 'LUCERI, ANTHONY V';
    ('', '') when the entry is a company, a single word, or empty."""
    full = " ".join((full or "").replace(".", " ").split())
    if not full or _COMPANY_WORDS.search(full) or re.search(r"\d", full):
        return "", ""
    # "ANTHONY V LUCERI, III": a comma before a suffix is not "LAST, FIRST".
    full = re.sub(r"\s*,\s*(JR|SR|II|III|IV|V)\s*$", "", full, flags=re.I)
    if "," in full:
        last_part, _, first_part = full.partition(",")
        firsts = first_part.split()
        lasts = [w for w in last_part.split() if w.upper() not in _SUFFIXES]
        if not firsts or not lasts:
            return "", ""
        first_name, last_name = firsts[0], " ".join(lasts)
    else:
        words = [w for w in full.split() if w.upper() not in _SUFFIXES]
        if len(words) < 2:
            return "", ""
        first_name, last_name = words[0], words[-1]
    if len(first_name) < 2 or len(last_name) < 2:
        return "", ""
    return tidy_name(first_name), tidy_name(last_name)


# --- contractors ----------------------------------------------------------------------------


def _date(value: str) -> str:
    """'20251128' -> '2025-11-28'; '' when unreadable."""
    try:
        return datetime.strptime((value or "")[:8], "%Y%m%d").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _int(value) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _years_since(iso: str, today: date) -> int:
    try:
        then = datetime.strptime(iso, "%Y-%m-%d").date()
    except ValueError:
        return 0
    return today.year - then.year - ((today.month, today.day) < (then.month, then.day))


@dataclass(frozen=True)
class Contractor:
    """One contractor from the census, with its officers."""

    dot_number: str
    legal_name: str
    dba: str
    address: str
    city: str
    state: str
    zip: str
    phone: str
    cell_phone: str
    email: str
    officer_first: str
    officer_last: str
    officer_full: str
    officer_level: str  # owner (surname in the business name), officer, unknown
    second_officer: str
    power_units: int
    drivers: int
    registered: str  # YYYY-MM-DD added to the census
    updated: str  # YYYY-MM-DD of the latest MCS-150 filing
    business_type: str
    interstate: bool
    safety_rating: str
    trade: str
    hauls: str = ""  # "own trucks" (a contractor's), "for hire", or both

    @property
    def key(self) -> str:
        return self.dot_number

    @property
    def company_name(self) -> str:
        """The name the public knows it by: the DBA when there is one."""
        return self.dba or self.legal_name

    @property
    def search_name(self) -> str:
        name = self.company_name
        for _ in range(2):
            name = re.sub(r"(?i)[,.]?\s+\b(llc|inc|corp|co|company|ltd|lp|llp|pc)\.?\s*$", "", name)
        return name.strip(" ,.")

    def years_registered(self, today: date | None = None) -> int:
        return _years_since(self.registered, today or date.today())

    def years_since_update(self, today: date | None = None) -> int:
        return _years_since(self.updated, today or date.today())


def _clean_dba(dba: str, legal: str) -> str:
    """'DBA A DIVISION OF HARSCO CORPORATION' -> 'A Division Of Harsco Corporation';
    '' when it only repeats the legal name."""
    dba = re.sub(r"(?i)^\s*(dba|d/b/a|t/a)\s+", "", dba or "").strip()
    if not dba or re.sub(r"\W", "", dba.lower()) == re.sub(r"\W", "", (legal or "").lower()):
        return ""
    return dba


def parse_carrier(record: dict, rules: ContractorRules, today: date | None = None) -> Contractor | None:
    """A Contractor from one census record, or None when it isn't a lead."""
    today = today or date.today()
    legal = (record.get("legal_name") or "").strip()
    dba = _clean_dba(record.get("dba_name"), legal)
    if not legal or rules.excluded(legal, dba):
        return None

    power_units = _int(record.get("power_units"))
    if not rules.min_power_units <= power_units <= rules.max_power_units:
        return None
    updated = _date(record.get("mcs150_date"))
    if not updated or _years_since(updated, today) >= rules.max_years_since_update:
        return None

    # Freight companies tick "construction" among many cargo types. A carrier
    # hauling only for hire stays a lead only when its name shows a trade
    # ("Carusone Construction", "Galasso Trucking") and no freight wording.
    classdef = (record.get("classdef") or "").upper()
    own_trucks = "PRIVATE" in classdef or not classdef
    for_hire = "FOR HIRE" in classdef
    trade = rules.trade_of(legal, dba)
    if not own_trucks and (rules.freight(legal, dba) or not trade):
        return None
    hauls = ("own work and for hire" if own_trucks and for_hire
             else "own work" if own_trucks else "for hire")

    officers = [o for o in (record.get("company_officer_1"), record.get("company_officer_2")) if (o or "").strip()]
    first = last = full = ""
    second = ""
    for officer in officers:
        f, l = split_person(officer)
        if f and not first:
            first, last, full = f, l, tidy_name(officer)
        elif f and not second:
            second = tidy_name(officer)
    names = f"{legal} {dba}".lower()
    if first and re.search(rf"\b{re.escape(last.lower())}\b", names):
        level = "owner"
    elif first:
        level = "officer"
    else:
        level = "unknown"

    email = (record.get("email_address") or "").strip().lower()
    if not re.fullmatch(r"[\w.%+-]+@[\w.-]+\.[a-z]{2,}", email):
        email = ""

    return Contractor(
        dot_number=str(record.get("dot_number") or ""),
        legal_name=tidy_name(legal),
        dba=tidy_name(dba),
        address=tidy_name(record.get("phy_street") or ""),
        city=tidy_name(record.get("phy_city") or ""),
        state=(record.get("phy_state") or "").upper(),
        zip=(record.get("phy_zip") or "")[:5],
        phone=_format_phone(re.sub(r"\D", "", record.get("phone") or "")),
        cell_phone=_format_phone(re.sub(r"\D", "", record.get("cell_phone") or "")),
        email=email,
        officer_first=first,
        officer_last=last,
        officer_full=full,
        officer_level=level,
        second_officer=second,
        power_units=power_units,
        drivers=_int(record.get("total_drivers")),
        registered=_date(record.get("add_date")),
        updated=updated,
        business_type=(record.get("business_org_desc") or "").title(),
        interstate=(record.get("carrier_operation") or "") == "A",
        safety_rating=SAFETY_RATINGS.get(record.get("safety_rating") or "", ""),
        trade=trade or "Construction (trade not in name)",
        hauls=hauls,
    )


# --- searching -----------------------------------------------------------------------------


class CensusClient:
    """The census on the US DOT's Socrata site, with paging, waiting out
    rate limits, and a week-long cache."""

    def __init__(self, session=None, app_token: str | None = None, cache_dir: Path | None = CACHE_DIR,
                 stop: threading.Event | None = None, wait_base: float = 5.0):
        self._session = session or requests.Session()
        if app_token:
            self._session.headers["X-App-Token"] = app_token
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.stop = stop or threading.Event()
        self.wait_base = wait_base
        self.requests = 0

    def _get(self, params: dict) -> list[dict]:
        last = ""
        for attempt in range(RETRIES):
            if self.stop.is_set():
                raise SearchStopped()
            try:
                response = self._session.get(CENSUS_URL, params=params, timeout=TIMEOUT)
                self.requests += 1
                if response.status_code == 200:
                    data = response.json()
                    if isinstance(data, list):
                        return data
                    last = str(data.get("message") or data)[:120]
                elif response.status_code == 429:
                    last = "busy (too many requests)"
                    log.info("US DOT data site is busy — waiting %ds and trying again "
                             "(a free SOCRATA_APP_TOKEN in .env avoids this)", self.wait_base * 2 ** attempt)
                else:
                    last = f"http {response.status_code}"
            except (requests.RequestException, ValueError) as exc:
                last = type(exc).__name__
            if self.stop.wait(self.wait_base * 2 ** attempt):
                raise SearchStopped()
        raise DotUnavailable(f"the US DOT data site didn't answer ({last}) — try again in a few minutes")

    def _cache_path(self, params: dict) -> Path | None:
        if self.cache_dir is None:
            return None
        digest = hashlib.sha1(json.dumps(params, sort_keys=True).encode()).hexdigest()[:20]
        return self.cache_dir / f"{digest}.json"

    def search(self, state: str, zip_prefixes: tuple[str, ...], cargo: tuple[str, ...]) -> list[dict]:
        """Every active carrier in the area hauling any of the cargo types."""
        conditions = [f"status_code='A'", f"phy_state='{state}'",
                      "(" + " OR ".join(f"{c}='X'" for c in cargo) + ")"]
        if zip_prefixes:
            conditions.append("(" + " OR ".join(f"phy_zip like '{z}%'" for z in zip_prefixes) + ")")
        params = {"$select": ",".join(COLUMNS), "$where": " AND ".join(conditions), "$order": "dot_number"}

        cache = self._cache_path(params)
        if cache is not None and cache.exists():
            try:
                saved = json.loads(cache.read_text(encoding="utf-8"))
                if datetime.now() - datetime.fromisoformat(saved["fetched"]) < timedelta(days=CACHE_DAYS):
                    return saved["results"]
            except (ValueError, KeyError, OSError):
                pass  # an unreadable cache entry is simply fetched again

        results: list[dict] = []
        offset = 0
        while True:
            page = self._get({**params, "$limit": PAGE_SIZE, "$offset": offset})
            results += page
            if len(page) < PAGE_SIZE:
                break
            offset += PAGE_SIZE

        if cache is not None:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"fetched": datetime.now().isoformat(), "results": results}),
                             encoding="utf-8")
        return results

    def close(self) -> None:
        self._session.close()


def find_contractors(client: CensusClient, rules: ContractorRules, state: str,
                     zip_prefixes: tuple[str, ...], today: date | None = None) -> list[Contractor]:
    """Every contractor lead in the area: census carriers hauling construction
    cargo, minus suppliers, public bodies, big fleets and stale filings."""
    records = client.search(state, zip_prefixes, rules.cargo)
    log.info("US DOT census: %d active carriers hauling construction cargo found", len(records))

    agents, agent_domains = filing_agents(records, rules.agent_email_carriers)

    leads = []
    for record in records:
        contractor = parse_carrier(record, rules, today)
        if contractor is not None:
            if contractor.email in agents or contractor.email.rpartition("@")[2] in agent_domains:
                contractor = replace(contractor, email="")
            leads.append(contractor)
    return leads


# Words in a carrier's name that say nothing about which carrier it is.
_GENERIC = {"inc", "llc", "corp", "company", "the", "and", "construction", "contracting", "services",
            "service", "group", "trucking", "transport", "transportation", "enterprises"}
# Fewest mailboxes named after their own carrier that mark a domain as a filing service's.
AGENT_NAMED_MAILBOXES = 3


def _named_after(email: str, record: dict) -> bool:
    """jones.paving@agency.com for Jones Paving Inc: a mailbox named after its carrier."""
    mailbox = re.sub(r"[^a-z]", "", email.partition("@")[0])
    words = re.findall(r"[a-z]+", f"{record.get('legal_name') or ''} {record.get('dba_name') or ''}".lower())
    return any(len(w) >= 4 and w not in _GENERIC and w in mailbox for w in words)


def filing_agents(records: list[dict], min_carriers: int) -> tuple[set[str], set[str]]:
    """Emails and domains that belong to a filing service, not the business.

    A service enters its own address on every carrier it files for
    (wyatt@transervices.com on dozens), or gives each client a mailbox named
    after it at its own domain (peninsula.paving@transervices.com,
    nagel.farm.services@transervices.com). Either reaches the service."""
    carriers_by_email: dict[str, set[str]] = {}
    by_domain: dict[str, dict[str, bool]] = {}
    for record in records:
        email = (record.get("email_address") or "").strip().lower()
        dot = str(record.get("dot_number") or "")
        if not email or "@" not in email:
            continue
        carriers_by_email.setdefault(email, set()).add(dot)
        by_domain.setdefault(email.rpartition("@")[2], {})[dot] = _named_after(email, record)
    agents = {e for e, dots in carriers_by_email.items() if len(dots) >= min_carriers}
    # Gmail, Comcast and the like hold countless mailboxes named after their
    # owners' companies (lavalandscaping@gmail.com): never a filing service.
    agent_domains = {d for d, named in by_domain.items()
                     if d not in FREE_MAIL and d not in SHARED_MAIL
                     and len(named) >= min_carriers and sum(named.values()) >= AGENT_NAMED_MAILBOXES}
    return agents, agent_domains
