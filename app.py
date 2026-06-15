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

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# ── GLOBAL STATE ───────────────────────────────────────────────
plans = {}  # {plan_key: {label, amount, duration_days, channel_id}}

bot_settings = {
    "upi_id": None,           # set by owner
    "verified_names": []      # optional extra names/UPI ids to match for auto-approve
}

pending_payments = {}
pending_reviews  = {}
owner_states     = {}
bot_links        = {}              # {link_id: plan_key}
active_subscriptions = []          # [{user_id, username, channel_id, plan_label, expires_at}]

EXPIRY_CHECK_INTERVAL = 1800  # 30 minutes

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

def ts_to_ist(ts):
    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.fromtimestamp(ts, ist).strftime("%d %b %Y %I:%M %p IST")

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

@bot.message_handler(commands=["cancel"])
def cancel_state(message):
    if message.from_user.id in owner_states:
        del owner_states[message.from_user.id]
        bot.send_message(message.chat.id, "✅ Cancelled. Send /admin to continue.")
    else:
        bot.send_message(message.chat.id, "Nothing to cancel.")

def send_user_menu(chat_id):
    if not plans:
        bot.send_message(chat_id, "👋 *Welcome!*\n\nNo plans available right now. Please check back later.",
            parse_mode="Markdown")
        return
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
        types.InlineKeyboardButton("📋 View Plans",     callback_data="owner_view_plans"),
        types.InlineKeyboardButton("➕ Add Plan",        callback_data="owner_add_plan"),
        types.InlineKeyboardButton("✏️ Edit Plan",      callback_data="owner_edit_plan"),
        types.InlineKeyboardButton("🗑 Delete Plan",    callback_data="owner_delete_plan"),
        types.InlineKeyboardButton("🔗 Create Link",    callback_data="owner_create_link"),
        types.InlineKeyboardButton("📊 View Links",     callback_data="owner_view_links"),
        types.InlineKeyboardButton("🎁 Free Access",    callback_data="owner_free_access"),
        types.InlineKeyboardButton("👥 Subscriptions",  callback_data="owner_subscriptions"),
        types.InlineKeyboardButton("⚙️ Settings",       callback_data="owner_settings"),
    )
    text = "👑 *Owner Panel*\n\nManage plans, channels, and settings."
    if message_id:
        bot.edit_message_text(text, chat_id, message_id,
            parse_mode="Markdown", reply_markup=markup)
    else:
        bot.send_message(chat_id, text,
            parse_mode="Markdown", reply_markup=markup)

@bot.callback_query_handler(func=lambda c: c.data == "owner_back")
def owner_back(call):
    if not is_owner(call.message.chat.id): return
    send_owner_menu(call.message.chat.id, call.message.message_id)

# ══════════════════════════════════════════════════════════════
# USER: SHOW PLANS / BUY
# ══════════════════════════════════════════════════════════════
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
        upi_id = bot_settings.get("upi_id")
        if not upi_id:
            bot.send_message(chat_id,
                "⚠️ Payments are not configured yet. Please contact the owner.")
            try:
                bot.send_message(OWNER_CHAT_ID,
                    "⚠️ A user tried to buy a plan but no UPI ID is set.\n"
                    "Set it via /admin → ⚙️ Settings → 💳 Set UPI ID")
            except:
                pass
            return

        amount = plan["amount"]
        duration = plan.get("duration_days", 30)
        pending_payments[user_id] = {
            "amount": amount,
            "plan": plan["label"],
            "plan_key": plan_key
        }

        upi_url = f"upi://pay?pa={upi_id}&pn=PremiumBot&am={amount}&cu=INR&tn=Purchase"
        qr = qrcode.QRCode(version=1, box_size=10, border=4)
        qr.add_data(upi_url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        bio = BytesIO()
        img.save(bio, format="PNG")
        bio.seek(0)

        bot.send_message(chat_id,
            f"🛒 *{plan['label']}*\n"
            f"💰 Amount: *₹{amount}*\n"
            f"⏳ Validity: *{duration} days*\n\n"
            f"📦 You'll get access to a private channel.\n\n"
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
# OWNER: VIEW / DELETE PLANS
# ══════════════════════════════════════════════════════════════
@bot.callback_query_handler(func=lambda c: c.data == "owner_view_plans")
def owner_view_plans(call):
    if not is_owner(call.message.chat.id): return
    if not plans:
        text = "📋 *No plans yet.* Use ➕ Add Plan to create one."
    else:
        text = "📋 *All Plans:*\n\n"
        for key, plan in plans.items():
            text += (
                f"🔑 `{key}`\n"
                f"📌 {plan['label']}\n"
                f"💰 ₹{plan['amount']}\n"
                f"⏳ {plan.get('duration_days', 'N/A')} days\n"
                f"📢 Channel: `{plan.get('channel_id', 'Not set')}`\n\n"
            )
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="owner_back"))
    bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
        parse_mode="Markdown", reply_markup=markup)

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

