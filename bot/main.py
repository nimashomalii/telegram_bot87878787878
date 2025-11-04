import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Dict, Tuple, Optional

from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

from exchange_utils import (
    list_supported_exchanges_sorted_by_volume,
    list_symbols_with_prices,
    get_latest_price,
    parse_exchange_id,
    fetch_ohlcv_data,
    get_ticker_details,
)
from charting import render_candlestick_chart_png
from excel_exporter import export_ohlcv_to_excel
from coin_info import get_coin_details


load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# Simple in-memory per-user state for multi-step prompts
USER_STATE: Dict[int, Dict[str, str]] = {}

# Very simple favorites store (per user) persisted in a json file
FAV_FILE = os.path.join("artifacts", "favorites.json")
STATE_FILE = os.path.join("artifacts", "user_states.json")

def _load_favorites() -> Dict[str, list]:
    try:
        os.makedirs("artifacts", exist_ok=True)
        if not os.path.exists(FAV_FILE):
            return {}
        import json
        with open(FAV_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}

def _save_favorites(data: Dict[str, list]) -> None:
    try:
        import json
        os.makedirs("artifacts", exist_ok=True)
        with open(FAV_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        logger.exception("save favorites failed")

def _load_user_states() -> Dict[str, Dict]:
    try:
        os.makedirs("artifacts", exist_ok=True)
        if not os.path.exists(STATE_FILE):
            return {}
        import json
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}

def _save_user_state(user_id: str, state: Dict) -> None:
    try:
        import json
        os.makedirs("artifacts", exist_ok=True)
        all_states = _load_user_states()
        all_states[user_id] = state
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(all_states, f, ensure_ascii=False, indent=2)
    except Exception:
        logger.exception("save user state failed")


def _get_user_state(user_id: int) -> Dict[str, str]:
    if user_id not in USER_STATE:
        # Try to load from file
        saved = _load_user_states().get(str(user_id), {})
        USER_STATE[user_id] = saved if saved else {}
    return USER_STATE[user_id]

def _persist_user_state(user_id: int) -> None:
    """Save current user state to file"""
    if user_id in USER_STATE:
        _save_user_state(str(user_id), USER_STATE[user_id])


