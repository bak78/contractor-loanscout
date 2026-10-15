"""The checks that keep bad leads out of the results: an inbox listed in an
earlier batch, a department's inbox, a national company's branch, and a
neighbour's Google listing at the same address."""

from pathlib import Path

from contractor_loanscout import cli, leads
from contractor_loanscout.contacts import is_wrong_department
from contractor_loanscout.export import CsvWriter, remembered_emails
from contractor_loanscout.places import match_listing
from test_dot import FakeSession, client
from test_email_finder import StubFinder, northstar
from test_run import TODAY, StubPlaces, _rows, contractor, crawl_of, crules, listing, workdir  # noqa: F401

# --- emails remembered across batches -----------------------------------------------------------


def test_an_email_is_remembered_after_its_batch_is_deleted(tmp_path):
    out = Path(tmp_path) / "leads.csv"
    with CsvWriter(out) as writer:
        writer.write({"key": "123456", "email": "SpanoAndSons@Comcast.net"})
    out.unlink()  # a fresh batch
    assert cli.emails_in(str(out)) == {"spanoandsons@comcast.net": "123456"}


def test_a_later_batch_skips_an_inbox_from_a_deleted_one(workdir):
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
    (workdir / "out/leads.csv").unlink()  # sent to the boss; a fresh batch starts
    later = StubPlaces(listing(website=""))
    cli.run_leads(options, census=client(SameInbox(1, 200)), places=later)
    assert later.lookups == 0, "skipped before any paid lookup"
    assert not (workdir / "out/leads.csv").exists()


def test_starting_over_forgets_the_emails_too(tmp_path):
    out = Path(tmp_path) / "leads.csv"
    with CsvWriter(out) as writer:
        writer.write({"key": "1", "email": "a@b.com"})
    with CsvWriter(out, resume=False):
        pass
    assert remembered_emails(out) == {}


# --- department inboxes ---------------------------------------------------------------------------


def test_department_inboxes():
    for email in ("hr@a.com", "careers@a.com", "billing@a.com", "compliance@a.com", "compliance.dept@a.com",
                  "payroll@a.com", "webmaster@a.com"):
        assert is_wrong_department(email), email
    for email in ("info@a.com", "office@a.com", "gbowman@a.com", "hrobinson@a.com", "dispatch@a.com"):
        assert not is_wrong_department(email), email


def test_a_filed_department_inbox_is_replaced_by_the_officers_own(rules, crules):
    finder = StubFinder()
    c = northstar(crules, email_address="compliance@northstar.com")
    lead = leads.build_lead(c, StubPlaces(listing(website="")), None, rules, crules, 8, TODAY, email_finder=finder)
    assert lead.email == "gbowman@northstar.com" and lead.filed_email == "compliance@northstar.com"


def test_a_department_inbox_with_nothing_better_is_left_out(workdir):
    class Compliance(FakeSession):
        def get(self, url, params, timeout):
            response = super().get(url, params, timeout)
            for rec in response.payload:
                rec["email_address"] = "compliance.spano@comcast.net"
            return response

    options = cli.RunOptions(zip_prefixes=("191",), limit=0, out="out/leads.csv", workers=1)
    assert cli.run_leads(options, census=client(Compliance(1)), places=StubPlaces(listing(website=""))) == cli.EXIT_OK
    assert _rows(workdir / "out/leads.csv") == []
    left = _rows(workdir / "out/leads_left_out.csv")
    assert left[0]["left_out_reason"] == "department inbox: compliance.spano@comcast.net"


def test_the_website_never_offers_a_department_inbox(rules, crules, monkeypatch):
    site = crawl_of(("https://spanoandsons.com/", "<p>careers@spanoandsons.com</p><p>office@spanoandsons.com</p>"))
    monkeypatch.setattr(leads, "crawl_site", lambda *a, **k: site)
    lead = leads.build_lead(contractor(crules, email_address=""), StubPlaces(), object(), rules, crules, 8, TODAY)
    assert lead.website_email == "office@spanoandsons.com"


# --- national companies ---------------------------------------------------------------------------


def test_a_national_companys_branch_is_left_out_without_a_paid_search(rules, crules):
    finder = StubFinder()
    c = northstar(crules, email_address="jryan@uhaul.com")
    lead = leads.build_lead(c, StubPlaces(listing(website="")), None, rules, crules, 8, TODAY, email_finder=finder)
    assert lead.score.disqualified and lead.score.disqualify_reason == "national_chain"
    assert finder.asked == []


def test_a_small_contractor_is_not_a_national_company(rules, crules):
    lead = leads.build_lead(contractor(crules), StubPlaces(listing(website="")), None, rules, crules, 8, TODAY)
    assert not lead.score.disqualified


# --- a neighbour's listing at the same address ----------------------------------------------------


def place(title, phone="(215) 555-0177"):
    return {"title": title, "address": "1234 Orthodox St, Philadelphia, PA 19124",
            "phoneNumber": phone, "website": "https://example.com/"}


def test_another_business_at_the_same_address_is_not_a_match():
    found = match_listing([place("Frankford Auto Body")], "Mike Spano & Sons", "1234 ORTHODOX ST", "19124")
    assert found.matched_on == ""
    assert "another business (Frankford Auto Body)" in found.note


def test_a_spelling_difference_still_matches():
    found = match_listing([place("North Star Contracting")], "Northstar Contracting Group Inc",
                          "1234 ORTHODOX ST", "19124")
    assert found.matched_on == "address"


def test_the_filed_phone_or_officers_name_can_confirm_it():
    assert match_listing([place("Ace Hauling", "(215) 423-3054")], "Mike Spano & Sons", "1234 ORTHODOX ST",
                         "19124", phones=("2154233054",)).matched_on == "address + phone"
    assert match_listing([place("Alfonso Spano Excavating")], "Mike Lopez Inc", "1234 ORTHODOX ST",
                         "19124", names=("Alfonso M Spano",)).matched_on == "address"


def test_shortened_or_spaced_names_still_match():
    for listed, filed in (("EJ Paving", "E J Paving Co Inc"), ("PJ's Roofing", "Pjs Roofing Inc"),
                          ("CHS Companies", "C H S Inc"), ("B. W. Electric, Inc.", "Bw Electric Inc")):
        assert match_listing([place(listed)], filed, "1234 ORTHODOX ST", "19124").matched_on == "address", filed


def test_the_companys_email_domain_on_the_listing_confirms_it():
    listed = place("Civil Pros Inc") | {"website": "https://www.civilprosinc.com/"}
    assert match_listing([listed], "Cp Builders Inc", "1234 ORTHODOX ST", "19124").matched_on == ""
    assert match_listing([listed], "Cp Builders Inc", "1234 ORTHODOX ST", "19124",
                         domains=("civilprosinc.com",)).matched_on == "address"
