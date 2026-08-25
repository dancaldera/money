from __future__ import annotations

from trading.live.broker import quantize_limit_price


def test_stock_limit_above_a_dollar_snaps_to_pennies():
    # The rejected paper order used 355.9188 (signal * 1.02).
    assert quantize_limit_price(355.9188, "stock") == 355.92


def test_stock_limit_below_a_dollar_allows_subpennies():
    assert quantize_limit_price(0.12345, "stock") == 0.1235


def test_crypto_limit_keeps_finer_decimals():
    assert quantize_limit_price(355.91881234, "crypto") == 355.91881234
