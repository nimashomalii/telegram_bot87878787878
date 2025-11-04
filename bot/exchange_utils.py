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
    # Format with B/M/K suffixes (e.g., 1.2M)
    absn = abs(num)
    if absn >= 1e9:
        return f"{num/1e9:.1f}B"
    if absn >= 1e6:
        return f"{num/1e6:.1f}M"
    if absn >= 1e3:
        return f"{num/1e3:.1f}K"
    return f"{num:.0f}"


def split_human_amount(human: str) -> (str, str):
    # returns (number_str, unit_str)
    if not human:
        return "0", ""
    if human[-1] in ["B", "M", "K"]:
        return human[:-1], human[-1]
    return human, ""


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
            # convenience for UI
            "volume_num_str": split_human_amount(_human_usd(vol))[0],
            "volume_unit": split_human_amount(_human_usd(vol))[1],
        })
    return out


async def list_symbols_with_prices(exchange_id: str, max_symbols: int = 100) -> List[Dict]:
    ex_cls = getattr(ccxt, exchange_id)
    ex = ex_cls({"enableRateLimit": True, "timeout": 15000})
    try:
        await ex.load_markets()
        symbols = list(ex.symbols or [])
        # Prefer USD-stable quoted pairs
        def quote_is_usd(sym: str) -> bool:
            parts = sym.split("/")
            quote = parts[1] if len(parts) > 1 else ""
            return quote in USD_STABLES

        # Fast path: fetch all tickers once (if supported)
        if ex.has.get("fetchTickers"):
            try:
                tickers = await _safe_call(ex.fetch_tickers(), timeout_s=20)
            except Exception:
                tickers = {}

            out: List[Dict] = []
            for sym, t in tickers.items():
                # only include symbols known in markets and having USD-like quote
                if sym not in ex.markets:
                    continue
                if not quote_is_usd(sym):
                    continue
                try:
                    last = float(t.get("last") or 0.0)
                    qv = t.get("quoteVolume") or 0.0
                    if not qv:
                        last_tmp = t.get("last") or 0.0
                        bv = t.get("baseVolume") or 0.0
                        qv = (last_tmp or 0.0) * (bv or 0.0)
                    base = sym.split("/")[0]
                    vol_h = _human_usd(float(qv or 0.0))
                    num_str, unit = split_human_amount(vol_h)
                    out.append({
                        "symbol": sym,
                        "base": base,
                        "fa_name": PERSIAN_BASES.get(base, base),
                        "price": last,
                        "price_human": f"{last:,.6f}",
                        "volume_usd": float(qv or 0.0),
                        "volume_human": vol_h,
                        "volume_num_str": num_str,
                        "volume_unit": unit,
                        "is_usd_quote": True,
                    })
                except Exception:
                    continue
            # Sort by volume desc, take top max_symbols
            out.sort(key=lambda x: -x["volume_usd"])
            return out[:max_symbols]

        # Slow path: fetch a subset concurrently (USD-quoted only)
        cand = [s for s in symbols if quote_is_usd(s)]
        cand = cand[:200]  # cap to limit API usage

        sem = asyncio.Semaphore(10)
        async def fetch_one(sym: str) -> Optional[Dict]:
            async with sem:
                try:
                    t = await _safe_call(ex.fetch_ticker(sym), timeout_s=12)
                    last = float(t.get("last") or 0.0)
                    qv = t.get("quoteVolume") or 0.0
                    if not qv:
                        last_tmp = t.get("last") or 0.0
                        bv = t.get("baseVolume") or 0.0
                        qv = (last_tmp or 0.0) * (bv or 0.0)
                    base = sym.split("/")[0]
                    vol_h = _human_usd(float(qv or 0.0))
                    num_str, unit = split_human_amount(vol_h)
                    return {
                        "symbol": sym,
                        "base": base,
                        "fa_name": PERSIAN_BASES.get(base, base),
                        "price": last,
                        "price_human": f"{last:,.6f}",
                        "volume_usd": float(qv or 0.0),
                        "volume_human": vol_h,
                        "volume_num_str": num_str,
                        "volume_unit": unit,
                        "is_usd_quote": True,
                    }
                except Exception:
                    return None

        tasks = [asyncio.create_task(fetch_one(s)) for s in cand]
        results = []
        for t in asyncio.as_completed(tasks):
            r = await t
            if r:
                results.append(r)
        results.sort(key=lambda x: -x["volume_usd"])
        return results[:max_symbols]
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


def timeframe_to_ms(tf: str) -> int:
    m = {
        "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
        "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "6h": 21_600_000, "12h": 43_200_000,
        "1d": 86_400_000, "3d": 259_200_000, "1w": 604_800_000
    }
    return m.get(tf, 60_000)


async def get_ticker_details(exchange_id: str, symbol: str) -> Dict:
    ex_cls = getattr(ccxt, exchange_id)
    ex = ex_cls({"enableRateLimit": True, "timeout": 15000})
    try:
        t = await _safe_call(ex.fetch_ticker(symbol), timeout_s=15)
        last = t.get("last")
        ts = t.get("timestamp")
        high = t.get("high")
        low = t.get("low")
        chg = t.get("percentage")
        qv = t.get("quoteVolume") or 0.0
        if not qv:
            last_tmp = t.get("last") or 0.0
            bv = t.get("baseVolume") or 0.0
            qv = (last_tmp or 0.0) * (bv or 0.0)
        return {
            "last": float(last) if last is not None else None,
            "timestamp": int(ts) if ts is not None else None,
            "high": float(high) if high is not None else None,
            "low": float(low) if low is not None else None,
            "percentage": float(chg) if chg is not None else None,
            "quoteVolume": float(qv or 0.0),
        }
    except Exception:
        return {}
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
        # If no since provided, fetch once
        if since is None and until is None:
            return await _safe_call(ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=min(limit, 500)), timeout_s=20)

        # Paginate in chunks of 500, step by timeframe to avoid overlap
        tf_ms = timeframe_to_ms(timeframe)
        out: List[List[float]] = []
        next_since = since or 0
        while True:
            chunk_limit = min(500, max(1, limit - len(out)))
            batch = await _safe_call(ex.fetch_ohlcv(symbol, timeframe=timeframe, since=next_since, limit=chunk_limit), timeout_s=25)
            if not batch:
                break
            # Ensure strictly increasing and avoid overlap
            if out and batch[0][0] <= out[-1][0]:
                # advance by one timeframe to skip overlap
                next_since = out[-1][0] + tf_ms
                continue
            out.extend(batch)
            if len(out) >= limit:
                break
            last_ts = batch[-1][0]
            if until is not None and last_ts >= until:
                break
            next_since = last_ts + tf_ms
        if until is not None:
            out = [row for row in out if row[0] <= until]
        return out[:limit]
    finally:
        try:
            await ex.close()
        except Exception:
            pass


