import os
import re
import json
import base64
import logging
from datetime import datetime
import httpx
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN  = os.environ.get("BOT_TOKEN", "8683088099:AAHtXcQy6ui6FeXqWARG2lcjYuYxfIHbMVA")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "AIzaSyAXRORlJ4hJvZRi0XZZrqBUiaAQe8X7cV8")
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_KEY}"

EXPENSES = []
COUNTER  = [0]

# ── CATEGORY KEYWORDS ──────────────────────────────────────────
CAT_KEYWORDS = {
    "Food":          ["jollibee","mcdo","mcdonald","kfc","chowking","mang inasal","ministop","711","7-eleven","grocery","groceries","palengke","market","food","lunch","dinner","breakfast","merienda","kain","ulam","rice","load","snack","restaurant","cafe","pizza","burger","siomai","lugaw"],
    "Transport":     ["grab","angkas","jeep","jeepney","tricycle","bus","lrt","mrt","taxi","uber","pedicab","toll","gas","petrol","gasoline","diesel","fare","commute","transport","pasahe"],
    "Utilities":     ["meralco","electricity","water","maynilad","nawasa","internet","wifi","globe","smart","dito","pldt","load","prepaid","bill","bills","bayad","payment"],
    "Shopping":      ["sm","robinsons","ayala","lazada","shopee","shop","mall","clothes","shoes","clothing","damit","sapatos","divisoria","ukay"],
    "Healthcare":    ["mercury","watsons","rose pharmacy","hospital","clinic","doctor","medicine","gamot","botika","pharmacy","checkup","dental","optical"],
    "Entertainment": ["netflix","spotify","youtube","cinema","movie","games","concert","event","ticket"],
}

def guess_category(name, note=""):
    text = (name + " " + note).lower()
    for cat, keywords in CAT_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            return cat
    return "Other"

# ── LOCAL TEXT PARSER ──────────────────────────────────────────
def parse_locally(text):
    """
    Accepts formats like:
    - spent 250 Jollibee
    - 250 Jollibee
    - Jollibee 250
    - bayad 1500 Meralco
    - groceries 2300 SM
    - load 100
    """
    text = text.strip()
    # Remove common Filipino trigger words
    cleaned = re.sub(r'^(spent|spend|bayad|nagbayad|nagbili|bili|gastos|nagastos|paid|pay)\s+', '', text, flags=re.IGNORECASE).strip()

    # Find amount (number with optional decimal)
    amount_match = re.search(r'[\₱]?\s*(\d+(?:\.\d{1,2})?)', cleaned)
    if not amount_match:
        return None

    amount = float(amount_match.group(1))
    if amount <= 0:
        return None

    # Remove the amount from text to get the name
    name_part = cleaned.replace(amount_match.group(0), "").strip()
    name_part = re.sub(r'\s+', ' ', name_part).strip(" -,.")

    if not name_part:
        name_part = "Expense"

    category = guess_category(name_part)

    return {
        "name": name_part.title(),
        "amount": amount,
        "category": category,
        "note": ""
    }

# ── GEMINI AI (for receipts) ───────────────────────────────────
async def ask_gemini(contents):
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            res = await client.post(GEMINI_URL, json={"contents": contents})
            data = res.json()
            if "candidates" not in data:
                logger.error(f"Gemini bad response: {data}")
                return None
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        logger.error(f"Gemini error: {e}")
        return None

async def parse_receipt_image(image_bytes):
    try:
        b64 = base64.standard_b64encode(image_bytes).decode()
        prompt = (
            "Read this receipt. Currency: Philippine Peso.\n"
            'Reply ONLY valid JSON no markdown: {"name":"merchant","amount":100.0,"category":"Food","note":"items"}\n'
            "Categories: Food, Transport, Utilities, Shopping, Entertainment, Healthcare, Other\n"
            "If unreadable reply: CANNOT_READ"
        )
        contents = [{"parts": [
            {"inline_data": {"mime_type": "image/jpeg", "data": b64}},
            {"text": prompt}
        ]}]
        result = await ask_gemini(contents)
        if not result or "CANNOT_READ" in result:
            return None
        cleaned = result.replace("```json","").replace("```","").strip()
        return json.loads(cleaned)
    except Exception as e:
        logger.error(f"Image parse error: {e}")
        return None

# ── STORAGE ────────────────────────────────────────────────────
def add_expense(name, amount, category="Other", note=""):
    COUNTER[0] += 1
    entry = {
        "id": COUNTER[0],
        "name": str(name),
        "amount": float(amount),
        "category": str(category),
        "note": str(note),
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    EXPENSES.append(entry)
    return entry

# ── COMMANDS ───────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Kamusta Jon! Wealth+ Bot here! 🇵🇭\n\n"
        "📸 Send a receipt PHOTO and I'll record it!\n\n"
        "Or TYPE an expense:\n"
        "• spent 250 Jollibee\n"
        "• 1500 Meralco\n"
        "• groceries 2300 SM\n"
        "• bayad 180 Grab\n"
        "• 100 load\n\n"
        "Commands:\n"
        "/expenses - recent list\n"
        "/total - by category\n"
        "/today - today only\n"
        "/delete 5 - remove #5\n\n"
        "Kaya natin to! 💪"
    )

