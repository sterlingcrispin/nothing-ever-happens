"""Async supervisor for the public nothing-happens runtime."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import logging
import os
import signal
import sys
import time

import aiohttp
from dotenv import load_dotenv

from bot.config import load_nothing_happens_config
from bot.exchange.paper import PaperExchangeClient
from bot.live_recovery import LiveRecoveryCoordinator
from bot.logging_config import configure_logging
from bot.nothing_happens_control import NothingHappensControlState
from bot.portfolio_state import PortfolioState
from bot.risk_controls import RiskConfig, RiskController
from bot.strategy import nothing_happens

logger = logging.getLogger(__name__)


def _record_supervisor_event(action: str, **extra) -> None:
    try:
        from bot.trade_ledger import record_order

        record_order(
            action=action,
            market_slug="",
            side="",
            token_id="",
            amount=0,
            **extra,
        )
    except Exception:
        pass


def _validate_live_runtime(exchange_cfg, database_url: str | None) -> None:
    if exchange_cfg.live_send_enabled and not database_url:
        raise ValueError("DATABASE_URL is required when live order transmission is enabled")


def _build_exchange(exchange_cfg):
    if exchange_cfg.live_send_enabled:
        from bot.exchange.polymarket_clob import PolymarketClobExchangeClient

        return PolymarketClobExchangeClient(exchange_cfg, allow_trading=True)
    return PaperExchangeClient()


def _resolve_live_wallet_address(exchange_cfg) -> str | None:
    if not exchange_cfg.live_send_enabled:
        return None
    if exchange_cfg.signature_type in {1, 2}:
        return exchange_cfg.funder_address
    if exchange_cfg.signature_type != 0 or not exchange_cfg.private_key:
        return None

    from eth_account import Account

    try:
        return str(Account.from_key(exchange_cfg.private_key).address)
    except Exception as exc:
        raise ValueError("Could not derive live wallet address from PRIVATE_KEY") from exc


def _patch_clob_http_timeout() -> None:
    """Increase py-clob-client's httpx read timeout from 5s to 12s."""
    try:
        import httpx
        from py_clob_client.http_helpers import helpers

        helpers._http_client = httpx.Client(
            http2=True,
            timeout=httpx.Timeout(connect=5.0, read=12.0, write=5.0, pool=5.0),
        )
    except Exception as exc:
        logging.getLogger(__name__).warning("Failed to patch CLOB HTTP timeout: %s", exc)


