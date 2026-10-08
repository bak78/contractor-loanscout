"""Hunter.io and whose inbox an email is — Hunter stubbed, so no credits are spent."""

import pytest

from contractor_loanscout import cli, leads
from contractor_loanscout.export import build_row
from contractor_loanscout.hunter import HunterClient, HunterResult, HunterUnavailable
from test_dot import TODAY
from test_run import StubPlaces, _census, contractor, crawl_of, crules, listing, workdir  # noqa: F401


class FakeResponse:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self.payload = payload or {}
        self.text = text

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params, timeout):
        self.calls.append(params)
        return self.responses.pop(0)

    def close(self):
        pass


def found(email="jobrien@unityhh.com", status="valid", score=97):
    return FakeResponse(200, {"data": {"email": email, "score": score, "position": "CEO",
                                       "verification": {"status": status}}})


# --- the client --------------------------------------------------------------------------


def test_a_verified_address_is_kept():
    hunter = HunterClient("key", FakeSession(found()))
    assert hunter.find("unityhh.com", "Jane", "O'Brien") == HunterResult("jobrien@unityhh.com", "valid", 97, "CEO")
    assert (hunter.lookups, hunter.found) == (1, 1)


@pytest.mark.parametrize("status", ["accept_all", "unknown", "invalid", ""])
def test_an_unverified_address_is_not(status):
    result = HunterClient("key", FakeSession(found(status=status))).find("unityhh.com", "Jane", "O'Brien")
    assert result.email == "" and result.status == status


def test_nothing_found_is_empty():
    result = HunterClient("key", FakeSession(FakeResponse(200, {"data": {"email": None}}))).find("x.com", "A", "B")
    assert result.email == ""


def test_the_search_sends_name_and_domain():
    session = FakeSession(found())
    HunterClient("key", session).find("unityhh.com", "Jane", "O'Brien")
    assert session.calls[0] == {"domain": "unityhh.com", "first_name": "Jane", "last_name": "O'Brien",
                                "api_key": "key"}


@pytest.mark.parametrize("response,message", [
    (FakeResponse(401), "rejected the API key"),
    (FakeResponse(429), "out of searches"),
    (FakeResponse(403, text='{"errors":[{"id":"usage_limit"}]}'), "out of searches"),
])
def test_a_refused_key_or_used_up_plan_stops_every_worker(response, message):
    hunter = HunterClient("key", FakeSession(response))
    with pytest.raises(HunterUnavailable, match=message):
        hunter.find("unityhh.com", "Jane", "O'Brien")
    with pytest.raises(HunterUnavailable):
        hunter.find("other.com", "Jane", "O'Brien")  # no second request


def test_a_server_error_is_retried_next_run():
    with pytest.raises(ConnectionError):
        HunterClient("key", FakeSession(FakeResponse(503))).find("unityhh.com", "Jane", "O'Brien")


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


# --- when Hunter is asked -------------------------------------------------------------------------


class StubHunter:
    def __init__(self, email="gbowman@northstar.com"):
        self.email = email
        self.asked = []

    def find(self, domain, first, last):
        self.asked.append((domain, first, last))
        return HunterResult(self.email, "valid" if self.email else "", 94)


def test_hunter_finds_the_officers_own_address_and_the_filed_one_is_kept(rules, crules):
    hunter = StubHunter()
    lead = leads.build_lead(northstar(crules), StubPlaces(listing(website="")), None, rules, crules, 8, TODAY,
                            hunter=hunter)
    assert hunter.asked == [("northstar.com", "Gary", "Bowman")], "the domain comes from the filed email"
    row = build_row(lead, rules)
    assert (row["first_name"], row["email"]) == ("Gary", "gbowman@northstar.com")
    assert row["filed_email"] == "jryan@northstar.com"
    assert row["email_source"].startswith("Hunter.io (verified")
    assert row["email_belongs_to"] == "the officer named"


def test_nothing_verified_keeps_the_filed_email_labelled(rules, crules):
    lead = leads.build_lead(northstar(crules), StubPlaces(listing(website="")), None, rules, crules, 8, TODAY,
                            hunter=StubHunter(""))
    assert (lead.email, lead.email_belongs_to, lead.filed_email) == ("jryan@northstar.com", "someone else", "")


def test_hunter_is_not_asked_when_the_email_is_the_officers(rules, crules):
    hunter = StubHunter()
    leads.build_lead(northstar(crules, email_address="gbowman@northstar.com"), StubPlaces(), None,
                     rules, crules, 8, TODAY, hunter=hunter)
    assert hunter.asked == []


def test_a_gmail_only_company_uses_its_website_domain_or_nothing(rules, crules):
    hunter = StubHunter("gbowman@northstarcg.com")
    c = northstar(crules, email_address="jryan77@gmail.com")
    leads.build_lead(c, StubPlaces(listing(website="")), None, rules, crules, 8, TODAY, hunter=hunter)
    assert hunter.asked == [], "no company domain to search"
    lead = leads.build_lead(c, StubPlaces(listing(website="https://northstarcg.com/")), None, rules, crules, 8,
                            TODAY, hunter=hunter)
    assert hunter.asked == [("northstarcg.com", "Gary", "Bowman")]
    assert lead.email == "gbowman@northstarcg.com"


def test_the_officers_address_on_the_website_beats_asking_hunter(rules, crules, monkeypatch):
    site = crawl_of(("https://spanoandsons.com/team", "<p>gbowman@spanoandsons.com</p>"))
    monkeypatch.setattr(leads, "crawl_site", lambda *a, **k: site)
    hunter = StubHunter()
    lead = leads.build_lead(northstar(crules), StubPlaces(), object(), rules, crules, 8, TODAY, hunter=hunter)
    assert hunter.asked == []
    assert (lead.email, lead.filed_email) == ("gbowman@spanoandsons.com", "jryan@northstar.com")


def test_a_run_stops_when_hunter_runs_out(workdir, monkeypatch):
    class OutHunter:
        lookups, found = 1, 0

        def find(self, *args):
            raise HunterUnavailable("Hunter is out of searches for this month")

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
                           hunter=OutHunter())
    assert result == cli.EXIT_SERPER_UNAVAILABLE
