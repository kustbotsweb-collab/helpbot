import os
import urllib.request
import urllib.error
import json
import time
import re
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, HTTPServer

# ==========================================
# 1. CONFIGURATION & ENVIRONMENT
# ==========================================
TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "https://yourdomain.com/webhook")
PORT = int(os.environ.get("PORT", 8080))

API_URL = f"https://api.telegram.org/bot{TOKEN}/"

# Default settings applied immediately when the bot is added to a new group
chat_settings = defaultdict(lambda: {
    "antilink": True,
    "antispam": True,
    "antiflood": True,
    "max_warnings": 3,
    "welcome_msg": "Welcome to the group, {name}!"
})

# In-memory storage
user_warnings = defaultdict(lambda: defaultdict(int))
flood_tracker = defaultdict(lambda: defaultdict(list))
chat_filters = defaultdict(dict)  # chat_id -> {word: reply_text}
chat_notes = defaultdict(dict)    # chat_id -> {note_name: note_text}
chat_rules = defaultdict(str)     # chat_id -> rules_text

FLOOD_LIMIT = 5      # Max messages allowed...
FLOOD_TIMEFRAME = 4  # ...in this many seconds
SPAM_KEYWORDS = ["crypto investment", "free crypto", "giveaway", "buy followers", "t.me/"]

# ==========================================
# 2. RAW HTTP API ENGINE
# ==========================================
def api_call(method, payload=None):
    """Handles raw HTTP requests to the Telegram API."""
    url = API_URL + method
    try:
        if payload:
            data = json.dumps(payload).encode('utf-8')
            req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
        else:
            req = urllib.request.Request(url)
            
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.loads(response.read().decode('utf-8'))
    except Exception as e:
        print(f"[API Error] {method}: {e}")
        return None

