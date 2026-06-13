import os, requests, threading, time, uuid, base64, json, logging
from flask import Flask, request as freq
from io import BytesIO
from datetime import datetime, timezone, timedelta
import telebot
from telebot import types
import qrcode

logging.basicConfig(level=logging.DEBUG)

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_CHAT_ID = os.environ["OWNER_CHAT_ID"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
PREMIUM_CHANNEL_ID = os.environ.get("PREMIUM_CHANNEL_ID", "")

YOUR_UPI_ID = "veerakumarchellaiyan125-1@okaxis"
VERIFIED_UPI_IDS = ["nitheshkumar05@fam", "veerakumarchellaiyan125-1@okaxis"]
VERIFIED_NAMES = ["nitheshkumar", "nithesh kumar", "nithesh"]

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

plans = {
    "plan_1month": {
        "label": "⭐ Premium - 1 Month",
        "amount": 99,
        "deliverables": ["private_channel"]
    },
    "plan_3month": {
        "label": "⭐ Premium - 3 Months",
        "amount": 249,
        "deliverables": ["private_channel", "access_token"]
    }
}

deliverable_types = {
    "private_channel": "📢 Private Channel Access (one-time invite link)",
    "access_token":    "🔑 Unique Access Token",
    "vip_badge":       "👑 VIP Badge / Role",
    "bonus_content":   "🎁 Bonus Content Access",
    "custom":          "✏️ Custom (defined by owner)"
}

pending_payments = {}
pending_reviews  = {}
owner_states     = {}
bot_links        = {}

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

def is_owner(chat_id):
    return str(chat_id) == str(OWNER_CHAT_ID)

def now_ist():
    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist).strftime("%d %b %Y %I:%M %p IST")

# ── START ─────────────────────────────────────────────────────
@bot.message_handler(commands=["start"])
def handle_start(message):
    try:
        args = message.text.split()
        if len(args) > 1:
            link_id = args[1]
            if link_id in bot_links:
                plan_key = bot_links[link_id]
                plan = plans.get(plan_key)
                if plan:
                    send_plan_payment(message.chat.id, message.from_user.id, plan_key, plan)
                    return
        if is_owner(message.chat.id):
            send_owner_menu(message.chat.id)
        else:
            send_user_menu(message.chat.id)
    except Exception as e:
        print(f"START ERROR: {str(e)}")

@bot.message_handler(commands=["admin"])
def admin_cmd(message):
    if is_owner(message.chat.id):
        send_owner_menu(message.chat.id)

def send_user_menu(chat_id):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🛒 View Plans", callback_data="show_plans"))
    bot.send_message(chat_id,
        "👋 *Welcome!*\n\nBrowse our plans and get instant access after payment.",
        parse_mode="Markdown",
        reply_markup=markup
    )

def send_owner_menu(chat_id, message_id=None):
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("📋 View Plans",    callback_data="owner_view_plans"),
        types.InlineKeyboardButton("➕ Add Plan",       callback_data="owner_add_plan"),
        types.InlineKeyboardButton("✏️ Edit Plan",     callback_data="owner_edit_plan"),
        types.InlineKeyboardButton("🗑 Delete Plan",   callback_data="owner_delete_plan"),
        types.InlineKeyboardButton("📦 Deliverables",  callback_data="owner_deliverables"),
        types.InlineKeyboardButton("🔗 Create Link",   callback_data="owner_create_link"),
        types.InlineKeyboardButton("📊 View Links",    callback_data="owner_view_links"),
    )
    text = "👑 *Owner Panel*\n\nManage plans, deliverables, and bot links."
    if message_id:
        bot.edit_message_text(text, chat_id, message_id,
            parse_mode="Markdown", reply_markup=markup)
    else:
        bot.send_message(chat_id, text,
            parse_mode="Markdown", reply_markup=markup)

# ── USER: SHOW PLANS ──────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data == "show_plans")
def show_plans(call):
    try:
        if not plans:
            bot.answer_callback_query(call.id, "No plans available yet.")
            return
        markup = types.InlineKeyboardMarkup()
        for key, plan in plans.items():
            markup.add(types.InlineKeyboardButton(
                f"{plan['label']} — ₹{plan['amount']}",
                callback_data=f"buy_{key}"
            ))
        bot.edit_message_text(
            "🛒 *Choose a plan:*",
            call.message.chat.id, call.message.message_id,
            parse_mode="Markdown", reply_markup=markup
        )
    except Exception as e:
        print(f"SHOW PLANS ERROR: {str(e)}")

