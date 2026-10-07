"""US DOT carrier census: settings, record parsing, names, trades and searching — the data site stubbed."""

import threading
from datetime import date

import pytest

from contractor_loanscout.config import ConfigError
from contractor_loanscout.dot import (
    CensusClient, DotUnavailable, SearchStopped, check_zip_prefixes, find_contractors,
    load_contractor_rules, parse_carrier, split_person, tidy_name,
)

TODAY = date(2026, 10, 7)


@pytest.fixture(scope="module")
def crules():
    return load_contractor_rules()


def carrier(**overrides):
    """A census record shaped like the real API's, for a Philadelphia paving contractor."""
    rec = {
        "dot_number": "123456",
        "legal_name": "MIKE SPANO & SONS INC",
        "company_officer_1": "ALFONSO M SPANO",
        "company_officer_2": "MICHAEL SPANO",
        "phone": "2154233054",
        "cell_phone": "2675550101",
        "email_address": "SpanoAndSons@Comcast.net",
        "phy_street": "1234 ORTHODOX ST",
        "phy_city": "PHILADELPHIA",
        "phy_state": "PA",
        "phy_zip": "19124-3210",
        "power_units": "5",
        "total_drivers": "5",
        "add_date": "19990315",
        "mcs150_date": "20250626",
        "business_org_desc": "CORPORATION",
        "carrier_operation": "C",
        "safety_rating": "S",
        "classdef": "PRIVATE PROPERTY",
    }
    rec.update(overrides)
    return rec


# --- settings ------------------------------------------------------------------------


def test_settings_load(crules):
    assert crules.state == "PA"
    assert crules.cargo == ("crgo_construct",)
    assert crules.trades[0].name == "Excavation & site work"


@pytest.mark.parametrize("prefix", ["", "191x", "123456"])
def test_zip_prefixes_need_one_to_five_digits(prefix):
    with pytest.raises(ConfigError, match="1 to 5 digits"):
        check_zip_prefixes((prefix,))


@pytest.mark.parametrize(
    "name,trade",
    [
        ("ACME EXCAVATING INC", "Excavation & site work"),
        ("TRI STATE PAVING", "Paving & asphalt"),
        ("DONNELLY ROOFING CO", "Roofing & siding"),
        ("A1 PLUMBING & HEATING", "Plumbing, heating & HVAC"),
        ("JJ WHITE ELECTRIC", "Electrical"),
        ("GREEN TREE SERVICE", "Landscaping & tree work"),
        ("BUCKLEY & COMPANY", ""),
    ],
)
def test_trade_comes_from_the_name(crules, name, trade):
    assert crules.trade_of(name) == trade


# --- names ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,split",
    [
        ("ALFONSO M SPANO", ("Alfonso", "Spano")),
        ("ANTHONY V. LUCERI III", ("Anthony", "Luceri")),
        ("ANTHONY V. LUCERI, III", ("Anthony", "Luceri")),
        ("LUCERI, ANTHONY V", ("Anthony", "Luceri")),
        ("MARY O'BRIEN-SMITH", ("Mary", "O'Brien-Smith")),
        ("SPANO CONSTRUCTION LLC", ("", "")),
        ("MADONNA", ("", "")),
        ("", ("", "")),
    ],
)
def test_officer_names_split_into_first_and_last(raw, split):
    assert split_person(raw) == split


def test_names_are_tidied():
    assert tidy_name("MIKE SPANO & SONS INC") == "Mike Spano & Sons Inc"
    assert tidy_name("1ST STATE PAVING LLC") == "1st State Paving LLC"
    assert tidy_name("DON'S HAULING") == "Don's Hauling"


# --- parsing -----------------------------------------------------------------------------


def test_a_record_becomes_a_contractor_with_its_officer(crules):
    c = parse_carrier(carrier(), crules, TODAY)
    assert (c.officer_first, c.officer_last) == ("Alfonso", "Spano")
    assert c.officer_level == "owner", "the surname is in the business name"
    assert c.second_officer == "Michael Spano"
    assert c.phone == "(215) 423-3054" and c.cell_phone == "(267) 555-0101"
    assert c.email == "spanoandsons@comcast.net"
    assert (c.address, c.city, c.zip) == ("1234 Orthodox St", "Philadelphia", "19124")
    assert (c.power_units, c.drivers) == (5, 5)
    assert (c.registered, c.updated) == ("1999-03-15", "2025-06-26")
    assert c.safety_rating == "Satisfactory"
    assert c.company_name == "Mike Spano & Sons Inc"
    assert c.search_name == "Mike Spano & Sons"


def test_an_officer_unrelated_to_the_name_is_an_officer(crules):
    c = parse_carrier(carrier(legal_name="J J WHITE INC", company_officer_1="ANDREW JONES",
                              company_officer_2=""), crules, TODAY)
    assert c.officer_level == "officer"


def test_a_company_officer_entry_is_skipped_for_the_next(crules):
    c = parse_carrier(carrier(company_officer_1="SPANO HOLDINGS LLC"), crules, TODAY)
    assert (c.officer_first, c.officer_last) == ("Michael", "Spano")


def test_dba_is_the_public_name(crules):
    c = parse_carrier(carrier(dba_name="DBA SPANO PAVING"), crules, TODAY)
    assert c.company_name == "Spano Paving"
    assert c.trade == "Paving & asphalt"


