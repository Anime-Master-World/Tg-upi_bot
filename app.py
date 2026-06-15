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

    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("💳 Set UPI ID",        callback_data="set_upi"),
        types.InlineKeyboardButton("👤 Set Verified Names", callback_data="set_names"),
        types.InlineKeyboardButton("🔙 Back",               callback_data="owner_back"),
    )
    bot.edit_message_text(
        f"⚙️ *Bot Settings*\n\n"
        f"💳 UPI ID: `{upi}`\n"
        f"👤 Verified Names: {names}",
        call.message.chat.id, call.message.message_id,
        parse_mode="Markdown", reply_markup=markup
    )

@bot.callback_query_handler(func=lambda c: c.data == "set_upi")
def set_upi(call):
    if not is_owner(call.message.chat.id): return
    owner_states[call.from_user.id] = {"action": "set_upi"}
    bot.send_message(call.message.chat.id,
        "💳 *Set UPI ID*\n\nSend your UPI ID:\n_(e.g. yourname@bank)_",
        parse_mode="Markdown")

@bot.callback_query_handler(func=lambda c: c.data == "set_names")
def set_names(call):
    if not is_owner(call.message.chat.id): return
    owner_states[call.from_user.id] = {"action": "set_names"}
    bot.send_message(call.message.chat.id,
        "👤 *Set Verified Names*\n\n"
        "Send names/UPI IDs (comma separated) that should match the "
        "*recipient* field for auto-approval:\n"
        "_(e.g. Nithesh Kumar, nitheshkumar05@fam)_",
        parse_mode="Markdown")

# ══════════════════════════════════════════════════════════════
# OWNER TEXT INPUT (general)
# ══════════════════════════════════════════════════════════════
@bot.message_handler(
    func=lambda m: m.from_user.id in owner_states
        and is_owner(m.chat.id)
        and owner_states[m.from_user.id]["action"] not in ["add_plan_channel", "edit_channel"],
    content_types=["text"]
)
def handle_owner_input(message):
    state = owner_states.get(message.from_user.id)
    print(f"OWNER STATE DEBUG: {state}")
    if not state:
        return

    action = state["action"]
    text = message.text.strip()

    # ── ADD PLAN FLOW ─────────────────────────────────────────
    if action == "add_plan":
        step = state["step"]
        data = state.get("data", {})

        if step == "name":
            data["label"] = text
            state["step"] = "amount"
            state["data"] = data
            bot.send_message(message.chat.id, "💰 Step 2/4: Enter the amount (₹):")

        elif step == "amount":
            clean = text.replace("₹", "").replace(",", "").strip()
            if not clean.isdigit():
                bot.send_message(message.chat.id, "❌ Enter a valid number.")
                return
            data["amount"] = int(clean)
            state["step"] = "duration"
            state["data"] = data
            bot.send_message(message.chat.id,
                "⏳ Step 3/4: Enter validity duration in *days*:\n_(e.g. 30)_",
                parse_mode="Markdown")

        elif step == "duration":
            clean = text.strip()
            if not clean.isdigit():
                bot.send_message(message.chat.id, "❌ Enter a valid number of days.")
                return
            data["duration_days"] = int(clean)
            state["step"] = "channel"
            state["data"] = data
            owner_states[message.from_user.id] = {"action": "add_plan_channel", "data": data}
            bot.send_message(message.chat.id,
                "📢 Step 4/4: Set the channel for this plan.\n\n"
                "➡️ Easiest: forward any message from the channel to me here.\n"
                "➡️ Or send the channel ID directly (starts with `-100`).\n\n"
                "⚠️ Bot must be admin in that channel with "
                "*Invite Users* and *Ban Users* permissions.",
                parse_mode="Markdown")

    # ── EDIT NAME ─────────────────────────────────────────────
    elif action == "edit_name":
        plan_key = state["plan_key"]
        if plan_key in plans:
            plans[plan_key]["label"] = text
            del owner_states[message.from_user.id]
            bot.send_message(message.chat.id,
                f"✅ Name updated to: *{text}*", parse_mode="Markdown")
        else:
            del owner_states[message.from_user.id]
            bot.send_message(message.chat.id, "❌ Plan no longer exists.")

    # ── EDIT AMOUNT ───────────────────────────────────────────
    elif action == "edit_amount":
        plan_key = state["plan_key"]
        clean = text.replace("₹", "").replace(",", "").strip()
        if not clean.isdigit():
            bot.send_message(message.chat.id, "❌ Enter a valid number (e.g. 199).")
            return
        if plan_key in plans:
            plans[plan_key]["amount"] = int(clean)
            del owner_states[message.from_user.id]
            bot.send_message(message.chat.id,
                f"✅ Amount updated to: *₹{clean}*", parse_mode="Markdown")
        else:
            del owner_states[message.from_user.id]
            bot.send_message(message.chat.id, "❌ Plan no longer exists.")

    # ── EDIT DURATION ─────────────────────────────────────────
    elif action == "edit_duration":
        plan_key = state["plan_key"]
        clean = text.strip()
        if not clean.isdigit():
            bot.send_message(message.chat.id, "❌ Enter a valid number of days.")
            return
        if plan_key in plans:
            plans[plan_key]["duration_days"] = int(clean)
            del owner_states[message.from_user.id]
            bot.send_message(message.chat.id,
                f"✅ Duration updated to: *{clean} days*", parse_mode="Markdown")
        else:
            del owner_states[message.from_user.id]
            bot.send_message(message.chat.id, "❌ Plan no longer exists.")

    # ── SET UPI ID ────────────────────────────────────────────
    elif action == "set_upi":
        bot_settings["upi_id"] = text
        del owner_states[message.from_user.id]
        bot.send_message(message.chat.id,
            f"✅ UPI ID set to: `{text}`", parse_mode="Markdown")

    # ── SET VERIFIED NAMES ────────────────────────────────────
    elif action == "set_names":
        names = [n.strip() for n in text.split(",") if n.strip()]
        bot_settings["verified_names"] = names
        del owner_states[message.from_user.id]
        bot.send_message(message.chat.id,
            f"✅ Verified names set to:\n{', '.join(names) if names else 'None'}")

    else:
        del owner_states[message.from_user.id]
        bot.send_message(message.chat.id, "⚠️ Session reset. Send /admin to continue.")