# ── USER: BUY PLAN ────────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data.startswith("buy_"))
def handle_plan_selection(call):
    try:
        plan_key = call.data.replace("buy_", "")
        plan = plans.get(plan_key)
        if not plan:
            bot.answer_callback_query(call.id, "Plan not found.")
            return
        send_plan_payment(call.message.chat.id, call.from_user.id, plan_key, plan)
    except Exception as e:
        print(f"PLAN SELECT ERROR: {str(e)}")

def send_plan_payment(chat_id, user_id, plan_key, plan):
    try:
        amount = plan["amount"]
        items_list = "\n".join([
            f"  • {deliverable_types.get(d, d)}"
            for d in plan.get("deliverables", [])
        ])
        pending_payments[user_id] = {
            "amount": amount,
            "plan": plan["label"],
            "plan_key": plan_key
        }

        upi_url = f"upi://pay?pa={YOUR_UPI_ID}&pn=PremiumBot&am={amount}&cu=INR&tn=Purchase"
        qr = qrcode.QRCode(version=1, box_size=10, border=4)
        qr.add_data(upi_url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        bio = BytesIO()
        img.save(bio, format="PNG")
        bio.seek(0)

        bot.send_message(chat_id,
            f"🛒 *{plan['label']}*\n"
            f"💰 Amount: *₹{amount}*\n\n"
            f"📦 *What you get:*\n{items_list}\n\n"
            f"1️⃣ Scan the QR code\n"
            f"2️⃣ Pay exactly *₹{amount}*\n"
            f"3️⃣ Send *payment screenshot* here\n\n"
            f"⚠️ Pay the exact amount shown",
            parse_mode="Markdown"
        )
        bot.send_photo(chat_id, bio,
            caption=f"📲 Scan & pay ₹{amount} via GPay / PhonePe / Paytm"
        )
    except Exception as e:
        print(f"SEND PLAN ERROR: {str(e)}")
        bot.send_message(chat_id, f"❌ Error: {str(e)}")

# ══════════════════════════════════════════════════════════════
# OWNER PANEL
# ══════════════════════════════════════════════════════════════

@bot.callback_query_handler(func=lambda c: c.data == "owner_back")
def owner_back(call):
    if not is_owner(call.message.chat.id): return
    send_owner_menu(call.message.chat.id, call.message.message_id)

# ── VIEW PLANS ────────────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data == "owner_view_plans")
def owner_view_plans(call):
    if not is_owner(call.message.chat.id): return
    if not plans:
        bot.answer_callback_query(call.id, "No plans yet.")
        return
    text = "📋 *All Plans:*\n\n"
    for key, plan in plans.items():
        items = ", ".join([deliverable_types.get(d, d) for d in plan.get("deliverables", [])])
        text += (
            f"🔑 `{key}`\n"
            f"📌 {plan['label']}\n"
            f"💰 ₹{plan['amount']}\n"
            f"📦 {items}\n\n"
        )
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="owner_back"))
    bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
        parse_mode="Markdown", reply_markup=markup)

# ── ADD PLAN ──────────────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data == "owner_add_plan")
def owner_add_plan(call):
    if not is_owner(call.message.chat.id): return
    owner_states[call.from_user.id] = {"action": "add_plan", "step": "name", "data": {}}
    bot.send_message(call.message.chat.id,
        "➕ *Add New Plan*\n\nStep 1: Enter the plan name:\n_(e.g. Gold - 6 Months)_",
        parse_mode="Markdown"
    )

# ── EDIT PLAN ─────────────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data == "owner_edit_plan")
def owner_edit_plan(call):
    if not is_owner(call.message.chat.id): return
    if not plans:
        bot.answer_callback_query(call.id, "No plans to edit.")
        return
    markup = types.InlineKeyboardMarkup()
    for key, plan in plans.items():
        markup.add(types.InlineKeyboardButton(
            plan["label"], callback_data=f"edit_select_{key}"
        ))
    markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="owner_back"))
    bot.edit_message_text("✏️ *Select plan to edit:*",
        call.message.chat.id, call.message.message_id,
        parse_mode="Markdown", reply_markup=markup)

@bot.callback_query_handler(func=lambda c: c.data.startswith("edit_select_"))
def edit_select_plan(call):
    if not is_owner(call.message.chat.id): return
    plan_key = call.data.replace("edit_select_", "")
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("📌 Edit Name",         callback_data=f"edit_name_{plan_key}"),
        types.InlineKeyboardButton("💰 Edit Amount",       callback_data=f"edit_amount_{plan_key}"),
        types.InlineKeyboardButton("📦 Edit Deliverables", callback_data=f"edit_deliv_{plan_key}"),
        types.InlineKeyboardButton("🔙 Back",              callback_data="owner_edit_plan"),
    )
    bot.edit_message_text(f"✏️ *Editing:* {plans[plan_key]['label']}",
        call.message.chat.id, call.message.message_id,
        parse_mode="Markdown", reply_markup=markup)