async def run():
    load_dotenv()
    configure_logging(os.getenv("LOG_LEVEL", "INFO"))
    _patch_clob_http_timeout()

    exchange_cfg, strategy_cfg = load_nothing_happens_config()
    strategy_wallet_address = _resolve_live_wallet_address(exchange_cfg)

    database_url = os.getenv("DATABASE_URL")
    _validate_live_runtime(exchange_cfg, database_url)

    if database_url:
        from bot.trade_ledger import init_db

        init_db(database_url)

    logger.info(
        "bot_starting",
        extra={
            "runtime": "nothing_happens",
            "host": exchange_cfg.host,
            "chain_id": exchange_cfg.chain_id,
            "signature_type": exchange_cfg.signature_type,
            "live_send_enabled": exchange_cfg.live_send_enabled,
            "cash_pct_per_trade": strategy_cfg.cash_pct_per_trade,
            "min_trade_amount": strategy_cfg.min_trade_amount,
            "max_entry_price": strategy_cfg.max_entry_price,
            "allowed_slippage": strategy_cfg.allowed_slippage,
            "price_poll_interval_sec": strategy_cfg.price_poll_interval_sec,
            "market_refresh_interval_sec": strategy_cfg.market_refresh_interval_sec,
            "max_new_positions": strategy_cfg.max_new_positions,
        },
    )

    exchange = _build_exchange(exchange_cfg)
    portfolio_state = PortfolioState()
    nothing_happens_control = NothingHappensControlState()
    background_executor = ThreadPoolExecutor(
        max_workers=max(4, int(os.getenv("PM_BACKGROUND_EXECUTOR_WORKERS", "8"))),
        thread_name_prefix="pm-bg",
    )
    risk = RiskController(RiskConfig.from_env())
    recovery = (
        LiveRecoveryCoordinator(database_url, background_executor=background_executor)
        if exchange_cfg.live_send_enabled
        else None
    )
    if exchange_cfg.live_send_enabled and (recovery is None or not recovery.enabled):
        raise RuntimeError("Durable live recovery must be enabled in live mode")
    if recovery is not None and recovery.enabled:
        recovery.restore_risk_controller(risk, now_value_us=int(time.time() * 1_000_000))

    redeemer = None
    rpc_url = (os.getenv("POLYGON_RPC_URL") or "").strip()
    if (
        exchange_cfg.live_send_enabled
        and exchange_cfg.private_key
        and exchange_cfg.signature_type == 2
        and exchange_cfg.funder_address
        and rpc_url
    ):
        from bot.redeemer import Redeemer

        redeemer = Redeemer(
            private_key=exchange_cfg.private_key,
            proxy_address=exchange_cfg.funder_address,
            chain_id=exchange_cfg.chain_id,
            rpc_url=rpc_url,
            session=None,
            check_interval_sec=strategy_cfg.redeemer_interval_sec,
        )
        logger.info("redeemer_enabled")

    dashboard_port = os.getenv("PORT") or os.getenv("DASHBOARD_PORT")
    dashboard_task = None
    if dashboard_port:
        from bot.dashboard import DashboardServer

        dashboard = DashboardServer(
            portfolio_state=portfolio_state,
            nothing_happens_control=nothing_happens_control,
            port=int(dashboard_port),
            exchange=exchange,
        )
        dashboard_task = asyncio.create_task(dashboard.run(), name="dashboard")
        logger.info("dashboard_starting", extra={"port": int(dashboard_port)})

    shutdown = asyncio.Event()

    def on_signal():
        logger.info("Shutdown signal received; stopping gracefully")
        shutdown.set()

    loop = asyncio.get_running_loop()
    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, on_signal)

    async with aiohttp.ClientSession() as session:
        if redeemer is not None:
            redeemer._session = session

        if exchange_cfg.live_send_enabled:
            await asyncio.to_thread(exchange.bootstrap_live_trading, None)
            logger.info("exchange_bootstrapped")
            if risk.cfg.max_daily_drawdown_usd > 0:
                try:
                    startup_balance = await asyncio.to_thread(exchange.get_collateral_balance)
                    risk.seed_balance_hwm(int(time.time() * 1_000_000), startup_balance)
                    logger.info(
                        "drawdown_hwm_seeded",
                        extra={"collateral_balance": startup_balance},
                    )
                    _record_supervisor_event(
                        "drawdown_hwm_seeded",
                        usdc_balance=startup_balance,
                    )
                except Exception as exc:
                    logger.warning("drawdown_hwm_seed_failed: %s", exc)

        feed_factories = {
            "strategy": lambda: nothing_happens.run(
                exchange=exchange,
                session=session,
                cfg=strategy_cfg,
                risk=risk,
                background_executor=background_executor,
                shutdown_event=shutdown,
                portfolio_state=portfolio_state,
                control_state=nothing_happens_control,
                recovery_coordinator=recovery,
                wallet_address=strategy_wallet_address,
            ),
        }
        if recovery is not None and exchange_cfg.live_send_enabled:
            feed_factories["ambiguous_recovery"] = lambda: recovery.run_ambiguous_worker(
                exchange=exchange,
                venue_state=None,
                background_executor=background_executor,
            )
        if redeemer is not None:
            feed_factories["redeemer"] = lambda: redeemer.run()

        tasks: dict[str, asyncio.Task] = {}
        for name, factory in feed_factories.items():
            tasks[name] = asyncio.create_task(factory(), name=name)
        if dashboard_task is not None:
            tasks["dashboard"] = dashboard_task

        logger.info(
            "launched_tasks",
            extra={"tasks": list(tasks.keys()), "count": len(tasks)},
        )

        async def _heartbeat():
            start = time.monotonic()
            while not shutdown.is_set():
                await asyncio.sleep(60)
                elapsed = time.monotonic() - start
                mins, secs = divmod(int(elapsed), 60)
                portfolio = portfolio_state.snapshot()
                control = nothing_happens_control.snapshot()
                logger.info(
                    "heartbeat",
                    extra={
                        "uptime": f"{mins}m{secs:02d}s",
                        "monitored_markets": portfolio.monitored_markets,
                        "eligible_markets": portfolio.eligible_markets,
                        "in_range_markets": portfolio.in_range_markets,
                        "open_positions": len(portfolio.positions),
                        "target_open_positions": control.target_open_positions,
                        "pending_entry_count": control.pending_entry_count,
                        "remaining_position_capacity": control.remaining_capacity,
                        "opened_this_run": control.opened_this_run,
                        "cash_balance": portfolio.cash_balance,
                        "last_market_refresh_ts": portfolio.last_market_refresh_ts,
                        "last_position_sync_ts": portfolio.last_position_sync_ts,
                        "last_price_cycle_ts": portfolio.last_price_cycle_ts,
                        "last_error": portfolio.last_error,
                    },
                )

        tasks["heartbeat"] = asyncio.create_task(_heartbeat(), name="heartbeat")

        max_restarts = 1000
        health_reset_sec = 3600

        async def _supervisor():
            restart_counts = {name: 0 for name in feed_factories}
            last_restart_time = {name: 0.0 for name in feed_factories}
            recent_restarts = {name: [] for name in feed_factories}
            while not shutdown.is_set():
                await asyncio.sleep(10)
                now = asyncio.get_running_loop().time()
                for name in list(feed_factories):
                    task = tasks.get(name)
                    if task is None or not task.done() or task.cancelled():
                        if (
                            restart_counts[name] > 0
                            and last_restart_time[name] > 0
                            and (now - last_restart_time[name]) > health_reset_sec
                        ):
                            logger.info(
                                "feed_restart_counter_reset",
                                extra={"feed": name, "was": restart_counts[name]},
                            )
                            restart_counts[name] = 0
                        continue
                    exc = task.exception()
                    count = restart_counts[name]
                    if count >= max_restarts:
                        if count == max_restarts:
                            logger.error(
                                "feed_dead",
                                extra={"feed": name, "restarts": max_restarts},
                            )
                            _record_supervisor_event(
                                "feed_dead",
                                feed=name,
                                restarts=max_restarts,
                            )
                            restart_counts[name] = max_restarts + 1
                        continue
                    restart_counts[name] = count + 1
                    last_restart_time[name] = now
                    recent_restarts[name] = [
                        ts for ts in recent_restarts[name] if (now - ts) <= health_reset_sec
                    ]
                    recent_restarts[name].append(now)
                    logger.error(
                        "feed_crashed",
                        extra={
                            "feed": name,
                            "error": str(exc) if exc else "no exception",
                            "restart": f"{count + 1}/{max_restarts}",
                        },
                    )
                    _record_supervisor_event(
                        "feed_crashed",
                        feed=name,
                        error=str(exc) if exc else "no exception",
                        restart=f"{count + 1}/{max_restarts}",
                    )
                    if len(recent_restarts[name]) > 10:
                        logger.warning(
                            "feed_restart_escalation",
                            extra={
                                "feed": name,
                                "restart_count_last_hour": len(recent_restarts[name]),
                            },
                        )
                    tasks[name] = asyncio.create_task(feed_factories[name](), name=name)

        tasks["supervisor"] = asyncio.create_task(_supervisor(), name="supervisor")

        await shutdown.wait()

        strategy_task = tasks.get("strategy")
        for name, task in tasks.items():
            if name == "strategy":
                continue
            task.cancel()

        if strategy_task is not None:
            try:
                await asyncio.wait_for(strategy_task, timeout=5.0)
            except asyncio.TimeoutError:
                strategy_task.cancel()
            except asyncio.CancelledError:
                pass

        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for (name, _), result in zip(tasks.items(), results):
            if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
                logger.error("task_error_on_shutdown", extra={"task": name, "error": str(result)})

    background_executor.shutdown(wait=True, cancel_futures=False)
    logger.info("shutdown_complete")


def main():
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
