import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SUPPORTED_RUNTIME = "nothing_happens"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_optional(name: str) -> str | None:
    raw = os.getenv(name)
    return raw if raw else None


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return float(raw)


def _compute_live_send_enabled() -> bool:
    mode = os.getenv("BOT_MODE", "paper").strip().lower()
    live_trading = _env_bool("LIVE_TRADING_ENABLED", False)
    dry_run = _env_bool("DRY_RUN", True)
    return mode == "live" and live_trading and not dry_run


def _load_config_file() -> dict[str, Any]:
    path = os.getenv("CONFIG_PATH", "config.json")
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"Config file not found: {path}. "
            f"Copy config.example.json to config.json and fill in your values."
        )
    with p.open() as f:
        return json.load(f)


def _get_nothing_happens_section(cfg: dict[str, Any]) -> dict[str, Any]:
    strategy_name = str(cfg.get("strategy", SUPPORTED_RUNTIME) or "").strip()
    if strategy_name and strategy_name != SUPPORTED_RUNTIME:
        raise ValueError(
            "Unsupported runtime strategy "
            f"'{strategy_name}'. This repository only supports '{SUPPORTED_RUNTIME}'."
        )

    strategies = cfg.get("strategies", {})
    if not isinstance(strategies, dict):
        raise ValueError("config.json field 'strategies' must be an object")

    strategy_cfg = strategies.get(SUPPORTED_RUNTIME)
    if strategy_cfg is None:
        raise ValueError("Missing strategies.nothing_happens section in config.json")
    if not isinstance(strategy_cfg, dict):
        raise ValueError("strategies.nothing_happens must be an object")
    return strategy_cfg


@dataclass(frozen=True)
class ExchangeConfig:
    host: str
    chain_id: int
    signature_type: int
    private_key: str | None
    funder_address: str | None
    builder_code: str | None = None  # V2: optional builder attribution code
    live_send_enabled: bool = False

    def validate(self) -> None:
        if self.signature_type not in {0, 1, 2}:
            raise ValueError(
                f"connection.signature_type must be 0, 1, or 2, got {self.signature_type}"
            )
        if self.live_send_enabled and not self.private_key:
            raise ValueError(
                "PRIVATE_KEY is required when live order transmission is enabled "
                "(BOT_MODE=live, LIVE_TRADING_ENABLED=true, DRY_RUN=false)"
            )
        if (
            self.live_send_enabled
            and self.signature_type in {1, 2}
            and not self.funder_address
        ):
            raise ValueError(
                "FUNDER_ADDRESS is required in live mode with signature_type "
                f"{self.signature_type} (proxy/delegated wallet)"
            )


def _build_exchange_config(conn: dict[str, Any]) -> ExchangeConfig:
    exchange = ExchangeConfig(
        host=str(conn.get("host", "https://clob.polymarket.com")),
        chain_id=int(conn.get("chain_id", 137)),
        signature_type=int(conn.get("signature_type", 2)),
        private_key=_env_optional("PRIVATE_KEY"),
        funder_address=_env_optional("FUNDER_ADDRESS"),
        builder_code=_env_optional("BUILDER_CODE"),
        live_send_enabled=_compute_live_send_enabled(),
    )
    exchange.validate()
    return exchange


@dataclass(frozen=True)
class NothingHappensConfig:
    market_refresh_interval_sec: int = 600
    price_poll_interval_sec: int = 60
    position_sync_interval_sec: int = 60
    order_dispatch_interval_sec: int = 60
    cash_pct_per_trade: float = 0.02
    min_trade_amount: float = 5.0
    fixed_trade_amount: float = 0.0
    max_entry_price: float = 0.65
    allowed_slippage: float = 0.30
    request_concurrency: int = 4
    buy_retry_count: int = 3
    buy_retry_base_delay_sec: float = 1.0
    max_backoff_sec: float = 900.0
    max_new_positions: int = -1
    shutdown_on_max_new_positions: bool = False
    redeemer_interval_sec: int = 1800


def load_nothing_happens_config() -> tuple[ExchangeConfig, NothingHappensConfig]:
    return _load_nothing_happens_config(_load_config_file())


