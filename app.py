import os
import re
import json
import uuid
import threading
import time
import base64
import requests
from datetime import datetime, timedelta
from dateutil import parser as date_parser
from flask import Flask
import telebot
from telebot import apihelper, types
import qrcode
import io

# ── ENVIRONMENT VARIABLES & SETTINGS ──────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").replace('"', '').replace("'", "").strip()
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").replace('"', '').replace("'", "").strip()
YOUR_UPI_ID = "nitheshkumar05@fam"

try:
    OWNER_CHAT_ID = int(os.environ.get("OWNER_CHAT_ID", 0))
except ValueError:
    OWNER_CHAT_ID = 0
    
PREMIUM_CHANNEL_ID = os.environ.get("PREMIUM_CHANNEL_ID", "").strip()

apihelper.SESSION_TIMEOUT = 120
bot = telebot.TeleBot(BOT_TOKEN) if BOT_TOKEN else None
app = Flask(__name__)

# ── DATABASE SYSTEM (Auto-Save) ──────────────────────────────
DB_FILE = "database.json"

def load_db():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"plans": {}, "bot_links": {}, "used_utrs": []}

def save_db():
    try:
        with open(DB_FILE, "w") as f:
            json.dump(db, f, indent=4)
    except Exception as e:
        print(f"Error saving database: {e}")

db = load_db()

# Temporary memory containers
user_states = {}  
pending_approvals = {}  
owner_states = {}

DELIVERABLE_TYPES = {
    "private_channel": "📢 Private Channel Access (1-Time Link)",
    "access_token": "🔑 Unique Access Token",
    "vip_badge": "👑 VIP Role",
    "bonus_content": "🎁 Bonus Content"
}

# ── RENDER HEALTH SERVER ─────────────────────────────────────
@app.route("/", methods=["GET"])
def home():
    return "✅ Premium Automation Bot is Live & Listening on Render!", 200

# ── UTILITIES ────────────────────────────────────────────────
def is_owner(chat_id):
    return str(chat_id) == str(OWNER_CHAT_ID)

def get_current_time():
    return datetime.now().strftime("%d %b %Y, %I:%M %p")

# ── GROQ VISION API ENGINE ───────────────────────────────────
def analyze_receipt_with_groq(image_bytes):
    # 1. Compress and resize the image to prevent Groq 400 Payload Too Large errors
    img = Image.open(io.BytesIO(image_bytes))
    img = img.convert("RGB") # Drop alpha channels which cause API issues
    img.thumbnail((1024, 1024)) # Scale down to a safe maximum size
    
    buffered = io.BytesIO()
    img.save(buffered, format="JPEG", quality=80) # Compress slightly
    base64_image = base64.b64encode(buffered.getvalue()).decode('utf-8')
    
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    
    prompt = """
    Analyze this payment receipt screenshot. Return ONLY a valid JSON object with NO markdown formatting, NO backticks, and NO extra text.
    The JSON must contain these exact keys:
    "utr": (string) The 12 to 22 digit Transaction ID, UTR, or Reference Number. Null if missing.
    "amount": (number) The exact amount paid. Strip out the currency symbol. Null if missing.
    "date": (string) The date and time of the transaction. Null if missing.
    "is_fake": (boolean) True if the image looks like a fake generator, dummy, prank, or edited. False otherwise.
    "receiver": (string) The name of the person or merchant paid. Null if missing.
    """
    
    payload = {
        "model": "llama-3.2-11b-vision-preview",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                ]
            }
        ],
        "temperature": 0.1,
        "max_tokens": 1024 # Forces Groq to allocate proper response space
    }
    
    response = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload, timeout=20)
    
    # Debugging: Print exact rejection reason if it fails again
    if response.status_code != 200:
        print(f"🚨 Groq API Rejection: {response.text}", flush=True)
        response.raise_for_status()
        
    content = response.json()["choices"][0]["message"]["content"].strip()
    
    # Strip markdown code blocks if the model ignores raw JSON instructions
    if content.startswith("```json"):
        content = content[7:-3]
    elif content.startswith("```"):
        content = content[3:-3]
        
    return json.loads(content.strip())
    