# ══════════════════════════════════════════════════════════════
# OWNER: ADD PLAN (name → amount → duration → channel)
# ══════════════════════════════════════════════════════════════
@bot.callback_query_handler(func=lambda c: c.data == "owner_add_plan")
def owner_add_plan(call):
    if not is_owner(call.message.chat.id): return
    owner_states[call.from_user.id] = {"action": "add_plan", "step": "name", "data": {}}
    bot.send_message(call.message.chat.id,
        "➕ *Add New Plan*\n\nStep 1/4: Enter the plan name:\n_(e.g. Gold - 1 Month)_",
        parse_mode="Markdown"
    )

# ══════════════════════════════════════════════════════════════
# OWNER: EDIT PLAN
# ══════════════════════════════════════════════════════════════
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
        types.InlineKeyboardButton("📌 Edit Name",     callback_data=f"edit_name_{plan_key}"),
        types.InlineKeyboardButton("💰 Edit Amount",   callback_data=f"edit_amount_{plan_key}"),
        types.InlineKeyboardButton("⏳ Edit Duration", callback_data=f"edit_duration_{plan_key}"),
        types.InlineKeyboardButton("📢 Edit Channel",  callback_data=f"edit_channel_{plan_key}"),
        types.InlineKeyboardButton("🔙 Back",          callback_data="owner_edit_plan"),
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
        f"💰 Enter new amount (₹) for `{plan_key}`:", parse_mode="Markdown")

@bot.callback_query_handler(func=lambda c: c.data.startswith("edit_duration_"))
def edit_duration(call):
    if not is_owner(call.message.chat.id): return
    plan_key = call.data.replace("edit_duration_", "")
    owner_states[call.from_user.id] = {"action": "edit_duration", "plan_key": plan_key}
    bot.send_message(call.message.chat.id,
        f"⏳ Enter new duration in *days* for `{plan_key}`:\n_(e.g. 30)_",
        parse_mode="Markdown")

@bot.callback_query_handler(func=lambda c: c.data.startswith("edit_channel_"))
def edit_channel(call):
    if not is_owner(call.message.chat.id): return
    plan_key = call.data.replace("edit_channel_", "")
    owner_states[call.from_user.id] = {"action": "edit_channel", "plan_key": plan_key}
    bot.send_message(call.message.chat.id,
        "📢 *Set Channel for this Plan*\n\n"
        "➡️ Easiest: forward any message from the channel to me here.\n"
        "➡️ Or send the channel ID directly (starts with `-100`).\n\n"
        "⚠️ Make sure the bot is added as *admin* in that channel with "
        "*Invite Users* and *Ban Users* permissions.",
        parse_mode="Markdown"
    )

# ══════════════════════════════════════════════════════════════
# OWNER: CREATE / VIEW BOT LINKS
# ══════════════════════════════════════════════════════════════
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
# OWNER: FREE ACCESS
# ══════════════════════════════════════════════════════════════
@bot.callback_query_handler(func=lambda c: c.data == "owner_free_access")
def owner_free_access(call):
    if not is_owner(call.message.chat.id): return
    if not plans:
        bot.answer_callback_query(call.id, "No plans available.")
        return
    markup = types.InlineKeyboardMarkup()
    for key, plan in plans.items():
        markup.add(types.InlineKeyboardButton(
            f"{plan['label']} — ₹{plan['amount']}",
            callback_data=f"freeget_{key}"
        ))
    markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="owner_back"))
    bot.edit_message_text(
        "🎁 *Free Access*\n\nSelect a plan to claim instantly (no payment required):",
        call.message.chat.id, call.message.message_id,
        parse_mode="Markdown", reply_markup=markup
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("freeget_"))
def owner_free_get(call):
    if not is_owner(call.message.chat.id): return
    plan_key = call.data.replace("freeget_", "")
    plan = plans.get(plan_key)
    if not plan:
        bot.answer_callback_query(call.id, "Plan not found.")
        return

    review_key = str(uuid.uuid4())[:8]
    grant_premium(call.from_user.id, plan["label"], plan_key, review_key,
        username=call.from_user.username, track_expiry=False)
    bot.answer_callback_query(call.id, "🎁 Granted!")

# ══════════════════════════════════════════════════════════════
# OWNER: ACTIVE SUBSCRIPTIONS
# ══════════════════════════════════════════════════════════════
@bot.callback_query_handler(func=lambda c: c.data == "owner_subscriptions")
def owner_subscriptions(call):
    if not is_owner(call.message.chat.id): return
    if not active_subscriptions:
        text = "👥 *No active subscriptions.*"
    else:
        text = "👥 *Active Subscriptions:*\n\n"
        for sub in active_subscriptions:
            text += (
                f"👤 {sub.get('username') or sub['user_id']}\n"
                f"📦 {sub['plan_label']}\n"
                f"📢 Channel: `{sub['channel_id']}`\n"
                f"⏳ Expires: {ts_to_ist(sub['expires_at'])}\n\n"
            )
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="owner_back"))
    bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
        parse_mode="Markdown", reply_markup=markup)

# ══════════════════════════════════════════════════════════════
# OWNER: SETTINGS
# ══════════════════════════════════════════════════════════════
@bot.callback_query_handler(func=lambda c: c.data == "owner_settings")
def owner_settings(call):
    if not is_owner(call.message.chat.id): return
    upi = bot_settings.get("upi_id") or "Not set"
    names = ", ".join(bot_settings.get("verified_names", [])) or "None"

    markup = types.Inlin