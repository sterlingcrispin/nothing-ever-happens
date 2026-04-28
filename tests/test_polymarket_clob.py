import pytest

from types import SimpleNamespace

import bot.exchange.polymarket_clob as polymarket_clob
from bot.exchange.polymarket_clob import (
    DEFAULT_ALLOWED_SLIPPAGE,
    PolymarketClobExchangeClient,
    _extract_allowance_value,
)
from bot.proxy_wallet import ensure_conditional_token_approvals
from bot.models import LimitOrderIntent, MarketOrderIntent, Side


class _StubClobClient:
    def __init__(self, trades=None):
        self._trades = trades or []

    def get_trades(self, params):
        _ = params
        return list(self._trades)


def _make_client(trades=None) -> PolymarketClobExchangeClient:
    client = object.__new__(PolymarketClobExchangeClient)
    client.private_key = "0xabc"
    client._trade_params = lambda asset_id, after=None: SimpleNamespace(asset_id=asset_id, after=after)
    client.client = _StubClobClient(trades=trades)
    return client


class _MarketOrderStubClient:
    def __init__(self, response=None):
        self.calculate_args = None
        self.created_args = None
        self.post_args = None
        self.update_calls = []
        self.response = response or {"orderID": "o1", "status": "matched"}

    def calculate_market_price(self, token_id, side, amount, order_type):
        self.calculate_args = (token_id, side, amount, order_type)
        return 0.42

    def create_market_order(self, order_args):
        self.created_args = order_args
        return {"signed": True, "order_args": order_args}

    def post_order(self, signed_order, order_type):
        self.post_args = (signed_order, order_type)
        return dict(self.response)

    def update_balance_allowance(self, params):
        self.update_calls.append(params)
        return {"ok": True}


def test_extract_allowance_value_supports_flat_field() -> None:
    raw = {"balance": "12.5", "allowance": "9.25"}

    allowance = _extract_allowance_value(raw)

    assert allowance == 9.25


def test_extract_allowance_value_supports_nested_allowances() -> None:
    raw = {
        "balance": "12.5",
        "allowances": {
            "exchange": "0",
            "neg_risk": "25.0",
            "adapter": "5.0",
        },
    }

    allowance = _extract_allowance_value(raw)

    assert allowance == 25.0


def test_get_trades_parses_live_taker_shape() -> None:
    client = _make_client(
        trades=[
            {
                "id": "trade-1",
                "taker_order_id": "order-1",
                "asset_id": "token",
                "side": "BUY",
                "size": "13",
                "fee": "0",  # V2: fee directly reported
                "price": "0.36",
                "status": "MATCHED",
                "match_time": "2026-03-07T21:20:17Z",
                "trader_side": "TAKER",
            }
        ]
    )

    trades = client.get_trades("token")

    assert len(trades) == 1
    assert trades[0].trade_id == "trade-1"
    assert trades[0].order_id == "order-1"
    assert trades[0].token_id == "token"
    assert trades[0].side == Side.BUY
    assert trades[0].price == 0.36
    assert trades[0].size == 13.0
    assert trades[0].timestamp == "2026-03-07T21:20:17Z"
    assert trades[0].fee == 0.0  # V2: fee extracted directly


def test_get_trades_parses_maker_rows() -> None:
    client = _make_client(
        trades=[
            {
                "id": "trade-1",
                "taker_order_id": "other-order",
                "asset_id": "token",
                "side": "BUY",
                "size": "13",
                "price": "0.36",
                "match_time": "2026-03-07T21:20:17Z",
                "trader_side": "MAKER",
                "maker_orders": [
                    {
                        "order_id": "maker-order-1",
                        "asset_id": "token",
                        "side": "SELL",
                        "price": "0.36",
                        "matched_amount": "4",
                        # V2: fee is directly reported on each order; maker fee is 0
                        "fee": "0.00144",
                    }
                ],
            }
        ]
    )

    trades = client.get_trades("token")

    assert len(trades) == 1
    assert trades[0].trade_id == "trade-1:0"
    assert trades[0].order_id == "maker-order-1"
    assert trades[0].side == Side.SELL
    assert trades[0].size == 4.0
    assert trades[0].fee == pytest.approx(0.00144)


def test_check_order_readiness_fails_closed_on_unparseable_allowance() -> None:
    client = _make_client()
    client._asset_type = SimpleNamespace(COLLATERAL="collateral", CONDITIONAL="conditional")
    client._get_balance_allowance = lambda *args, **kwargs: (_ for _ in ()).throw(
        ValueError("unexpected allowance shape")
    )

    readiness = client.check_order_readiness(
        LimitOrderIntent(token_id="token", side=Side.BUY, price=0.5, size=1.0)
    )

    assert not readiness.ready
    assert readiness.reason == "Could not verify venue balance/allowance"


