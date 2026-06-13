import os, requests, threading, time, uuid, base64, json, logging
from flask import Flask, request as freq
import telebot
from telebot import types

logging.basicConfig(level=logging.DEBUG)

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_CHAT_ID = os.environ["OWNER_CHAT_ID"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
PREMIUM_CHANNEL_ID = os.environ.get("PREMIUM_CHANNEL_ID", "")

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

pending_payments = {}
pending_reviews = {}

PLANS = {
    "premium_1month": {"label": "⭐ Premium - 1 Month", "amount": 99},
    "premium_3month": {"label": "⭐ Premium - 3 Months", "amount": 249},
}

# ── WEBHOOK ──────────────────────────────────────────────────
@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    try:
        update = telebot.types.Update.de_json(freq.get_json())
        bot.process_new_updates([update])
        return "OK", 200
    except Exception as e:
        print(f"WEBHOOK ERROR: {str(e)}")
        return "OK", 200

@app.route("/", methods=["GET", "POST"])
def home():
    return "✅ UPI Payment Bot is running!", 200

# ── START ─────────────────────────────────────────────────────
@bot.message_handler(commands=["start", "help"])
def help_cmd(message):
    try:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("⭐ Get Premium", callback_data="show_plans"))
        bot.reply_to(message,
            "👋 Welcome!\n\nGet *Premium Access* to unlock all features.",
            parse_mode="Markdown",
            reply_markup=markup
        )
    except Exception as e:
        print(f"START ERROR: {str(e)}")

# ── SHOW PLANS ────────────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data == "show_plans")
def show_plans(call):
    try:
        markup = types.InlineKeyboardMarkup()
        for key, plan in PLANS.items():
            markup.add(types.InlineKeyboardButton(
                f"{plan['label']} — ₹{plan['amount']}",
                callback_data=f"buy_{key}"
            ))
        bot.edit_message_text(
            "🎯 Choose your plan:",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup
        )
    except Exception as e:
        print(f"PLANS ERROR: {str(e)}")

# ── USER SELECTS PLAN → SEND QR ───────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data.startswith("buy_"))
@bot.callback_query_handler(func=lambda c: c.data.startswith("buy_"))
def handle_plan_selection(call):
    try:
        plan_key = call.data.replace("buy_", "")
        plan = PLANS.get(plan_key)
        if not plan:
            bot.answer_callback_query(call.id, "Invalid plan.")
            return

        user_id = call.from_user.id
        amount = plan["amount"]
        pending_payments[user_id] = {"amount": amount, "plan": plan["label"]}

        YOUR_UPI_ID = "veerakumarchellaiyan125-1@okaxis"  # 👈 Replace with your UPI ID

        bot.send_message(call.message.chat.id,
            f"💳 *{plan['label']}*\n"
            f"Amount: *₹{amount}*\n\n"
            f"Send payment to:\n"
            f"📲 UPI ID: `{YOUR_UPI_ID}`\n\n"
            f"After paying, send the *payment screenshot* here.\n"
            f"⚠️ Make sure to pay exactly ₹{amount}",
            parse_mode="Markdown"
        )

    except Exception as e:
        print(f"PLAN SELECT ERROR: {str(e)}")
        bot.send_message(call.message.chat.id, f"❌ Error: {str(e)}"))

# ── USER SENDS SCREENSHOT ─────────────────────────────────────
@bot.message_handler(content_types=["photo"])
def handle_screenshot(message):
    try:
        user_id = message.from_user.id

        if user_id not in pending_payments:
            bot.reply_to(message, "⚠️ No pending payment found. Use /start to begin.")
            return

        bot.reply_to(message, "🔍 Scanning your payment screenshot...")

        photo = message.photo[-1]
        file_info = bot.get_file(photo.file_id)
        file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}"

        img_response = requests.get(file_url, timeout=10)
        img_base64 = base64.b64encode(img_response.content).decode("utf-8")

        gemini_response = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}",
            headers={"Content-Type": "application/json"},
            json={
                "contents": [{
                    "parts": [
                        {
                            "inline_data": {
                                "mime_type": "image/jpeg",
                                "data": img_base64
                            }
                        },
                        {
                            "text": """Analyze this image. Extract ONLY these fields as JSON:
{
  "is_payment_screenshot": true or false,
  "transaction_id": "...",
  "amount": "...",
  "recipient": "...",
  "status": "SUCCESS or FAILED or UNKNOWN"
}
If this is NOT a payment screenshot return is_payment_screenshot as false and all other fields as null.
Return ONLY raw JSON. No markdown, no backticks, no explanation."""
                        }
                    ]
                }]
            },
            timeout=30
        )

        result = gemini_response.json()
        print(f"GEMINI RESPONSE: {result}")
        text = result["candidates"][0]["content"]["parts"][0]["text"].strip()
        text = text.replace("```json", "").replace("```", "").strip()
        data = json.loads(text)

        if not data.get("is_payment_screenshot"):
            bot.reply_to(message,
                "❌ This doesn't look like a payment screenshot.\n"
                "Please send a valid UPI payment screenshot."
            )
            return

        txn_id = data.get("transaction_id", "N/A")
        amount_paid = data.get("amount", "N/A")
        recipient = data.get("recipient", "N/A")
        status = data.get("status", "UNKNOWN")
        expected = pending_payments[user_id]["amount"]
        plan_label = pending_payments[user_id]["plan"]

        bot.reply_to(message,
            f"✅ *Screenshot Scanned Successfully!*\n\n"
            f"📋 *Transaction Details:*\n"
            f"• Transaction ID: `{txn_id}`\n"
            f"• Amount Paid: ₹{amount_paid}\n"
            f"• Paid To: {recipient}\n"
            f"• Status: {status}\n\n"
            f"⏳ Waiting for owner approval...",
            parse_mode="Markdown"
        )

        review_key = str(uuid.uuid4())[:8]
        pending_reviews[review_key] = {
            "user_id": user_id,
            "user_name": message.from_user.first_name,
            "username": message.from_user.username or "N/A",
            "amount_paid": amount_paid,
            "expected_amount": expected,
            "txn_id": txn_id,
            "recipient": recipient,
            "status": status,
            "plan": plan_label,
            "file_id": photo.file_id
        }

        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton("✅ Approve", callback_data=f"approve_{review_key}"),
            types.InlineKeyboardButton("❌ Decline", callback_data=f"decline_{review_key}")
        )

        bot.send_photo(
            OWNER_CHAT_ID,
            photo.file_id,
            caption=(
                f"🔔 *New Payment Review*\n\n"
                f"👤 User: {message.from_user.first_name} (@{message.from_user.username or 'N/A'})\n"
                f"📦 Plan: {plan_label}\n"
                f"💰 Expected: ₹{expected}\n"
                f"💸 Paid: ₹{amount_paid}\n"
                f"🏦 Paid To: {recipient}\n"
                f"🔖 Txn ID: `{txn_id}`\n"
                f"📊 Status: {status}\n"
                f"🔑 Key: `{review_key}`"
            ),
            parse_mode="Markdown",
            reply_markup=markup
        )

    except Exception as e:
        print(f"SCREENSHOT ERROR: {str(e)}")
        bot.reply_to(message, f"❌ Error: {str(e)}")