def _load_nothing_happens_config(
    cfg: dict[str, Any],
) -> tuple[ExchangeConfig, NothingHappensConfig]:
    conn = cfg.get("connection", {})
    if not isinstance(conn, dict):
        raise ValueError("config.json field 'connection' must be an object")
    strat = _get_nothing_happens_section(cfg)

    exchange = _build_exchange_config(conn)
    strategy = NothingHappensConfig(
        market_refresh_interval_sec=_env_int(
            "PM_NH_MARKET_REFRESH_INTERVAL_SEC",
            int(strat.get("market_refresh_interval_sec", 600)),
        ),
        price_poll_interval_sec=_env_int(
            "PM_NH_PRICE_POLL_INTERVAL_SEC",
            int(strat.get("price_poll_interval_sec", 60)),
        ),
        position_sync_interval_sec=_env_int(
            "PM_NH_POSITION_SYNC_INTERVAL_SEC",
            int(strat.get("position_sync_interval_sec", 60)),
        ),
        order_dispatch_interval_sec=_env_int(
            "PM_NH_ORDER_DISPATCH_INTERVAL_SEC",
            int(strat.get("order_dispatch_interval_sec", 60)),
        ),
        cash_pct_per_trade=_env_float(
            "PM_NH_CASH_PCT_PER_TRADE",
            float(strat.get("cash_pct_per_trade", 0.02)),
        ),
        min_trade_amount=_env_float(
            "PM_NH_MIN_TRADE_AMOUNT",
            float(strat.get("min_trade_amount", 5.0)),
        ),
        fixed_trade_amount=_env_float(
            "PM_NH_FIXED_TRADE_AMOUNT_USD",
            float(strat.get("fixed_trade_amount", 0.0)),
        ),
        max_entry_price=_env_float(
            "PM_NH_MAX_ENTRY_PRICE",
            float(strat.get("max_entry_price", 0.65)),
        ),
        allowed_slippage=_env_float(
            "PM_NH_ALLOWED_SLIPPAGE",
            float(strat.get("allowed_slippage", 0.30)),
        ),
        request_concurrency=_env_int(
            "PM_NH_REQUEST_CONCURRENCY",
            int(strat.get("request_concurrency", 4)),
        ),
        buy_retry_count=_env_int(
            "PM_NH_BUY_RETRY_COUNT",
            int(strat.get("buy_retry_count", 3)),
        ),
        buy_retry_base_delay_sec=_env_float(
            "PM_NH_BUY_RETRY_BASE_DELAY_SEC",
            float(strat.get("buy_retry_base_delay_sec", 1.0)),
        ),
        max_backoff_sec=_env_float(
            "PM_NH_MAX_BACKOFF_SEC",
            float(strat.get("max_backoff_sec", 900.0)),
        ),
        max_new_positions=_env_int(
            "PM_NH_MAX_NEW_POSITIONS",
            int(strat.get("max_new_positions", -1)),
        ),
        shutdown_on_max_new_positions=_env_bool(
            "PM_NH_SHUTDOWN_ON_MAX_NEW_POSITIONS",
            bool(strat.get("shutdown_on_max_new_positions", False)),
        ),
        redeemer_interval_sec=_env_int(
            "PM_NH_REDEEMER_INTERVAL_SEC",
            int(strat.get("redeemer_interval_sec", 1800)),
        ),
    )
    _validate_nothing_happens_config(strategy)
    return exchange, strategy


def _validate_nothing_happens_config(cfg: NothingHappensConfig) -> None:
    if cfg.market_refresh_interval_sec < 60:
        raise ValueError(
            f"market_refresh_interval_sec must be >= 60, got {cfg.market_refresh_interval_sec}"
        )
    if cfg.price_poll_interval_sec < 15:
        raise ValueError(
            f"price_poll_interval_sec must be >= 15, got {cfg.price_poll_interval_sec}"
        )
    if cfg.position_sync_interval_sec < 15:
        raise ValueError(
            f"position_sync_interval_sec must be >= 15, got {cfg.position_sync_interval_sec}"
        )
    if cfg.order_dispatch_interval_sec < 15:
        raise ValueError(
            f"order_dispatch_interval_sec must be >= 15, got {cfg.order_dispatch_interval_sec}"
        )
    if not (0 < cfg.cash_pct_per_trade <= 1.0):
        raise ValueError(
            f"cash_pct_per_trade must be in (0, 1.0], got {cfg.cash_pct_per_trade}"
        )
    if cfg.min_trade_amount <= 0:
        raise ValueError(f"min_trade_amount must be > 0, got {cfg.min_trade_amount}")
    if cfg.fixed_trade_amount < 0:
        raise ValueError(f"fixed_trade_amount must be >= 0, got {cfg.fixed_trade_amount}")
    if not (0 < cfg.max_entry_price <= 1.0):
        raise ValueError(f"max_entry_price must be in (0, 1.0], got {cfg.max_entry_price}")
    if not (0 < cfg.allowed_slippage <= 1.0):
        raise ValueError(f"allowed_slippage must be in (0, 1.0], got {cfg.allowed_slippage}")
    if cfg.request_concurrency < 1:
        raise ValueError(f"request_concurrency must be >= 1, got {cfg.request_concurrency}")
    if cfg.buy_retry_count < 1:
        raise ValueError(f"buy_retry_count must be >= 1, got {cfg.buy_retry_count}")
    if cfg.buy_retry_base_delay_sec < 0:
        raise ValueError(
            f"buy_retry_base_delay_sec must be >= 0, got {cfg.buy_retry_base_delay_sec}"
        )
    if cfg.max_backoff_sec <= 0:
        raise ValueError(f"max_backoff_sec must be > 0, got {cfg.max_backoff_sec}")
    if cfg.max_new_positions < -1:
        raise ValueError(f"max_new_positions must be >= -1, got {cfg.max_new_positions}")
    if cfg.redeemer_interval_sec < 60:
        raise ValueError(f"redeemer_interval_sec must be >= 60, got {cfg.redeemer_interval_sec}")