def test_place_market_order_uses_ws_price_when_reference_provided() -> None:
    """When reference_price is set, skip the REST calculate_market_price call
    and use the WS price directly with slippage buffer."""
    client = object.__new__(PolymarketClobExchangeClient)
    client.allow_trading = True
    client._buy = "BUY"
    client._sell = "SELL"
    client._order_type = SimpleNamespace(FAK="FAK")
    client._market_order_args = lambda **kwargs: SimpleNamespace(**kwargs)
    client.client = _MarketOrderStubClient()

    result = client.place_market_order(
        MarketOrderIntent(token_id="token", side=Side.BUY, amount=5.0, reference_price=0.4)
    )

    # REST calculate_market_price should NOT be called
    assert client.client.calculate_args is None
    # Price should be reference_price + slippage buffer
    assert client.client.created_args.price == pytest.approx(0.4 + DEFAULT_ALLOWED_SLIPPAGE)
    assert client.client.created_args.order_type == "FAK"
    assert client.client.post_args[1] == "FAK"
    assert result.raw["_market_price"] == pytest.approx(0.4)
    assert result.raw["_buffered_price"] == pytest.approx(0.4 + DEFAULT_ALLOWED_SLIPPAGE)
    assert result.raw["_price_source"] == "ws"


def test_place_market_order_falls_back_to_rest_without_reference() -> None:
    """When reference_price is None, fall back to the REST calculate_market_price call."""
    client = object.__new__(PolymarketClobExchangeClient)
    client.allow_trading = True
    client._buy = "BUY"
    client._sell = "SELL"
    client._order_type = SimpleNamespace(FAK="FAK")
    client._market_order_args = lambda **kwargs: SimpleNamespace(**kwargs)
    client.client = _MarketOrderStubClient()

    result = client.place_market_order(
        MarketOrderIntent(token_id="token", side=Side.BUY, amount=5.0)
    )

    # REST calculate_market_price SHOULD be called
    assert client.client.calculate_args == ("token", "BUY", 5.0, "FAK")
    # Price should be REST price (0.42) + slippage buffer
    assert client.client.created_args.price == pytest.approx(0.42 + DEFAULT_ALLOWED_SLIPPAGE)
    assert result.raw["_market_price"] == pytest.approx(0.42)
    assert result.raw["_buffered_price"] == pytest.approx(0.42 + DEFAULT_ALLOWED_SLIPPAGE)
    assert result.raw["_price_source"] == "rest"


def test_place_market_order_sell_uses_ws_price_with_negative_slippage() -> None:
    """SELL with reference_price skips both sync+sleep AND REST call."""
    client = object.__new__(PolymarketClobExchangeClient)
    client.allow_trading = True
    client._buy = "BUY"
    client._sell = "SELL"
    client._order_type = SimpleNamespace(FAK="FAK")
    client._asset_type = SimpleNamespace(CONDITIONAL="CONDITIONAL")
    client._market_order_args = lambda **kwargs: SimpleNamespace(**kwargs)
    stub = _MarketOrderStubClient()
    client.client = stub
    sync_calls = []
    client._sync_balance_allowance = lambda *args, **kwargs: sync_calls.append(1)

    result = client.place_market_order(
        MarketOrderIntent(token_id="token", side=Side.SELL, amount=10.0, reference_price=0.6)
    )

    # Sync should be SKIPPED (caller did prepare_sell beforehand)
    assert sync_calls == []
    # REST should NOT be called (WS price provided)
    assert stub.calculate_args is None
    # SELL: buffered_price = reference_price - slippage
    assert stub.created_args.price == pytest.approx(0.6 - DEFAULT_ALLOWED_SLIPPAGE)
    assert result.raw["_price_source"] == "ws"


def test_place_market_order_sell_without_reference_syncs_and_uses_rest() -> None:
    """SELL without reference_price does sync+sleep then REST call."""
    client = object.__new__(PolymarketClobExchangeClient)
    client.allow_trading = True
    client._buy = "BUY"
    client._sell = "SELL"
    client._order_type = SimpleNamespace(FAK="FAK")
    client._asset_type = SimpleNamespace(CONDITIONAL="CONDITIONAL")
    client._market_order_args = lambda **kwargs: SimpleNamespace(**kwargs)
    stub = _MarketOrderStubClient()
    client.client = stub
    sync_calls = []
    client._sync_balance_allowance = lambda *args, **kwargs: sync_calls.append(1)

    import bot.exchange.polymarket_clob as _mod
    orig_delay = _mod.SELL_BALANCE_SYNC_DELAY
    _mod.SELL_BALANCE_SYNC_DELAY = 0  # skip sleep in test
    try:
        result = client.place_market_order(
            MarketOrderIntent(token_id="token", side=Side.SELL, amount=10.0)
        )
    finally:
        _mod.SELL_BALANCE_SYNC_DELAY = orig_delay

    # Sync SHOULD be called (no prepare_sell done)
    assert len(sync_calls) == 1
    # REST SHOULD be called (no reference_price)
    assert stub.calculate_args == ("token", "SELL", 10.0, "FAK")
    assert result.raw["_price_source"] == "rest"


