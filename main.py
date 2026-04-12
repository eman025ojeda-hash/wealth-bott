import os
import json
import base64
import logging
from datetime import datetime
import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN  = os.environ.get("BOT_TOKEN", "8683088099:AAHtXcQy6ui6FeXqWARG2lcjYuYxfIHbMVA")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "AIzaSyAXRORlJ4hJvZRi0XZZrqBUiaAQe8X7cV8")
DATA_FILE  = "expenses.json"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_KEY}"

def load_expenses():
    try:
        with open(DATA_FILE, "r") as f: return json.load(f)
    except: return []

def save_expenses(expenses):
    with open(DATA_FILE, "w") as f: json.dump(expenses, f, indent=2, ensure_ascii=False)

def add_expense(name, amount, category="Other", note=""):
    expenses = load_expenses()
    entry = {"id": len(expenses)+1, "name": name, "amount": float(amount), "category": category, "note": note, "date": datetime.now().strftime("%Y-%m-%d %H:%M")}
    expenses.append(entry)
    save_expenses(expenses)
    return entry

async def ask_gemini(contents):
    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.post(GEMINI_URL, json={"contents": contents})
        data = res.json()
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()

async def parse_text_expense(text):
    prompt = f"""Extract expense from this message (Filipino/English, currency is Philippine Peso):
"{text}"
Reply ONLY in JSON: {{"name":"store name","amount":123.45,"category":"Food/Transport/Utilities/Shopping/Entertainment/Healthcare/Other","note":"brief note"}}
If NOT an expense reply: NOT_EXPENSE"""
    try:
        result = await ask_gemini([{"parts": [{"text": prompt}]}])
        if "NOT_EXPENSE" in result: return None
        return json.loads(result.replace("```json","").replace("```","").strip())
    except Exception as e:
        logger.error(f"Text parse error: {e}"); return None

async def parse_receipt_image(image_bytes):
    b64 = base64.standard_b64encode(image_bytes).decode()
    prompt = """Read this receipt. Currency is Philippine Peso.
Reply ONLY in JSON: {"name":"merchant","amount":123.45,"category":"Food/Transport/Utilities/Shopping/Entertainment/Healthcare/Other","note":"items summary"}
If unreadable reply: CANNOT_READ"""
    try:
        result = await ask_gemini([{"parts": [{"inline_data": {"mime_type":"image/jpeg","data":b64}}, {"text": prompt}]}])
        if "CANNOT_READ" in result: return None
        return json.loads(result.replace("```json","").replace("```","").strip())
    except Exception as e:
        logger.error(f"Image parse error: {e}"); return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Kamusta Jon! Wealth+ Bot here!* 🇵🇭\n\n"
        "📸 Send a *receipt photo* → I'll record it!\n\n"
        "💬 Or type an expense:\n"
        "• `spent ₱250 Jollibee`\n"
        "• `bayad ₱1500 Meralco`\n"
        "• `groceries ₱2300 SM`\n"
        "• `grab ₱180 papunta work`\n\n"
        "📊 *Commands:*\n"
        "/expenses — recent expenses\n"
        "/total — spending by category\n"
        "/today — today's total\n"
        "/delete 5 — delete expense #5\n\n"
        "Kaya natin to! 💪", parse_mode="Markdown")

async def expenses_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    expenses = load_expenses()
    if not expenses:
        await update.message.reply_text("Wala pang expenses!\n\nSend a receipt photo or type:\n`spent ₱250 Jollibee`", parse_mode="Markdown"); return
    recent = expenses[-15:][::-1]
    lines = [f"#{e['id']} *{e['name']}* — ₱{e['amount']:,.2f}\n   {e['category']} · {e['date'][:10]}" for e in recent]
    total = sum(e["amount"] for e in expenses)
    await update.message.reply_text("📋 *Recent Expenses*\n\n" + "\n\n".join(lines) + f"\n\n💸 *Total: ₱{total:,.2f}*", parse_mode="Markdown")

