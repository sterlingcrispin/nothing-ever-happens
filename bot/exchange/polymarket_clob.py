import logging
import os
import time
from typing import Any

from bot.config import ExchangeConfig
from bot.models import (
    LimitOrderIntent,
    MarketOrderIntent,
    MarketRules,
    OpenOrder,
    OrderBookLevel,
    OrderBookSnapshot,
    OrderReadiness,
    OrderResult,
    Side,
    Trade,
)
from bot.proxy_wallet import ensure_conditional_token_approvals

logger = logging.getLogger(__name__)
DEFAULT_ALLOWED_SLIPPAGE = 0.05
SELL_BALANCE_SYNC_DELAY = 1.0
SELL_RETRY_DELAY = 2.0
TOKEN_DECIMAL_FACTOR = 10**6  # Both USDC and conditional tokens use 6 decimals on Polygon


class PolymarketClobExchangeClient:
    def __init__(self, config: ExchangeConfig, allow_trading: bool) -> None:
        try:
            from py_clob_client_v2.client import ClobClient
            from py_clob_client_v2.clob_types import (
                AssetType,
                BalanceAllowanceParams,
                MarketOrderArgs,
                OpenOrderParams,
                OrderArgs,
                OrderType,
                TradeParams,
            )
            from py_clob_client_v2.order_builder.constants import BUY, SELL
        except ImportError as exc:
            raise RuntimeError(
                "Missing dependency py-clob-client-v2. Install with: pip install -r requirements.txt"
            ) from exc

        self.allow_trading = allow_trading
        self.private_key = config.private_key
        self.signature_type = config.signature_type
        self.funder_address = config.funder_address
        self.chain_id = config.chain_id
        self.builder_code = getattr(config, "builder_code", None)
        self.rpc_url = (os.getenv("POLYGON_RPC_URL") or "").strip()
        self._asset_type = AssetType
        self._balance_allowance_params = BalanceAllowanceParams
        self._market_order_args = MarketOrderArgs
        self._order_args = OrderArgs
        self._order_type = OrderType
        self._open_order_params = OpenOrderParams
        self._trade_params = TradeParams
        self._buy = BUY
        self._sell = SELL

        # V2: all params as kwargs. L2 auth via api_key/api_secret/api_passphrase.
        client_kwargs: dict[str, Any] = {
            "host": config.host,
            "chain_id": config.chain_id,
        }
        if config.private_key:
            client_kwargs["key"] = config.private_key
            if config.signature_type is not None:
                client_kwargs["signature_type"] = config.signature_type
        if config.funder_address:
            client_kwargs["funder"] = config.funder_address

        # L2 API credentials (HMAC) — env vars unchanged from V1
        # Compatible with existing POLY_API_KEY / POLY_SECRET / POLY_PASSPHRASE
        api_key = (os.getenv("POLY_API_KEY") or "").strip() or None
        api_secret = (os.getenv("POLY_API_SECRET") or os.getenv("POLY_SECRET") or "").strip() or None
        api_passphrase = (os.getenv("POLY_API_PASSPHRASE") or os.getenv("POLY_PASS_PHRASE") or "").strip() or None
        if api_key:
            client_kwargs["api_key"] = api_key
        if api_secret:
            client_kwargs["api_secret"] = api_secret
        if api_passphrase:
            client_kwargs["api_passphrase"] = api_passphrase

        self.client = ClobClient(**client_kwargs)

        if self.allow_trading and not config.private_key:
            raise ValueError("PRIVATE_KEY is required when order transmission is enabled")

    def get_mid_price(self, token_id: str) -> float:
        midpoint = self.client.get_midpoint(token_id)
        if isinstance(midpoint, dict):
            value = midpoint.get("mid")
            if value is None:
                raise ValueError(f"Missing 'mid' field in midpoint response: {midpoint}")
        else:
            value = midpoint

        try:
            return float(value)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Could not parse midpoint value: {value!r}") from exc

    def get_market_rules(self, token_id: str) -> MarketRules | None:
        try:
            order_book = self.client.get_order_book(token_id)
            tick_size = float(order_book.tick_size)
            min_order_size = float(order_book.min_order_size)
            return MarketRules(tick_size=tick_size, min_order_size=min_order_size)
        except Exception as exc:
            logger.warning(
                "get_market_rules failed",
                extra={
                    "token_id": token_id,
                    "error": str(exc),
                },
            )
            return None

    def get_order_book(self, token_id: str) -> OrderBookSnapshot:
        order_book = self.client.get_order_book(token_id)
        bids = tuple(
            OrderBookLevel(price=float(level.price), size=float(level.size))
            for level in (order_book.bids or [])
        )
        asks = tuple(
            OrderBookLevel(price=float(level.price), size=float(level.size))
            for level in (order_book.asks or [])
        )
        try:
            timestamp = int(order_book.timestamp or 0)
        except (TypeError, ValueError):
            timestamp = 0
        return OrderBookSnapshot(
            token_id=token_id,
            bids=bids,
            asks=asks,
            tick_size=float(order_book.tick_size),
            min_order_size=float(order_book.min_order_size),
            timestamp=timestamp,
        )

    def warm_token_cache(self, token_id: str) -> None:
        """Pre-fetch tick_size, neg_risk for a token so
        place_market_order doesn't hit the network on first trade."""
        try:
            self.client.get_tick_size(token_id)
            self.client.get_neg_risk(token_id)
        except Exception as e:
            logger.warning("warm_token_cache failed for %s: %s", token_id[:20], e)

    def get_open_orders(self, token_id: str) -> list[OpenOrder]:
        if not self.private_key:
            return []

        raw_orders = self.client.get_orders(self._open_order_params(asset_id=token_id))
        parsed: list[OpenOrder] = []
        for raw in raw_orders:
            try:
                parsed.append(self._parse_order_snapshot(raw, default_token_id=token_id))
            except Exception as exc:
                logger.warning(
                    "failed to parse open order",
                    extra={
                        "error": str(exc),
                        "raw_keys": list(raw.keys()) if isinstance(raw, dict) else type(raw).__name__,
                    },
                )
                continue
        return parsed

    def get_order(self, order_id: str) -> OpenOrder | None:
        if not self.private_key:
            return None

        try:
            raw = self.client.get_order(order_id)
        except Exception as exc:
            logger.warning(
                "get_order failed",
                extra={"order_id": order_id, "error": str(exc)},
            )
            return None

        try:
            return self._parse_order_snapshot(raw)
        except Exception as exc:
            logger.warning(
                "failed to parse order snapshot",
                extra={"order_id": order_id, "error": str(exc)},
            )
            return None

    def place_limit_order(self, order: LimitOrderIntent) -> OrderResult:
        if not self.allow_trading:
            raise RuntimeError("Order transmission is disabled")

        side = self._buy if order.side == Side.BUY else self._sell
        # V2: removed feeRateBps, nonce, taker; expiration=0 for GTC; optional builder
        order_kwargs: dict[str, Any] = {
            "price": order.price,
            "size": order.size,
            "side": side,
            "token_id": order.token_id,
            "expiration": 0,  # GTC
        }
        if self.builder_code:
            order_kwargs["builder"] = self.builder_code

        order_args = self._order_args(**order_kwargs)
        signed_order = self.client.create_order(order_args)
        response: Any = self.client.post_order(signed_order, self._order_type.GTC)

        if not isinstance(response, dict):
            raise ValueError(f"Expected dict from post_order, got {type(response).__name__}: {response!r}")

        order_id = _require_field(response, "orderID", aliases=["order_id", "id"])
        status = str(response.get("status") or "submitted")

        logger.info(
            "post_order_response",
            extra={
                "order_id": order_id,
                "status": status,
                "response_keys": list(response.keys()),
            },
        )

        return OrderResult(order_id=order_id, status=status, raw=response)

    def place_market_order(self, order: MarketOrderIntent) -> OrderResult:
        if not self.allow_trading:
            raise RuntimeError("Order transmission is disabled")

        side = self._buy if order.side == Side.BUY else self._sell

        # For SELL orders, force-sync the conditional token balance right before
        # building the order. The CLOB has a known propagation delay between
        # balance-allowance/update and POST /order validation.
        # Skip if caller already did this via prepare_sell() (indicated by
        # reference_price being set — the caller synced, waited, then
        # re-read fresh WS prices before calling us).
        if order.side == Side.SELL and order.reference_price is None:
            self._sync_balance_allowance(self._asset_type.CONDITIONAL, token_id=order.token_id)
            time.sleep(SELL_BALANCE_SYNC_DELAY)

        # Use the WS-sourced reference_price when available to avoid the
        # ~500ms REST call to calculate_market_price.  Fall back to REST
        # only when the caller didn't supply a reference price.
        if order.reference_price is not None and order.reference_price > 0:
            market_price = order.reference_price
            price_source = "ws"
        else:
            market_price = self.client.calculate_market_price(
                order.token_id,
                side,
                order.amount,
                self._order_type.FAK,
            )
            price_source = "rest"

        allowed_slippage = float(
            order.allowed_slippage if order.allowed_slippage is not None else DEFAULT_ALLOWED_SLIPPAGE
        )
        if order.price_cap is not None:
            buffered_price = _clamp_probability(order.price_cap)
        else:
            buffered_price = _clamp_probability(
                market_price + allowed_slippage if order.side == Side.BUY else market_price - allowed_slippage
            )
        # V2: MarketOrderArgs no longer takes order_type parameter
        order_kwargs: dict[str, Any] = {
            "token_id": order.token_id,
            "amount": order.amount,
            "side": side,
            "price": buffered_price,
        }
        if self.builder_code:
            order_kwargs["builder"] = self.builder_code

        order_args = self._market_order_args(**order_kwargs)
        # create_market_order takes order_type as separate parameter
        signed_order = self.client.create_market_order(order_args, self._order_type.FAK)
        response = self._post_order_with_sell_retry(signed_order, order)

        if not isinstance(response, dict):
            raise ValueError(f"Expected dict from post_order, got {type(response).__name__}: {response!r}")

        order_id = _require_field(response, "orderID", aliases=["order_id", "id"])
        status = str(response.get("status") or "submitted")
        response["_market_price"] = market_price
        response["_allowed_slippage"] = allowed_slippage
        response["_buffered_price"] = buffered_price
        response["_price_source"] = price_source

        # --- Fill data from post_order response ---
        # FAK responses include takingAmount/makingAmount directly —
        # no extra API call needed.
        # The exchange reports "taking" as the asset we receive and
        # "making" as the asset we give:
        #   BUY:  takingAmount = shares received, makingAmount = USDC spent
        #   SELL: takingAmount = USDC received, makingAmount = shares sold
        fill_price = None
        taking = response.get("takingAmount")
        making = response.get("makingAmount")
        try:
            taking_f = float(taking) if taking else None
            making_f = float(making) if making else None
            if taking_f and making_f and taking_f > 0 and making_f > 0:
                if order.side == Side.BUY:
                    fill_price = making_f / taking_f   # USDC / shares
                else:
                    fill_price = taking_f / making_f   # USDC / shares
        except (ValueError, TypeError):
            pass
        response["_fill_price"] = fill_price

        logger.info(
            "post_market_order_response",
            extra={
                "order_id": order_id,
                "status": status,
                "market_price": market_price,
                "buffered_price": buffered_price,
                "price_source": price_source,
                "fill_price": fill_price,
                "taking_amount": taking,
                "making_amount": making,
                "trade_ids": response.get("tradeIDs"),
                "tx_hashes": response.get("transactionsHashes"),
                "response_keys": list(response.keys()),
            },
        )

        return OrderResult(order_id=order_id, status=status, raw=response)

    def _post_order_with_sell_retry(self, signed_order: Any, order: MarketOrderIntent, max_retries: int = 2) -> Any:
        """Post order with retry logic for SELL balance/allowance propagation delays."""
        last_exc: Exception | None = None
        for attempt in range(1 + max_retries):
            try:
                return self.client.post_order(signed_order, self._order_type.FAK)
            except Exception as exc:
                exc_lower = str(exc).lower()
                if order.side != Side.SELL or ("balance" not in exc_lower and "allowance" not in exc_lower):
                    raise
                last_exc = exc
                if attempt < max_retries:
                    logger.warning(
                        "sell_balance_retry",
                        extra={
                            "attempt": attempt + 1,
                            "max_retries": max_retries,
                            "error": str(exc),
                            "delay": SELL_RETRY_DELAY,
                        },
                    )
                    self._sync_balance_allowance(self._asset_type.CONDITIONAL, token_id=order.token_id)
                    time.sleep(SELL_RETRY_DELAY)
        raise last_exc

    def prepare_sell(self, token_id: str) -> bool:
        """Pre-sync conditional token balance and wait for propagation.

        Call this before place_market_order(SELL, reference_price=...) so the
        caller can re-read fresh WS prices AFTER the delay, then pass them
        in as reference_price. When reference_price is set, place_market_order
        skips its own sync+sleep, avoiding both redundant delay AND the slow
        REST calculate_market_price call.

        Returns True if sync succeeded, False if it failed (caller should
        abort rather than sleeping 1s for nothing).
        """
        ok = self._sync_balance_allowance(self._asset_type.CONDITIONAL, token_id=token_id)
        if not ok:
            return False
        time.sleep(SELL_BALANCE_SYNC_DELAY)
        return True

    def get_trades(self, token_id: str, after_timestamp: int | None = None) -> list[Trade]:
        if not self.private_key:
            return []

        try:
            params = self._trade_params(asset_id=token_id, after=after_timestamp)
            raw_trades = self.client.get_trades(params)
        except Exception as exc:
            logger.error(
                "get_trades failed",
                extra={
                    "token_id": token_id,
                    "after_timestamp": after_timestamp,
                    "error": str(exc),
                },
            )
            return []

        parsed: list[Trade] = []
        for raw in raw_trades:
            try:
                parsed.extend(self._parse_trade_rows(raw, default_token_id=token_id))
            except Exception as exc:
                logger.warning(
                    "failed to parse trade",
                    extra={
                        "error": str(exc),
                        "raw_keys": list(raw.keys()) if isinstance(raw, dict) else type(raw).__name__,
                    },
                )
                continue
        return parsed

    def bootstrap_live_trading(self, token_id: str | None = None) -> None:
        if not self.private_key:
            return

        if self.signature_type == 2 and self.funder_address:
            if not self.rpc_url:
                raise ValueError("POLYGON_RPC_URL is required for proxy-wallet approval bootstrap")
            approvals_set = ensure_conditional_token_approvals(
                private_key=self.private_key,
                proxy_address=self.funder_address,
                chain_id=self.chain_id,
                rpc_url=self.rpc_url,
            )
            logger.info(
                "proxy_ct_approvals_checked",
                extra={
                    "proxy_address": self.funder_address,
                    "approvals_set": approvals_set,
                },
            )

        if not self._sync_balance_allowance(self._asset_type.COLLATERAL):
            raise RuntimeError("Bootstrap failed: could not sync COLLATERAL balance allowance")
        if token_id is None:
            return
        if not self._sync_balance_allowance(self._asset_type.CONDITIONAL, token_id=token_id):
            raise RuntimeError("Bootstrap failed: could not sync CONDITIONAL balance allowance")

    def get_conditional_balance(self, token_id: str) -> float:
        """Get the actual on-chain conditional token balance in human-readable units."""
        self._sync_balance_allowance(self._asset_type.CONDITIONAL, token_id=token_id)
        snapshot = self._get_balance_allowance(self._asset_type.CONDITIONAL, token_id=token_id)
        return snapshot["balance"]

    def get_collateral_balance(self) -> float:
        """Get the pUSD collateral balance in human-readable units (6 decimals).

        V2: Polymarket USD (pUSD) replaces USDC.e as the collateral token.
        """
        self._sync_balance_allowance(self._asset_type.COLLATERAL)
        snapshot = self._get_balance_allowance(self._asset_type.COLLATERAL)
        return snapshot["balance"]

    def check_order_readiness(self, order: LimitOrderIntent | MarketOrderIntent) -> OrderReadiness:
        if not self.private_key:
            return OrderReadiness(False, "Authenticated exchange access is unavailable")

        try:
            if order.side == Side.BUY:
                self._sync_balance_allowance(self._asset_type.COLLATERAL)
                snapshot = self._get_balance_allowance(self._asset_type.COLLATERAL)
                required = order.notional
                asset_label = "collateral"
            else:
                self._sync_balance_allowance(self._asset_type.CONDITIONAL, token_id=order.token_id)
                snapshot = self._get_balance_allowance(self._asset_type.CONDITIONAL, token_id=order.token_id)
                required = order.size
                asset_label = "conditional"
        except Exception as exc:
            logger.warning(
                "readiness_check_failed",
                extra={
                    "token_id": order.token_id,
                    "side": order.side.value,
                    "error": str(exc),
                },
            )
            return OrderReadiness(False, "Could not verify venue balance/allowance")

        balance = snapshot["balance"]
        allowance = snapshot["allowance"]

        logger.info(
            "readiness_check",
            extra={
                "token_id": order.token_id,
                "side": order.side.value,
                "asset": asset_label,
                "balance": balance,
                "allowance": allowance,
                "required": required,
            },
        )

        if balance + 1e-9 < required:
            return OrderReadiness(
                False,
                f"Insufficient {asset_label} balance for order",
                balance=balance,
                allowance=allowance,
            )
        if allowance + 1e-9 < required:
            return OrderReadiness(
                False,
                f"Insufficient {asset_label} allowance for order",
                balance=balance,
                allowance=allowance,
            )
        return OrderReadiness(True, "ok", balance=balance, allowance=allowance)

    def cancel_order(self, order_id: str) -> bool:
        if not self.allow_trading:
            return False
        try:
            self.client.cancel(order_id)
            return True
        except Exception as exc:
            logger.warning(
                "cancel_order failed",
                extra={
                    "order_id": order_id,
                    "error": str(exc),
                },
            )
            return False

    def cancel_all(self) -> bool:
        if not self.allow_trading:
            return False
        try:
            self.client.cancel_all()
            return True
        except Exception as exc:
            logger.warning("cancel_all failed", extra={"error": str(exc)})
            return False

    def _get_balance_allowance(self, asset_type: Any, token_id: str | None = None) -> dict[str, float]:
        try:
            raw = self.client.get_balance_allowance(
                params=self._balance_allowance_params(
                    asset_type=asset_type,
                    token_id=token_id,
                    signature_type=self.signature_type,
                )
            )
        except Exception as exc:
            raise RuntimeError(f"Failed to fetch balance/allowance from venue: {exc}") from exc

        if not isinstance(raw, dict):
            raise ValueError(f"Expected balance/allowance response dict, got {type(raw).__name__}: {raw!r}")

        balance_raw = _extract_float_field(raw, "balance", aliases=["available_balance", "available"])
        allowance_raw = _extract_allowance_value(raw)

        # The CLOB API returns balances in raw token units (6 decimals for both
        # USDC and conditional tokens on Polygon). Convert to human-readable.
        balance = balance_raw / TOKEN_DECIMAL_FACTOR
        allowance = allowance_raw / TOKEN_DECIMAL_FACTOR
        return {"balance": balance, "allowance": allowance}

    def _sync_balance_allowance(self, asset_type: Any, token_id: str | None = None) -> bool:
        """Sync balance allowance with the CLOB. Returns True on success."""
        try:
            self.client.update_balance_allowance(
                params=self._balance_allowance_params(
                    asset_type=asset_type,
                    token_id=token_id,
                    signature_type=self.signature_type,
                )
            )
            return True
        except Exception as exc:
            logger.warning(
                "sync_balance_allowance_failed",
                extra={
                    "asset_type": str(asset_type),
                    "token_id": token_id,
                    "error": str(exc),
                },
            )
            return False

    def _parse_order_snapshot(self, raw: dict[str, Any], default_token_id: str | None = None) -> OpenOrder:
        order_id = _require_field(raw, "id", aliases=["orderID", "order_id"])
        order_token = str(raw.get("asset_id") or raw.get("token_id") or default_token_id or "")
        side = self._normalize_side(str(raw.get("side", "")))
        price = float(raw["price"])
        size_matched = (
            float(raw["size_matched"])
            if raw.get("size_matched") not in {None, ""}
            else None
        )
        original_size = (
            float(raw["original_size"])
            if raw.get("original_size") not in {None, ""}
            else (
                float(raw["size"]) if raw.get("size") not in {None, ""} else None
            )
        )
        return OpenOrder(
            order_id=order_id,
            token_id=order_token,
            side=side,
            price=price,
            size_matched=size_matched,
            original_size=original_size,
            status=str(raw.get("status")) if raw.get("status") is not None else None,
        )

    def _parse_trade_rows(self, raw: dict[str, Any], default_token_id: str) -> list[Trade]:
        trade_id = _require_field(raw, "id", aliases=["tradeID", "trade_id"])
        trader_role = str(raw.get("trader_side") or raw.get("type") or "").strip().upper()
        timestamp = raw.get("match_time") or raw.get("created_at") or raw.get("timestamp") or raw.get("last_update")
        token_id = str(raw.get("asset_id") or raw.get("token_id") or default_token_id)
        side = self._normalize_side(str(raw.get("side", "")))
        price = float(raw["price"])
        size = float(raw["size"])

        if trader_role == "MAKER":
            maker_orders = raw.get("maker_orders")
            if isinstance(maker_orders, list):
                parsed: list[Trade] = []
                for index, maker_order in enumerate(maker_orders):
                    if not isinstance(maker_order, dict):
                        continue
                    maker_order_id = _require_field(maker_order, "order_id", aliases=["orderID", "id"])
                    maker_token_id = str(maker_order.get("asset_id") or token_id)
                    maker_side = self._normalize_side(str(maker_order.get("side", raw.get("side", ""))))
                    maker_price = _extract_float_field(maker_order, "price", aliases=["matched_price"])
                    maker_size = _extract_float_field(
                        maker_order,
                        "matched_amount",
                        aliases=["size", "original_size"],
                    )
                    maker_fee = _extract_trade_fee(maker_order, maker_price, maker_size, fallback=raw)
                    parsed.append(
                        Trade(
                            trade_id=f"{trade_id}:{index}",
                            order_id=maker_order_id,
                            token_id=maker_token_id,
                            side=maker_side,
                            price=maker_price,
                            size=maker_size,
                            fee=maker_fee,
                            timestamp=timestamp,
                        )
                    )
                if parsed:
                    return parsed

        order_id = _require_field(
            raw,
            "orderID",
            aliases=["order_id", "taker_order_id", "takerOrderId"],
        )
        fee = _extract_trade_fee(raw, price, size)
        return [
            Trade(
                trade_id=trade_id,
                order_id=order_id,
                token_id=token_id,
                side=side,
                price=price,
                size=size,
                fee=fee,
                timestamp=timestamp,
            )
        ]

    @staticmethod
    def _normalize_side(value: str) -> Side:
        upper = value.strip().upper()
        if upper == "BUY":
            return Side.BUY
        if upper == "SELL":
            return Side.SELL
        raise ValueError(f"Unknown side value: {value!r}")


