"""Whose inbox an email is, and the email finder — stubbed, so no credits are spent."""

import pytest

from contractor_loanscout import cli, leads
from contractor_loanscout.export import build_row
from contractor_loanscout.anymailfinder import AnymailfinderUnavailable
from contractor_loanscout.emailfinder import FinderResult
from test_dot import TODAY
from test_run import StubPlaces, _census, contractor, crawl_of, crules, listing, workdir  # noqa: F401


# --- whose inbox the filed email is -------------------------------------------------------------


def northstar(crules, **overrides):
    values = dict(legal_name="NORTHSTAR CONTRACTING GROUP INC", company_officer_1="GARY BOWMAN",
                  company_officer_2="RICH SEMEGA", email_address="jryan@northstar.com")
    values.update(overrides)
    return contractor(crules, **values)


@pytest.mark.parametrize("email,owner", [
    ("gbowman@northstar.com", "officer"),
    ("gary@northstar.com", "officer"),
    ("rsemega@northstar.com", "second officer"),
    ("info@northstar.com", "company inbox"),
    ("northstarcontracting@gmail.com", "company inbox"),
    ("jryan@northstar.com", "someone else"),
    ("murf716@yahoo.com", "unclear"),
])
def test_whose_inbox_the_email_is(crules, email, owner):
    assert leads.email_owner(email, northstar(crules)) == owner


def test_a_nickname_is_the_officers(crules):
    c = contractor(crules, company_officer_1="RICHARD GALASSO", email_address="rich@galassotrucking.com")
    assert leads.email_owner(c.email, c) == "officer"


def test_someone_elses_email_is_labelled_so_the_message_isnt_addressed_to_the_officer(rules, crules):
    row = build_row(leads.build_lead(northstar(crules), None, None, rules, crules, 8, TODAY), rules)
    assert (row["first_name"], row["email"]) == ("Gary", "jryan@northstar.com")
    assert row["email_belongs_to"].startswith("someone else at the company")
    assert row["email_source"] == "US DOT filing"


def test_the_second_officers_email_makes_them_the_contact(rules, crules):
    c = northstar(crules, email_address="rsemega@northstar.com")
    row = build_row(leads.build_lead(c, None, None, rules, crules, 8, TODAY), rules)
    assert (row["first_name"], row["last_name"], row["email"]) == ("Rich", "Semega", "rsemega@northstar.com")
    assert row["second_officer"] == "Gary Bowman"
    assert row["email_belongs_to"] == "the officer named"


# --- when the email finder is asked -------------------------------------------------------------------------


class StubFinder:
    name = "Anymailfinder"
    def __init__(self, email="gbowman@northstar.com"):
        self.email = email
        self.asked = []

    def find(self, domain, first, last):
        self.asked.append((domain, first, last))
        return FinderResult(self.email, "valid" if self.email else "")


def test_email_finder_finds_the_officers_own_address_and_the_filed_one_is_kept(rules, crules):
    finder = StubFinder()
    lead = leads.build_lead(northstar(crules), StubPlaces(listing(website="")), None, rules, crules, 8, TODAY,
                            email_finder=finder)
    assert finder.asked == [("northstar.com", "Gary", "Bowman")], "the domain comes from the filed email"
    row = build_row(lead, rules)
    assert (row["first_name"], row["email"]) == ("Gary", "gbowman@northstar.com")
    assert row["filed_email"] == "jryan@northstar.com"
    assert row["email_source"].startswith("Anymailfinder (verified")
    assert row["email_belongs_to"] == "the officer named"


def test_nothing_verified_keeps_the_filed_email_labelled(rules, crules):
    lead = leads.build_lead(northstar(crules), StubPlaces(listing(website="")), None, rules, crules, 8, TODAY,
                            email_finder=StubFinder(""))
    assert (lead.email, lead.email_belongs_to, lead.filed_email) == ("jryan@northstar.com", "someone else", "")


def test_email_finder_is_not_asked_when_the_email_is_the_officers(rules, crules):
    finder = StubFinder()
    leads.build_lead(northstar(crules, email_address="gbowman@northstar.com"), StubPlaces(), None,
                     rules, crules, 8, TODAY, email_finder=finder)
    assert finder.asked == []


def test_a_gmail_only_company_uses_its_website_domain_or_nothing(rules, crules):
    finder = StubFinder("gbowman@northstarcg.com")
    c = northstar(crules, email_address="jryan77@gmail.com")
    leads.build_lead(c, StubPlaces(listing(website="")), None, rules, crules, 8, TODAY, email_finder=finder)
    assert finder.asked == [], "no company domain to search"
    lead = leads.build_lead(c, StubPlaces(listing(website="https://northstarcg.com/")), None, rules, crules, 8,
                            TODAY, email_finder=finder)
    assert finder.asked == [("northstarcg.com", "Gary", "Bowman")]
    assert lead.email == "gbowman@northstarcg.com"


def test_the_officers_address_on_the_website_beats_asking_email_finder(rules, crules, monkeypatch):
    site = crawl_of(("https://spanoandsons.com/team", "<p>gbowman@spanoandsons.com</p>"))
    monkeypatch.setattr(leads, "crawl_site", lambda *a, **k: site)
    finder = StubFinder()
    lead = leads.build_lead(northstar(crules), StubPlaces(), object(), rules, crules, 8, TODAY, email_finder=finder)
    assert finder.asked == []
    assert (lead.email, lead.filed_email) == ("gbowman@spanoandsons.com", "jryan@northstar.com")


def test_a_run_stops_when_email_finder_runs_out(workdir, monkeypatch):
    class OutOfCredits:
        name = "Anymailfinder"
        lookups, found = 1, 0

        def find(self, *args, **kwargs):
            raise AnymailfinderUnavailable("Anymailfinder is out of credits")

        def close(self):
            pass

    from test_dot import FakeSession, client

    class OtherPerson(FakeSession):
        def get(self, url, params, timeout):
            response = super().get(url, params, timeout)
            for i, rec in enumerate(response.payload):
                rec["email_address"] = f"office.manager{i}@contractor{i}.com"
            return response

    monkeypatch.setattr("contractor_loanscout.emailcheck.EmailChecker.problem", lambda self, e: "")
    options = cli.RunOptions(zip_prefixes=("191",), limit=0, out="out/leads.csv", workers=1)
    result = cli.run_leads(options, census=client(OtherPerson(3)), places=StubPlaces(listing(website="")),
                           email_finder=OutOfCredits())
    assert result == cli.EXIT_SERPER_UNAVAILABLE