# ── COMMANDS & MENUS ─────────────────────────────────────────
@bot.message_handler(commands=["start"])
def handle_start(message):
    args = message.text.split()
    if len(args) > 1:
        link_id = args[1]
        if link_id in db["bot_links"]:
            plan_key = db["bot_links"][link_id]
            if plan_key in db["plans"]:
                send_plan_payment(message.chat.id, message.from_user.id, plan_key, db["plans"][plan_key])
                return

    if is_owner(message.chat.id):
        send_owner_menu(message.chat.id)
    else:
        send_user_menu(message.chat.id)

@bot.message_handler(commands=["admin"])
def admin_cmd(message):
    if is_owner(message.chat.id):
        send_owner_menu(message.chat.id)

def send_user_menu(chat_id):
    markup = types.InlineKeyboardMarkup()
    if not db["plans"]:
        bot.send_message(chat_id, "🚧 No plans are currently available. Check back later!")
        return
    
    for key, plan in db["plans"].items():
        markup.add(types.InlineKeyboardButton(f"{plan['label']} — ₹{plan['amount']}", callback_data=f"buy_{key}"))
    bot.send_message(chat_id, "👋 *Welcome!*\n\nSelect a plan below to gain instant premium access:", parse_mode="Markdown", reply_markup=markup)

def send_owner_menu(chat_id, message_id=None):
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("➕ Add Plan", callback_data="owner_add_plan"),
        types.InlineKeyboardButton("🗑 Delete Plan", callback_data="owner_delete_plan"),
        types.InlineKeyboardButton("🔗 Create Plan Link", callback_data="owner_create_link"),
        types.InlineKeyboardButton("📊 View Links", callback_data="owner_view_links"),
    )
    text = f"👑 *Admin Dashboard*\n🕒 {get_current_time()}\n\nManage your automated delivery system."
    if message_id:
        bot.edit_message_text(text, chat_id, message_id, parse_mode="Markdown", reply_markup=markup)
    else:
        bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=markup)

