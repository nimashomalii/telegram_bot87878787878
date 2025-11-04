import html
import re
from typing import Optional

import requests

from exchange_utils import PERSIAN_BASES, USD_STABLES, parse_exchange_id


COINGECKO_API = "https://api.coingecko.com/api/v3"


def _normalize_base_from_symbol(symbol: str) -> str:
    base = symbol.split("/")[0].upper()
    # Strip prefixes like PERP, 1000SHIB -> heuristic keep letters
    base = re.sub(r"[^A-Z]", "", base)
    return base


def _find_coingecko_id(base_symbol: str) -> Optional[str]:
    try:
        r = requests.get(f"{COINGECKO_API}/coins/list", timeout=15)
        r.raise_for_status()
        items = r.json()
        base_lower = base_symbol.lower()
        # Exact symbol match first
        for it in items:
            if it.get("symbol", "").lower() == base_lower:
                return it.get("id")
        # Fallback: name contains
        for it in items:
            if base_lower in (it.get("name", "").lower()):
                return it.get("id")
    except Exception:
        return None
    return None


async def get_coin_details(exchange_id: str, symbol: str) -> str:
    base = _normalize_base_from_symbol(symbol)
    fa_base = PERSIAN_BASES.get(base, base)
    ex_name = parse_exchange_id(exchange_id)

    # CoinGecko details
    cg_text = ""
    cg_id = _find_coingecko_id(base)
    if cg_id:
        try:
            r = requests.get(f"{COINGECKO_API}/coins/{cg_id}", params={"localization": "false"}, timeout=15)
            r.raise_for_status()
            j = r.json()
            genesis = j.get("genesis_date") or "نامشخص"
            homepage = (j.get("links", {}) or {}).get("homepage", [None])[0]
            desc = (j.get("description", {}) or {}).get("en", "")
            short_desc = html.escape(desc[:400] + ("..." if len(desc) > 400 else "")) if desc else ""
            cg_text = (
                f"تاریخ عرضه: {genesis}\n"
                f"صفحه اصلی: {homepage or '—'}\n"
                f"توضیحات: {short_desc}\n"
            )
        except Exception:
            cg_text = ""

    # Basic guidance; deep exchange-specific listing/margin data requires exchange APIs; keep it simple.
    text = (
        f"<b>توضیحات {fa_base} ({symbol})</b>\n"
        f"صرافی: {ex_name}\n"
        f"- حجم معاملات مرتبط (تقریبی) فقط در نمایش اصلی فهرست صرافی‌ها لحاظ شده است.\n"
        f"- قابلیت مارجین/فیوچرز ممکن است بسته به صرافی متفاوت باشد.\n"
    )
    if cg_text:
        text += "\n" + cg_text
    return text


