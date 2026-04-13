import os
import re
import json
import base64
import logging
import asyncio
from datetime import datetime, time
from zoneinfo import ZoneInfo

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN  = os.environ.get("BOT_TOKEN", "")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
CHAT_ID    = os.environ.get("CHAT_ID", "1603569746")
PORT       = int(os.environ.get("PORT", 8080))
PH_TZ      = ZoneInfo("Asia/Manila")
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_KEY}"

EXPENSES = []
LOANS    = []  # {name, amount, due_day}
COUNTER  = [0]
PENDING  = {}

# ── FASTAPI ────────────────────────────────────────────────────
api = FastAPI()
api.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@api.get("/")
def root():
    return {"status": "Wealth+ Bot is running!", "expenses": len(EXPENSES)}

@api.get("/expenses")
def get_expenses():
    return {"expenses": EXPENSES, "total": sum(e["amount"] for e in EXPENSES)}

@api.get("/today")
def get_today():
    today = datetime.now(PH_TZ).strftime("%Y-%m-%d")
    today_exp = [e for e in EXPENSES if e["date"].startswith(today)]
    return {"expenses": today_exp, "total": sum(e["amount"] for e in today_exp)}

@api.get("/loans")
def get_loans():
    return {"loans": LOANS}

@api.get("/all")
def get_all():
    today = datetime.now(PH_TZ).strftime("%Y-%m-%d")
    today_exp = [e for e in EXPENSES if e["date"].startswith(today)]
    by_cat = {}
    for e in EXPENSES:
        by_cat[e["category"]] = by_cat.get(e["category"], 0) + e["amount"]
    return {
        "expenses": EXPENSES,
        "loans": LOANS,
        "total_spent": sum(e["amount"] for e in EXPENSES),
        "today_spent": sum(e["amount"] for e in today_exp),
        "by_category": by_cat,
    }

# ── HELPERS ────────────────────────────────────────────────────
CAT_KEYWORDS = {
    "Food":          ["jollibee","mcdo","mcdonald","kfc","chowking","mang inasal","ministop","711","7-eleven","grocery","groceries","palengke","market","food","lunch","dinner","breakfast","merienda","snack","restaurant","cafe","pizza","burger"],
    "Transport":     ["grab","angkas","jeep","jeepney","tricycle","bus","lrt","mrt","taxi","uber","toll","gas","petrol","gasoline","diesel","fare","commute","transport"],
    "Utilities":     ["meralco","electricity","water","maynilad","nawasa","internet","wifi","globe","smart","dito","pldt","load","prepaid","bill","bills"],
    "Shopping":      ["sm","robinsons","ayala","lazada","shopee","shop","mall","clothes","shoes","clothing","divisoria","ukay","aquaflask","greenhills"],
    "Healthcare":    ["mercury","watsons","rose pharmacy","hospital","clinic","doctor","medicine","botika","pharmacy","checkup","dental","optical"],
    "Entertainment": ["netflix","spotify","youtube","cinema","movie","games","concert","event","ticket"],
}

def guess_category(text):
    t = text.lower()
    for cat, keywords in CAT_KEYWORDS.items():
        if any(kw in t for kw in keywords):
            return cat
    return "Shopping"

def parse_locally(text):
    text = text.strip()
    cleaned = re.sub(r'^(spent|spend|bayad|paid|pay|bought|buy)\s+', '', text, flags=re.IGNORECASE).strip()
    amount_match = re.search(r'[₱P]?\s*(\d+(?:\.\d{1,2})?)', cleaned)
    if not amount_match:
        return None
    amount = float(amount_match.group(1))
    if amount <= 0:
        return None
    name_part = cleaned.replace(amount_match.group(0), "").strip(" -,.")
    name_part = re.sub(r'\s+', ' ', name_part).strip()
    if not name_part:
        name_part = "Expense"
    return {"name": name_part.title(), "amount": amount, "category": guess_category(name_part), "note": ""}

def add_expense(name, amount, category="Other", note="", source="manual"):
    COUNTER[0] += 1
    entry = {
        "id": COUNTER[0],
        "name": str(name),
        "amount": float(amount),
        "category": str(category),
        "note": str(note),
        "date": datetime.now(PH_TZ).strftime("%Y-%m-%d %H:%M"),
        "source": source,
    }
    EXPENSES.append(entry)
    return entry