# ── USER CHECKOUT FLOW ───────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data.startswith("buy_"))
def process_premium_request(call):
    plan_key = call.data.replace("buy_", "")
    plan = db["plans"].get(plan_key)
    if not plan:
        bot.answer_callback_query(call.id, "Plan not found.", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    send_plan_payment(call.message.chat.id, call.from_user.id, plan_key, plan)

def send_plan_payment(chat_id, user_id, plan_key, plan):
    bot.send_message(chat_id, "⏳ Generating your secure UPI payment QR code...")
    amount = str(plan["amount"])
    
    try:
        upi_url = f"upi://pay?pa={YOUR_UPI_ID}&pn=PremiumBot&am={amount}&cu=INR"
        qr = qrcode.QRCode(version=1, box_size=10, border=4)
        qr.add_data(upi_url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        
        bio = io.BytesIO()
        bio.name = 'payment_qr.png'
        img.save(bio, 'PNG')
        bio.seek(0)
        
        items_list = "\n".join([f"  • {DELIVERABLE_TYPES.get(d, d)}" for d in plan.get("deliverables", [])])
        
        caption = (
            f"🛒 *Checkout: {plan['label']}*\n"
            f"📦 *Rewards:*\n{items_list}\n\n"
            f"💳 *PAYMENT INSTRUCTIONS*\n"
            f"1️⃣ Scan the QR code below.\n"
            f"2️⃣ Pay exactly *₹{amount}*.\n"
            f"3️⃣ Take a clear screenshot of the successful receipt.\n"
            f"4️⃣ *Send the screenshot here for auto-verification.*\n\n"
            f"_(Do not edit the amount or the verification will fail)_"
        )
        
        bot.send_photo(chat_id, photo=bio, caption=caption, parse_mode="Markdown")
        user_states[chat_id] = {"status": "AWAITING_SCREENSHOT", "expected_amount": amount, "plan": plan}
        
    except Exception as e:
        bot.send_message(chat_id, f"❌ Failed to generate QR code. Please try again.")
        print(f"QR Error: {e}", flush=True)

# ── SCREENSHOT PROCESSING & AUTO-APPROVE ENGINE ──────────────
@bot.message_handler(content_types=["photo"])
def handle_screenshot(message):
    chat_id = message.chat.id
    state = user_states.get(chat_id)
    
    if not state or state.get("status") != "AWAITING_SCREENSHOT":
        return 

    status_msg = bot.reply_to(message, "🔍 Groq AI is scanning your receipt. Please hold on...")
    expected_amount = state["expected_amount"]
    plan = state["plan"]
    
    try:
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        
        analysis = analyze_receipt_with_groq(downloaded_file)
        
        utr = str(analysis.get("utr") or "UNKNOWN_UTR")
        scanned_amount = str(analysis.get("amount") or "0")
        is_fake = analysis.get("is_fake", False)
        receiver = str(analysis.get("receiver") or "Unknown")
        scanned_date_str = analysis.get("date")

        date_valid, date_reason = is_date_within_24h(scanned_date_str)
        
        auto_approve = True
        flag_reason = []

        if is_fake:
            auto_approve = False
            flag_reason.append("AI Detected potential fake/dummy receipt")
        if utr in db["used_utrs"] or utr == "UNKNOWN_UTR":
            auto_approve = False
            flag_reason.append("Duplicate or Missing UTR")
        try:
            if float(scanned_amount) != float(expected_amount):
                auto_approve = False
                flag_reason.append(f"Amount mismatch (Expected: {expected_amount}, Found: {scanned_amount})")
        except ValueError:
            auto_approve = False
            flag_reason.append("Could not parse AI extracted amount.")
            
        if not date_valid:
            auto_approve = False
            flag_reason.append(f"Date check failed: {date_reason}")

        if auto_approve:
            db["used_utrs"].append(utr)
            save_db()
            bot.delete_message(chat_id, status_msg.message_id)
            deliver_rewards(chat_id, plan)
            
            if OWNER_CHAT_ID:
                admin_text = f"✅ *AUTO-APPROVED PAYMENT*\n\n👤 User: `{chat_id}`\n💰 Amount: ₹{expected_amount}\n🔖 UTR: `{utr}`\n🕒 Scanned Date: {scanned_date_str}\n🕒 Approved At: {get_current_time()}"
                bot.send_message(OWNER_CHAT_ID, admin_text, parse_mode="Markdown")
            
        else:
            bot.edit_message_text("⚠️ Auto-Verification failed. Sending to Admin for manual review. Please wait.", chat_id, status_msg.message_id)
            
            approval_key = f"{chat_id}_{int(time.time())}"
            pending_approvals[approval_key] = {"user_id": chat_id, "plan": plan, "utr": utr}
            
            if OWNER_CHAT_ID:
                admin_markup = types.InlineKeyboardMarkup()
                admin_markup.row(
                    types.InlineKeyboardButton("✅ Approve", callback_data=f"ap_{approval_key}"),
                    types.InlineKeyboardButton("❌ Decline", callback_data=f"dec_{approval_key}")
                )
                admin_payload = (
                    "🔔 *MANUAL REVIEW REQUIRED*\n\n"
                    f"👤 *User ID:* `{chat_id}`\n"
                    f"📦 *Plan:* {plan['label']} (₹{expected_amount})\n"
                    f"🔖 *Scanned UTR:* `{utr}`\n"
                    f"🏦 *Scanned Receiver:* {receiver}\n"
                    f"🕒 *Scanned Time:* {scanned_date_str}\n"
                    f"🕒 *Request Time:* {get_current_time()}\n\n"
                    f"⚠️ *Groq AI Flags:* {', '.join(flag_reason)}\n\n"
                    "Review the attached image and verify with your bank."
                )
                bot.send_photo(OWNER_CHAT_ID, message.photo[-1].file_id, caption=admin_payload, reply_markup=admin_markup, parse_mode="Markdown")

        user_states.pop(chat_id, None)

    except Exception as e:
        bot.edit_message_text("❌ Error communicating with AI Vision model. Admin has been notified.", chat_id, status_msg.message_id)
        print(f"Groq API Error: {e}", flush=True)

def deliver_rewards(user_id, plan):
    deliverables = plan.get("deliverables", [])
    success_text = f"🎉 *PAYMENT SUCCESSFUL!*\n\nYou have purchased **{plan['label']}**.\n\n"
    
    if "private_channel" in deliverables and PREMIUM_CHANNEL_ID:
        try:
            invite = bot.create_chat_invite_link(chat_id=PREMIUM_CHANNEL_ID, member_limit=1).invite_link
            success_text += f"🔗 *Your Private Channel Link:*\n{invite}\n_(Note: This link works for exactly 1 person. Do not share it.)_\n"
        except Exception:
            success_text += "⚠️ Error generating channel link. Please contact admin.\n"
            
    if "access_token" in deliverables:
        success_text += f"🔑 *Your Access Token:*\n`{str(uuid.uuid4())}`\n"
        
    bot.send_message(user_id, success_text, parse_mode="Markdown")

# ── ADMIN APPROVAL ROUTING ───────────────────────────────────
@bot.callback_query_handler(func=lambda call: call.data.startswith(("ap_", "dec_")))
def handle_admin_decision(call):
    action, approval_key = call.data.split("_", 1)
    record = pending_approvals.get(approval_key)
    
    if not record:
        bot.answer_callback_query(call.id, "❌ Record expired.", show_alert=True)
        return
        
    user_id = record["user_id"]
    
    if action == "ap":
        if record["utr"] != "UNKNOWN_UTR" and record["utr"] not in db["used_utrs"]:
            db["used_utrs"].append(record["utr"])
            save_db()
        deliver_rewards(user_id, record["plan"])
        bot.edit_message_caption(f"✅ Handled: Approved manually on {get_current_time()}", call.message.chat.id, call.message.message_id)
            
    elif action == "dec":
        bot.send_message(user_id, "❌ *Payment Declined*\n\nYour screenshot was manually reviewed and rejected. Common reasons include mismatched amounts, duplicate transaction IDs, or fake receipts. Contact support if this is a mistake.", parse_mode="Markdown")
        bot.edit_message_caption(f"❌ Handled: Declined manually on {get_current_time()}", call.message.chat.id, call.message.message_id)
        
    pending_approvals.pop(approval_key, None)
    bot.answer_callback_query(call.id)

# ── OWNER CONFIGURATION LOGIC ────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data == "owner_back")
def owner_back(call):
    send_owner_menu(call.message.chat.id, call.message.message_id)

@bot.callback_query_handler(func=lambda c: c.data == "owner_add_plan")
def owner_add_plan(call):
    owner_states[call.from_user.id] = {"action": "add_plan", "step": "name", "data": {}}
    bot.send_message(call.message.chat.id, "➕ *Add New Plan*\n\nStep 1: Enter the plan name (e.g., VIP 1 Month):", parse_mode="Markdown")

@bot.callback_query_handler(func=lambda c: c.data == "owner_delete_plan")
def owner_delete_plan(call):
    markup = types.InlineKeyboardMarkup()
    for key, plan in db["plans"].items():
        markup.add(types.InlineKeyboardButton(f"🗑 {plan['label']}", callback_data=f"delete_{key}"))
    markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="owner_back"))
    bot.edit_message_text("🗑 Select plan to delete:", call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda c: c.data.startswith("delete_"))
