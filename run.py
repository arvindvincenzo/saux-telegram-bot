import os
import sys
import json
import sqlite3
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from datetime import datetime
from pathlib import Path

# Add bot directory to sys.path
sys.path.insert(0, str(Path(__file__).parent / "bot"))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

DB_PATH = Path(__file__).parent / "data" / "database.db"

def get_db_data():
    if not DB_PATH.exists():
        return {"status": "ok", "total_users": 0, "active_today": 0, "users": []}

    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5.0)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT id, data FROM users ORDER BY rowid DESC")
        rows = cursor.fetchall()

        now = datetime.now()
        real_users = []
        active_today_count = 0

        for r in rows:
            uid = r[0]
            if uid == 8851751300:  # bot account
                continue
            try:
                u = json.loads(r[1])
                uname = str(u.get("username") or "")
                if uname.startswith("test_") or uname.startswith("ref_") or uname in ("alice", "bob", "charlie", "david"):
                    continue

                # get dialog count
                d_cursor = conn.cursor()
                d_cursor.execute("SELECT count(*) FROM dialogs WHERE user_id = ?", (uid,))
                d_row = d_cursor.fetchone()
                dialog_count = d_row[0] if d_row else 0

                # check if active today
                last_act = u.get("last_interaction")
                if isinstance(last_act, dict) and "val" in last_act:
                    last_act_str = str(last_act["val"])
                else:
                    last_act_str = str(last_act or "")

                if last_act_str:
                    try:
                        act_dt = datetime.fromisoformat(last_act_str)
                        if act_dt.date() == now.date():
                            active_today_count += 1
                    except Exception:
                        pass

                first_seen = u.get("first_seen")
                if isinstance(first_seen, dict) and "val" in first_seen:
                    first_seen_str = str(first_seen["val"])
                else:
                    first_seen_str = str(first_seen or "")

                # tokens
                tokens = u.get("n_used_tokens") or {}
                total_tokens = sum(v.get("n_input_tokens", 0) + v.get("n_output_tokens", 0) for v in tokens.values())

                real_users.append({
                    "id": uid,
                    "username": u.get("username") or None,
                    "first_name": u.get("first_name") or "",
                    "last_name": u.get("last_name") or "",
                    "first_seen": first_seen_str,
                    "last_interaction": last_act_str,
                    "dialog_count": dialog_count,
                    "chat_mode": u.get("current_chat_mode") or "assistant",
                    "model": u.get("current_model") or "openai/gpt-oss-120b",
                    "total_tokens": total_tokens,
                    "is_premium": False
                })
            except Exception as parse_err:
                logging.warning(f"Error parsing user row {uid}: {parse_err}")

        conn.close()
        return {
            "status": "ok",
            "total_users": len(real_users),
            "active_today": active_today_count,
            "server_time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "users": real_users
        }
    except Exception as e:
        logging.error(f"Error reading sqlite database: {e}")
        return {"status": "error", "message": str(e), "users": []}


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
  <title>SAUX Userbase & Analytics</title>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <style>
    :root {
      --bg-color: #0f172a;
      --card-bg: rgba(30, 41, 59, 0.7);
      --card-border: rgba(255, 255, 255, 0.08);
      --text-main: #f8fafc;
      --text-muted: #94a3b8;
      --accent: #38bdf8;
      --accent-glow: rgba(56, 189, 248, 0.2);
      --success: #10b981;
      --warning: #f59e0b;
      --badge-vip: linear-gradient(135deg, #f59e0b, #d97706);
      --font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    }

    * { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }

    body {
      background-color: var(--bg-color);
      color: var(--text-main);
      font-family: var(--font-family);
      padding: 16px;
      min-height: 100vh;
      overflow-x: hidden;
    }

    .header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 16px;
      padding-bottom: 12px;
      border-bottom: 1px solid var(--card-border);
    }

    .header-title h1 {
      font-size: 1.25rem;
      font-weight: 700;
      display: flex;
      align-items: center;
      gap: 6px;
      color: var(--text-main);
    }

    .header-title p {
      font-size: 0.8rem;
      color: var(--text-muted);
      margin-top: 2px;
    }

    .badge-live {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      background: rgba(16, 185, 129, 0.15);
      color: #34d399;
      padding: 4px 10px;
      border-radius: 9999px;
      font-size: 0.75rem;
      font-weight: 600;
      border: 1px solid rgba(16, 185, 129, 0.3);
    }

    .dot {
      width: 7px;
      height: 7px;
      background-color: #34d399;
      border-radius: 50%;
      animation: pulse 1.5s infinite;
    }

    @keyframes pulse {
      0% { opacity: 0.4; transform: scale(0.9); }
      50% { opacity: 1; transform: scale(1.2); }
      100% { opacity: 0.4; transform: scale(0.9); }
    }

    .kpi-grid {
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 10px;
      margin-bottom: 16px;
    }

    .kpi-card {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 12px;
      padding: 12px;
      backdrop-filter: blur(8px);
    }

    .kpi-label {
      font-size: 0.75rem;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }

    .kpi-value {
      font-size: 1.4rem;
      font-weight: 800;
      margin-top: 4px;
      color: var(--text-main);
    }

    .search-box {
      margin-bottom: 16px;
      position: relative;
    }

    .search-input {
      width: 100%;
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      color: var(--text-main);
      padding: 10px 14px 10px 36px;
      border-radius: 10px;
      font-size: 0.9rem;
      outline: none;
      transition: all 0.2s;
    }

    .search-input:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 3px var(--accent-glow);
    }

    .search-icon {
      position: absolute;
      left: 12px;
      top: 50%;
      transform: translateY(-50%);
      color: var(--text-muted);
      font-size: 0.85rem;
    }

    .user-list {
      display: flex;
      flex-direction: column;
      gap: 12px;
    }

    .user-card {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 14px;
      padding: 14px;
      backdrop-filter: blur(10px);
      transition: transform 0.15s, border-color 0.15s;
    }

    .user-card:active {
      transform: scale(0.99);
    }

    .user-card-header {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      margin-bottom: 10px;
    }

    .user-identity {
      display: flex;
      align-items: center;
      gap: 10px;
    }

    .avatar {
      width: 38px;
      height: 38px;
      border-radius: 50%;
      background: linear-gradient(135deg, #0284c7, #38bdf8);
      display: flex;
      align-items: center;
      justify-content: center;
      font-weight: 700;
      font-size: 1rem;
      color: white;
      text-transform: uppercase;
    }

    .user-name-box h3 {
      font-size: 0.95rem;
      font-weight: 700;
      color: var(--text-main);
    }

    .user-handle {
      font-size: 0.8rem;
      color: var(--accent);
      font-weight: 500;
    }

    .badge-tier {
      font-size: 0.7rem;
      padding: 3px 8px;
      border-radius: 6px;
      font-weight: 600;
    }

    .tier-vip {
      background: var(--badge-vip);
      color: #fff;
    }

    .tier-free {
      background: rgba(148, 163, 184, 0.15);
      color: var(--text-muted);
    }

    .meta-grid {
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 8px;
      margin-top: 10px;
      padding-top: 10px;
      border-top: 1px solid rgba(255, 255, 255, 0.05);
      font-size: 0.78rem;
    }

    .meta-item {
      color: var(--text-muted);
    }

    .meta-item b {
      color: var(--text-main);
      font-weight: 600;
    }

    .tokens-tag {
      background: rgba(56, 189, 248, 0.1);
      color: var(--accent);
      padding: 2px 6px;
      border-radius: 4px;
      font-size: 0.75rem;
      font-family: monospace;
      margin-top: 4px;
      display: inline-block;
    }

    .refresh-btn {
      position: fixed;
      bottom: 20px;
      right: 20px;
      background: var(--accent);
      color: #0f172a;
      width: 48px;
      height: 48px;
      border-radius: 50%;
      border: none;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 1.3rem;
      box-shadow: 0 4px 14px rgba(56, 189, 248, 0.4);
      cursor: pointer;
      z-index: 100;
      transition: transform 0.2s;
    }

    .refresh-btn:active {
      transform: scale(0.9) rotate(90deg);
    }

    .empty-state {
      text-align: center;
      padding: 40px 20px;
      color: var(--text-muted);
    }
  </style>
</head>
<body>
  <div class="header">
    <div class="header-title">
      <h1>👑 SAUX Userbase</h1>
      <p>Autonomous Behavior & Analytics</p>
    </div>
    <div class="badge-live"><span class="dot"></span> Live Sync</div>
  </div>

  <div class="kpi-grid">
    <div class="kpi-card">
      <div class="kpi-label">Total Users</div>
      <div class="kpi-value" id="kpi-total">...</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Active Today</div>
      <div class="kpi-value" id="kpi-active" style="color: #34d399;">...</div>
    </div>
  </div>

  <div class="search-box">
    <span class="search-icon">🔍</span>
    <input type="text" class="search-input" id="search-input" placeholder="Search name, @username, or ID..." oninput="filterUsers()">
  </div>

  <div class="user-list" id="user-list">
    <div class="empty-state">Loading live database...</div>
  </div>

  <button class="refresh-btn" onclick="loadUsers()" title="Refresh">🔄</button>

  <script>
    if (window.Telegram && window.Telegram.WebApp) {
      window.Telegram.WebApp.ready();
      window.Telegram.WebApp.expand();
      if (window.Telegram.WebApp.setHeaderColor) {
        window.Telegram.WebApp.setHeaderColor('#0f172a');
      }
    }

    let allUsers = [];

    function formatDate(val) {
      if (!val) return "N/A";
      try {
        const d = new Date(val);
        if (isNaN(d.getTime())) return val.slice(0, 19).replace('T', ' ');
        return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
      } catch (e) {
        return val;
      }
    }

    async function loadUsers() {
      try {
        const resp = await fetch('/api/users');
        const data = await resp.json();
        if (data.status === 'ok') {
          allUsers = data.users || [];
          document.getElementById('kpi-total').textContent = data.total_users || 0;
          document.getElementById('kpi-active').textContent = data.active_today || 0;
          renderList(allUsers);
        } else {
          document.getElementById('user-list').innerHTML = `<div class="empty-state">Error: ${data.message}</div>`;
        }
      } catch (err) {
        document.getElementById('user-list').innerHTML = `<div class="empty-state">Failed to connect to database.</div>`;
      }
    }

    function renderList(users) {
      const listEl = document.getElementById('user-list');
      if (!users.length) {
        listEl.innerHTML = `<div class="empty-state">No matching users found.</div>`;
        return;
      }

      listEl.innerHTML = users.map(u => {
        const fname = u.first_name || '';
        const lname = u.last_name || '';
        const fullName = (fname + ' ' + lname).trim() || 'Anonymous';
        const initial = fullName.charAt(0) || '?';
        const handle = u.username ? `@${u.username}` : '[No @handle]';
        const joined = formatDate(u.first_seen);
        const lastAct = formatDate(u.last_interaction);
        const tokensFormatted = Number(u.total_tokens || 0).toLocaleString();
        const tierBadge = u.is_premium ? `<span class="badge-tier tier-vip">VIP</span>` : `<span class="badge-tier tier-free">Free</span>`;

        return `
          <div class="user-card">
            <div class="user-card-header">
              <div class="user-identity">
                <div class="avatar">${initial}</div>
                <div class="user-name-box">
                  <h3>${fullName}</h3>
                  <div class="user-handle">${handle}</div>
                </div>
              </div>
              ${tierBadge}
            </div>

            <div class="meta-grid">
              <div class="meta-item">🆔 ID: <b>${u.id}</b></div>
              <div class="meta-item">💬 Dialogs: <b>${u.dialog_count || 0}</b></div>
              <div class="meta-item">📅 Joined: <b>${joined}</b></div>
              <div class="meta-item">⏱️ Active: <b>${lastAct}</b></div>
            </div>

            <div style="margin-top: 8px; display: flex; justify-content: space-between; align-items: center;">
              <span class="tokens-tag">⚡ ${tokensFormatted} tokens</span>
              <span style="font-size: 0.72rem; color: var(--text-muted);">${u.model || 'openai/gpt-oss-120b'}</span>
            </div>
          </div>
        `;
      }).join('');
    }

    function filterUsers() {
      const q = document.getElementById('search-input').value.toLowerCase().trim();
      if (!q) {
        renderList(allUsers);
        return;
      }
      const filtered = allUsers.filter(u => {
        const full = ((u.first_name || '') + ' ' + (u.last_name || '')).toLowerCase();
        const uname = (u.username || '').toLowerCase();
        const idStr = String(u.id);
        return full.includes(q) || uname.includes(q) || idStr.includes(q);
      });
      renderList(filtered);
    }

    loadUsers();
  </script>
</body>
</html>
"""


class UnifiedServerHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        # 1. Healthcheck for Railway web service
        if path == "" or path == "/health":
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK - SAUX Bot is running live")
            return

        # 2. REST API for Live Users
        if path == "/api/users":
            data = get_db_data()
            payload = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(payload)
            return

        # 3. Userbase Mini App HTML
        if path in ("/admin", "/userbase"):
            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_TEMPLATE.encode("utf-8"))
            return

        self.send_response(404)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Not Found")

    def log_message(self, format, *args):
        pass


def start_health_check_server():
    port_str = os.environ.get("PORT")
    if port_str:
        try:
            port = int(port_str)
            server = HTTPServer(("0.0.0.0", port), UnifiedServerHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            logging.info(f"Unified Web & Mini App server listening on 0.0.0.0:{port}")
        except Exception as e:
            logging.warning(f"Could not start web server on port {port_str}: {e}")


from bot import run_bot

if __name__ == "__main__":
    start_health_check_server()
    run_bot()