async def total_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    expenses = load_expenses()
    if not expenses:
        await update.message.reply_text("Wala pang expenses!"); return
    by_cat = {}
    for e in expenses: by_cat[e.get("category","Other")] = by_cat.get(e.get("category","Other"),0) + e["amount"]
    total = sum(e["amount"] for e in expenses)
    lines = [f"• {cat}: ₱{amt:,.2f}" for cat,amt in sorted(by_cat.items(), key=lambda x:-x[1])]
    await update.message.reply_text("📊 *Spending by Category*\n\n" + "\n".join(lines) + f"\n\n💸 *Total: ₱{total:,.2f}*", parse_mode="Markdown")

async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    expenses = load_expenses()
    today = datetime.now().strftime("%Y-%m-%d")
    today_exp = [e for e in expenses if e["date"].startswith(today)]
    if not today_exp:
        await update.message.reply_text(f"Wala pang expenses today ({today})!"); return
    lines = [f"• *{e['name']}* — ₱{e['amount']:,.2f} ({e['category']})" for e in today_exp]
    total = sum(e["amount"] for e in today_exp)
    await update.message.reply_text(f"📅 *Today's Expenses*\n\n" + "\n".join(lines) + f"\n\n💸 Total ngayon: ₱{total:,.2f}", parse_mode="Markdown")

async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /delete [id]\nHalimbawa: /delete 5"); return
    try:
        exp_id = int(context.args[0])
        expenses = load_expenses()
        new_list = [e for e in expenses if e["id"] != exp_id]
        if len(new_list) < len(expenses):
            save_expenses(new_list)
            await update.message.reply_text(f"✅ Expense #{exp_id} deleted na!")
        else:
            await update.message.reply_text(f"❌ Expense #{exp_id} not found.")
    except: await update.message.reply_text("❌ Invalid. Example: /delete 5")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📸 Binabasa ang receipt mo... sandali!")
    try:
        file = await context.bot.get_file(update.message.photo[-1].file_id)
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(file.file_path)
        result = await parse_receipt_image(resp.content)
        if not result:
            await update.message.reply_text("😕 Hindi ko nabasa ang receipt. Make sure maliwanag ang photo!\n\nO i-type mo na lang:\n`spent ₱250 SM Grocery`", parse_mode="Markdown"); return
        entry = add_expense(result["name"], result["amount"], result.get("category","Other"), result.get("note",""))
        await update.message.reply_text(
            f"✅ *Naitala na!*\n\n"
            f"🏪 *{entry['name']}*\n"
            f"💸 ₱{entry['amount']:,.2f}\n"
            f"📂 {entry['category']}\n"
            f"📝 {entry['note']}\n"
            f"📅 {entry['date']}\n\n"
            f"_Expense #{entry['id']} saved!_", parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Photo error: {e}")
        await update.message.reply_text("❌ May error. Try ulit o i-type mo na lang ang expense.")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text.startswith("/"): return
    result = await parse_text_expense(text)
    if not result:
        await update.message.reply_text(
            "🤔 Hindi ko nakuha yan. Try mo:\n\n"
            "• `spent ₱250 Jollibee`\n"
            "• `bayad ₱1500 Meralco`\n"
            "• `groceries ₱2300`\n"
            "• O mag-send ng 📸 receipt photo!", parse_mode="Markdown"); return
    entry = add_expense(result["name"], result["amount"], result.get("category","Other"), result.get("note",""))
    await update.message.reply_text(
        f"✅ *Naitala na!*\n\n"
        f"🏪 *{entry['name']}*\n"
        f"💸 ₱{entry['amount']:,.2f}\n"
        f"📂 {entry['category']}\n"
        f"📅 {entry['date']}\n\n"
        f"_#{entry['id']} saved! /today para makita lahat._", parse_mode="Markdown")

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("expenses", expenses_cmd))
    app.add_handler(CommandHandler("total", total_cmd))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("delete", delete_cmd))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("🚀 Wealth+ Bot running with Gemini AI!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