# ── NON-PHOTO DURING PENDING ──────────────────────────────────
@bot.message_handler(content_types=["document", "video", "audio", "sticker", "voice"])
def handle_non_photo(message):
    try:
        user_id = message.from_user.id
        if user_id in pending_payments:
            bot.reply_to(message,
                "⚠️ Please send a *payment screenshot* (image only).\n"
                "Other file types are not accepted.",
                parse_mode="Markdown"
            )
    except Exception as e:
        print(f"NON PHOTO ERROR: {str(e)}")

# ── OWNER: APPROVE ────────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data.startswith("approve_"))
def approve_payment(call):
    try:
        if str(call.message.chat.id) != str(OWNER_CHAT_ID):
            bot.answer_callback_query(call.id, "⛔ Not authorized.")
            return

        review_key = call.data.replace("approve_", "")
        review = pending_reviews.get(review_key)

        if not review:
            bot.answer_callback_query(call.id, "⚠️ Already processed.")
            return

        user_id = review["user_id"]
        plan = review["plan"]
        access_token = str(uuid.uuid4()).replace("-", "")[:16].upper()

        invite_link = ""
        if PREMIUM_CHANNEL_ID:
            try:
                link_obj = bot.create_chat_invite_link(
                    PREMIUM_CHANNEL_ID,
                    member_limit=1,
                    expire_date=int(time.time()) + 30 * 24 * 3600
                )
                invite_link = link_obj.invite_link
            except Exception as e:
                invite_link = f"Could not generate: {str(e)}"

        msg = (
            f"🎉 *Payment Approved! Welcome to Premium!*\n\n"
            f"📦 Plan: {plan}\n"
            f"🔑 Access Token: `{access_token}`\n"
        )
        if invite_link:
            msg += f"📲 Join Here: {invite_link}\n"
        msg += "\nThank you for your purchase! 🙏"

        bot.send_message(user_id, msg, parse_mode="Markdown")
        bot.edit_message_caption(
            f"✅ *APPROVED*\n\n" + call.message.caption,
            call.message.chat.id,
            call.message.message_id,
            parse_mode="Markdown"
        )

        pending_reviews.pop(review_key, None)
        pending_payments.pop(user_id, None)
        bot.answer_callback_query(call.id, "✅ Approved!")

    except Exception as e:
        print(f"APPROVE ERROR: {str(e)}")

# ── OWNER: DECLINE ────────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data.startswith("decline_"))
def decline_payment(call):
    try:
        if str(call.message.chat.id) != str(OWNER_CHAT_ID):
            bot.answer_callback_query(call.id, "⛔ Not authorized.")
            return

        review_key = call.data.replace("decline_", "")
        review = pending_reviews.get(review_key)

        if not review:
            bot.answer_callback_query(call.id, "⚠️ Already processed.")
            return

        user_id = review["user_id"]

        bot.send_message(user_id,
            "❌ *Payment Not Confirmed*\n\n"
            "Your payment could not be verified.\n\n"
            "Possible reasons:\n"
            "• Amount paid does not match\n"
            "• Payment sent to wrong UPI ID\n"
            "• Screenshot is unclear\n\n"
            "Please try /start again or contact support.",
            parse_mode="Markdown"
        )

        bot.edit_message_caption(
            f"❌ *DECLINED*\n\n" + call.message.caption,
            call.message.chat.id,
            call.message.message_id,
            parse_mode="Markdown"
        )

        pending_reviews.pop(review_key, None)
        pending_payments.pop(user_id, None)
        bot.answer_callback_query(call.id, "❌ Declined!")

    except Exception as e:
        print(f"DECLINE ERROR: {str(e)}")

# ── STARTUP ───────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=7860)
