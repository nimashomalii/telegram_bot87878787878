import os
from datetime import datetime, timezone
from typing import List

import pandas as pd


def _ts_to_iso(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


async def export_ohlcv_to_excel(symbol: str, timeframe: str, ohlcv: List[List[float]]) -> str:
    if not ohlcv:
        raise ValueError("No OHLCV to export")

    rows = []
    for row in ohlcv:
        rows.append({
            "Date": _ts_to_iso(int(row[0])),
            "Open": float(row[1]),
            "High": float(row[2]),
            "Low": float(row[3]),
            "Close": float(row[4]),
            "Volume": float(row[5]) if len(row) > 5 else None,
        })

    df = pd.DataFrame(rows, columns=["Date", "Open", "High", "Low", "Close", "Volume"])
    out_dir = "./artifacts"
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"ohlcv_{symbol.replace('/', '_')}_{timeframe}.xlsx")
    df.to_excel(out_path, index=False)
    return out_path


