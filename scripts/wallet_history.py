#!/usr/bin/env python3
"""Pull all trading data for a wallet over the last N hours.

Usage:
  python scripts/wallet_history.py [--hours 12] [--wallet 0x...]
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

DATA_API_BASE = "https://data-api.polymarket.com"

# ANSI
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[91m"
GREEN = "\033[92m"
CYAN = "\033[96m"
RESET = "\033[0m"


def resolve_default_wallet() -> str | None:
    for env_name in ("TARGET_WALLET", "WALLET_ADDRESS", "FUNDER_ADDRESS"):
        value = (os.getenv(env_name) or "").strip()
        if value:
            return value

    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from bot.config import load_nothing_happens_config

        exchange_cfg, _ = load_nothing_happens_config()
        if exchange_cfg.signature_type in {1, 2} and exchange_cfg.funder_address:
            return exchange_cfg.funder_address
        if exchange_cfg.signature_type == 0 and exchange_cfg.private_key:
            from eth_account import Account

            return str(Account.from_key(exchange_cfg.private_key).address)
    except Exception:
        return None
    return None


def fetch_positions(wallet: str) -> list[dict]:
    """Fetch all positions (open + redeemable) from Data API."""
    positions = []
    for redeemable in ("true", "false"):
        resp = requests.get(
            f"{DATA_API_BASE}/positions",
            params={"user": wallet, "redeemable": redeemable, "sizeThreshold": "0"},
            timeout=30,
        )
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                for p in data:
                    p["_redeemable"] = redeemable == "true"
                positions.extend(data)
    return positions


def fetch_trades_authenticated(after_ts: int) -> list[dict]:
    """Fetch trades using the authenticated CLOB client.

    The py-clob-client's get_trades() expects a TradeParams dataclass and
    handles cursor-based pagination internally, returning a flat list.
    """
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from bot.config import load_nothing_happens_config
        from bot.exchange.polymarket_clob import PolymarketClobExchangeClient
        from py_clob_client_v2.clob_types import TradeParams

        exchange_cfg, _ = load_nothing_happens_config()
        client = PolymarketClobExchangeClient(exchange_cfg, allow_trading=False)

        params = TradeParams(after=after_ts)
        # Library auto-paginates and returns a flat list of trade dicts
        trades = client.client.get_trades(params)
        return trades if isinstance(trades, list) else []
    except Exception as e:
        print(f"{RED}Auth trade fetch failed: {e}{RESET}", file=sys.stderr)
        return []


def fetch_usdc_balance_polygon(wallet: str) -> float | None:
    """Try to get current USDC balance via Polygonscan."""
    api_key = os.getenv("POLYGONSCAN_API_KEY")
    if not api_key:
        return None
    usdc_contract = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
    resp = requests.get(
        "https://api.polygonscan.com/api",
        params={
            "module": "account",
            "action": "tokenbalance",
            "contractaddress": usdc_contract,
            "address": wallet,
            "tag": "latest",
            "apikey": api_key,
        },
        timeout=15,
    )
    if resp.status_code == 200:
        data = resp.json()
        if data.get("status") == "1":
            return int(data["result"]) / 1e6
    return None


def format_ts(ts_val) -> str:
    """Convert various timestamp formats to readable string."""
    if isinstance(ts_val, str):
        # Try ISO format
        try:
            dt = datetime.fromisoformat(ts_val.replace("Z", "+00:00"))
            return dt.strftime("%H:%M:%S")
        except ValueError:
            pass
        # Try unix string
        try:
            ts_val = float(ts_val)
        except ValueError:
            return ts_val[:8]

    if isinstance(ts_val, (int, float)):
        # Milliseconds?
        if ts_val > 1e12:
            ts_val = ts_val / 1000
        dt = datetime.fromtimestamp(ts_val, tz=timezone.utc)
        return dt.strftime("%H:%M:%S")
    return "??:??:??"


def main():
    parser = argparse.ArgumentParser(description="Fetch wallet trading history")
    parser.add_argument("--hours", type=int, default=12)
    parser.add_argument("--wallet")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    args = parser.parse_args()
    wallet = args.wallet or resolve_default_wallet()
    if not wallet:
        parser.error(
            "provide --wallet or set TARGET_WALLET/WALLET_ADDRESS/FUNDER_ADDRESS"
        )

    cutoff = int(time.time()) - (args.hours * 3600)
    print(f"{BOLD}Wallet: {wallet}{RESET}")
    print(f"{BOLD}Period: last {args.hours} hours (since {datetime.fromtimestamp(cutoff, tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}){RESET}")
    print()

    # 1. Current positions
    print(f"{CYAN}=== Current Positions ==={RESET}")
    positions = fetch_positions(wallet)
    if positions:
        for p in sorted(positions, key=lambda x: x.get("slug", "")):
            slug = p.get("slug") or p.get("title") or p.get("conditionId", "")[:20]
            size = p.get("size", 0)
            redeemable = p.get("_redeemable", False)
            outcome = p.get("outcome", "?")
            cur_price = p.get("curPrice", "?")
            print(f"  {slug}")
            print(f"    size={size}  outcome={outcome}  price={cur_price}  redeemable={redeemable}")
    else:
        print("  No positions found")
    print()

    # 2. USDC balance
    print(f"{CYAN}=== USDC Balance ==={RESET}")
    bal = fetch_usdc_balance_polygon(wallet)
    if bal is not None:
        print(f"  ${bal:.2f}")
    else:
        print(f"  {DIM}(no POLYGONSCAN_API_KEY set, skipping){RESET}")
    print()

    # 3. Trade history (authenticated)
    print(f"{CYAN}=== Trade History (last {args.hours}h) ==={RESET}")
    trades = fetch_trades_authenticated(cutoff)

    if args.json:
        print(json.dumps(trades, indent=2))
        return

    if not trades:
        print("  No trades found (or auth failed)")
        print()
    else:
        # Sort by time
        def trade_sort_key(t):
            ts = t.get("match_time") or t.get("created_at") or t.get("timestamp") or 0
            if isinstance(ts, str):
                try:
                    return float(ts)
                except ValueError:
                    return 0
            return float(ts)

        trades.sort(key=trade_sort_key)

        # Group by market slug
        by_market: dict[str, list[dict]] = {}
        for t in trades:
            market = t.get("market") or t.get("asset_id", "unknown")[:20]
            by_market.setdefault(market, []).append(t)

        total_spent = 0.0
        total_received = 0.0

        for market, mtrades in by_market.items():
            print(f"\n  {BOLD}{market}{RESET}")
            for t in mtrades:
                ts = format_ts(t.get("match_time") or t.get("created_at") or t.get("timestamp", 0))
                side = t.get("side", "?")
                price = float(t.get("price", 0))
                size = float(t.get("size", 0))
                fee = float(t.get("fee", 0))
                cost = price * size
                trader_side = t.get("trader_side") or t.get("type") or ""

                if side.upper() == "BUY":
                    total_spent += cost + fee
                    color = GREEN
                else:
                    total_received += cost - fee
                    color = RED

                print(f"    {ts}  {color}{side:4s}{RESET}  px={price:.4f}  sz={size:.2f}  "
                      f"cost=${cost:.2f}  fee=${fee:.4f}  {DIM}{trader_side}{RESET}")

        print(f"\n  {BOLD}Summary:{RESET}")
        print(f"    Total trades: {len(trades)}")
        print(f"    Total spent:    ${total_spent:.2f}")
        print(f"    Total received: ${total_received:.2f}")
        net = total_received - total_spent
        color = GREEN if net >= 0 else RED
        print(f"    Net:            {color}${net:+.2f}{RESET}")

    # 4. Reconstruct balance timeline from trade ledger logs
    print(f"\n{CYAN}=== Balance Timeline (from logs) ==={RESET}")
    try:
        log_path = os.path.join(os.path.dirname(__file__), "..", "logs", "latest.json")
        if os.path.exists(log_path):
            balances = []
            with open(log_path) as f:
                for line in f:
                    if "dashboard_starting_balance" in line:
                        idx = line.find("{")
                        if idx >= 0:
                            msg = json.loads(line[idx:])
                            balances.append((msg.get("timestamp", ""), msg.get("balance", 0)))
                    elif '"action": "buy"' in line or '"action": "flip_sell"' in line:
                        idx = line.find("{")
                        if idx >= 0:
                            msg = json.loads(line[idx:])
                            if msg.get("message") == "trade_ledger":
                                action = msg.get("action", "")
                                side = msg.get("side", "")
                                amount = msg.get("amount", 0)
                                mkt_px = msg.get("market_price", msg.get("reference_price", 0))
                                ts_str = msg.get("timestamp", "")
                                slug = msg.get("market_slug", "")
                                print(f"  {ts_str[:19]}  {action:10s}  {side:4s}  "
                                      f"${amount:.2f} @ {mkt_px}  {slug}")
            if balances:
                for ts_str, bal in balances:
                    print(f"  {ts_str[:19]}  BALANCE     ${bal:.2f}")
        else:
            print(f"  {DIM}No logs/latest.json — populate it with logshtml.sh first{RESET}")
    except Exception as e:
        print(f"  {RED}Error reading logs: {e}{RESET}")

    print()


if __name__ == "__main__":
    main()