async def expenses_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not EXPENSES:
        await update.message.reply_text("Wala pang expenses!\nType: spent 250 Jollibee")
        return
    recent = EXPENSES[-15:][::-1]
    lines = [f"#{e['id']} {e['name']} - P{e['amount']:,.2f} ({e['category']}) {e['date'][:10]}" for e in recent]
    total = sum(e["amount"] for e in EXPENSES)
    await update.message.reply_text("Recent Expenses:\n\n" + "\n".join(lines) + f"\n\nTotal: P{total:,.2f}")

async def total_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not EXPENSES:
        await update.message.reply_text("Wala pang expenses!")
        return
    by_cat = {}
    for e in EXPENSES:
        cat = e.get("category","Other")
        by_cat[cat] = by_cat.get(cat, 0) + e["amount"]
    total = sum(e["amount"] for e in EXPENSES)
    lines = [f"• {cat}: P{amt:,.2f}" for cat, amt in sorted(by_cat.items(), key=lambda x: -x[1])]
    await update.message.reply_text("Spending by Category:\n\n" + "\n".join(lines) + f"\n\nTotal: P{total:,.2f}")

async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now().strftime("%Y-%m-%d")
    today_exp = [e for e in EXPENSES if e["date"].startswith(today)]
    if not today_exp:
        await update.message.reply_text("Wala pang expenses today!")
        return
    lines = [f"• {e['name']} - P{e['amount']:,.2f} ({e['category']})" for e in today_exp]
    total = sum(e["amount"] for e in today_exp)
    await update.message.reply_text("Today:\n\n" + "\n".join(lines) + f"\n\nTotal ngayon: P{total:,.2f}")

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
            await update.message.reply_text(f"#{exp_id} not found. Use /expenses to see IDs.")
    except:
        await update.message.reply_text("Usage: /delete 5")

# ── HANDLERS ───────────────────────────────────────────────────
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.reply_text("Binabasa ang receipt... sandali!")
        file = await context.bot.get_file(update.message.photo[-1].file_id)
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(file.file_path)
        result = await parse_receipt_image(resp.content)
        if not result:
            await update.message.reply_text(
                "Hindi ko nabasa ang receipt.\n"
                "Make sure maliwanag ang photo!\n\n"
                "O i-type mo na lang:\nspent 250 SM Grocery"
            )
            return
        entry = add_expense(result.get("name","Unknown"), result.get("amount",0), result.get("category","Other"), result.get("note",""))
        await update.message.reply_text(
            f"Naitala na! ✅\n\n"
            f"Store: {entry['name']}\n"
            f"Amount: P{entry['amount']:,.2f}\n"
            f"Category: {entry['category']}\n"
            f"Note: {entry['note']}\n"
            f"Date: {entry['date']}\n\n"
            f"#{entry['id']} saved! /today to review."
        )
    except Exception as e:
        logger.error(f"Photo error: {e}")
        await update.message.reply_text("Error sa photo. Try again!")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        text = update.message.text.strip()
        if text.startswith("/"):
            return

        # Try local parser first (fast, no API needed)
        result = parse_locally(text)

        if not result:
            await update.message.reply_text(
                "Hindi ko nakuha. Try mo:\n\n"
                "• spent 250 Jollibee\n"
                "• 1500 Meralco\n"
                "• groceries 2300\n"
                "• bayad 180 Grab\n\n"
                "Or send a receipt photo! 📸"
            )
            return

        entry = add_expense(result["name"], result["amount"], result["category"], result.get("note",""))
        await update.message.reply_text(
            f"Naitala na! ✅\n\n"
            f"Store: {entry['name']}\n"
            f"Amount: P{entry['amount']:,.2f}\n"
            f"Category: {entry['category']}\n"
            f"Date: {entry['date']}\n\n"
            f"#{entry['id']} saved! /today to review."
        )
    except Exception as e:
        logger.error(f"Text error: {e}")
        await update.message.reply_text("Error. Try again!")

# ── MAIN ───────────────────────────────────────────────────────
def main():
    logger.info("Starting Wealth+ Bot...")
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",    start))
    app.add_handler(CommandHandler("help",     start))
    app.add_handler(CommandHandler("expenses", expenses_cmd))
    app.add_handler(CommandHandler("total",    total_cmd))
    app.add_handler(CommandHandler("today",    today_cmd))
    app.add_handler(CommandHandler("delete",   delete_cmd))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("Bot polling started!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
