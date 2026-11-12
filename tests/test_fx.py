import pytest

from stock_analyzer import config, db, fx


@pytest.fixture
def pairs(temp_db, monkeypatch):
    table = {"USDEUR": 0.9, "KRWEUR": 0.0007, "GBPEUR": 1.2, "EURUSD": 1.11, "JPYUSD": 0.0063}
    calls = []

    def fake(pair):
        calls.append(pair)
        return table.get(pair)

    monkeypatch.setattr(fx, "_fetch", fake)
    fx._cache.clear()
    yield table, calls
    fx._cache.clear()


def test_same_currency_needs_no_lookup(pairs):
    _, calls = pairs
    assert fx.rate("EUR", "EUR") == 1.0
    assert calls == []


def test_direct_pair_and_cache(pairs):
    _, calls = pairs
    assert fx.rate("KRW", "EUR") == 0.0007
    assert fx.rate("KRW", "EUR") == 0.0007
    assert calls == ["KRWEUR"]


def test_pence_are_a_hundredth_of_a_pound(pairs):
    assert fx.rate("GBp", "EUR") == pytest.approx(0.012)


def test_inverse_pair_when_direct_is_missing(pairs):
    assert fx.rate("USD", "JPY") == pytest.approx(1 / 0.0063)


def test_cross_through_the_dollar(pairs):
    # JPYEUR is not quoted in the fake table: JPY -> USD -> EUR.
    assert fx.rate("JPY", "EUR") == pytest.approx(0.0063 * 0.9)


def test_unknown_currency_is_none(pairs):
    assert fx.rate("XXX", "EUR") is None
    assert fx.rate(None, "EUR") is None
    assert fx.rates(["KRW", None, "XXX", "KRW"], "EUR") == {"KRW": 0.0007, "XXX": None}


def test_last_known_rate_survives_an_outage(pairs):
    table, _ = pairs
    assert fx.rate("KRW", "EUR") == 0.0007
    fx._cache.clear()
    table.clear()  # Yahoo unreachable
    assert fx.rate("KRW", "EUR") == 0.0007


def test_display_currency_defaults_to_eur_and_can_switch(temp_db):
    assert config.load()["display_currency"] == "EUR"
    db.set_setting("display_currency", "USD")
    assert config.load()["display_currency"] == "USD"
    db.set_setting("display_currency", "CHF")  # not offered: fall back to the file default
    assert config.load()["display_currency"] == "EUR"