def test_place_market_order_buy_records_fill_price_in_probability_units() -> None:
    client = object.__new__(PolymarketClobExchangeClient)
    client.allow_trading = True
    client._buy = "BUY"
    client._sell = "SELL"
    client._order_type = SimpleNamespace(FAK="FAK")
    client._market_order_args = lambda **kwargs: SimpleNamespace(**kwargs)
    client.client = _MarketOrderStubClient(
        response={
            "orderID": "o1",
            "status": "matched",
            "takingAmount": "10.435274",
            "makingAmount": "4.999999",
        }
    )

    result = client.place_market_order(
        MarketOrderIntent(token_id="token", side=Side.BUY, amount=5.0, reference_price=0.4790064331665475)
    )

    assert result.raw["_fill_price"] == pytest.approx(4.999999 / 10.435274)


def test_place_market_order_sell_records_fill_price_in_probability_units() -> None:
    client = object.__new__(PolymarketClobExchangeClient)
    client.allow_trading = True
    client._buy = "BUY"
    client._sell = "SELL"
    client._order_type = SimpleNamespace(FAK="FAK")
    client._asset_type = SimpleNamespace(CONDITIONAL="CONDITIONAL")
    client._market_order_args = lambda **kwargs: SimpleNamespace(**kwargs)
    client.client = _MarketOrderStubClient(
        response={
            "orderID": "o1",
            "status": "matched",
            "takingAmount": "3.081",
            "makingAmount": "10.27",
        }
    )

    result = client.place_market_order(
        MarketOrderIntent(token_id="token", side=Side.SELL, amount=10.27, reference_price=0.37)
    )

    assert result.raw["_fill_price"] == pytest.approx(3.081 / 10.27)


def test_bootstrap_live_trading_ensures_proxy_approval_and_syncs_cache(monkeypatch) -> None:
    approval_calls = []

    def _fake_ensure_conditional_token_approvals(*, private_key, proxy_address, chain_id, rpc_url):
        approval_calls.append((private_key, proxy_address, chain_id, rpc_url))
        return 2

    monkeypatch.setattr(polymarket_clob, "ensure_conditional_token_approvals", _fake_ensure_conditional_token_approvals)

    client = object.__new__(PolymarketClobExchangeClient)
    client.private_key = "0xabc"
    client.signature_type = 2
    client.funder_address = "0xfunder"
    client.chain_id = 137
    client.rpc_url = "https://polygon-rpc.example"
    client._asset_type = SimpleNamespace(COLLATERAL="COLLATERAL", CONDITIONAL="CONDITIONAL")
    client._balance_allowance_params = lambda **kwargs: SimpleNamespace(**kwargs)
    client.client = _MarketOrderStubClient()

    client.bootstrap_live_trading("token")

    assert approval_calls == [("0xabc", "0xfunder", 137, "https://polygon-rpc.example")]
    assert len(client.client.update_calls) == 2
    assert client.client.update_calls[0].asset_type == "COLLATERAL"
    assert client.client.update_calls[1].asset_type == "CONDITIONAL"
    assert client.client.update_calls[1].token_id == "token"


def test_bootstrap_live_trading_requires_polygon_rpc_for_proxy_wallet_mode() -> None:
    client = object.__new__(PolymarketClobExchangeClient)
    client.private_key = "0xabc"
    client.signature_type = 2
    client.funder_address = "0xfunder"
    client.chain_id = 137
    client.rpc_url = ""
    client._asset_type = SimpleNamespace(COLLATERAL="COLLATERAL", CONDITIONAL="CONDITIONAL")
    client._balance_allowance_params = lambda **kwargs: SimpleNamespace(**kwargs)
    client.client = _MarketOrderStubClient()

    with pytest.raises(ValueError, match="POLYGON_RPC_URL is required"):
        client.bootstrap_live_trading("token")


def test_ensure_conditional_token_approvals_requires_explicit_rpc() -> None:
    with pytest.raises(ValueError, match="POLYGON_RPC_URL is required"):
        ensure_conditional_token_approvals(
            private_key="0xabc",
            proxy_address="0x0000000000000000000000000000000000000001",
            chain_id=137,
            rpc_url="",
        )
