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


def _get_user_state(user_id: int) -> Dict[str, str]:
    if user_id not in USER_STATE:
        USER_STATE[user_id] = {}
    return USER_STATE[user_id]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await update.message.reply_text(
        f"سلام {user.first_name or ''}!\n"
        "به ربات کریپتو خوش آمدید. صرافی مورد نظر را انتخاب کنید تا شروع کنیم.")

    await send_exchange_list(update, context)


async def send_exchange_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    exchanges = await list_supported_exchanges_sorted_by_volume(max_exchanges=25, timeout_s=20)
    if not exchanges:
        text = "نتوانستم فهرست صرافی‌ها را دریافت کنم. لطفاً بعداً تلاش کنید."
        if update.message:
            await update.message.reply_text(text)
        elif update.callback_query:
            await update.callback_query.edit_message_text(text)
        return

    buttons = []
    for ex in exchanges:
        fa_name = ex.get("fa_name", ex["name"])  # Persian name/transliteration if available
        label = f"{fa_name} ({ex['name']}) — حجم معاملات: {ex['volume_human']} USD"
        buttons.append([InlineKeyboardButton(label, callback_data=f"ex:{ex['id']}")])

    markup = InlineKeyboardMarkup(buttons)
    text = "یکی از صرافی‌ها را انتخاب کنید:" 
    if update.message:
        await update.message.reply_text(text, reply_markup=markup)
    elif update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup)


async def on_exchange_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, exchange_id = query.data.split(":", 1)

    await query.edit_message_text("در حال دریافت لیست نمادها و قیمت‌ها...")
    symbols = await list_symbols_with_prices(exchange_id, max_symbols=60)

    if not symbols:
        await query.edit_message_text("نتوانستم نمادها را دریافت کنم. لطفاً صرافی دیگری را انتخاب کنید.")
        return

    # Build buttons with 1 per row for readability
    rows = []
    for s in symbols:
        fa_name = s.get("fa_name", s["base"])
        label = f"{fa_name} ({s['symbol']}) — {s['price_human']} دلار"
        rows.append([InlineKeyboardButton(label, callback_data=f"sym:{exchange_id}:{s['symbol']}")])

    markup = InlineKeyboardMarkup(rows)
    await query.edit_message_text(
        f"صرافی انتخاب‌شده: {parse_exchange_id(exchange_id)}\nلطفاً نماد/جفت‌ارز را انتخاب کنید:",
        reply_markup=markup,
    )


async def on_symbol_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, exchange_id, symbol = query.data.split(":", 2)

    state = _get_user_state(query.from_user.id)
    state["exchange_id"] = exchange_id
    state["symbol"] = symbol

    buttons = [
        [InlineKeyboardButton("قیمت فعلی", callback_data="act:price")],
        [InlineKeyboardButton("رسم نمودار", callback_data="act:chart")],
        [InlineKeyboardButton("دریافت اکسل", callback_data="act:excel")],
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
        price, ts = await get_latest_price(exchange_id, symbol)
        if price is None:
            await query.edit_message_text("نتوانستم قیمت را دریافت کنم.")
            return
        dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).astimezone()
        await query.edit_message_text(
            f"قیمت فعلی {symbol}: {price:,.6f} دلار\nزمان: {dt.strftime('%Y-%m-%d %H:%M:%S %Z')}"
        )
    elif action == "chart":
        state["awaiting"] = "chart_params_count"
        await query.edit_message_text("چند کندل می‌خواهید؟ (مثلاً 100)")
    elif action == "excel":
        state["awaiting"] = "excel_params_tf"
        await query.edit_message_text("برای اکسل چه تایم‌فریمی؟ (مثلاً 1m,5m,1h,1d)")


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
        await update.message.reply_text("چه تایم‌فریمی؟ (1m,5m,15m,1h,4h,1d)")
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
        return
    elif awaiting == "excel_params_tf":
        state["excel_tf"] = text
        state["awaiting"] = "excel_params_from"
        await update.message.reply_text("از چه تاریخی؟ (YYYY-MM-DD)")
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
            ohlcv = await fetch_ohlcv_data(exchange_id, symbol, tf, since=since_ms, until=to_ms, limit=2000)
            path = await export_ohlcv_to_excel(symbol, tf, ohlcv)
            with open(path, "rb") as f:
                await update.message.reply_document(document=InputFile(f), filename=os.path.basename(path), caption="فایل اکسل OHLCV")
        except Exception:
            logger.exception("excel error")
            await update.message.reply_text("خطا در تولید فایل اکسل.")
        finally:
            for k in ["awaiting", "excel_tf", "excel_from", "excel_to"]:
                state.pop(k, None)
        return

    # Help text
    await update.message.reply_text(
        "دستورها: /start برای شروع.\nپس از انتخاب نماد، می‌توانید ‘توضیحات’ تایپ کنید.")


async def button_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    data = query.data
    if data.startswith("ex:"):
        await on_exchange_selected(update, context)
    elif data.startswith("sym:"):
        await on_symbol_selected(update, context)
    elif data.startswith("act:"):
        await on_action(update, context)


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


