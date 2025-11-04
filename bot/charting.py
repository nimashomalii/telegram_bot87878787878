import os
from datetime import datetime
from typing import List

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


async def render_candlestick_chart_png(symbol: str, timeframe: str, ohlcv: List[List[float]]) -> str:
    if not ohlcv:
        raise ValueError("No OHLCV data")

    # Prepare data
    times = [x[0] for x in ohlcv]
    opens = [x[1] for x in ohlcv]
    highs = [x[2] for x in ohlcv]
    lows = [x[3] for x in ohlcv]
    closes = [x[4] for x in ohlcv]

    fig, ax = plt.subplots(figsize=(12, 6))
    width = 0.6
    up_color = "#26a69a"
    down_color = "#ef5350"

    # Simple candlestick drawing
    for i, (t, o, h, l, c) in enumerate(zip(times, opens, highs, lows, closes)):
        color = up_color if c >= o else down_color
        ax.plot([i, i], [l, h], color=color, linewidth=1)
        rect_bottom = min(o, c)
        rect_height = abs(c - o) if abs(c - o) > 1e-12 else 1e-12
        ax.add_patch(plt.Rectangle((i - width/2, rect_bottom), width, rect_height, color=color, alpha=0.8))

    ax.set_title(f"{symbol} — {timeframe}")
    ax.set_xlabel("کندل")
    ax.set_ylabel("قیمت")
    ax.grid(True, linestyle='--', alpha=0.3)

    # X ticks sparsely
    step = max(1, len(times)//10)
    xticks = list(range(0, len(times), step))
    ax.set_xticks(xticks)
    ax.set_xlim(-1, len(times))

    out_dir = "./artifacts"
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"chart_{symbol.replace('/', '_')}_{timeframe}.png")
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


