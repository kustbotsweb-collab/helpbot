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
# Fetching from Environment Variables for Render deployment
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
def send_message(chat_id, text, reply_to=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if reply_to:
        payload["reply_to_message_id"] = reply_to
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

    # Don't check admins or the bot itself
    if is_admin(chat_id, user_id) or message["from"].get("is_bot"):
        return False

    # 1. Anti-Link (Checks Telegram Entities)
    if settings["antilink"]:
        entities = message.get("entities", []) + message.get("caption_entities", [])
        if any(ent["type"] in ["url", "text_link", "mention"] for ent in entities):
            delete_message(chat_id, message_id)
            apply_strike(chat_id, user_id, user_name, "Posting Links (Link protection by @linkremoverlbot)")
            return True

    # 2. Anti-Spam (Keywords)
    if settings["antispam"]:
        text_lower = text.lower()
        if any(keyword in text_lower for keyword in SPAM_KEYWORDS):
            delete_message(chat_id, message_id)
            apply_strike(chat_id, user_id, user_name, "Blacklisted Keywords (Abuse protection by @abuseblockerbot)")
            return True

    # 3. Anti-Flood (Fast message frequency)
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

    # 4. Word Filters (Auto-replies)
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
    user_id = message["from"]["id"]
    user_name = message["from"].get("first_name", "User")
    raw_text = message.get("text", "")
    text_args = raw_text.split()
    cmd = text_args[0].lower()
    
    reply_to = message.get("reply_to_message")
    target_id = reply_to["from"]["id"] if reply_to else None
    target_name = reply_to["from"].get("first_name", "User") if reply_to else None
    
    # ----------------------------------------
    # SECTION A: PUBLIC COMMANDS (ALL USERS)
    # ----------------------------------------
    if cmd == "/help":
        help_text = (
            "🛠 **Group Management Bot Help**\n\n"
            "**Public Commands:**\n"
            "`/staff`, `/notes`, `/note [name]`\n"
            "`/rules`, `/id`, `/info`\n\n"
            "**Admin Commands:**\n"
            "🛡 **Moderation (Reply to user):**\n"
            "`/ban`, `/tban [time]`, `/unban`\n"
            "`/kick`, `/mute`, `/tmute [time]`, `/unmute`\n"
            "`/warn [reason]`, `/unwarn`\n"
            "`/promote`, `/fullpromote`, `/lowpromote`, `/demote`\n"
            "`/purge`, `/del`, `/pin`, `/unpin`\n\n"
            "⚙️ **Group Setup:**\n"
            "`/settings`, `/setwelcome [msg]`\n"
            "`/settitle [title]`, `/setdesc [text]`\n"
            "`/lock`, `/unlock`, `/setrules [rules]`\n"
            "`/filter [word] [reply]`, `/stop [word]`\n"
            "`/save [name] [text]`\n"
            "`/set_antilink [on/off]`, `/set_antispam [on/off]`, `/set_antiflood [on/off]`\n\n"
            "🔗 **Advanced Protections:**\n"
            "For dedicated link removal: Add @linkremoverlbot\n"
            "For deep abuse blocking: Add @abuseblockerbot"
        )
        send_message(chat_id, help_text)
        return

    elif cmd == "/id":
        reply = f"**Chat ID:** `{chat_id}`\n**Your ID:** `{user_id}`"
        if target_id:
            reply += f"\n**Target ID:** `{target_id}`"
        send_message(chat_id, reply)
        return

    elif cmd == "/info":
        if target_id:
            send_message(chat_id, f"👤 **User Info:**\n**Name:** {target_name}\n**ID:** `{target_id}`")
        else:
            send_message(chat_id, f"👤 **User Info:**\n**Name:** {user_name}\n**ID:** `{user_id}`")
        return

    elif cmd == "/rules":
        rules = chat_rules.get(chat_id, "")
        if rules:
            send_message(chat_id, f"📜 **Group Rules:**\n\n{rules}")
        else:
            send_message(chat_id, "No rules have been set for this group yet.")
        return

    elif cmd == "/note" and len(text_args) > 1:
        note_name = text_args[1].lower()
        if note_name in chat_notes[chat_id]:
            send_message(chat_id, chat_notes[chat_id][note_name])
        else:
            send_message(chat_id, f"⚠️ Note '{note_name}' not found.")
        return

    elif cmd == "/notes":
        if not chat_notes[chat_id]:
            send_message(chat_id, "No notes saved in this group.")
        else:
            notes_list = "\n".join([f"- {name}" for name in chat_notes[chat_id].keys()])
            send_message(chat_id, f"📝 **Saved Notes:**\n{notes_list}\n\nUse `/note [name]` to read.")
        return

    elif cmd in ["/staff", "/admins"]:
        admins = api_call("getChatAdministrators", {"chat_id": chat_id})
        if admins and admins.get("ok"):
            admin_list = [f"👑 {a['user'].get('first_name', 'Admin')}" for a in admins["result"]]
            send_message(chat_id, "🛡 **Group Staff:**\n" + "\n".join(admin_list))
        return

    # ----------------------------------------
    # SECTION B: SECURITY BARRIER
    # ----------------------------------------
    if not is_admin(chat_id, user_id):
        return

    # ----------------------------------------
    # SECTION C: ADMIN CONFIGURATION COMMANDS
    # ----------------------------------------
    if cmd == "/settings":
        s = chat_settings[chat_id]
        status = (
            f"🛠 **Group Settings:**\n"
            f"Anti-Link: {s['antilink']} (Uses @linkremoverlbot logic)\n"
            f"Anti-Spam: {s['antispam']} (Uses @abuseblockerbot logic)\n"
            f"Anti-Flood: {s['antiflood']}\n"
            f"Max Warnings: {s['max_warnings']}\n\n"
            f"For dedicated link protection add: @linkremoverlbot\n"
            f"For advanced abuse blocking add: @abuseblockerbot"
        )
        send_message(chat_id, status)
    
    elif cmd in ["/set_antilink", "/set_antispam", "/set_antiflood"]:
        if len(text_args) > 1 and text_args[1].lower() in ["on", "off"]:
            setting_key = cmd.replace("/set_", "")
            chat_settings[chat_id][setting_key] = (text_args[1].lower() == "on")
            send_message(chat_id, f"✅ {setting_key.capitalize()} is now {text_args[1].upper()}.")
        else:
            send_message(chat_id, f"Usage: {cmd} [on/off]")

    elif cmd == "/setwelcome":
        if len(text_args) > 1:
            chat_settings[chat_id]["welcome_msg"] = raw_text.split(maxsplit=1)[1]
            send_message(chat_id, "✅ Welcome message updated. (Use {name} to mention the user).")
        else:
            send_message(chat_id, "Usage: `/setwelcome Hello {name}, welcome!`")

    elif cmd == "/setrules":
        if len(text_args) > 1:
            chat_rules[chat_id] = raw_text.split(maxsplit=1)[1]
            send_message(chat_id, "✅ Group rules updated.")
        else:
            send_message(chat_id, "Usage: `/setrules [rules text]`")

    elif cmd == "/settitle":
        if len(text_args) > 1:
            new_title = raw_text.split(maxsplit=1)[1]
            api_call("setChatTitle", {"chat_id": chat_id, "title": new_title})
            send_message(chat_id, f"✅ Chat title updated to: {new_title}")
        else:
            send_message(chat_id, "Usage: `/settitle [New Title]`")

    elif cmd in ["/setdescription", "/setdesc"]:
        if len(text_args) > 1:
            new_desc = raw_text.split(maxsplit=1)[1]
            api_call("setChatDescription", {"chat_id": chat_id, "description": new_desc})
            send_message(chat_id, "✅ Chat description updated.")
        else:
            send_message(chat_id, "Usage: `/setdesc [New Description]`")

    elif cmd == "/lock":
        api_call("setChatPermissions", {"chat_id": chat_id, "permissions": {
            "can_send_messages": False, "can_send_audios": False, "can_send_documents": False,
            "can_send_photos": False, "can_send_videos": False, "can_send_other_messages": False
        }})
        send_message(chat_id, "🔒 Chat has been locked. Only admins can send messages.")

    elif cmd == "/unlock":
        api_call("setChatPermissions", {"chat_id": chat_id, "permissions": {
            "can_send_messages": True, "can_send_audios": True, "can_send_documents": True,
            "can_send_photos": True, "can_send_videos": True, "can_send_other_messages": True,
            "can_add_web_page_previews": True, "can_invite_users": True, "can_send_polls": True
        }})
        send_message(chat_id, "🔓 Chat has been unlocked. All members can send messages.")

    elif cmd == "/filter":
        if len(text_args) > 2:
            word = text_args[1].lower()
            reply = raw_text.split(maxsplit=2)[2]
            chat_filters[chat_id][word] = reply
            send_message(chat_id, f"✅ Filter added. When someone says '{word}', I will reply.")
        else:
            send_message(chat_id, "Usage: `/filter [word] [reply text]`")

    elif cmd == "/stop":
        if len(text_args) > 1:
            word = text_args[1].lower()
            if word in chat_filters[chat_id]:
                del chat_filters[chat_id][word]
                send_message(chat_id, f"✅ Filter for '{word}' removed.")
        else:
            send_message(chat_id, "Usage: `/stop [word]`")

    elif cmd == "/save":
        if len(text_args) > 2:
            note_name = text_args[1].lower()
            note_text = raw_text.split(maxsplit=2)[2]
            chat_notes[chat_id][note_name] = note_text
            send_message(chat_id, f"✅ Note '{note_name}' saved. Retrieve with `/note {note_name}`.")
        else:
            send_message(chat_id, "Usage: `/save [name] [text]`")

    # ----------------------------------------
    # SECTION D: REPLY-BASED ADMIN ACTIONS
    # ----------------------------------------
    elif reply_to:
        if cmd == "/del":
            delete_message(chat_id, reply_to["message_id"])
            delete_message(chat_id, message["message_id"])

        elif cmd == "/pin":
            api_call("pinChatMessage", {"chat_id": chat_id, "message_id": reply_to["message_id"]})
            send_message(chat_id, "📌 Message pinned.")

        elif cmd == "/unpin":
            api_call("unpinChatMessage", {"chat_id": chat_id, "message_id": reply_to["message_id"]})
            send_message(chat_id, "📌 Message unpinned.")

        elif cmd == "/purge":
            start_id = reply_to["message_id"]
            end_id = message["message_id"]
            message_ids = list(range(start_id, end_id + 1))
            
            for i in range(0, len(message_ids), 100):
                batch = message_ids[i:i+100]
                api_call("deleteMessages", {"chat_id": chat_id, "message_ids": batch})
            
            send_message(chat_id, f"✅ Purged {len(message_ids)} messages.")

        elif target_id:
            if cmd == "/ban":
                api_call("banChatMember", {"chat_id": chat_id, "user_id": target_id})
                send_message(chat_id, f"🔨 {target_name} has been permanently banned.")

            elif cmd == "/tban" and len(text_args) > 1:
                duration = parse_time(text_args[1])
                api_call("banChatMember", {"chat_id": chat_id, "user_id": target_id, "until_date": int(time.time()) + duration})
                send_message(chat_id, f"🔨 {target_name} has been temporarily banned for {text_args[1]}.")

            elif cmd == "/unban":
                api_call("unbanChatMember", {"chat_id": chat_id, "user_id": target_id, "only_if_banned": True})
                send_message(chat_id, f"✅ {target_name} has been unbanned.")

            elif cmd == "/kick":
                api_call("banChatMember", {"chat_id": chat_id, "user_id": target_id})
                api_call("unbanChatMember", {"chat_id": chat_id, "user_id": target_id})
                send_message(chat_id, f"👢 {target_name} has been kicked.")

            elif cmd == "/mute":
                restrict_user(chat_id, target_id, until_date=0, mute=True)
                send_message(chat_id, f"🔇 {target_name} has been permanently muted.")

            elif cmd == "/tmute" and len(text_args) > 1:
                duration = parse_time(text_args[1])
                restrict_user(chat_id, target_id, until_date=duration, mute=True)
                send_message(chat_id, f"🔇 {target_name} has been muted for {text_args[1]}.")

            elif cmd == "/unmute":
                restrict_user(chat_id, target_id, mute=False)
                send_message(chat_id, f"🔊 {target_name} has been unmuted.")

            elif cmd == "/warn":
                reason = " ".join(text_args[1:]) if len(text_args) > 1 else "Admin warning"
                apply_strike(chat_id, target_id, target_name, reason)

            elif cmd == "/unwarn":
                if user_warnings[chat_id][target_id] > 0:
                    user_warnings[chat_id][target_id] -= 1
                    send_message(chat_id, f"✅ A warning was removed from {target_name}.")

            # ----------------------------------------
            # SECTION E: PROMOTION COMMANDS
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
                    send_message(chat_id, f"🛡 {target_name} has been granted `{cmd}` privileges.")
                else:
                    send_message(chat_id, "❌ Failed to promote. Ensure I have the required admin rights.")

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
                    send_message(chat_id, f"📉 {target_name} has been demoted and lost all admin privileges.")
                else:
                    send_message(chat_id, "❌ Failed to demote. Ensure I have the required admin rights.")

# ==========================================
# 6. WEBHOOK HTTP SERVER ENGINE
# ==========================================
class TelegramWebhookHandler(BaseHTTPRequestHandler):
    
    def do_GET(self):
        """Render uses GET requests for Health Checks. This ensures the service stays alive."""
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Telegram Bot is running properly!")

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
            if "message" in update:
                msg = update["message"]
                chat_type = msg["chat"]["type"]
                
                if chat_type in ["group", "supergroup"]:
                    # Handle New Members (Welcome Message)
                    if "new_chat_members" in msg:
                        chat_id = msg["chat"]["id"]
                        welcome_msg = chat_settings[chat_id]["welcome_msg"]
                        if welcome_msg:
                            for member in msg["new_chat_members"]:
                                name = member.get("first_name", "User")
                                final_msg = welcome_msg.replace("{name}", name)
                                send_message(chat_id, final_msg)
                    
                    # Handle Commands
                    elif msg.get("text", "").startswith("/"):
                        handle_command(msg)
                    
                    # Handle Anti-Spam / Filters
                    else:
                        process_message(msg)
        except Exception as e:
            print(f"[Webhook Parse Error]: {e}")

    def log_message(self, format, *args):
        # Override to suppress basic HTTP server terminal logs and keep display clean
        return

def main():
    if not TOKEN or TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("❌ Error: BOT_TOKEN is missing. Please set it in your environment variables.")
        return
        
    print(f"Initializing Webhook registration to: {WEBHOOK_URL}...")
    
    # Configure Telegram to point directly to your URL endpoint
    webhook_setup = api_call("setWebhook", {"url": WEBHOOK_URL})
    if webhook_setup and webhook_setup.get("ok"):
        print(f"✅ Webhook linked successfully!")
    else:
        print(f"❌ Webhook link failure: {webhook_setup}")
        return

    # Start local server listener instance (Binding to 0.0.0.0 is required for Render)
    server_address = ('0.0.0.0', PORT)
    httpd = HTTPServer(server_address, TelegramWebhookHandler)
    print(f"🚀 Bot server listening for payloads on port {PORT}...")
    
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping bot gracefully...")
        httpd.server_close()

if __name__ == "__main__":
    main()