async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show main menu - can be called from start or callback"""
    user = update.effective_user
    if not user:
        user = update.callback_query.from_user if update.callback_query else None
    if not user:
        return
    
    user_id = user.id
    state = _get_user_state(user_id)
    
    buttons = [
        [
            InlineKeyboardButton("📊 فهرست صرافی‌ها", callback_data="menu:exchanges"),
            InlineKeyboardButton("🪙 نمادها", callback_data="menu:symbols"),
            InlineKeyboardButton("⭐ پنل من", callback_data="panel:open"),
        ]
    ]
    
    # If user has a saved state with symbol, show continue option
    if state.get("exchange_id") and state.get("symbol"):
        buttons.append([InlineKeyboardButton("▶️ ادامه از آخرین مرحله", callback_data=f"sym:{state.get('exchange_id')}:{state.get('symbol')}")])
    
    text = f"سلام {user.first_name or ''}!\nبه ربات کریپتو خوش آمدید. از میان گزینه‌های زیر انتخاب کنید."
    markup = InlineKeyboardMarkup(buttons)
    
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup)
    elif update.message:
        await update.message.reply_text(text, reply_markup=markup)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_main_menu(update, context)


def _build_exchanges_keyboard(exchanges: list, page: int) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton("🏦 صرافی", callback_data="noop"),
        InlineKeyboardButton("💱 حجم معاملات", callback_data="noop"),
        InlineKeyboardButton("🧮 واحد", callback_data="noop"),
    ]]
    start = page * 10
    end = start + 10
    for ex in exchanges[start:end]:
        fa_name = ex.get("fa_name", ex["name"])  # Persian name/transliteration if available
        name_label = f"🏦 {fa_name} ({ex['name']})"
        vol_num = ex.get("volume_num_str", "0")
        vol_unit = ex.get("volume_unit", "")
        rows.append([
            InlineKeyboardButton(name_label, callback_data=f"ex:{ex['id']}"),
            InlineKeyboardButton(f"{vol_num}", callback_data="noop"),
            InlineKeyboardButton(vol_unit or "—", callback_data="noop"),
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⏮️ قبلی", callback_data=f"expage:{page-1}"))
    if end < len(exchanges):
        nav.append(InlineKeyboardButton("⏭️ بعدی", callback_data=f"expage:{page+1}"))
    if nav:
        rows.append(nav)
    # Add back to main menu button
    rows.append([InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="menu:home")])
    return InlineKeyboardMarkup(rows)


async def send_exchange_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    exchanges = await list_supported_exchanges_sorted_by_volume(max_exchanges=50, timeout_s=25)
    if not exchanges:
        text = "نتوانستم فهرست صرافی‌ها را دریافت کنم. لطفاً بعداً تلاش کنید."
        if update.message:
            await update.message.reply_text(text)
        elif update.callback_query:
            await update.callback_query.edit_message_text(text)
        return

    user_id = update.effective_user.id if update.effective_user else 0
    state = _get_user_state(user_id)
    state["exchanges"] = exchanges
    state["ex_page"] = "0"

    markup = _build_exchanges_keyboard(exchanges, page=0)
    text = "ستون‌ها: صرافی | حجم معاملات | واحد\nیک صرافی را انتخاب کنید:"
    if update.message:
        await update.message.reply_text(text, reply_markup=markup)
    elif update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup)


async def on_exchange_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, exchange_id = query.data.split(":", 1)

    try:
        await query.message.delete()
    except Exception:
        pass

    sent = await query.message.chat.send_message("در حال دریافت لیست نمادها و قیمت‌ها...")
    symbols = await list_symbols_with_prices(exchange_id, max_symbols=200)

    if not symbols:
        await sent.edit_text("نتوانستم نمادها را دریافت کنم. لطفاً صرافی دیگری را انتخاب کنید.")
        return

    user_id = query.from_user.id
    state = _get_user_state(user_id)
    state["exchange_id"] = exchange_id
    state["symbols"] = symbols
    state["sym_page"] = "0"
    _persist_user_state(user_id)

    markup = _build_symbols_keyboard(symbols, exchange_id, page=0)
    await sent.edit_text(
        f"🧾 صرافی: {parse_exchange_id(exchange_id)}\nستون‌ها: 🪙 رمز ارز | 🧾 نماد | 💵 قیمت | $\nیکی را انتخاب کنید:",
        reply_markup=markup,
    )


def _build_symbols_keyboard(symbols: list, exchange_id: str, page: int) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton("🪙 رمز ارز", callback_data="noop"),
        InlineKeyboardButton("🧾 نماد", callback_data="noop"),
        InlineKeyboardButton("💵 قیمت", callback_data="noop"),
        InlineKeyboardButton("$", callback_data="noop"),
    ]]
    start = page * 10
    end = start + 10
    for s in symbols[start:end]:
        fa_name = s.get("fa_name", s["base"])
        price_num = s.get("price_human", "0")
        rows.append([
            InlineKeyboardButton(f"🪙 {fa_name}", callback_data=f"sym:{exchange_id}:{s['symbol']}"),
            InlineKeyboardButton(s['symbol'], callback_data=f"sym:{exchange_id}:{s['symbol']}"),
            InlineKeyboardButton(price_num, callback_data="noop"),
            InlineKeyboardButton("$", callback_data="noop"),
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⏮️ قبلی", callback_data=f"sympage:{exchange_id}:{page-1}"))
    if end < len(symbols):
        nav.append(InlineKeyboardButton("⏭️ بعدی", callback_data=f"sympage:{exchange_id}:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("🔙 بازگشت به صرافی‌ها", callback_data="expage:0")])
    return InlineKeyboardMarkup(rows)


async def on_symbol_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, exchange_id, symbol = query.data.split(":", 2)

    state = _get_user_state(query.from_user.id)
    state["exchange_id"] = exchange_id
    state["symbol"] = symbol
    _persist_user_state(query.from_user.id)

    buttons = [
        [InlineKeyboardButton("💰 قیمت فعلی", callback_data="act:price")],
        [InlineKeyboardButton("📈 رسم نمودار", callback_data="act:chart")],
        [InlineKeyboardButton("📥 دریافت اکسل", callback_data="act:excel")],
        [InlineKeyboardButton("⭐ افزودن به علاقه‌مندی‌ها", callback_data="fav:add")],
        [InlineKeyboardButton("⭐ لیست علاقه‌مندی‌ها", callback_data="panel:open")],
        [InlineKeyboardButton("🔙 بازگشت به فهرست نمادها", callback_data=f"sympage:{exchange_id}:{state.get('sym_page','0')}")],
    ]
    markup = InlineKeyboardMarkup(buttons)
    await query.edit_message_text(
        f"نماد انتخاب‌شده: {symbol}\nچه کاری انجام دهم؟",
        reply_markup=markup,
    )


async def on_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]
    state = _get_user_state(query.from_user.id)
    exchange_id = state.get("exchange_id")
    symbol = state.get("symbol")

    if not exchange_id or not symbol:
        await query.edit_message_text("جلسه شما منقضی شده است. لطفاً از ابتدا شروع کنید /start")
        return

    if action == "price":
        t = await get_ticker_details(exchange_id, symbol)
        price = t.get("last")
        ts = t.get("timestamp")
        if price is None:
            await query.edit_message_text("نتوانستم قیمت را دریافت کنم.")
            return
        dt = datetime.fromtimestamp((ts or 0) / 1000, tz=timezone.utc).astimezone()
        hi = t.get("high")
        lo = t.get("low")
        ch = t.get("percentage")
        qv = t.get("quoteVolume", 0.0)
        lines = [
            f"📌 نماد: <b>{symbol}</b>",
            f"💰 قیمت: <b>{price:,.6f}</b> دلار",
            f"📈 بیشینه 24ساعته: {hi if hi is not None else '—'}",
            f"📉 کمینه 24ساعته: {lo if lo is not None else '—'}",
            f"📊 تغییر: {ch:.2f}%" if ch is not None else None,
            f"💵 حجم 24ساعته: {qv:,.0f} دلار",
            f"🕒 زمان: {dt.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        ]
        msg = "\n".join([x for x in lines if x is not None])
        buttons = [[InlineKeyboardButton("🔙 بازگشت", callback_data=f"sympage:{exchange_id}:{state.get('sym_page','0')}")]]
        await query.edit_message_text(msg, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons))
    elif action == "chart":
        state["awaiting"] = "chart_params_count"
        await query.edit_message_text("📈 چند کندل می‌خواهید؟ (مثلاً 100)")
    elif action == "excel":
        # show timeframe buttons
        state["awaiting"] = "excel_params_tf"
        tfs = ["1m","5m","15m","1h","4h","1d"]
        rows = [[InlineKeyboardButton(tf, callback_data=f"tf:excel:{tf}") for tf in tfs]]
        rows.append([InlineKeyboardButton("❌ انصراف", callback_data=f"sympage:{exchange_id}:{state.get('sym_page','0')}")])
        await query.edit_message_text("📥 برای اکسل یکی از تایم‌فریم‌ها را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(rows))


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    user_id = update.effective_user.id
    state = _get_user_state(user_id)
    exchange_id = state.get("exchange_id")
    symbol = state.get("symbol")

    # Info command in Persian
    if text == "توضیحات" and exchange_id and symbol:
        info = await get_coin_details(exchange_id, symbol)
        await update.message.reply_text(info, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        return

    awaiting = state.get("awaiting")
    if awaiting == "chart_params_count":
        # Expect integer count
        try:
            count = int(text)
            state["chart_count"] = str(max(10, min(count, 2000)))
        except Exception:
            await update.message.reply_text("عدد نامعتبر است. دوباره تعداد کندل‌ها را وارد کنید.")
            return
        state["awaiting"] = "chart_params_tf"
        # show timeframe buttons instead of text
        tfs = ["1m","5m","15m","1h","4h","1d"]
        rows = [[InlineKeyboardButton(tf, callback_data=f"tf:chart:{tf}") for tf in tfs]]
        await update.message.reply_text("📈 تایم‌فریم را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(rows))
        return
    elif awaiting == "chart_params_tf":
        tf = text
        state["chart_tf"] = tf
        count = int(state.get("chart_count", "200"))
        await update.message.reply_text("در حال دریافت داده‌ها و رسم نمودار...")
        try:
            ohlcv = await fetch_ohlcv_data(exchange_id, symbol, tf, limit=count)
            img_path = await render_candlestick_chart_png(symbol, tf, ohlcv)
            with open(img_path, "rb") as f:
                await update.message.reply_photo(photo=InputFile(f), caption=f"نمودار {symbol} در تایم‌فریم {tf}")
        except Exception as e:
            logger.exception("chart error")
            await update.message.reply_text("خطا در رسم نمودار. دوباره تلاش کنید.")
        finally:
            state.pop("awaiting", None)
            state.pop("chart_count", None)
            state.pop("chart_tf", None)
            await _show_symbol_menu(update, exchange_id, symbol)
        return
    elif awaiting == "excel_params_tf":
        state["excel_tf"] = text
        state["awaiting"] = "excel_params_from"
        quick = [
            InlineKeyboardButton("⏱️ 7 روز اخیر", callback_data="rng:excel:7d"),
            InlineKeyboardButton("📅 30 روز اخیر", callback_data="rng:excel:30d"),
        ]
        await update.message.reply_text(
            "از چه تاریخی؟ (فرمت: YYYY-MM-DD)\nمثال: 2024-01-01",
            reply_markup=InlineKeyboardMarkup([quick])
        )
        return
    elif awaiting == "excel_params_from":
        state["excel_from"] = text
        state["awaiting"] = "excel_params_to"
        await update.message.reply_text("تا چه تاریخی؟ (YYYY-MM-DD)")
        return
    elif awaiting == "excel_params_to":
        tf = state.get("excel_tf")
        from_s = state.get("excel_from")
        to_s = text
        try:
            since_ms = int(datetime.fromisoformat(from_s).replace(tzinfo=timezone.utc).timestamp() * 1000)
            to_ms = int(datetime.fromisoformat(to_s).replace(tzinfo=timezone.utc).timestamp() * 1000)
        except Exception:
            await update.message.reply_text("فرمت تاریخ نادرست است. از قالب YYYY-MM-DD استفاده کنید.")
            return

        await update.message.reply_text("در حال تهیه فایل اکسل...")
        try:
            # estimate limit based on timeframe and date range
            from exchange_utils import timeframe_to_ms
            tf_ms = timeframe_to_ms(tf)
            expected = min(10000, max(500, (to_ms - since_ms) // tf_ms + 10))
            ohlcv = await fetch_ohlcv_data(exchange_id, symbol, tf, since=since_ms, until=to_ms, limit=expected)
            if not ohlcv:
                await update.message.reply_text("❌ هیچ داده‌ای برای بازه زمانی انتخاب شده یافت نشد. لطفاً بازه دیگری را امتحان کنید.")
            else:
                path = await export_ohlcv_to_excel(symbol, tf, ohlcv)
                with open(path, "rb") as f:
                    await update.message.reply_document(document=InputFile(f), filename=os.path.basename(path), caption=f"📥 فایل اکسل OHLCV ({len(ohlcv)} کندل)")
        except ValueError as e:
            if "No OHLCV" in str(e):
                await update.message.reply_text("❌ هیچ داده‌ای برای بازه زمانی انتخاب شده یافت نشد.")
            else:
                raise
        except Exception:
            logger.exception("excel error")
            await update.message.reply_text("❌ خطا در تولید فایل اکسل. لطفاً دوباره تلاش کنید.")
        finally:
            for k in ["awaiting", "excel_tf", "excel_from", "excel_to"]:
                state.pop(k, None)
            await _show_symbol_menu(update, exchange_id, symbol)
        return

    # Help text
    await update.message.reply_text(
        "دستورها: /start برای شروع.\nپس از انتخاب نماد، می‌توانید ‘توضیحات’ تایپ کنید.")


async def button_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    data = query.data
    if data.startswith("menu:home"):
        await show_main_menu(update, context)
    elif data.startswith("menu:exchanges"):
        await send_exchange_list(update, context)
    elif data.startswith("menu:symbols"):
        # show favorites or ask to choose exchange first
        state = _get_user_state(update.callback_query.from_user.id)
        if state.get("exchange_id") and state.get("symbols"):
            await on_symbol_page(update, context)
        else:
            await update.callback_query.answer("ابتدا یک صرافی انتخاب کنید.", show_alert=True)
    elif data.startswith("ex:"):
        await on_exchange_selected(update, context)
    elif data.startswith("sym:"):
        await on_symbol_selected(update, context)
    elif data.startswith("act:"):
        await on_action(update, context)
    elif data.startswith("expage:"):
        await on_exchange_page(update, context)
    elif data.startswith("sympage:"):
        await on_symbol_page(update, context)
    elif data.startswith("panel:" ):
        await on_panel(update, context)
    elif data.startswith("fav:remove:"):
        await on_favorite_remove(update, context)
    elif data.startswith("fav:" ):
        await on_favorite(update, context)
    elif data.startswith("tf:chart:"):
        await on_chart_tf_selected(update, context)
    elif data.startswith("tf:excel:"):
        await on_excel_tf_selected(update, context)
    elif data.startswith("rng:excel:"):
        await on_excel_range_quick(update, context)
    elif data == "noop":
        await query.answer(" ", show_alert=False)


async def on_exchange_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    page = int(query.data.split(":", 1)[1])
    state = _get_user_state(query.from_user.id)
    exchanges = state.get("exchanges", [])
    state["ex_page"] = str(page)
    _persist_user_state(query.from_user.id)
    markup = _build_exchanges_keyboard(exchanges, page)
    await query.edit_message_text("ستون‌ها: صرافی | حجم معاملات | واحد\nیک صرافی را انتخاب کنید:", reply_markup=markup)


async def on_symbol_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, exchange_id, page_s = query.data.split(":", 2)
    page = int(page_s)
    state = _get_user_state(query.from_user.id)
    symbols = state.get("symbols", [])
    state["sym_page"] = str(page)
    _persist_user_state(query.from_user.id)
    markup = _build_symbols_keyboard(symbols, exchange_id, page)
    await query.edit_message_text(
        f"🧾 صرافی: {parse_exchange_id(exchange_id)}\nستون‌ها: 🪙 رمز ارز | 🧾 نماد | 💵 قیمت | $\nیکی را انتخاب کنید:",
        reply_markup=markup,
    )


async def on_chart_tf_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    tf = query.data.split(":", 2)[2]
    state = _get_user_state(query.from_user.id)
    state["chart_tf"] = tf
    exchange_id = state.get("exchange_id")
    symbol = state.get("symbol")
    count = int(state.get("chart_count", "200"))
    await query.edit_message_text("در حال دریافت داده‌ها و رسم نمودار...")
    try:
        ohlcv = await fetch_ohlcv_data(exchange_id, symbol, tf, limit=count)
        img_path = await render_candlestick_chart_png(symbol, tf, ohlcv)
        with open(img_path, "rb") as f:
            await query.message.reply_photo(photo=InputFile(f), caption=f"📈 نمودار {symbol} در تایم‌فریم {tf}")
    except Exception:
        logger.exception("chart error")
        await query.message.reply_text("خطا در رسم نمودار. دوباره تلاش کنید.")
    finally:
        state.pop("awaiting", None)
        state.pop("chart_count", None)
        state.pop("chart_tf", None)
        await _show_symbol_menu(update, exchange_id, symbol)


async def on_excel_tf_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    tf = query.data.split(":", 2)[2]
    state = _get_user_state(query.from_user.id)
    state["excel_tf"] = tf
    # ask for dates with quick range
    quick = [
        InlineKeyboardButton("⏱️ 7 روز اخیر", callback_data="rng:excel:7d"),
        InlineKeyboardButton("📅 30 روز اخیر", callback_data="rng:excel:30d"),
    ]
    await query.edit_message_text(
        "از چه تاریخی؟ (فرمت: YYYY-MM-DD)\nمثال: 2024-01-01",
        reply_markup=InlineKeyboardMarkup([quick])
    )
    state["awaiting"] = "excel_params_from"


async def on_excel_range_quick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    rng = query.data.split(":", 2)[2]
    state = _get_user_state(query.from_user.id)
    exchange_id = state.get("exchange_id")
    symbol = state.get("symbol")
    tf = state.get("excel_tf", "1h")
    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    if rng == "7d":
        since_ms = now_ms - 7 * 24 * 60 * 60 * 1000
    else:
        since_ms = now_ms - 30 * 24 * 60 * 60 * 1000
    await query.edit_message_text("در حال تهیه فایل اکسل...")
    try:
        # estimate candle count and cap
        from exchange_utils import timeframe_to_ms
        tf_ms = timeframe_to_ms(tf)
        expected = min(10000, max(500, (now_ms - since_ms) // tf_ms + 5))
        ohlcv = await fetch_ohlcv_data(exchange_id, symbol, tf, since=since_ms, until=now_ms, limit=expected)
        if not ohlcv:
            await query.message.reply_text("❌ هیچ داده‌ای برای بازه زمانی انتخاب شده یافت نشد. لطفاً بازه دیگری را امتحان کنید.")
        else:
            path = await export_ohlcv_to_excel(symbol, tf, ohlcv)
            with open(path, "rb") as f:
                await query.message.reply_document(document=InputFile(f), filename=os.path.basename(path), caption=f"📥 فایل اکسل OHLCV ({len(ohlcv)} کندل)")
    except ValueError as e:
        if "No OHLCV" in str(e):
            await query.message.reply_text("❌ هیچ داده‌ای برای بازه زمانی انتخاب شده یافت نشد.")
        else:
            raise
    except Exception:
        logger.exception("excel quick range error")
        await query.message.reply_text("❌ خطا در تولید فایل اکسل با بازه سریع. لطفاً دوباره تلاش کنید.")
    finally:
        for k in ["awaiting", "excel_tf", "excel_from", "excel_to"]:
            state.pop(k, None)
        await _show_symbol_menu(update, exchange_id, symbol)


async def _show_symbol_menu(update: Update, exchange_id: str, symbol: str) -> None:
    user_id = update.effective_user.id if update.effective_user else 0
    state = _get_user_state(user_id)
    state["exchange_id"] = exchange_id
    state["symbol"] = symbol
    _persist_user_state(user_id)
    buttons = [
        [InlineKeyboardButton("💰 قیمت فعلی", callback_data="act:price")],
        [InlineKeyboardButton("📈 رسم نمودار", callback_data="act:chart")],
        [InlineKeyboardButton("📥 دریافت اکسل", callback_data="act:excel")],
        [InlineKeyboardButton("⭐ افزودن به علاقه‌مندی‌ها", callback_data="fav:add")],
        [InlineKeyboardButton("⭐ پنل من", callback_data="panel:open")],
        [InlineKeyboardButton("🔙 بازگشت به فهرست نمادها", callback_data=f"sympage:{exchange_id}:{state.get('sym_page','0')}")],
    ]
    markup = InlineKeyboardMarkup(buttons)
    text = f"📌 نماد انتخاب‌شده: {symbol}\nچه کاری انجام دهم؟"
    # Support both callback_query and message
    if update.callback_query:
        await update.callback_query.message.reply_text(text, reply_markup=markup)
    elif update.message:
        await update.message.reply_text(text, reply_markup=markup)


async def on_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    favs = _load_favorites().get(user_id, [])
    rows = []
    if favs:
        rows.append([
            InlineKeyboardButton("⭐ علاقه‌مندی‌ها", callback_data="noop"),
            InlineKeyboardButton("🗑️ حذف", callback_data="noop")
        ])
        for item in favs[:10]:
            ex = item.get("exchange")
            sym = item.get("symbol")
            rows.append([
                InlineKeyboardButton(f"🪙 {sym} @ {ex}", callback_data=f"sym:{ex}:{sym}"),
                InlineKeyboardButton("❌", callback_data=f"fav:remove:{ex}:{sym}")
            ])
    else:
        rows.append([InlineKeyboardButton("(هنوز موردی اضافه نشده)", callback_data="noop")])
    rows.append([InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="menu:home")])
    await query.edit_message_text("⭐ پنل کاربری شما:", reply_markup=InlineKeyboardMarkup(rows))


async def on_favorite(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    state = _get_user_state(query.from_user.id)
    ex = state.get("exchange_id")
    sym = state.get("symbol")
    data = _load_favorites()
    lst = data.get(user_id, [])
    # toggle add/remove
    if any(x.get("exchange") == ex and x.get("symbol") == sym for x in lst):
        lst = [x for x in lst if not (x.get("exchange") == ex and x.get("symbol") == sym)]
        data[user_id] = lst
        _save_favorites(data)
        await query.edit_message_text("از علاقه‌مندی‌ها حذف شد.")
    else:
        lst.append({"exchange": ex, "symbol": sym})
        data[user_id] = lst
        _save_favorites(data)
        await query.edit_message_text("به علاقه‌مندی‌ها اضافه شد.")
    # show menu again
    await _show_symbol_menu(update, ex, sym)


async def on_favorite_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove favorite from panel"""
    query = update.callback_query
    await query.answer()
    _, _, exchange_id, symbol = query.data.split(":", 3)
    user_id = str(query.from_user.id)
    data = _load_favorites()
    lst = data.get(user_id, [])
    # Remove the item
    lst = [x for x in lst if not (x.get("exchange") == exchange_id and x.get("symbol") == symbol)]
    data[user_id] = lst
    _save_favorites(data)
    await query.answer("✅ از علاقه‌مندی‌ها حذف شد.", show_alert=True)
    # Refresh panel
    await on_panel(update, context)


def build_application() -> Application:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set. Use .env or environment variable.")
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    return app


def main() -> None:
    app = build_application()
    logger.info("Bot is starting...")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()