# ══════════════════════════════════════════════════════════════
# OWNER: CHANNEL SETUP (forwarded message or -100 ID)
# ══════════════════════════════════════════════════════════════
@bot.message_handler(
    func=lambda m: m.from_user.id in owner_states
        and is_owner(m.chat.id)
        and owner_states[m.from_user.id]["action"] in ["add_plan_channel", "edit_channel"],
    content_types=["text", "photo", "video", "document", "audio", "voice", "sticker", "animation"]
)
def handle_channel_input(message):
    state = owner_states[message.from_user.id]
    channel_id = None

    if message.forward_from_chat:
        channel_id = str(message.forward_from_chat.id)
    elif message.text and message.text.strip().startswith("-100"):
        channel_id = message.text.strip()

    if not channel_id:
        bot.send_message(message.chat.id,
            "❌ Forward a message from the channel, or send the channel ID starting with `-100`.",
            parse_mode="Markdown")
        return

    # Verify bot is admin in that channel
    try:
        bot_member = bot.get_chat_member(channel_id, bot.get_me().id)
        if bot_member.status not in ["administrator", "creator"]:
            bot.send_message(message.chat.id,
                "⚠️ The bot must be an *admin* in that channel with "
                "*Invite Users* and *Ban Users* permissions.",
                parse_mode="Markdown")
            return
    except Exception as e:
        bot.send_message(message.chat.id,
            f"❌ Could not access that channel: {str(e)}\n"
            f"Make sure the bot is added as admin there.")
        return

    if state["action"] == "add_plan_channel":
        data = state["data"]
        plan_key = f"plan_{uuid.uuid4().hex[:6]}"
        plans[plan_key] = {
            "label": data["label"],
            "amount": data["amount"],
            "duration_days": data["duration_days"],
            "channel_id": channel_id
        }
        del owner_states[message.from_user.id]
        bot.send_message(message.chat.id,
            f"✅ *Plan Created!*\n\n"
            f"🔑 `{plan_key}`\n"
            f"📌 {data['label']}\n"
            f"💰 ₹{data['amount']}\n"
            f"⏳ {data['duration_days']} days\n"
            f"📢 Channel: `{channel_id}`",
            parse_mode="Markdown")

    elif state["action"] == "edit_channel":
        plan_key = state["plan_key"]
        if plan_key in plans:
            plans[plan_key]["channel_id"] = channel_id
            del owner_states[message.from_user.id]
            bot.send_message(message.chat.id,
                f"✅ Channel updated to: `{channel_id}`", parse_mode="Markdown")
        else:
            del owner_states[message.from_user.id]
            bot.send_message(message.chat.id, "❌ Plan no longer exists.")

