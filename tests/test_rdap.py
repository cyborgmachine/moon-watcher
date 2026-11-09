from datetime import datetime
from unittest import mock

import pytest

from moon_watcher.monitors import domain as dom
from moon_watcher.monitors import rdap

# trimmed down from what a registry sends back
SAMPLE = {
    "ldhName": "example.dev",
    "status": ["client transfer prohibited"],
    "events": [
        {"eventAction": "registration", "eventDate": "2020-03-01T10:00:00Z"},
        {"eventAction": "expiration", "eventDate": "2027-03-01T10:00:00Z"},
        {"eventAction": "last changed", "eventDate": "2026-02-10T08:30:00.123Z"},
    ],
    "nameservers": [{"ldhName": "NS1.EXAMPLE.DEV"}, {"ldhName": "ns2.example.dev"}],
    "entities": [
        {"roles": ["abuse"], "vcardArray": ["vcard", [["fn", {}, "text", "Abuse Desk"]]]},
        {"roles": ["registrar"], "vcardArray": ["vcard", [["version", {}, "text", "4.0"],
                                                         ["fn", {}, "text", "Some Registrar Inc"]]]},
    ],
}

BOOTSTRAP = {"services": [
    [["dev", "app"], ["http://insecure.example/", "https://pubapi.registry.google/rdap/"]],
    [["com"], ["https://rdap.verisign.com/com/v1/"]],
]}


def response(payload=None, status=200):
    r = mock.Mock(status_code=status)
    r.json.return_value = payload
    r.raise_for_status.side_effect = None if status < 400 else RuntimeError(f"HTTP {status}")
    return r


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    monkeypatch.setattr(rdap, "_servers", {})
    monkeypatch.setattr(rdap, "_servers_loaded_at", 0.0)


def test_parse_response():
    rec = rdap.parse_response(SAMPLE)
    assert rec["registrar"] == "Some Registrar Inc"
    assert rec["expiration_date"] == datetime(2027, 3, 1, 10, 0)
    assert rec["creation_date"] == datetime(2020, 3, 1, 10, 0)
    assert rec["updated_date"].year == 2026
    assert rec["name_servers"] == ["ns1.example.dev", "ns2.example.dev"]
    assert rec["status"] == ["client transfer prohibited"]


def test_parse_response_with_missing_pieces():
    rec = rdap.parse_response({})
    assert rec["expiration_date"] is None and rec["registrar"] is None
    assert rec["name_servers"] == [] and rec["status"] == []


def test_bad_date_is_ignored():
    rec = rdap.parse_response({"events": [{"eventAction": "expiration", "eventDate": "soon"}]})
    assert rec["expiration_date"] is None


def test_lookup_uses_bootstrap_and_prefers_https():
    with mock.patch.object(rdap._session, "get", side_effect=[response(BOOTSTRAP), response(SAMPLE)]) as get:
        rec = rdap.lookup("example.dev")
    assert rec["registrar"] == "Some Registrar Inc"
    assert get.call_args_list[1].args[0] == "https://pubapi.registry.google/rdap/domain/example.dev"


def test_bootstrap_is_cached():
    replies = [response(BOOTSTRAP), response(SAMPLE), response(SAMPLE)]
    with mock.patch.object(rdap._session, "get", side_effect=replies) as get:
        rdap.lookup("example.dev")
        rdap.lookup("other.app")
    assert get.call_count == 3  # one bootstrap, two lookups


def test_tld_without_rdap():
    with mock.patch.object(rdap._session, "get", return_value=response(BOOTSTRAP)):
        with pytest.raises(rdap.RdapUnavailable):
            rdap.lookup("example.xyz-no-rdap")


def test_not_found():
    with mock.patch.object(rdap._session, "get", side_effect=[response(BOOTSTRAP), response(status=404)]):
        with pytest.raises(ValueError, match="not found"):
            rdap.lookup("nope.dev")


# lookup_domain: rdap first, whois as the fallback

def rec(exp):
    return {"registrar": "R", "creation_date": None, "expiration_date": exp, "updated_date": None,
            "name_servers": [], "status": []}


def test_rdap_result_is_used_without_touching_whois():
    exp = datetime(2030, 1, 1)
    with mock.patch.object(dom.rdap, "lookup", return_value=rec(exp)), \
            mock.patch.object(dom, "_whois_record") as w:
        assert dom.lookup_domain("example.dev")["expiration_date"] == exp
    w.assert_not_called()


def test_whois_fallback_when_tld_has_no_rdap():
    with mock.patch.object(dom.rdap, "lookup", side_effect=rdap.RdapUnavailable("de")), \
            mock.patch.object(dom, "_whois_record", return_value=rec(datetime(2031, 1, 1))):
        assert dom.lookup_domain("example.de")["expiration_date"].year == 2031


def test_whois_fallback_when_rdap_errors():
    with mock.patch.object(dom.rdap, "lookup", side_effect=RuntimeError("timeout")), \
            mock.patch.object(dom, "_whois_record", return_value=rec(datetime(2031, 1, 1))):
        assert dom.lookup_domain("example.com")["expiration_date"].year == 2031


def test_rdap_without_expiry_is_kept_if_whois_fails():
    with mock.patch.object(dom.rdap, "lookup", return_value=rec(None)), \
            mock.patch.object(dom, "_whois_record", side_effect=RuntimeError("no whois")):
        assert dom.lookup_domain("example.dev")["registrar"] == "R"


def test_error_mentions_both_sources():
    with mock.patch.object(dom.rdap, "lookup", side_effect=RuntimeError("rdap down")), \
            mock.patch.object(dom, "_whois_record", side_effect=RuntimeError("whois down")):
        with pytest.raises(LookupError) as e:
            dom.lookup_domain("example.dev")
    assert "RDAP: rdap down" in str(e.value) and "WHOIS: whois down" in str(e.value)
