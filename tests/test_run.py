"""Leads, CSV rows, and whole runs — with the census, Serper, and websites stubbed."""

import csv
import threading
from datetime import date

import pytest

from contractor_loanscout import cli, leads
from contractor_loanscout.crawl import SiteCrawl
from contractor_loanscout.dot import find_contractors, load_contractor_rules, parse_carrier
from contractor_loanscout.export import COLUMNS, build_row
from contractor_loanscout.extract import build_page
from contractor_loanscout.places import Listing, SerperUnavailable
from test_dot import TODAY, FakeSession, carrier, client


@pytest.fixture(scope="module")
def crules():
    return load_contractor_rules()


def contractor(crules, **overrides):
    return parse_carrier(carrier(**overrides), crules, TODAY)


def listing(**overrides):
    values = dict(name="Mike Spano & Sons", address="1234 Orthodox St, Philadelphia, PA 19124",
                  phone="+1 215-555-0177", website="https://spanoandsons.com/", matched_on="address",
                  rating=4.6, reviews=88, category="Paving contractor")
    values.update(overrides)
    return Listing(**values)


class StubPlaces:
    def __init__(self, result=None, error=None):
        self.result = result or listing()
        self.error = error
        self.lookups = 0
        self.queries = []

    def find(self, *args):
        self.lookups += 1
        self.queries.append(args)
        if self.error:
            raise self.error
        return self.result

    def close(self):
        pass


def crawl_of(*pages):
    crawl = SiteCrawl(domain="spanoandsons.com", start_url="https://spanoandsons.com/")
    crawl.pages = [build_page(url, html) for url, html in pages]
    return crawl


# --- scoring ------------------------------------------------------------------------------


def test_census_facts_score_without_any_lookup(rules, crules):
    lead = leads.build_lead(contractor(crules), None, None, rules, crules, 8, TODAY)
    found = set(lead.score.signal_ids)
    assert {"dot_registered", "dot_established", "dot_recently_updated", "officer_owner",
            "fleet_medium", "email_on_file"} <= found
    assert "Alfonso M Spano (surname in the business name)" in " ".join(h.snippet for h in lead.score.hits)


def test_google_facts_from_serper_raise_the_weight(rules, crules):
    preview = leads.build_lead(contractor(crules), None, None, rules, crules, 8, TODAY)
    looked_up = leads.build_lead(contractor(crules), StubPlaces(), None, rules, crules, 8, TODAY)
    assert {"google_listing", "google_reviews_many", "google_good_rating"} <= set(looked_up.score.signal_ids)
    assert looked_up.score.total > preview.score.total


def test_a_bigger_fleet_outweighs_a_smaller_one(rules, crules):
    big = leads.build_lead(contractor(crules, power_units="25"), None, None, rules, crules, 8, TODAY)
    small = leads.build_lead(contractor(crules, power_units="1"), None, None, rules, crules, 8, TODAY)
    assert "fleet_large" in big.score.signal_ids
    assert big.score.total > small.score.total


def test_lookup_uses_the_public_name_and_census_address(rules, crules):
    places = StubPlaces()
    leads.build_lead(contractor(crules), places, None, rules, crules, 8, TODAY)
    assert places.queries[0] == ("Mike Spano & Sons", "1234 Orthodox St", "Philadelphia", "PA", "19124")


def test_google_category_names_the_trade_when_the_business_name_doesnt(rules, crules):
    c = contractor(crules, legal_name="BUCKLEY & COMPANY INC", company_officer_1="ANTHONY V LUCERI III")
    assert c.trade == leads.DEFAULT_TRADE
    lead = leads.build_lead(c, StubPlaces(listing(category="Paving contractor")), None, rules, crules, 8, TODAY)
    assert lead.trade == "Paving & asphalt"


def test_no_listing_keeps_the_census_contacts(rules, crules):
    gone = StubPlaces(Listing(note="no listings found"))
    row = build_row(leads.build_lead(contractor(crules), gone, None, rules, crules, 8, TODAY), rules)
    assert "may have moved" in row["notes"]
    assert (row["phone"], row["email"]) == ("(215) 423-3054", "spanoandsons@comcast.net")


# --- email -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "email,belongs",
    [("aspano@spanoandsons.com", True), ("alfonso@x.com", True), ("info@x.com", False), ("mark@x.com", False)],
)
def test_email_belongs_to_the_officer(email, belongs):
    assert leads.email_belongs_to(email, "Alfonso", "Spano") is belongs


def test_the_officers_own_website_email_is_found(crules):
    crawl = crawl_of(
        ("https://spanoandsons.com/", '<a href="mailto:office@spanoandsons.com">Email</a>'),
        ("https://spanoandsons.com/about", "<p>Reach Al at aspano@spanoandsons.com</p>"),
    )
    assert leads.website_email(crawl, contractor(crules)) == (
        "aspano@spanoandsons.com", "https://spanoandsons.com/about", True)