@pytest.mark.parametrize(
    "overrides",
    [
        {"legal_name": "NEW DEAL LUMBER & MILLWORK CO INC"},
        {"legal_name": "CRAFT-BILT MANUFACTURING CO"},
        {"legal_name": "HARSCO CORPORATION"},
        {"legal_name": "CITY OF PHILADELPHIA"},
        {"dba_name": "ABC SUPPLY"},
        {"power_units": "400"},
        {"power_units": "0"},
        {"mcs150_date": "20190101"},
        {"mcs150_date": ""},
    ],
)
def test_suppliers_public_bodies_big_fleets_and_stale_filings_are_not_leads(crules, overrides):
    assert parse_carrier(carrier(**overrides), crules, TODAY) is None


@pytest.mark.parametrize(
    "name,classdef,kept,hauls",
    [
        ("HAINES & KIBBLEHOUSE INC", "PRIVATE PROPERTY", True, "own work"),
        ("CARUSONE CONSTRUCTION INC", "AUTHORIZED FOR HIRE", True, "for hire"),
        ("GALASSO TRUCKING SERVICE INC", "AUTHORIZED FOR HIRE", True, "for hire"),
        ("LEVELS TRUCKING LLC", "PRIVATE PROPERTY;AUTHORIZED FOR HIRE", True, "own work and for hire"),
        ("MOONSTAR EXPRESS LLC", "AUTHORIZED FOR HIRE", False, ""),
        ("EBN LOGISTICS LLC", "AUTHORIZED FOR HIRE", False, ""),
        ("ROAD RIDER ONE LLC", "AUTHORIZED FOR HIRE", False, ""),
    ],
)
def test_freight_carriers_are_told_apart_from_contractors(crules, name, classdef, kept, hauls):
    c = parse_carrier(carrier(legal_name=name, classdef=classdef, company_officer_1="JOHN DOE"), crules, TODAY)
    assert (c is not None) is kept
    if kept:
        assert c.hauls == hauls


def test_a_bad_email_is_dropped(crules):
    assert parse_carrier(carrier(email_address="NONE"), crules, TODAY).email == ""


# --- searching ---------------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status

    def json(self):
        return self.payload


class FakeSession:
    """Answers census searches with a fixed number of records, after some 429s."""

    def __init__(self, count=3, busy=0, status=200):
        self.count = count
        self.busy = busy
        self.status = status
        self.calls = []
        self.headers = {}

    def get(self, url, params, timeout):
        self.calls.append(dict(params))
        if self.busy:
            self.busy -= 1
            return FakeResponse({"message": "Too many requests"}, 429)
        if self.status != 200:
            return FakeResponse({}, self.status)
        start, size = params["$offset"], params["$limit"]
        n = max(0, min(size, self.count - start))
        return FakeResponse([
            carrier(dot_number=str(1000 + start + i), legal_name=f"CONTRACTOR {start + i} PAVING",
                    company_officer_1=f"JOHN DOE{chr(65 + i % 26)}", phy_street=f"{i} MAIN ST")
            for i in range(n)
        ])

    def close(self):
        pass


def client(session, **kw):
    return CensusClient(session, cache_dir=None, wait_base=0, **kw)


def test_search_asks_for_active_construction_carriers_in_the_area():
    session = FakeSession(2)
    client(session).search("PA", ("191", "190"), ("crgo_construct",))
    where = session.calls[0]["$where"]
    assert "status_code='A'" in where and "phy_state='PA'" in where
    assert "crgo_construct='X'" in where
    assert "phy_zip like '191%'" in where and "phy_zip like '190%'" in where


def test_search_pages_through_large_areas(monkeypatch):
    monkeypatch.setattr("contractor_loanscout.dot.PAGE_SIZE", 2)
    session = FakeSession(5)
    assert len(client(session).search("PA", (), ("crgo_construct",))) == 5
    assert [c["$offset"] for c in session.calls] == [0, 2, 4]


def test_a_busy_data_site_is_waited_out():
    session = FakeSession(2, busy=2)
    assert len(client(session).search("PA", ("191",), ("crgo_construct",))) == 2
    assert len(session.calls) == 3


def test_an_app_token_is_sent():
    session = FakeSession(1)
    client(session, app_token="abc123")
    assert session.headers["X-App-Token"] == "abc123"


def test_an_unreachable_data_site_is_reported(monkeypatch):
    monkeypatch.setattr("contractor_loanscout.dot.RETRIES", 2)
    with pytest.raises(DotUnavailable, match="try again"):
        client(FakeSession(status=503)).search("PA", ("191",), ("crgo_construct",))


def test_stop_ends_a_search():
    stop = threading.Event()
    stop.set()
    with pytest.raises(SearchStopped):
        client(FakeSession(), stop=stop).search("PA", ("191",), ("crgo_construct",))


def test_results_are_cached_between_runs(tmp_path):
    CensusClient(FakeSession(3), cache_dir=tmp_path, wait_base=0).search("PA", ("191",), ("crgo_construct",))
    again = FakeSession(0)
    results = CensusClient(again, cache_dir=tmp_path, wait_base=0).search("PA", ("191",), ("crgo_construct",))
    assert len(results) == 3 and again.calls == []


def test_find_returns_contractors(crules):
    found = find_contractors(client(FakeSession(4)), crules, "PA", ("191",), TODAY)
    assert len(found) == 4
    assert {c.trade for c in found} == {"Paving & asphalt"}