# ==========================================
# 3. HELPER FUNCTIONS
# ==========================================
def send_message(chat_id, text, reply_to=None, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    if reply_markup:
        payload["reply_markup"] = reply_markup
    api_call("sendMessage", payload)

def delete_message(chat_id, message_id):
    api_call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

def is_admin(chat_id, user_id):
    res = api_call("getChatMember", {"chat_id": chat_id, "user_id": user_id})
    if res and res.get("ok"):
        return res["result"]["status"] in ["administrator", "creator"]
    return False

def restrict_user(chat_id, user_id, until_date=0, mute=True):
    permissions = {
        "can_send_messages": not mute,
        "can_send_audios": not mute,
        "can_send_documents": not mute,
        "can_send_photos": not mute,
        "can_send_videos": not mute,
        "can_send_other_messages": not mute
    }
    payload = {
        "chat_id": chat_id, 
        "user_id": user_id, 
        "permissions": permissions
    }
    if until_date > 0:
        payload["until_date"] = int(time.time()) + until_date
    api_call("restrictChatMember", payload)

def apply_strike(chat_id, user_id, user_name, reason):
    settings = chat_settings[chat_id]
    user_warnings[chat_id][user_id] += 1
    strikes = user_warnings[chat_id][user_id]
    
    if strikes >= settings["max_warnings"]:
        api_call("banChatMember", {"chat_id": chat_id, "user_id": user_id})
        send_message(chat_id, f"🚫 {user_name} has been banned. Reason: Reached max warnings ({reason}).")
        user_warnings[chat_id][user_id] = 0
    else:
        send_message(chat_id, f"⚠️ {user_name}, warning {strikes}/{settings['max_warnings']}. Reason: {reason}")

def parse_time(time_str):
    """Converts a string like 10m, 2h, 1d into seconds."""
    match = re.match(r"(\d+)([smhd])", time_str.lower())
    if not match:
        return 0
    val, unit = int(match.group(1)), match.group(2)
    if unit == 's': return val
    elif unit == 'm': return val * 60
    elif unit == 'h': return val * 3600
    elif unit == 'd': return val * 86400
    return 0

# ==========================================
# 4. ANTI-SPAM & MESSAGE PROCESSING LOGIC
# ==========================================
def process_message(message):
    chat_id = message["chat"]["id"]
    user_id = message["from"]["id"]
    user_name = message["from"].get("first_name", "User")
    text = message.get("text", "") or message.get("caption", "")
    message_id = message["message_id"]
    settings = chat_settings[chat_id]

    if is_admin(chat_id, user_id) or message["from"].get("is_bot"):
        return False

    if settings["antilink"]:
        entities = message.get("entities", []) + message.get("caption_entities", [])
        if any(ent["type"] in ["url", "text_link", "mention"] for ent in entities):
            delete_message(chat_id, message_id)
            apply_strike(chat_id, user_id, user_name, "Posting Links (Link protection by @linkremoverlbot)")
            return True

    if settings["antispam"]:
        text_lower = text.lower()
        if any(keyword in text_lower for keyword in SPAM_KEYWORDS):
            delete_message(chat_id, message_id)
            apply_strike(chat_id, user_id, user_name, "Blacklisted Keywords (Abuse protection by @abuseblockerbot)")
            return True

    if settings["antiflood"]:
        now = time.time()
        user_history = flood_tracker[chat_id][user_id]
        user_history.append(now)
        user_history = [ts for ts in user_history if now - ts <= FLOOD_TIMEFRAME]
        flood_tracker[chat_id][user_id] = user_history
        
        if len(user_history) > FLOOD_LIMIT:
            delete_message(chat_id, message_id)
            restrict_user(chat_id, user_id, until_date=300, mute=True)
            send_message(chat_id, f"🔇 {user_name} muted for 5 minutes for flooding.")
            flood_tracker[chat_id][user_id] = []
            return True

    if text:
        words = text.lower().split()
        for word in words:
            if word in chat_filters[chat_id]:
                send_message(chat_id, chat_filters[chat_id][word], reply_to=message_id)
                break 

    return False

# ==========================================
# 5. COMMAND HANDLER
# ==========================================
def handle_command(message):
    chat_id = message["chat"]["id"]
    chat_type = message["chat"]["type"]
    user_id = message["from"]["id"]
    user_name = message["from"].get("first_name", "User")
    raw_text = message.get("text", "")
    text_args = raw_text.split()
    cmd = text_args[0].lower().split("@")[0] # Strip bot username if present
    
    reply_to = message.get("reply_to_message")
    target_id = reply_to["from"]["id"] if reply_to else None
    target_name = reply_to["from"].get("first_name", "User") if reply_to else None
    
    # ----------------------------------------
    # PRIVATE DM ONLY COMMANDS
    # ----------------------------------------
    if chat_type == "private":
        if cmd == "/start":
            welcome_text = (
                f"👋 **Hello, {user_name}!**\n\n"
                f"> I am an advanced, plug-and-play **Group Management Bot** engineered to keep your community safe with zero setup configuration required.\n\n"
                f"🛡️ **How to protect your chat:**\n"
                f"1. Add me to your Telegram Group.\n"
                f"2. Promote me to **Administrator**.\n"
                f"3. Grant me **Delete Messages** & **Ban Users** rights.\n\n"
                f"Once promoted, my active shields immediately go online!"
            )
            buttons = {
                "inline_keyboard": [
                    [{"text": "🟢 Add Me to Your Group", "url": "https://t.me/kustguardbot?startgroup=true"}],
                    [{"text": "🔵 View Command Manual", "callback_data": "help_manual"}]
                ]
            }
            # Fallback if bot username isn't resolved gracefully in pure webhooks
            if "kustguardbot" in buttons["inline_keyboard"][0][0]["url"] and "entities" in message:
                for ent in message.get("entities", []):
                    if ent["type"] == "bot_command" and "/start" in raw_text:
                        pass
            send_message(chat_id, welcome_text, reply_markup=buttons)
            return

        elif cmd == "/help":
            help_text = (
                "🛠 **Complete User Command Manual**\n\n"
                "**✨ Public Commands (Anyone Can Use):**\n"
                "• `/staff` — View list of group administrators.\n"
                "• `/rules` — Read current group guidelines.\n"
                "• `/id` — Fetch your user ID and active chat ID.\n"
                "• `/info` — View analytical account metadata.\n"
                "• `/notes` — Look at all saved items.\n"
                "• `/note [name]` — Open a saved notebook entry.\n\n"
                "**🛡️ Administration Actions (Replying to Target):**\n"
                "• `/ban` | `/tban [time]` — Remove users permanently/temporarily.\n"
                "• `/unban` — Forgive user and allow re-entry.\n"
                "• `/mute` | `/tmute [time]` — Restrict chat communication access.\n"
                "• `/unmute` — Re-enable transmission permissions.\n"
                "• `/kick` — Evict member out from the group space.\n"
                "• `/warn` | `/unwarn` — Add or modify user warnings tally.\n"
                "• `/purge` — Rapid clean history down to the target entry.\n"
                "• `/del` — Strike away the selected message.\n"
                "• `/pin` | `/unpin` — Stick vital post assets down to top header.\n\n"
                "**⚙️ Security Configurations (Group Admins):**\n"
                "• `/settings` — Display live operating anti-spam state.\n"
                "• `/lock` | `/unlock` — Mute global conversation stream dynamically.\n"
                "• `/setwelcome [text]` — Update new entry greeting layouts.\n"
                "• `/setrules [text]` — Change structural policy data values.\n"
                "• `/save [name] [text]` — Map an quick recall asset note entry.\n"
                "• `/filter [word] [reply]` — Build an instant text-trigger array.\n"
                "• `/set_antilink [on/off]` — Regulate link deletion shields.\n"
                "• `/set_antispam [on/off]` — Override text abuse processors.\n"
                "• `/set_antiflood [on/off]` — Modify instant high frequency filters."
            )
            send_message(chat_id, help_text)
            return

    # ----------------------------------------
    # UNIVERSAL PUBLIC COMMANDS (GROUPS & DMs)
    # ----------------------------------------
    if cmd == "/id":
        reply = f"📊 **System Identifiers:**\n\n> **Active Chat ID:** `{chat_id}`\n> **Your Account ID:** `{user_id}`"
        if target_id:
            reply += f"\n> **Target User ID:** `{target_id}`"
        send_message(chat_id, reply)
        return

    elif cmd == "/info":
        if target_id:
            send_message(chat_id, f"👤 **Target Profile Information:**\n\n> **First Name:** {target_name}\n> **Account ID:** `{target_id}`")
        else:
            send_message(chat_id, f"👤 **Your Profile Information:**\n\n> **First Name:** {user_name}\n> **Account ID:** `{user_id}`")
        return

    elif cmd == "/rules":
        rules = chat_rules.get(chat_id, "") if chat_type != "private" else "Rules can only be queried inside structural group nodes."
        if chat_type != "private":
            if rules:
                send_message(chat_id, f"📜 **Active Group Rules:**\n\n{rules}")
            else:
                send_message(chat_id, "ℹ️ **Notice:** No rules have been defined for this group node yet.")
        else:
            send_message(chat_id, f"ℹ️ {rules}")
        return

    elif cmd == "/note" and len(text_args) > 1:
        note_name = text_args[1].lower()
        if note_name in chat_notes[chat_id]:
            send_message(chat_id, f"📝 **Notebook Entry [{note_name}]:**\n\n{chat_notes[chat_id][note_name]}")
        else:
            send_message(chat_id, f"⚠️ **Error:** Note vector reference '{note_name}' does not exist.")
        return

    elif cmd == "/notes":
        if not chat_notes[chat_id]:
            send_message(chat_id, "ℹ️ **Notice:** No custom data notes saved inside this chat instance.")
        else:
            notes_list = "\n".join([f"• `{name}`" for name in chat_notes[chat_id].keys()])
            send_message(chat_id, f"📝 **Saved Data Notes Index:**\n\n{notes_list}\n\nRecall item via `/note [name]`")
        return

    elif cmd in ["/staff", "/admins"]:
        if chat_type == "private":
            send_message(chat_id, "⚠️ **Error:** Staff indexing commands cannot be initialized within private profiles.")
            return
        admins = api_call("getChatAdministrators", {"chat_id": chat_id})
        if admins and admins.get("ok"):
            admin_list = [f"👑 {a['user'].get('first_name', 'Admin')}" for a in admins["result"]]
            send_message(chat_id, "🛡️ **Active Group Staff Members:**\n\n" + "\n".join(admin_list))
        return

    # ----------------------------------------
    # SECURITY CONTROL WALL (GROUPS ONLY)
    # ----------------------------------------
    if chat_type == "private":
        if cmd in ["/ban", "/tban", "/unban", "/mute", "/tmute", "/unmute", "/kick", "/warn", "/unwarn", "/purge", "/del", "/pin", "/unpin", "/settings", "/setwelcome", "/setrules", "/settitle", "/setdesc", "/lock", "/unlock", "/filter", "/stop", "/save", "/set_antilink", "/set_antispam", "/set_antiflood"]:
            send_message(chat_id, "⚠️ **Management Failure:** Administrative moderation instructions must be executed inside your managed target groups.")
        return

    if not is_admin(chat_id, user_id):
        return

    # ----------------------------------------
    # ADMIN GROUP MANAGEMENT SYSTEM
    # ----------------------------------------
    if cmd == "/settings":
        s = chat_settings[chat_id]
        status = (
            f"🛠 **Core Security Module Metrics:**\n\n"
            f"> **Anti-Link Shield:** `{s['antilink']}` *(Managed by @linkremoverlbot framework)*\n"
            f"> **Anti-Spam Filter:** `{s['antispam']}` *(Monitored by @abuseblockerbot infrastructure)*\n"
            f"> **Anti-Flood Engine:** `{s['antiflood']}`\n"
            f"> **Warning Strike Threshold:** `{s['max_warnings']}`\n\n"
            f"💡 *No parameters need configurations. Protection modules run automatically on setup.*"
        )
        send_message(chat_id, status)
    
    elif cmd in ["/set_antilink", "/set_antispam", "/set_antiflood"]:
        if len(text_args) > 1 and text_args[1].lower() in ["on", "off"]:
            setting_key = cmd.replace("/set_", "")
            chat_settings[chat_id][setting_key] = (text_args[1].lower() == "on")
            send_message(chat_id, f"✅ **Success:** Module `{setting_key.capitalize()}` configuration altered to **{text_args[1].upper()}**.")
        else:
            send_message(chat_id, f"ℹ️ **Usage Format:** `{cmd} [on/off]`")

    elif cmd == "/setwelcome":
        if len(text_args) > 1:
            chat_settings[chat_id]["welcome_msg"] = raw_text.split(maxsplit=1)[1]
            send_message(chat_id, "✅ **Success:** Global new member welcome template cached.")
        else:
            send_message(chat_id, "ℹ️ **Usage Format:** `/setwelcome Hello {name}, welcome to our community!`")

    elif cmd == "/setrules":
        if len(text_args) > 1:
            chat_rules[chat_id] = raw_text.split(maxsplit=1)[1]
            send_message(chat_id, "✅ **Success:** System structural guidelines database updated.")
        else:
            send_message(chat_id, "ℹ️ **Usage Format:** `/setrules [Write rule strings here]`")

    elif cmd == "/settitle":
        if len(text_args) > 1:
            new_title = raw_text.split(maxsplit=1)[1]
            api_call("setChatTitle", {"chat_id": chat_id, "title": new_title})
            send_message(chat_id, f"✅ **Success:** Chat identity modified to: **{new_title}**")
        else:
            send_message(chat_id, "ℹ️ **Usage Format:** `/settitle [New Group Name]`")

    elif cmd in ["/setdescription", "/setdesc"]:
        if len(text_args) > 1:
            new_desc = raw_text.split(maxsplit=1)[1]
            api_call("setChatDescription", {"chat_id": chat_id, "description": new_desc})
            send_message(chat_id, "✅ **Success:** Meta information index updated description field.")
        else:
            send_message(chat_id, "ℹ️ **Usage Format:** `/setdesc [Write description payload]`")

    elif cmd == "/lock":
        api_call("setChatPermissions", {"chat_id": chat_id, "permissions": {
            "can_send_messages": False, "can_send_audios": False, "can_send_documents": False,
            "can_send_photos": False, "can_send_videos": False, "can_send_other_messages": False
        }})
        send_message(chat_id, "🔒 **Channel Locked:** Global communication pipeline frozen. Only administrators can transmit packages.")

    elif cmd == "/unlock":
        api_call("setChatPermissions", {"chat_id": chat_id, "permissions": {
            "can_send_messages": True, "can_send_audios": True, "can_send_documents": True,
            "can_send_photos": True, "can_send_videos": True, "can_send_other_messages": True,
            "can_add_web_page_previews": True, "can_invite_users": True, "can_send_polls": True
        }})
        send_message(chat_id, "🔓 **Channel Unlocked:** Global transmission paths restored. Public users can interact freely.")

    elif cmd == "/filter":
        if len(text_args) > 2:
            word = text_args[1].lower()
            reply = raw_text.split(maxsplit=2)[2]
            chat_filters[chat_id][word] = reply
            send_message(chat_id, f"✅ **Success:** Text regex intercept routine created for key value: '{word}'.")
        else:
            send_message(chat_id, "ℹ️ **Usage Format:** `/filter [trigger_word] [automated_response_text]`")

    elif cmd == "/stop":
        if len(text_args) > 1:
            word = text_args[1].lower()
            if word in chat_filters[chat_id]:
                del chat_filters[chat_id][word]
                send_message(chat_id, f"✅ **Success:** Automated intercept dictionary element '{word}' dropped.")
        else:
            send_message(chat_id, "ℹ️ **Usage Format:** `/stop [trigger_word]`")

    elif cmd == "/save":
        if len(text_args) > 2:
            note_name = text_args[1].lower()
            note_text = raw_text.split(maxsplit=2)[2]
            chat_notes[chat_id][note_name] = note_text
            send_message(chat_id, f"✅ **Success:** Entry '{note_name}' safely synchronized down to database notes matrix.")
        else:
            send_message(chat_id, "ℹ️ **Usage Format:** `/save [note_name] [content_data_stream]`")

    # ----------------------------------------
    # ADMINISTRATIVE ACTION MATRIX (REPLY REQUIRED)
    # ----------------------------------------
    elif reply_to:
        if cmd == "/del":
            delete_message(chat_id, reply_to["message_id"])
            delete_message(chat_id, message["message_id"])

        elif cmd == "/pin":
            api_call("pinChatMessage", {"chat_id": chat_id, "message_id": reply_to["message_id"]})
            send_message(chat_id, "📌 **System Info:** Important thread element pinned permanently to top viewport panel.")

        elif cmd == "/unpin":
            api_call("unpinChatMessage", {"chat_id": chat_id, "message_id": reply_to["message_id"]})
            send_message(chat_id, "📌 **System Info:** Thread element released from the top sticky viewport panel.")

        elif cmd == "/purge":
            start_id = reply_to["message_id"]
            end_id = message["message_id"]
            message_ids = list(range(start_id, end_id + 1))
            
            for i in range(0, len(message_ids), 100):
                batch = message_ids[i:i+100]
                api_call("deleteMessages", {"chat_id": chat_id, "message_ids": batch})
            
            send_message(chat_id, f"🧹 **Purge Complete:** Cleaned out `{len(message_ids)}` timeline entries safely.")

        elif target_id:
            if cmd == "/ban":
                api_call("banChatMember", {"chat_id": chat_id, "user_id": target_id})
                send_message(chat_id, f"🔨 **Punishment Engine:** Profile {target_name} permanently dropped from community index.")

            elif cmd == "/tban" and len(text_args) > 1:
                duration = parse_time(text_args[1])
                api_call("banChatMember", {"chat_id": chat_id, "user_id": target_id, "until_date": int(time.time()) + duration})
                send_message(chat_id, f"🔨 **Punishment Engine:** Profile {target_name} temporarily banned for standard window: `{text_args[1]}`.")

            elif cmd == "/unban":
                api_call("unbanChatMember", {"chat_id": chat_id, "user_id": target_id, "only_if_banned": True})
                send_message(chat_id, f"✅ **Pardon Engine:** Target credentials for {target_name} restored. Re-entry route cleared.")

            elif cmd == "/kick":
                api_call("banChatMember", {"chat_id": chat_id, "user_id": target_id})
                api_call("unbanChatMember", {"chat_id": chat_id, "user_id": target_id})
                send_message(chat_id, f"👢 **Eviction Engine:** Member {target_name} expelled from group architecture nodes.")

            elif cmd == "/mute":
                restrict_user(chat_id, target_id, until_date=0, mute=True)
                send_message(chat_id, f"🔇 **Mute Engine:** Account transmission link for {target_name} killed indefinitely.")

            elif cmd == "/tmute" and len(text_args) > 1:
                duration = parse_time(text_args[1])
                restrict_user(chat_id, target_id, until_date=duration, mute=True)
                send_message(chat_id, f"🔇 **Mute Engine:** Account transmission link for {target_name} suspended for timeframe duration: `{text_args[1]}`.")

            elif cmd == "/unmute":
                restrict_user(chat_id, target_id, mute=False)
                send_message(chat_id, f"🔊 **Mute Engine:** Voice connection pipes reopened. User {target_name} can interact again.")

            elif cmd == "/warn":
                reason = " ".join(text_args[1:]) if len(text_args) > 1 else "Unspecified structural violation."
                apply_strike(chat_id, target_id, target_name, reason)

            elif cmd == "/unwarn":
                if user_warnings[chat_id][target_id] > 0:
                    user_warnings[chat_id][target_id] -= 1
                    send_message(chat_id, f"✅ **Strike System:** Violation metric count adjusted down for player: {target_name}.")

            # ----------------------------------------
            # SPECIAL PROMOTIONS / ACCREDITATIONS
            # ----------------------------------------
            elif cmd in ["/promote", "/fullpromote", "/lowpromote"]:
                perms = {
                    "is_anonymous": False,
                    "can_manage_chat": True,
                    "can_delete_messages": True,
                    "can_invite_users": True,
                    "can_pin_messages": True,
                    "can_manage_video_chats": False,
                    "can_restrict_members": False,
                    "can_change_info": False,
                    "can_promote_members": False,
                    "can_manage_topics": False
                }
                
                if cmd in ["/promote", "/fullpromote"]:
                    perms["can_restrict_members"] = True
                    perms["can_change_info"] = True
                    perms["can_manage_video_chats"] = True
                    
                if cmd == "/fullpromote":
                    perms["can_promote_members"] = True
                    perms["can_manage_topics"] = True

                payload = {"chat_id": chat_id, "user_id": target_id, **perms}
                res = api_call("promoteChatMember", payload)
                
                if res and res.get("ok"):
                    send_message(chat_id, f"🛡️ **Security Database:** Account {target_name} elevated to level tier privilege access: `{cmd}`.")
                else:
                    send_message(chat_id, "❌ **API Failure:** Promotion assignment rejected. Verify internal bot access privileges.")

            elif cmd == "/demote":
                perms = {k: False for k in [
                    "can_manage_chat", "can_delete_messages", "can_invite_users", 
                    "can_pin_messages", "can_manage_video_chats", "can_restrict_members", 
                    "can_change_info", "can_promote_members", "can_manage_topics"
                ]}
                perms["is_anonymous"] = False
                
                payload = {"chat_id": chat_id, "user_id": target_id, **perms}
                res = api_call("promoteChatMember", payload)
                
                if res and res.get("ok"):
                    send_message(chat_id, f"📉 **Security Database:** Admin authentication privileges stripped from target: {target_name}.")
                else:
                    send_message(chat_id, "❌ **API Failure:** Revocation sequence aborted. Verify internal bot clearance access hierarchy structures.")

# ==========================================
# 6. WEBHOOK HTTP SERVER ENGINE
# ==========================================
class TelegramWebhookHandler(BaseHTTPRequestHandler):
    
    def do_GET(self):
        """Render architecture gateway ping endpoint validation loop."""
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Telegram Bot Engine is running perfectly on Render!")

    def do_POST(self):
        """Listens for JSON updates sent securely from Telegram servers."""
        content_length = int(self.headers['Content-Length'])
        post_data = self.rfile.read(content_length)
        
        # Acknowledge Telegram instantly with 200 OK to prevent duplicate update transmissions
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({"status": "ok"}).encode('utf-8'))
        
        try:
            update = json.loads(post_data.decode('utf-8'))
            
            # Handle inline menu button clicks inside DMs safely
            if "callback_query" in update:
                cb = update["callback_query"]
                if cb.get("data") == "help_manual":
                    # Re-route manually requested inline query data down to the structural manual array
                    pseudo_msg = {
                        "chat": cb["message"]["chat"],
                        "from": cb["from"],
                        "text": "/help",
                        "message_id": cb["message"]["message_id"]
                    }
                    handle_command(pseudo_msg)
                return

            if "message" in update:
                msg = update["message"]
                chat_type = msg["chat"]["type"]
                
                # Global Context: Direct Messages Configuration Matrix
                if chat_type == "private":
                    if msg.get("text", "").startswith("/"):
                        handle_command(msg)
                
                # Context Node Framework: Managed Structural Group Layout Arrays
                elif chat_type in ["group", "supergroup"]:
                    if "new_chat_members" in msg:
                        chat_id = msg["chat"]["id"]
                        welcome_msg = chat_settings[chat_id]["welcome_msg"]
                        if welcome_msg:
                            for member in msg["new_chat_members"]:
                                name = member.get("first_name", "User")
                                send_message(chat_id, welcome_msg.replace("{name}", name))
                    elif msg.get("text", "").startswith("/"):
                        handle_command(msg)
                    else:
                        process_message(msg)
        except Exception as e:
            print(f"[Webhook Routing Crash Matrix Error]: {e}")

    def log_message(self, format, *args):
        # Mute simple standard log formats to optimize operational runtime output windows on Render
        return

def main():
    if not TOKEN or TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("❌ Error: BOT_TOKEN environment variable target configurations missing.")
        return
        
    print(f"Initializing target gateway path configuration map across node: {WEBHOOK_URL}...")
    
    webhook_setup = api_call("setWebhook", {"url": WEBHOOK_URL})
    if webhook_setup and webhook_setup.get("ok"):
        print(f"✅ Webhook integration verification mapped correctly down to structural endpoints!")
    else:
        print(f"❌ Critical Error: Target endpoint mapping deployment aborted: {webhook_setup}")
        return

    server_address = ('0.0.0.0', PORT)
    httpd = HTTPServer(server_address, TelegramWebhookHandler)
    print(f"🚀 Deployment Initialization Finalized. Interface running operational on bind port {PORT}...")
    
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nTermination routine initiated. Closing socket stream components safely...")
        httpd.server_close()

if __name__ == "__main__":
    main()