def test_the_census_email_comes_first_and_the_websites_beside_it(rules, crules, monkeypatch):
    site = crawl_of(("https://spanoandsons.com/about", "<p>aspano@spanoandsons.com</p>"))
    monkeypatch.setattr(leads, "crawl_site", lambda *a, **k: site)
    lead = leads.build_lead(contractor(crules), StubPlaces(), object(), rules, crules, 8, TODAY)
    row = build_row(lead, rules)
    assert row["email"] == "spanoandsons@comcast.net"
    assert row["website_email"] == "aspano@spanoandsons.com (officer's own)"


def test_without_a_census_email_the_websites_is_used(rules, crules, monkeypatch):
    site = crawl_of(("https://spanoandsons.com/contact", '<a href="mailto:office@spanoandsons.com">E</a>'))
    monkeypatch.setattr(leads, "crawl_site", lambda *a, **k: site)
    lead = leads.build_lead(contractor(crules, email_address=""), StubPlaces(), object(), rules, crules, 8, TODAY)
    row = build_row(lead, rules)
    assert row["email"] == "office@spanoandsons.com"
    assert row["website_email"] == ""


# --- rows -------------------------------------------------------------------------------------


def test_row_leads_with_the_requested_columns(rules, crules):
    assert COLUMNS[:9] == ["first_name", "last_name", "title", "decision_maker", "email", "phone",
                           "company_name", "trade", "score"]
    row = build_row(leads.build_lead(contractor(crules), StubPlaces(), None, rules, crules, 8, TODAY), rules)
    assert (row["first_name"], row["last_name"]) == ("Alfonso", "Spano")
    assert row["decision_maker"] == "owner (surname in business name)"
    assert row["phone_source"] == "US DOT filing"
    assert (row["trucks"], row["drivers"], row["usdot_number"]) == (5, 5, "123456")
    assert (row["google_rating"], row["google_reviews"]) == ("4.6", 88)
    assert row["second_officer"] == "Michael Spano"
    assert set(row) == set(COLUMNS)


def test_a_poor_safety_rating_is_noted(rules, crules):
    row = build_row(leads.build_lead(contractor(crules, safety_rating="U"), None, None, rules, crules, 8, TODAY),
                    rules)
    assert "safety rating: Unsatisfactory" in row["notes"]


# --- whole runs -------------------------------------------------------------------------------


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SERPER_API_KEY", "test-key")
    monkeypatch.setenv("HUNTER_API_KEY", "")  # never spend real Hunter credits in tests
    monkeypatch.setenv("ANYMAILFINDER_API_KEY", "")  # nor Anymailfinder credits
    return tmp_path


def _census(count=3):
    return client(FakeSession(count))


def _rows(path):
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_preview_is_free_and_writes_every_contractor(workdir):
    places = StubPlaces()
    options = cli.RunOptions(zip_prefixes=("191",), dry_run=True, out="out/leads.csv")
    assert cli.run_leads(options, census=_census(5), places=places) == cli.EXIT_OK
    assert places.lookups == 0
    assert len(_rows(workdir / "out/leads_preview.csv")) == 5


def test_run_looks_up_the_limit_and_resumes_where_it_stopped(workdir):
    places = StubPlaces(listing(website=""))
    options = cli.RunOptions(zip_prefixes=("191",), limit=2, out="out/leads.csv", workers=2)
    assert cli.run_leads(options, census=_census(3), places=places) == cli.EXIT_OK
    assert places.lookups == 2
    assert cli.run_leads(options, census=_census(3), places=places) == cli.EXIT_OK
    assert places.lookups == 3, "the second run only looked up the one left"
    assert len(_rows(workdir / "out/leads.csv")) == 3
    assert cli.run_leads(options, census=_census(3), places=places) == cli.EXIT_DEPLETED


def test_rows_are_sorted_by_score(workdir):
    options = cli.RunOptions(zip_prefixes=("191",), limit=0, out="out/leads.csv", workers=1)
    cli.run_leads(options, census=_census(4), places=StubPlaces(listing(website="")))
    scores = [float(r["score"]) for r in _rows(workdir / "out/leads.csv")]
    assert scores == sorted(scores, reverse=True)


def test_out_of_credits_stops_the_run(workdir):
    places = StubPlaces(error=SerperUnavailable("Serper is out of credits"))
    options = cli.RunOptions(zip_prefixes=("191",), out="out/leads.csv")
    assert cli.run_leads(options, census=_census(), places=places) == cli.EXIT_SERPER_UNAVAILABLE


def test_one_failed_lookup_is_retried_next_run(workdir):
    places = StubPlaces(error=ConnectionError("network blip"))
    options = cli.RunOptions(zip_prefixes=("191",), out="out/leads.csv")
    assert cli.run_leads(options, census=_census(1), places=places) == cli.EXIT_LOOKUPS_FAILED
    places.error = None
    places.result = listing(website="")
    assert cli.run_leads(options, census=_census(1), places=places) == cli.EXIT_OK
    assert len(_rows(workdir / "out/leads.csv")) == 1