@bot.callback_query_handler(func=lambda c: c.data.startswith("edit_name_"))
def edit_name(call):
    if not is_owner(call.message.chat.id): return
    plan_key = call.data.replace("edit_name_", "")
    owner_states[call.from_user.id] = {"action": "edit_name", "plan_key": plan_key}
    bot.send_message(call.message.chat.id,
        f"📌 Enter new name for `{plan_key}`:", parse_mode="Markdown")

@bot.callback_query_handler(func=lambda c: c.data.startswith("edit_amount_"))
def edit_amount(call):
    if not is_owner(call.message.chat.id): return
    plan_key = call.data.replace("edit_amount_", "")
    owner_states[call.from_user.id] = {"action": "edit_amount", "plan_key": plan_key}
    bot.send_message(call.message.chat.id,
        f"💰 Enter new amount for `{plan_key}`:", parse_mode="Markdown")

@bot.callback_query_handler(func=lambda c: c.data.startswith("edit_deliv_"))
def edit_deliverables(call):
    if not is_owner(call.message.chat.id): return
    plan_key = call.data.replace("edit_deliv_", "")
    owner_states[call.from_user.id] = {"action": "edit_deliv", "plan_key": plan_key}
    options = "\n".join([f"`{k}` — {v}" for k, v in deliverable_types.items()])
    bot.send_message(call.message.chat.id,
        f"📦 *Available deliverable types:*\n\n{options}\n\n"
        f"Send keys separated by comma:\n_(e.g. private\_channel, access\_token)_",
        parse_mode="Markdown"
    )

# ── DELETE PLAN ───────────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data == "owner_delete_plan")
def owner_delete_plan(call):
    if not is_owner(call.message.chat.id): return
    if not plans:
        bot.answer_callback_query(call.id, "No plans to delete.")
        return
    markup = types.InlineKeyboardMarkup()
    for key, plan in plans.items():
        markup.add(types.InlineKeyboardButton(
            f"🗑 {plan['label']}", callback_data=f"delete_confirm_{key}"
        ))
    markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="owner_back"))
    bot.edit_message_text("🗑 *Select plan to delete:*",
        call.message.chat.id, call.message.message_id,
        parse_mode="Markdown", reply_markup=markup)

@bot.callback_query_handler(func=lambda c: c.data.startswith("delete_confirm_"))
def delete_confirm(call):
    if not is_owner(call.message.chat.id): return
    plan_key = call.data.replace("delete_confirm_", "")
    if plan_key in plans:
        del plans[plan_key]
        bot.answer_callback_query(call.id, "✅ Plan deleted!")
        send_owner_menu(call.message.chat.id, call.message.message_id)
    else:
        bot.answer_callback_query(call.id, "Plan not found.")

# ── DELIVERABLES ──────────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data == "owner_deliverables")
def owner_deliverables(call):
    if not is_owner(call.message.chat.id): return
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("➕ Add Type",  callback_data="add_deliv_type"),
        types.InlineKeyboardButton("📋 View All",  callback_data="view_deliv_types"),
        types.InlineKeyboardButton("🔙 Back",      callback_data="owner_back"),
    )
    bot.edit_message_text("📦 *Manage Deliverable Types:*",
        call.message.chat.id, call.message.message_id,
        parse_mode="Markdown", reply_markup=markup)

@bot.callback_query_handler(func=lambda c: c.data == "view_deliv_types")
def view_deliv_types(call):
    if not is_owner(call.message.chat.id): return
    text = "📦 *Deliverable Types:*\n\n"
    for k, v in deliverable_types.items():
        text += f"`{k}` — {v}\n"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="owner_deliverables"))
    bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
        parse_mode="Markdown", reply_markup=markup)

@bot.callback_query_handler(func=lambda c: c.data == "add_deliv_type")
def add_deliv_type(call):
    if not is_owner(call.message.chat.id): return
    owner_states[call.from_user.id] = {"action": "add_deliv_type", "step": "key"}
    bot.send_message(call.message.chat.id,
        "📦 *Add Deliverable Type*\n\nStep 1: Enter a short key:\n_(e.g. ebook\_access)_",
        parse_mode="Markdown"
    )

