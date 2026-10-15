"""Keeping results to small-business leads: filing agencies' mailboxes,
corporate groups, disqualified companies, and inboxes already used in an
earlier run."""

import csv

from contractor_loanscout import cli, leads
from contractor_loanscout.cleanup import leave_out_chains, never_a_chain
from contractor_loanscout.dot import filing_agents, find_contractors
from contractor_loanscout.emailfinder import FinderResult
from contractor_loanscout.export import COLUMNS
from test_dot import TODAY, FakeSession, carrier, client
from test_run import StubPlaces, _rows, contractor, crawl_of, crules, listing, workdir  # noqa: F401


def record(dot, name, email):
    return carrier(dot_number=str(dot), legal_name=name, email_address=email)


# --- filing agencies ------------------------------------------------------------------------------


def test_a_domain_giving_each_client_a_named_mailbox_is_a_filing_agency():
    records = [record(1, "PENINSULA PAVING", "peninsula.paving@transervices.com"),
               record(2, "MERIT CONSTRUCTION ENGINEERS INC", "merit.construction@transervices.com"),
               record(3, "NAGEL FARM SERVICES INC", "nagel.farm.services@transervices.com"),
               record(4, "DRF PUMPING SERVICES", "delaray@transervices.com"),
               record(5, "GROUND CONTROL", "milford.housing@transervices.com")]
    agents, domains = filing_agents(records, 5)
    assert domains == {"transervices.com"}


def test_one_address_filed_for_many_carriers_is_still_an_agents():
    records = [record(i, f"CARRIER {i} PAVING", "wyatt@transervices.com") for i in range(5)]
    agents, _ = filing_agents(records, 5)
    assert agents == {"wyatt@transervices.com"}


def test_gmail_is_never_a_filing_agency():
    records = [record(i, f"LAVA LANDSCAPING {i}", f"lavalandscaping{i}@gmail.com") for i in range(8)]
    assert filing_agents(records, 5) == (set(), set())


def test_a_company_with_its_own_people_is_not_an_agency():
    records = [record(i, "NORTHSTAR CONTRACTING GROUP INC", f"{p}@northstar.com")
               for i, p in enumerate(["gbowman", "jryan", "rsemega", "tsmith", "kjones"])]
    assert filing_agents(records, 5) == (set(), set())


def test_an_agencys_mailbox_is_blanked_before_any_lookup(crules):
    class Agency(FakeSession):
        def get(self, url, params, timeout):
            response = super().get(url, params, timeout)
            for i, rec in enumerate(response.payload):
                rec["email_address"] = f"contractor{i}.paving@transervices.com"
            return response

    found = find_contractors(client(Agency(6)), crules, "DE", ("197",), TODAY)
    assert {c.email for c in found} == {""}


# --- corporate groups --------------------------------------------------------------------------------


def results(tmp_path, rows):
    path = tmp_path / "leads.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        for i, (name, email) in enumerate(rows):
            writer.writerow({"key": str(i), "company_name": name, "email": email})
    return path


def test_a_groups_subsidiaries_move_out_and_franchisees_stay(tmp_path):
    never = never_a_chain(["servpro.com"], [])
    path = results(tmp_path, [("Abs Insulating", "wbrush@installed.net"),
                              ("Anchor Insulation", "wbrush@installed.net"),
                              ("Accurate Insulation Of Delaware", "pamela.henson@installed.net"),
                              *[(f"Servpro Of Town {i}", f"owner{i}@servpro.com") for i in range(3)],
                              *[(f"Paving {i}", f"paving{i}@cox.net") for i in range(3)],
                              ("Spano Paving", "aspano@spanopaving.com")])
    assert leave_out_chains(path, COLUMNS, 3, never) == {"installed.net": 3}
    with path.open(newline="", encoding="utf-8") as handle:
        assert len(list(csv.DictReader(handle))) == 7


# --- disqualified, and inboxes used in an earlier run ----------------------------------------------


def test_no_paid_search_for_a_company_its_website_disqualifies(rules, crules, monkeypatch):
    class Finder:
        name = "Anymailfinder"
        asked = []

        def find(self, *args, **kwargs):
            self.asked.append(args)
            return FinderResult()

    finder = Finder()
    site = crawl_of(("https://spanoandsons.com/", "<p>We are a 501(c)(3) nonprofit organization.</p>"))
    monkeypatch.setattr(leads, "crawl_site", lambda *a, **k: site)
    c = contractor(crules, email_address="office@spanoandsons.com")
    lead = leads.build_lead(c, StubPlaces(), object(), rules, crules, 8, TODAY, email_finder=finder)
    assert lead.score.disqualified and finder.asked == []


def test_a_disqualified_company_goes_to_the_left_out_file(workdir, monkeypatch):
    site = crawl_of(("https://spanoandsons.com/", "<p>We are a 501(c)(3) nonprofit organization.</p>"))
    monkeypatch.setattr(leads, "crawl_site", lambda *a, **k: site)
    options = cli.RunOptions(zip_prefixes=("191",), limit=0, out="out/leads.csv", workers=1)
    assert cli.run_leads(options, census=client(FakeSession(2)), places=StubPlaces()) == cli.EXIT_OK
    assert _rows(workdir / "out/leads.csv") == []
    left = _rows(workdir / "out/leads_left_out.csv")
    assert len(left) == 2 and left[0]["left_out_reason"].startswith("disqualified: nonprofit")


def test_an_inbox_already_used_in_an_earlier_run_is_not_listed_again(workdir):
    class SameInbox(FakeSession):
        def __init__(self, count, start):
            super().__init__(count)
            self.start = start

        def get(self, url, params, timeout):
            response = super().get(url, params, timeout)
            for i, rec in enumerate(response.payload):
                rec["dot_number"] = str(self.start + i)
                rec["email_address"] = "sharedfamilybusiness@gmail.com"
            return response

    options = cli.RunOptions(zip_prefixes=("191",), limit=0, out="out/leads.csv", workers=1)
    cli.run_leads(options, census=client(SameInbox(1, 100)), places=StubPlaces(listing(website="")))
    # Another state's run: a different company, the same inbox.
    cli.run_leads(options, census=client(SameInbox(1, 200)), places=StubPlaces(listing(website="")))
    assert [r["email"] for r in _rows(workdir / "out/leads.csv")] == ["sharedfamilybusiness@gmail.com"]


def test_a_government_page_is_never_a_group(tmp_path):
    path = tmp_path / "leads.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        for i in range(5):
            writer.writerow({"key": str(i), "company_name": f"Trucking {i}", "email": f"t{i}@trucking{i}.com",
                             "website": f"https://safer.fmcsa.dot.gov/query.asp?n={i}"})
    assert leave_out_chains(path, COLUMNS, 3, never_a_chain([], [])) == {}