# ══════════════════════════════════════════════════════════════
# SCREENSHOT SCANNING
# ══════════════════════════════════════════════════════════════
def scan_with_groq(img_base64, mime_type):
    ist = timezone(timedelta(hours=5, minutes=30))
    current_time = datetime.now(ist).strftime("%d %b %Y %I:%M %p IST")

    response = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json"
        },
        json={
            "model": "meta-llama/llama-4-scout-17b-16e-instruct",
            "messages": [{
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime_type};base64,{img_base64}"}
                    },
                    {
                        "type": "text",
                        "text": f"""Current date and time is: {current_time}

Analyze this image carefully. Your job is to:
1. Detect if this is a real UPI payment screenshot
2. Extract payment details
3. Check if payment was made within the last 24 hours
4. Detect if screenshot looks fake, edited, or manipulated

Return ONLY this JSON:
{{
  "is_payment_screenshot": true or false,
  "is_fake": true or false,
  "fake_reason": "reason if fake else null",
  "transaction_id": "UTR/transaction ID or null",
  "amount": "amount as number only or null",
  "recipient": "recipient name or UPI ID or null",
  "status": "SUCCESS or FAILED or UNKNOWN",
  "payment_date": "date from screenshot or null",
  "payment_time": "time from screenshot or null",
  "within_24_hours": true or false or null
}}

Fake detection rules:
- Check for mismatched fonts, blur, pixelation around numbers
- Check if logo or bank name looks genuine
- Check if amounts or dates look edited
- If payment date is more than 24 hours ago mark within_24_hours as false

Return ONLY raw JSON. No markdown, no backticks, no explanation."""
                    }
                ]
            }],
            "temperature": 0,
            "max_tokens": 512
        },
        timeout=30
    )

    result = response.json()
    print(f"GROQ FULL RESPONSE: {result}")

    if "error" in result:
        raise Exception(f"Groq error: {result['error'].get('message', 'Unknown')}")
    if "choices" not in result or not result["choices"]:
        raise Exception("Groq returned no choices")

    raw_text = result["choices"][0]["message"]["content"].strip()
    print(f"GROQ RAW TEXT: {raw_text}")
    clean_text = raw_text.replace("```json", "").replace("```", "").strip()
    return json.loads(clean_text)