# ── CREATE BOT LINK ───────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data == "owner_create_link")
def owner_create_link(call):
    if not is_owner(call.message.chat.id): return
    if not plans:
        bot.answer_callback_query(call.id, "No plans available.")
        return
    markup = types.InlineKeyboardMarkup()
    for key, plan in plans.items():
        markup.add(types.InlineKeyboardButton(
            plan["label"], callback_data=f"createlink_{key}"
        ))
    markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="owner_back"))
    bot.edit_message_text("🔗 *Select plan to create link for:*",
        call.message.chat.id, call.message.message_id,
        parse_mode="Markdown", reply_markup=markup)

@bot.callback_query_handler(func=lambda c: c.data.startswith("createlink_"))
def create_link_for_plan(call):
    if not is_owner(call.message.chat.id): return
    plan_key = call.data.replace("createlink_", "")
    link_id = str(uuid.uuid4())[:8]
    bot_links[link_id] = plan_key
    bot_username = bot.get_me().username
    link = f"https://t.me/{bot_username}?start={link_id}"
    plan = plans.get(plan_key, {})
    bot.send_message(call.message.chat.id,
        f"🔗 *Bot Link Created!*\n\n"
        f"📌 Plan: {plan.get('label', plan_key)}\n"
        f"💰 Amount: ₹{plan.get('amount', 'N/A')}\n\n"
        f"🔗 Link:\n`{link}`\n\n"
        f"Share this link — users go directly to this plan.",
        parse_mode="Markdown"
    )

# ── VIEW BOT LINKS ────────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data == "owner_view_links")
def owner_view_links(call):
    if not is_owner(call.message.chat.id): return
    if not bot_links:
        bot.answer_callback_query(call.id, "No links created yet.")
        return
    bot_username = bot.get_me().username
    text = "📊 *Active Bot Links:*\n\n"
    for link_id, plan_key in bot_links.items():
        plan = plans.get(plan_key, {})
        link = f"https://t.me/{bot_username}?start={link_id}"
        text += f"📌 {plan.get('label', plan_key)}\n🔗 `{link}`\n\n"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="owner_back"))
    bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
        parse_mode="Markdown", reply_markup=markup)

# ══════════════════════════════════════════════════════════════
# OWNER TEXT INPUT
# ══════════════════════════════════════════════════════════════
@bot.message_handler(
    func=lambda m: m.from_user.id in owner_states and is_owner(m.chat.id),
    content_types=["text"]
)
def handle_owner_input(message):
    state = owner_states.get(message.from_user.id)
    if not state: return
    action = state["action"]
    text = message.text.strip()

    if action == "add_plan":
        step = state["step"]
        data = state.get("data", {})

        if step == "name":
            data["label"] = text
            state["step"] = "amount"
            state["data"] = data
            bot.send_message(message.chat.id, "💰 Step 2: Enter the amount (₹):")

        elif step == "amount":
            if not text.isdigit():
                bot.send_message(message.chat.id, "❌ Enter a valid number.")
                return
            data["amount"] = int(text)
            state["step"] = "deliverables"
            state["data"] = data
            options = "\n".join([f"`{k}` — {v}" for k, v in deliverable_types.items()])
            bot.send_message(message.chat.id,
                f"📦 Step 3: Choose deliverables:\n\n{options}\n\n"
                f"Send keys separated by comma:\n_(e.g. private\_channel, access\_token)_",
                parse_mode="Markdown"
            )

        elif step == "deliverables":
            keys = [k.strip() for k in text.split(",")]
            valid = [k for k in keys if k in deliverable_types]
            if not valid:
                bot.send_message(message.chat.id, "❌ No valid keys. Try again.")
                return
            data["deliverables"] = valid
            plan_key = f"plan_{uuid.uuid4().hex[:6]}"
            plans[plan_key] = {
                "label": data["label"],
                "amount": data["amount"],
                "deliverables": valid
            }
            del owner_states[message.from_user.id]
            items = "\n".join([f"  • {deliverable_types[d]}" for d in valid])
            bot.send_message(message.chat.id,
                f"✅ *Plan Created!*\n\n"
                f"🔑 Key: `{plan_key}`\n"
                f"📌 Name: {data['label']}\n"
                f"💰 Amount: ₹{data['amount']}\n"
                f"📦 Deliverables:\n{items}",
                parse_mode="Markdown"
            )

    elif action == "edit_name":
        plan_key = state["plan_key"]
        if plan_key in plans:
            plans[plan_key]["label"] = text
            del owner_states[message.from_user.id]
            bot.send_message(message.chat.id,
                f"✅ Name updated to: *{text}*", parse_mode="Markdown")

    elif action == "edit_amount":
        plan_key = state["plan_key"]
        if not text.isdigit():
            bot.send_message(message.chat.id, "❌ Enter a valid number.")
            return
        if plan_key in plans:
            plans[plan_key]["amount"] = int(text)
            del owner_stat