# ── GEMINI ─────────────────────────────────────────────────────
async def ask_gemini_image(image_bytes):
    if not GEMINI_KEY:
        return None
    try:
        b64 = base64.standard_b64encode(image_bytes).decode()
        prompt = """This is a receipt or payment slip from the Philippines.
Find: 1) Merchant/store name  2) Total amount paid in Philippine Peso  3) Category
Return ONLY this JSON with no extra text:
{"name":"MERCHANT","amount":210.00,"category":"Shopping","note":"description"}
Categories: Food, Transport, Utilities, Shopping, Entertainment, Healthcare, Other
For card slips look for SALE AMOUNT. Merchant name is usually at the top."""
        contents = [{"parts": [
            {"inline_data": {"mime_type": "image/jpeg", "data": b64}},
            {"text": prompt}
        ]}]
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.post(GEMINI_URL, json={"contents": contents})
            data = res.json()
            if "candidates" not in data:
                return None
            text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
            json_match = re.search(r'\{[^{}]+\}', text, re.DOTALL)
            if json_match:
                return json.loads(json_match.group(0))
    except Exception as e:
        logger.error(f"Gemini error: {e}")
    return None

# ── SCHEDULED NOTIFICATIONS ────────────────────────────────────
async def send_daily_summary(bot: Bot):
    """Send daily spending summary every 9PM Manila time."""
    today = datetime.now(PH_TZ).strftime("%Y-%m-%d")
    today_exp = [e for e in EXPENSES if e["date"].startswith(today)]

    if not today_exp:
        msg = (
            "🌙 Good evening Jon!\n\n"
            "No expenses recorded today.\n"
            "Don't forget to log your spending! 📝"
        )
    else:
        total = sum(e["amount"] for e in today_exp)
        by_cat = {}
        for e in today_exp:
            by_cat[e["category"]] = by_cat.get(e["category"], 0) + e["amount"]
        lines = [f"• {cat}: ₱{amt:,.2f}" for cat, amt in sorted(by_cat.items(), key=lambda x: -x[1])]
        msg = (
            f"🌙 Daily Summary — {today}\n\n"
            + "\n".join(lines) +
            f"\n\n💸 Total today: ₱{total:,.2f}\n\n"
            "Keep tracking! 💪"
        )
    await bot.send_message(chat_id=CHAT_ID, text=msg)
    logger.info("Daily summary sent!")

async def send_weekly_report(bot: Bot):
    """Send weekly budget report every Sunday 8PM Manila time."""
    if not EXPENSES:
        await bot.send_message(chat_id=CHAT_ID, text="📊 Weekly Report\n\nNo expenses recorded this week yet!")
        return

    total = sum(e["amount"] for e in EXPENSES)
    by_cat = {}
    for e in EXPENSES:
        by_cat[e["category"]] = by_cat.get(e["category"], 0) + e["amount"]
    lines = [f"• {cat}: ₱{amt:,.2f}" for cat, amt in sorted(by_cat.items(), key=lambda x: -x[1])]
    top_cat = max(by_cat.items(), key=lambda x: x[1])[0] if by_cat else "N/A"

    msg = (
        f"📊 Weekly Report\n\n"
        f"Total Expenses: ₱{total:,.2f}\n"
        f"Transactions: {len(EXPENSES)}\n"
        f"Top Category: {top_cat}\n\n"
        "Breakdown:\n" + "\n".join(lines) +
        "\n\nHave a great week ahead! 🇵🇭💪"
    )
    await bot.send_message(chat_id=CHAT_ID, text=msg)
    logger.info("Weekly report sent!")

async def check_due_dates(bot: Bot):
    """Check loan due dates every morning 8AM Manila time."""
    if not LOANS:
        return
    today = datetime.now(PH_TZ)
    tomorrow = today.day + 1
    reminders = []
    for loan in LOANS:
        due = loan.get("due_day", 0)
        if due == today.day:
            reminders.append(f"🔴 DUE TODAY: {loan['name']} — ₱{loan['amount']:,.2f}")
        elif due == tomorrow:
            reminders.append(f"🟡 DUE TOMORROW: {loan['name']} — ₱{loan['amount']:,.2f}")
        elif due == today.day + 3:
            reminders.append(f"🟠 DUE IN 3 DAYS: {loan['name']} — ₱{loan['amount']:,.2f}")

    if reminders:
        msg = "💳 Loan Payment Reminder!\n\n" + "\n".join(reminders) + "\n\nDon't miss your payments! 💪"
        await bot.send_message(chat_id=CHAT_ID, text=msg)
        logger.info(f"Due date reminders sent: {len(reminders)}")