# ── USER SENDS SCREENSHOT ─────────────────────────────────────
@bot.message_handler(content_types=["photo"])
def handle_screenshot(message):
    try:
        user_id = message.from_user.id

        if user_id not in pending_payments:
            bot.reply_to(message, "⚠️ No pending payment. Use /start to begin.")
            return

        bot.reply_to(message, "🔍 Scanning your payment screenshot...")

        photo = message.photo[-1]
        file_info = bot.get_file(photo.file_id)
        file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}"
        img_response = requests.get(file_url, timeout=10)
        img_base64 = base64.b64encode(img_response.content).decode("utf-8")

        content_type = img_response.headers.get("Content-Type", "image/jpeg")
        if "png" in content_type:
            mime_type = "image/png"
        elif "webp" in content_type:
            mime_type = "image/webp"
        else:
            mime_type = "image/jpeg"

        data = scan_with_groq(img_base64, mime_type)

        # ── NOT A PAYMENT SCREENSHOT ──────────────────────────
        if not data.get("is_payment_screenshot"):
            bot.reply_to(message,
                "❌ *This doesn't look like a payment screenshot.*\n\n"
                "Please send a valid UPI payment screenshot showing:\n"
                "• Transaction ID / UTR number\n"
                "• Amount paid\n"
                "• Recipient name or UPI ID\n"
                "• Payment status",
                parse_mode="Markdown"
            )
            return

        # ── FAKE DETECTION ────────────────────────────────────
        if data.get("is_fake"):
            fake_reason = data.get("fake_reason", "Screenshot appears manipulated")
            bot.reply_to(message,
                f"🚨 *Fake Payment Detected!*\n\n"
                f"Reason: {fake_reason}\n\n"
                f"Please send a genuine payment screenshot.",
                parse_mode="Markdown"
            )
            bot.send_message(OWNER_CHAT_ID,
                f"🚨 *Fake Payment Attempt!*\n\n"
                f"👤 User: {message.from_user.first_name} (@{message.from_user.username or 'N/A'})\n"
                f"🕐 Time: {now_ist()}\n"
                f"⚠️ Reason: {fake_reason}",
                parse_mode="Markdown"
            )
            return

        # ── 24 HOUR CHECK ─────────────────────────────────────
        if data.get("within_24_hours") == False:
            bot.reply_to(message,
                "⏰ *Payment screenshot is older than 24 hours.*\n\n"
                "Please make a fresh payment and send the new screenshot.",
                parse_mode="Markdown"
            )
            return

        txn_id      = data.get("transaction_id", "N/A")
        amount_paid = str(data.get("amount", "N/A"))
        recipient   = str(data.get("recipient", "N/A")).strip()
        status      = data.get("status", "UNKNOWN")
        pay_date    = data.get("payment_date", "N/A")
        pay_time    = data.get("payment_time", "N/A")
        expected    = pending_payments[user_id]["amount"]
        plan_label  = pending_payments[user_id]["plan"]
        plan_key    = pending_payments[user_id]["plan_key"]
        submitted_at = now_ist()

        # ── VERIFY RECIPIENT ──────────────────────────────────
        upi_id = bot_settings.get("upi_id", "") or ""
        verified_names = bot_settings.get("verified_names", [])
        recipient_lower = recipient.lower()

        upi_match = bool(upi_id) and upi_id.lower() in recipient_lower
        name_match = any(n.lower() in recipient_lower for n in verified_names if n)

        # ── VERIFY EXACT AMOUNT ───────────────────────────────
        try:
            paid = float(str(amount_paid).replace(",", "").strip())
            expected_float = float(str(expected))
            amount_match = abs(paid - expected_float) < 0.01
        except:
            amount_match = False

        # ── AUTO APPROVE ONLY IF ALL CONDITIONS MATCH ────────
        auto_verified = (upi_match or name_match) and status == "SUCCESS" and amount_match

        bot.reply_to(message,
            f"✅ *Screenshot Scanned!*\n\n"
            f"📋 *Transaction Details:*\n"
            f"• Transaction ID: `{txn_id}`\n"
            f"• Amount Paid: ₹{amount_paid}\n"
            f"• Status: {status}\n"
            f"• Payment Date: {pay_date}\n"
            f"• Payment Time: {pay_time}\n\n"
            f"⏳ Processing your order...",
            parse_mode="Markdown"
        )

        review_key = str(uuid.uuid4())[:8]
        pending_reviews[review_key] = {
            "user_id":      user_id,
            "user_name":    message.from_user.first_name,
            "username":     message.from_user.username or "N/A",
            "amount_paid":  amount_paid,
            "expected":     expected,
            "txn_id":       txn_id,
            "recipient":    recipient,
            "status":       status,
            "pay_date":     pay_date,
            "pay_time":     pay_time,
            "plan":         plan_label,
            "plan_key":     plan_key,
            "submitted_at": submitted_at,
            "file_id":      photo.file_id
        }

        owner_caption = (
            f"🔔 *New Payment — {'✅ Auto Approved' if auto_verified else '⚠️ Manual Review'}*\n\n"
            f"👤 User: {message.from_user.first_name} (@{message.from_user.username or 'N/A'})\n"
            f"📦 Plan: {plan_label}\n"
            f"💰 Expected: ₹{expected}\n"
            f"💸 Paid: ₹{amount_paid}\n"
            f"🏦 Paid To: {recipient}\n"
            f"🔖 Txn ID: `{txn_id}`\n"
            f"📊 Status: {status}\n"
            f"📅 Payment Date: {pay_date}\n"
            f"🕐 Payment Time: {pay_time}\n"
            f"🕐 Submitted At: {submitted_at}\n"
            f"🔑 Key: `{review_key}`"
        )

        if auto_verified:
            grant_premium(user_id, plan_label, plan_key, review_key,
                username=message.from_user.username)
            bot.send_photo(OWNER_CHAT_ID, photo.file_id,
                caption=owner_caption, parse_mode="Markdown")
        else:
            if not (upi_match or name_match):
                reason = "Recipient not verified"
            elif not amount_match:
                reason = f"Amount mismatch — Expected ₹{expected}, Paid ₹{amount_paid}"
            elif status != "SUCCESS":
                reason = f"Payment status: {status}"
            else:
                reason = "Manual review required"

            markup = types.InlineKeyboardMarkup()
            markup.row(
                types.InlineKeyboardButton("✅ Approve", callback_data=f"approve_{review_key}"),
                types.InlineKeyboardButton("❌ Decline", callback_data=f"decline_{review_key}")
            )
            bot.send_photo(OWNER_CHAT_ID, photo.file_id,
                caption=f"{owner_caption}\n⚠️ Reason: {reason}",
                parse_mode="Markdown", reply_markup=markup)
            bot.send_message(user_id,
                "⏳ *Your order is under review.*\n\nWe'll confirm shortly.",
                parse_mode="Markdown")

    except Exception as e:
        print(f"SCREENSHOT ERROR: {str(e)}")
        bot.reply_to(message, f"❌ Error scanning screenshot: {str(e)}")

# ── NON-PHOTO ─────────────────────────────────────────────────
@bot.message_handler(content_types=["document", "video", "audio", "sticker", "voice"])
def handle_non_photo(message):
    try:
        if message.from_user.id in pending_payments:
            bot.reply_to(message,
                "⚠️ Please send a *payment screenshot* (image only).",
                parse_mode="Markdown")
    except Exception as e:
        print(f"NON PHOTO ERROR: {str(e)}")

# ══════════════════════════════════════════════════════════════
# GRANT ACCESS — generates one-time channel invite + tracks expiry
# ══════════════════════════════════════════════════════════════
def grant_premium(user_id, plan