def _require_field(d: dict, key: str, aliases: list[str] | None = None) -> str:
    value = d.get(key)
    if value is not None:
        return str(value)
    for alias in aliases or []:
        value = d.get(alias)
        if value is not None:
            return str(value)
    tried = [key] + (aliases or [])
    raise KeyError(f"None of {tried} found in response. Keys present: {list(d.keys())}")


def _extract_float_field(d: dict[str, Any], key: str, aliases: list[str] | None = None) -> float:
    raw = d.get(key)
    if raw is None:
        for alias in aliases or []:
            raw = d.get(alias)
            if raw is not None:
                break
    if raw is None:
        raise KeyError(f"Missing field '{key}' in response. Keys present: {list(d.keys())}")
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Could not parse float field '{key}' from value {raw!r}") from exc


def _extract_allowance_value(d: dict[str, Any]) -> float:
    raw_allowance = d.get("allowance")
    if raw_allowance is not None:
        return _coerce_float(raw_allowance, field_name="allowance")

    allowances = d.get("allowances")
    if allowances is None:
        raise KeyError(f"Missing field 'allowance' in response. Keys present: {list(d.keys())}")

    values = _collect_float_values(allowances)
    if not values:
        raise ValueError(f"Could not parse any allowance values from {allowances!r}")

    unique_values = sorted(set(values))
    if len(unique_values) > 1:
        logger.warning(
            "multiple_allowances_seen",
            extra={"allowances": unique_values},
        )
    return max(values)


def _collect_float_values(value: Any) -> list[float]:
    if isinstance(value, dict):
        collected: list[float] = []
        for nested in value.values():
            collected.extend(_collect_float_values(nested))
        return collected

    if isinstance(value, list):
        collected: list[float] = []
        for nested in value:
            collected.extend(_collect_float_values(nested))
        return collected

    try:
        return [_coerce_float(value, field_name="allowances")]
    except ValueError:
        return []


def _extract_trade_fee(trade: dict[str, Any], price: float, size: float, fallback: dict[str, Any] | None = None) -> float:
    # V2: fees are now directly reported by the exchange; no fee_rate_bps field
    raw_fee = trade.get("fee")
    if raw_fee not in {None, ""}:
        return _coerce_float(raw_fee, field_name="fee")
    return 0.0


def _coerce_float(raw: Any, field_name: str) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Could not parse float field '{field_name}' from value {raw!r}") from exc


def _clamp_probability(price: float) -> float:
    return max(0.01, min(0.99, float(price)))
