import asyncio
import time
from typing import List, Dict, Optional, Tuple

import ccxt.async_support as ccxt  # use async version


# Very small Persian mapping for top common exchanges and bases
PERSIAN_NAMES_EX = {
    "binance": "بایننس",
    "coinbase": "کوین‌بیس",
    "okx": "اوکی‌ایکس",
    "bybit": "بای‌بیت",
    "kraken": "کراکن",
    "kucoin": "کوکوین",
}

PERSIAN_BASES = {
    "BTC": "بیت‌کوین",
    "ETH": "اتریوم",
    "BNB": "بایننس‌کوین",
    "SOL": "سولانا",
    "ADA": "کاردانو",
    "XRP": "ریپل",
}


USD_STABLES = {"USD", "USDT", "USDC", "BUSD", "TUSD"}


def parse_exchange_id(exchange_id: str) -> str:
    return PERSIAN_NAMES_EX.get(exchange_id, exchange_id.capitalize())


def _human_usd(num: float) -> str:
    # Format with B/M/K suffixes
    absn = abs(num)
    if absn >= 1e9:
        return f"{num/1e9:.1f}B"
    if absn >= 1e6:
        return f"{num/1e6:.1f}M"
    if absn >= 1e3:
        return f"{num/1e3:.1f}K"
    return f"{num:.0f}"


async def _safe_call(coro, timeout_s: float = 15):
    return await asyncio.wait_for(coro, timeout=timeout_s)


async def _fetch_exchange_volume_usd(ex_id: str, timeout_s: float = 15) -> float:
    ex_cls = getattr(ccxt, ex_id)
    ex = ex_cls({"enableRateLimit": True})
    try:
        markets = await _safe_call(ex.load_markets(), timeout_s)
        total = 0.0
        if ex.has.get("fetchTickers"):
            tickers = await _safe_call(ex.fetch_tickers(), timeout_s)
            for sym, t in tickers.items():
                try:
                    parts = sym.split("/")
                    quote = parts[1] if len(parts) > 1 else ""
                    if quote not in USD_STABLES:
                        continue
                    qv = t.get("quoteVolume") or 0.0
                    # Fallback: price * baseVolume when quoteVolume missing
                    if not qv:
                        last = t.get("last") or 0.0
                        bv = t.get("baseVolume") or 0.0
                        qv = (last or 0.0) * (bv or 0.0)
                    total += float(qv or 0.0)
                except Exception:
                    continue
        return total
    except Exception:
        return 0.0
    finally:
        try:
            await ex.close()
        except Exception:
            pass


async def list_supported_exchanges_sorted_by_volume(max_exchanges: int = 30, timeout_s: float = 15) -> List[Dict]:
    all_ex = ccxt.exchanges
    # Limit to avoid rate explosion
    selected = all_ex[: max_exchanges * 2]

    async def compute(ex_id: str):
        vol = await _fetch_exchange_volume_usd(ex_id, timeout_s=timeout_s)
        return ex_id, vol

    tasks = [asyncio.create_task(compute(e)) for e in selected]
    results: List[Tuple[str, float]] = []
    for t in asyncio.as_completed(tasks):
        try:
            results.append(await t)
        except Exception:
            continue

    # Sort desc and take top
    results.sort(key=lambda x: x[1], reverse=True)
    top = results[:max_exchanges]

    out = []
    for ex_id, vol in top:
        out.append({
            "id": ex_id,
            "name": ex_id.capitalize(),
            "fa_name": PERSIAN_NAMES_EX.get(ex_id, ex_id.capitalize()),
            "volume_usd": vol,
            "volume_human": _human_usd(vol),
        })
    return out


async def list_symbols_with_prices(exchange_id: str, max_symbols: int = 80) -> List[Dict]:
    ex_cls = getattr(ccxt, exchange_id)
    ex = ex_cls({"enableRateLimit": True})
    try:
        await ex.load_markets()
        symbols = list(ex.symbols or [])
        # Prefer USD-stable quoted pairs
        def score(sym: str) -> int:
            parts = sym.split("/")
            quote = parts[1] if len(parts) > 1 else ""
            return 1 if quote in USD_STABLES else 0
        symbols.sort(key=score, reverse=True)
        symbols = symbols[:max_symbols]

        out = []
        for sym in symbols:
            try:
                t = await ex.fetch_ticker(sym)
                last = float(t.get("last") or 0.0)
                base = sym.split("/")[0]
                out.append({
                    "symbol": sym,
                    "base": base,
                    "fa_name": PERSIAN_BASES.get(base, base),
                    "price": last,
                    "price_human": f"{last:,.6f}",
                })
            except Exception:
                continue
        return out
    finally:
        try:
            await ex.close()
        except Exception:
            pass


async def get_latest_price(exchange_id: str, symbol: str) -> Tuple[Optional[float], Optional[int]]:
    ex_cls = getattr(ccxt, exchange_id)
    ex = ex_cls({"enableRateLimit": True})
    try:
        t = await ex.fetch_ticker(symbol)
        last = t.get("last")
        ts = t.get("timestamp")
        return (float(last) if last is not None else None, int(ts) if ts is not None else None)
    except Exception:
        return None, None
    finally:
        try:
            await ex.close()
        except Exception:
            pass


async def fetch_ohlcv_data(exchange_id: str, symbol: str, timeframe: str, limit: int = 200, since: Optional[int] = None, until: Optional[int] = None) -> List[List[float]]:
    ex_cls = getattr(ccxt, exchange_id)
    ex = ex_cls({"enableRateLimit": True})
    try:
        await ex.load_markets()
        # ccxt does not support until param directly on all exchanges; we page if needed
        if since is None:
            data = await ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            return data
        # paginate by timeframe until reaching until or limit
        out: List[List[float]] = []
        next_since = since
        while True:
            batch = await ex.fetch_ohlcv(symbol, timeframe=timeframe, since=next_since, limit=min(500, limit))
            if not batch:
                break
            out.extend(batch)
            if len(out) >= limit:
                break
            last_ts = batch[-1][0]
            if until is not None and last_ts >= until:
                break
            next_since = last_ts + 1
        if until is not None:
            out = [row for row in out if row[0] <= until]
        return out[:limit]
    finally:
        try:
            await ex.close()
        except Exception:
            pass


