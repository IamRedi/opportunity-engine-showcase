"""Normalizimi i URL-ve kanonike — baza e dedup L2 (spec §13)."""
from repository import canonical_url


def test_lowercase_host_and_scheme():
    assert canonical_url("HTTPS://Example.COM/Jobs/5") == "https://example.com/Jobs/5"


def test_strips_tracking_params():
    url = "https://example.com/job/1?utm_source=x&utm_medium=y&gclid=abc&fbclid=z&igshid=q&_ga=1.2"
    assert canonical_url(url) == "https://example.com/job/1"


def test_keeps_meaningful_params_sorted():
    assert canonical_url("https://e.com/p?b=2&a=1") == "https://e.com/p?a=1&b=2"


def test_strips_fragment():
    assert canonical_url("https://e.com/p#section") == "https://e.com/p"


def test_strips_trailing_slash_but_not_root():
    assert canonical_url("https://e.com/p/") == "https://e.com/p"
    assert canonical_url("https://e.com/") == "https://e.com/"


def test_default_ports_stripped_nondefault_kept():
    assert canonical_url("http://e.com:80/x") == "http://e.com/x"
    assert canonical_url("https://e.com:443/x") == "https://e.com/x"
    assert canonical_url("https://e.com:8443/x") == "https://e.com:8443/x"


def test_same_page_both_variants_collapse():
    a = canonical_url("https://Example.com/jobs/42/?utm_campaign=mail")
    b = canonical_url("https://example.com/jobs/42")
    assert a == b


def test_empty_and_garbage():
    assert canonical_url("") == ""
    assert canonical_url("   ") == ""