async def scheduler(bot: Bot):
    """Run scheduled tasks based on Manila time."""
    logger.info("Scheduler started!")
    while True:
        now = datetime.now(PH_TZ)
        hour, minute, weekday = now.hour, now.minute, now.weekday()

        # Daily summary: 9:00 PM every day
        if hour == 21 and minute == 0:
            await send_daily_summary(bot)

        # Weekly report: 8:00 PM every Sunday (weekday 6)
        if hour == 20 and minute == 0 and weekday == 6:
            await send_weekly_report(bot)

        # Due date check: 8:00 AM every day
        if hour == 8 and minute == 0:
            await check_due_dates(bot)

        # Sleep until next minute
        await asyncio.sleep(60)

# ── BOT COMMANDS ───────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi Jon! Wealth+ Bot here! 🇵🇭\n\n"
        "📸 Send a receipt photo and I'll record it!\n\n"
        "Or type an expense:\n"
        "• spent 250 Jollibee\n"
        "• 1500 Meralco\n"
        "• 210 Aquaflask\n\n"
        "Auto Notifications:\n"
        "🌙 9PM — Daily spending summary\n"
        "📅 8AM — Loan due date reminders\n"
        "📊 Sunday 8PM — Weekly report\n\n"
        "Commands:\n"
        "/expenses - recent list\n"
        "/total - by category\n"
        "/today - today only\n"
        "/summary - get summary now\n"
        "/addloan - add a loan reminder\n"
        "/loans - see your loans\n"
        "/delete 5 - remove expense #5\n"
        "/link - get app sync URL\n\n"
        "Let's track those pesos! 💪"
    )

async def link_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
    if domain:
        await update.message.reply_text(
            f"Your Bot Sync URL:\n\nhttps://{domain}/expenses\n\n"
            "Paste this in your Wealth+ app under the 🤖 Bot tab!"
        )
    else:
        await update.message.reply_text("Domain not set. Add RAILWAY_PUBLIC_DOMAIN in Railway Variables.")

async def summary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot = context.bot
    await send_daily_summary(bot)

async def addloan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) < 3:
        await update.message.reply_text(
            "Usage: /addloan [name] [amount] [due_day]\n\n"
            "Example:\n"
            "/addloan BDO-Credit 5000 15\n"
            "/addloan Meralco 2500 20\n\n"
            "due_day = day of month payment is due"
        )
        return
    try:
        name = context.args[0]
        amount = float(context.args[1])
        due_day = int(context.args[2])
        LOANS.append({"name": name, "amount": amount, "due_day": due_day})
        await update.message.reply_text(
            f"Loan reminder added! ✅\n\n"
            f"📋 {name}\n"
            f"💸 ₱{amount:,.2f}\n"
            f"📅 Due every day {due_day} of the month\n\n"
            f"You'll get reminders 3 days before, 1 day before, and on the due date!"
        )
    except:
        await update.message.reply_text("Error. Example: /addloan BDO-Credit 5000 15")

async def loans_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not LOANS:
        await update.message.reply_text("No loan reminders yet!\nAdd one: /addloan BDO-Credit 5000 15")
        return
    lines = [f"• {l['name']} — ₱{l['amount']:,.2f} (due day {l['due_day']})" for l in LOANS]
    await update.message.reply_text("Your Loan Reminders:\n\n" + "\n".join(lines))

async def expenses_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not EXPENSES:
        await update.message.reply_text("No expenses yet!\nType: spent 250 Jollibee")
        return
    recent = EXPENSES[-15:][::-1]
    lines = [f"#{e['id']} {e['name']} - ₱{e['amount']:,.2f} ({e['category']}) {e['date'][:10]}" for e in recent]
    total = sum(e["amount"] for e in EXPENSES)
    await update.message.reply_text("Recent Expenses:\n\n" + "\n".join(lines) + f"\n\nTotal: ₱{total:,.2f}")

async def total_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not EXPENSES:
        await update.message.reply_text("No expenses yet!")
        return
    by_cat = {}
    for e in EXPENSES:
        cat = e.get("category","Other")
        by_cat[cat] = by_cat.get(cat, 0) + e["amount"]
    total = sum(e["amount"] for e in EXPENSES)
    lines = [f"• {cat}: ₱{amt:,.2f}" for cat, amt in sorted(by_cat.items(), key=lambda x: -x[1])]
    await update.message.reply_text("Spending by Category:\n\n" + "\n".join(lines) + f"\n\nTotal: ₱{total:,.2f}")

async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now(PH_TZ).strftime("%Y-%m-%d")
    today_exp = [e for e in EXPENSES if e["date"].startswith(today)]
    if not today_exp:
        await update.message.reply_text("No expenses today yet!")
        return
    lines = [f"• {e['name']} - ₱{e['amount']:,.2f} ({e['category']})" for e in today_exp]
    total = sum(e["amount"] for e in today_exp)
    await update.message.reply_text("Today's Expenses:\n\n" + "\n".join(lines) + f"\n\nToday's Total: ₱{total:,.2f}")