def execute_delete(call):
    key = call.data.replace("delete_", "")
    if key in db["plans"]:
        del db["plans"][key]
        save_db()
        bot.answer_callback_query(call.id, "✅ Deleted!")
        owner_back(call)

@bot.callback_query_handler(func=lambda c: c.data == "owner_create_link")
def owner_create_link(call):
    markup = types.InlineKeyboardMarkup()
    for key, plan in db["plans"].items():
        markup.add(types.InlineKeyboardButton(plan["label"], callback_data=f"createlink_{key}"))
    markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="owner_back"))
    bot.edit_message_text("🔗 Select plan for direct link:", call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda c: c.data.startswith("createlink_"))
def generate_link(call):
    plan_key = call.data.replace("createlink_", "")
    link_id = str(uuid.uuid4())[:8]
    db["bot_links"][link_id] = plan_key
    save_db()
    
    link = f"https://t.me/{bot.get_me().username}?start={link_id}"
    bot.send_message(call.message.chat.id, f"🔗 *Direct Bot Link Created!*\n\n`{link}`\n\nUsers clicking this will go straight to payment.", parse_mode="Markdown")

@bot.callback_query_handler(func=lambda c: c.data == "owner_view_links")
def owner_view_links(call):
    text = "📊 *Active Custom Links:*\n\n"
    for link_id, plan_key in db["bot_links"].items():
        plan_name = db["plans"].get(plan_key, {}).get("label", "Deleted Plan")
        text += f"📌 {plan_name}\n🔗 `https://t.me/{bot.get_me().username}?start={link_id}`\n\n"
    if not db["bot_links"]:
        text += "No active links."
    bot.send_message(call.message.chat.id, text, parse_mode="Markdown")