def test_nothing_found_is_its_own_outcome(workdir):
    options = cli.RunOptions(zip_prefixes=("191",), dry_run=True, out="out/leads.csv")
    assert cli.run_leads(options, census=_census(0), places=StubPlaces()) == cli.EXIT_NO_MATCHES


def test_a_bad_zip_stops_before_searching(workdir):
    session = FakeSession()
    assert cli.run_leads(cli.RunOptions(zip_prefixes=("19x",), dry_run=True),
                         census=client(session)) == cli.EXIT_CONFIG_ERROR
    assert session.calls == []


def test_run_without_a_key_explains_how_to_add_one(workdir, monkeypatch):
    monkeypatch.setenv("SERPER_API_KEY", "")
    monkeypatch.setattr("contractor_loanscout.config.load_dotenv", lambda *a, **k: None)
    options = cli.RunOptions(zip_prefixes=("191",), out="out/leads.csv")
    assert cli.run_leads(options, census=_census()) == cli.EXIT_CONFIG_ERROR


def test_stop_during_lookups_keeps_finished_rows(workdir):
    stop = threading.Event()

    class SlowPlaces(StubPlaces):
        def find(self, *args):
            stop.set()  # pressing Stop while the first lookups are in flight
            return super().find(*args)

    options = cli.RunOptions(zip_prefixes=("191",), limit=0, out="out/leads.csv", workers=1)
    result = cli.run_leads(options, stop, census=_census(20), places=SlowPlaces(listing(website="")))
    assert result == cli.EXIT_INTERRUPTED
    assert len(_rows(workdir / "out/leads.csv")) < 20


# --- only leads with a working email ----------------------------------------------------------


def test_contractors_without_a_working_email_are_left_out_before_any_lookup(workdir, monkeypatch):
    emails = {0: "", 1: "none@none.com", 2: "bob@gmial.com", 3: "contractor3@gmail.com",
              4: "contractor3@gmail.com", 5: "contractor5@gmail.com"}

    class Session(FakeSession):
        def get(self, url, params, timeout):
            response = super().get(url, params, timeout)
            for i, rec in enumerate(response.payload if isinstance(response.payload, list) else []):
                rec["email_address"] = emails[i]
            return response

    places = StubPlaces(listing(website=""))
    options = cli.RunOptions(zip_prefixes=("191",), limit=0, out="out/leads.csv")
    assert cli.run_leads(options, census=client(Session(6)), places=places) == cli.EXIT_OK
    rows = _rows(workdir / "out/leads.csv")
    assert sorted(r["email"] for r in rows) == ["contractor3@gmail.com", "contractor5@gmail.com"]
    assert places.lookups == 2, "no credits spent on the ones left out"


def test_an_email_filed_for_many_carriers_is_an_agents(crules):
    class Session(FakeSession):
        def get(self, url, params, timeout):
            response = super().get(url, params, timeout)
            for rec in response.payload:
                rec["email_address"] = "filings@dotcompliance.com"
            return response

    found = find_contractors(client(Session(5)), crules, "PA", ("191",), TODAY)
    assert {c.email for c in found} == {""}


def test_a_website_email_on_someone_elses_domain_is_not_theirs(crules):
    crawl = crawl_of(("https://spanoandsons.com/", "<footer>Site by design@webshop.com</footer>"))
    assert leads.website_email(crawl, contractor(crules)) == ("", "", False)


# --- the email check on a big area ------------------------------------------------------------


class SlowResolver:
    """DNS that takes a while to answer each domain, like a whole state's worth."""

    def __init__(self, delay):
        self.delay = delay
        self.asked = []

    def resolve(self, domain, kind):
        import time
        self.asked.append(domain)
        time.sleep(self.delay)

        class Mx:
            exchange = f"mx.{domain}."
        return [Mx()]


def test_stop_during_the_email_check_is_prompt(crules):
    import time
    from contractor_loanscout.dot import SearchStopped
    from contractor_loanscout.emailcheck import EmailChecker

    many = [contractor(crules, dot_number=str(i), email_address=f"office@contractor{i}.com") for i in range(200)]
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    started = time.monotonic()
    with pytest.raises(SearchStopped):
        cli.keep_working_emails(many, EmailChecker(resolver=SlowResolver(2.0)), cli.RunSummary(), stop)
    assert time.monotonic() - started < 1.5, "Stop shouldn't wait for every domain"


def test_each_email_domain_is_asked_once(crules):
    from contractor_loanscout.emailcheck import EmailChecker

    resolver = SlowResolver(0)
    shared = [contractor(crules, dot_number=str(i), email_address=f"person{i}@bigcontractor.com") for i in range(5)]
    kept = cli.keep_working_emails(shared, EmailChecker(resolver=resolver), cli.RunSummary())
    assert len(kept) == 5
    assert resolver.asked == ["bigcontractor.com"]