async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /delete 5")
        return
    try:
        exp_id = int(context.args[0])
        to_remove = next((e for e in EXPENSES if e["id"] == exp_id), None)
        if to_remove:
            EXPENSES.remove(to_remove)
            await update.message.reply_text(f"Deleted #{exp_id} {to_remove['name']}!")
        else:
            await update.message.reply_text(f"#{exp_id} not found.")
    except:
        await update.message.reply_text("Usage: /delete 5")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.reply_text("📸 Reading your receipt...")
        file = await context.bot.get_file(update.message.photo[-1].file_id)
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(file.file_path)
        result = await ask_gemini_image(resp.content)
        user_id = update.effective_user.id
        if result and result.get("amount", 0) > 0:
            PENDING[user_id] = result
            keyboard = [[
                InlineKeyboardButton("✅ Yes, save it!", callback_data="confirm_yes"),
                InlineKeyboardButton("❌ Cancel", callback_data="confirm_no"),
            ]]
            await update.message.reply_text(
                f"Found this from your receipt:\n\n"
                f"🏪 Store: {result.get('name','?')}\n"
                f"💸 Amount: ₱{float(result.get('amount',0)):,.2f}\n"
                f"📂 Category: {result.get('category','?')}\n\n"
                f"Is this correct?",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await update.message.reply_text(
                "Couldn't read the receipt clearly.\n\n"
                "Please type it manually:\n"
                "• 210 Aquaflask\n"
                "• 350 SM Grocery\n"
                "• 1500 Meralco"
            )
    except Exception as e:
        logger.error(f"Photo error: {e}")
        await update.message.reply_text("Error reading photo. Please type it manually!")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if query.data == "confirm_yes":
        result = PENDING.pop(user_id, None)
        if result:
            entry = add_expense(result.get("name","Unknown"), result.get("amount",0), result.get("category","Shopping"), result.get("note",""), source="receipt")
            await query.edit_message_text(
                f"Saved! ✅\n\n"
                f"🏪 {entry['name']}\n"
                f"💸 ₱{entry['amount']:,.2f}\n"
                f"📂 {entry['category']}\n"
                f"📅 {entry['date']}\n\n"
                f"#{entry['id']} recorded!"
            )
    elif query.data == "confirm_no":
        PENDING.pop(user_id, None)
        await query.edit_message_text("Cancelled. Type manually:\nExample: 210 Aquaflask")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        text = update.message.text.strip()
        if text.startswith("/"):
            return
        result = parse_locally(text)
        if not result:
            await update.message.reply_text(
                "I didn't understand that. Try:\n\n"
                "• spent 250 Jollibee\n"
                "• 1500 Meralco\n"
                "• 210 Aquaflask\n"
                "• groceries 2300\n\n"
                "Or send a receipt photo! 📸"
            )
            return
        entry = add_expense(result["name"], result["amount"], result["category"], result.get("note",""), source="text")
        await update.message.reply_text(
            f"Saved! ✅\n\n"
            f"🏪 Store: {entry['name']}\n"
            f"💸 Amount: ₱{entry['amount']:,.2f}\n"
            f"📂 Category: {entry['category']}\n"
            f"📅 Date: {entry['date']}\n\n"
            f"#{entry['id']} recorded!"
        )
    except Exception as e:
        logger.error(f"Text error: {e}")
        await update.message.reply_text("Error. Try again!")

# ── MAIN ───────────────────────────────────────────────────────
async def run_api():
    config = uvicorn.Config(api, host="0.0.0.0", port=PORT, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()

async def run_bot():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",    start))
    app.add_handler(CommandHandler("help",     start))
    app.add_handler(CommandHandler("link",     link_cmd))
    app.add_handler(CommandHandler("summary",  summary_cmd))
    app.add_handler(CommandHandler("addloan",  addloan_cmd))
    app.add_handler(CommandHandler("loans",    loans_cmd))
    app.add_handler(CommandHandler("expenses", expenses_cmd))
    app.add_handler(CommandHandler("total",    total_cmd))
    app.add_handler(CommandHandler("today",    today_cmd))
    app.add_handler(CommandHandler("delete",   delete_cmd))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    logger.info("Bot polling started!")
    # Start scheduler with the bot instance
    asyncio.create_task(scheduler(app.bot))
    await asyncio.Event().wait()

async def main():
    logger.info(f"Starting Wealth+ Bot + Scheduler on port {PORT}...")
    await asyncio.gather(run_bot(), run_api())

if __name__ == "__main__":
    asyncio.run(main())