@bot.message_handler(func=lambda m: m.from_user.id in owner_states, content_types=["text"])
def process_owner_input(message):
    state = owner_states[message.from_user.id]
    if state["action"] == "add_plan":
        if state["step"] == "name":
            state["data"]["label"] = message.text
            state["step"] = "amount"
            bot.send_message(message.chat.id, "💰 Step 2: Enter exact Amount in ₹ (e.g., 99):")
        elif state["step"] == "amount":
            if not message.text.isdigit():
                bot.send_message(message.chat.id, "❌ Enter numbers only.")
                return
            state["data"]["amount"] = int(message.text)
            state["step"] = "deliv"
            options = "\n".join([f"`{k}` — {v}" for k, v in DELIVERABLE_TYPES.items()])
            bot.send_message(message.chat.id, f"📦 Step 3: Send deliverable keys separated by comma:\n\n{options}", parse_mode="Markdown")
        elif state["step"] == "deliv":
            keys = [k.strip() for k in message.text.split(",")]
            state["data"]["deliverables"] = [k for k in keys if k in DELIVERABLE_TYPES]
            
            plan_key = f"plan_{uuid.uuid4().hex[:6]}"
            db["plans"][plan_key] = state["data"]
            save_db()
            
            del owner_states[message.from_user.id]
            bot.send_message(message.chat.id, f"✅ *Plan Created!* Users can now buy {state['data']['label']}.", parse_mode="Markdown")

# ── STARTUP SEQUENCE ─────────────────────────────────────────
def boot_telegram_bot():
    if not BOT_TOKEN:
        print("🚨 BOT_TOKEN is missing!")
        return

    print("⏳ Waiting 15s for Render network to initialize...", flush=True)
    time.sleep(15)
    
    while True:
        try:
            res = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook", timeout=60)
            if res.status_code == 200:
                print("✅ Network line secured!", flush=True)
                break
            time.sleep(5)
        except Exception:
            time.sleep(5)

    print("🛡️ Bot is live and actively listening for messages!", flush=True)
    while True:
        try:
            bot.polling(non_stop=True, timeout=60, long_polling_timeout=60, skip_pending=True)
        except Exception as e:
            print(f"⚠️ Network error, reconnecting... {e}", flush=True)
            time.sleep(5)

threading.Thread(target=boot_telegram_bot, daemon=True).start()

if __name__ == "__main__":
    import logging
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)
    
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
