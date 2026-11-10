import os
import sys
import json
import time
import base64
import signal
import secrets
from datetime import datetime, timezone, timedelta
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SECRETS_FILE = os.path.join(BASE_DIR, ".secrets.json")
STATE_FILE = os.path.join(BASE_DIR, "signals_state.json")
LOG_FILE = os.path.join(BASE_DIR, "eztrading_bot.log")

API_BASE = "https://eztradingai.com:8001"
ZALO_API_BASE = "https://bot-api.zaloplatforms.com"
VN_TZ = timezone(timedelta(hours=7))

STOP_REQUESTED = False


def _handle_stop(signum, frame):
    global STOP_REQUESTED
    STOP_REQUESTED = True
    log(f"Nhận tín hiệu dừng ({signum}), đang thoát an toàn...")


signal.signal(signal.SIGTERM, _handle_stop)
signal.signal(signal.SIGINT, _handle_stop)


def log(msg):
    ts = datetime.now(VN_TZ).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def create_session():
    s = requests.Session()
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        backoff_factor=1.0,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=None,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "application/json",
    })
    return s


session = create_session()


def load_secrets():
    cfg = {
        "ZALO_BOT_TOKEN": os.environ.get("ZALO_BOT_TOKEN") or os.environ.get("EZ_ZALO_BOT_TOKEN", ""),
        "ZALO_CHAT_ID": os.environ.get("ZALO_CHAT_ID") or os.environ.get("EZ_ZALO_CHAT_ID", ""),
        "EZ_EMAIL": os.environ.get("EZ_EMAIL", ""),
        "EZ_PASSWORD": os.environ.get("EZ_PASSWORD", ""),
        "EZ_TOKEN": os.environ.get("EZ_TOKEN", ""),
        "POLL_INTERVAL": int(os.environ.get("POLL_INTERVAL", "10")),
    }
    if os.path.exists(SECRETS_FILE):
        try:
            with open(SECRETS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k in cfg:
                if not cfg[k] and data.get(k) not in (None, ""):
                    cfg[k] = data[k]
        except Exception as e:
            log(f"Lỗi đọc .secrets.json: {e}")
    return cfg


def save_secrets(cfg):
    try:
        with open(SECRETS_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"initialized": False, "signals": {}}


def sync_state_to_github_api(state):
    """Đồng bộ signals_state.json trực tiếp qua GitHub REST API khi chạy trên Cloud."""
    gh_token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    gh_repo = os.environ.get("GITHUB_REPOSITORY")
    if not gh_token or not gh_repo:
        return
    api_url = f"https://api.github.com/repos/{gh_repo}/contents/signals_state.json"
    headers = {
        "Authorization": f"Bearer {gh_token}",
        "Accept": "application/vnd.github+json",
    }
    try:
        r_get = session.get(api_url, headers=headers, timeout=15)
        sha = r_get.json().get("sha") if r_get.status_code == 200 else None
        raw_bytes = json.dumps(state, indent=2, ensure_ascii=False).encode("utf-8")
        b64_content = base64.b64encode(raw_bytes).decode("ascii")
        payload = {
            "message": "chore: update signals_state.json [skip ci]",
            "content": b64_content,
        }
        if sha:
            payload["sha"] = sha
        r_put = session.put(api_url, headers=headers, json=payload, timeout=15)
        if r_put.status_code in (200, 201):
            log("Đã đồng bộ signals_state.json lên GitHub Cloud.")
    except Exception as e:
        log(f"Lỗi đồng bộ GitHub API state: {e}")


def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        if os.environ.get("GITHUB_ACTIONS") == "true":
            sync_state_to_github_api(state)
    except Exception as e:
        log(f"Lỗi lưu state: {e}")


def send_zalo(bot_token, chat_id, text):
    if not bot_token or not chat_id:
        return False
    url = f"{ZALO_API_BASE}/bot{bot_token}/sendMessage"
    try:
        resp = session.post(
            url,
            json={"chat_id": str(chat_id), "text": text[:1950]},
            timeout=15,
        )
        data = resp.json() if resp.content else {}
        if resp.status_code == 200 and data.get("ok", False):
            log(f"Đã gửi Zalo tới {chat_id}")
            return True
        else:
            log(f"Gửi Zalo thất bại ({resp.status_code}): {resp.text[:200]}")
            return False
    except Exception as e:
        log(f"Lỗi kết nối Zalo Bot API: {e}")
        return False


def poll_zalo_updates(bot_token, wait_timeout=3):
    """Lấy tin nhắn người dùng gửi cho con Bot Zalo mới qua getUpdates."""
    if not bot_token or STOP_REQUESTED:
        return None, None, None
    url = f"{ZALO_API_BASE}/bot{bot_token}/getUpdates"
    try:
        resp = session.post(url, json={"timeout": wait_timeout}, timeout=wait_timeout + 6)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("ok") and data.get("result"):
                res = data["result"]
                msg_obj = res.get("message", {}) if isinstance(res, dict) else {}
                cid = msg_obj.get("chat", {}).get("id")
                sender_name = msg_obj.get("from", {}).get("display_name", "Bạn")
                txt = msg_obj.get("text", "")
                if cid:
                    return str(cid), str(txt), str(sender_name)
    except Exception:
        pass
    return None, None, None


def login_or_register_eztrading(cfg):
    """Đăng nhập hoặc tự tạo tài khoản reader trên eztradingai.com:8001."""
    email = cfg.get("EZ_EMAIL", "").strip()
    password = cfg.get("EZ_PASSWORD", "").strip()
    if email and password:
        try:
            r = session.post(
                f"{API_BASE}/api/auth/login",
                json={"email": email, "password": password},
                timeout=20,
            )
            if r.status_code == 200:
                token = r.json().get("access_token")
                if token:
                    cfg["EZ_TOKEN"] = token
                    save_secrets(cfg)
                    log(f"Đăng nhập eztradingai.com thành công ({email})")
                    return token
        except Exception as e:
            log(f"Lỗi khi đăng nhập eztradingai.com: {e}")

    new_email = f"zalo_signal_bot_{secrets.token_hex(3)}@eztradingai.com"
    new_pwd = f"EzBot#{secrets.token_hex(6)}"
    try:
        r = session.post(
            f"{API_BASE}/api/auth/register",
            json={
                "email": new_email,
                "password": new_pwd,
                "accepted_terms_version": "2026-11-08",
            },
            timeout=20,
        )
        if r.status_code in (200, 201):
            token = r.json().get("access_token")
            if token:
                cfg["EZ_EMAIL"] = new_email
                cfg["EZ_PASSWORD"] = new_pwd
                cfg["EZ_TOKEN"] = token
                save_secrets(cfg)
                log(f"Tự động khởi tạo tài khoản đọc tín hiệu thành công ({new_email})")
                return token
    except Exception as e:
        log(f"Lỗi tự động tạo tài khoản đọc tín hiệu: {e}")
    return None


def fetch_signals(cfg, limit=50):
    """Lấy danh sách tín hiệu từ /api/signals."""
    token = cfg.get("EZ_TOKEN", "").strip()
    if not token:
        token = login_or_register_eztrading(cfg)
        if not token:
            return None

    headers = {"Authorization": f"Bearer {token}"}
    try:
        r = session.get(f"{API_BASE}/api/signals?limit={limit}", headers=headers, timeout=20)
        if r.status_code == 401:
            log("Token eztradingai.com hết hạn (401), đang tự động lấy lại...")
            token = login_or_register_eztrading(cfg)
            if not token:
                return None
            headers = {"Authorization": f"Bearer {token}"}
            r = session.get(f"{API_BASE}/api/signals?limit={limit}", headers=headers, timeout=20)

        if r.status_code == 200:
            return r.json()
        else:
            log(f"Lỗi lấy /api/signals ({r.status_code}): {r.text[:200]}")
            return None
    except Exception as e:
        log(f"Lỗi kết nối tới eztradingai.com:8001: {e}")
        return None


def format_vn_time(iso_str):
    if not iso_str:
        return "N/A"
    try:
        s = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(VN_TZ).strftime("%H:%M:%S %d/%m/%Y")
    except Exception:
        return str(iso_str)


def format_price(val):
    if val is None or val == "":
        return "N/A"
    try:
        f = float(val)
        if f >= 100:
            return f"{f:,.2f}"
        elif f >= 1:
            return f"{f:,.4f}".rstrip("0").rstrip(".")
        else:
            return f"{f:.6f}".rstrip("0").rstrip(".")
    except Exception:
        return str(val)


def format_tp_list(tp_json):
    if not tp_json:
        return "N/A"
    if isinstance(tp_json, list):
        parts = []
        for idx, item in enumerate(tp_json, 1):
            if isinstance(item, dict):
                p = format_price(item.get("price"))
                pct = item.get("close_pct") or item.get("pct") or item.get("percentage") or item.get("size_pct")
                if pct:
                    parts.append(f"TP{idx}: {p} ({pct}%)")
                else:
                    parts.append(f"TP{idx}: {p}")
            else:
                parts.append(f"TP{idx}: {format_price(item)}")
        return " | ".join(parts)
    return str(tp_json)


def format_signal_message(sig, header="🚨 TÍN HIỆU MỚI TỪ EZTRADING AI"):
    side = str(sig.get("side", "")).upper()
    side_icon = "🟢 LONG" if side == "LONG" else ("🔴 SHORT" if side == "SHORT" else f"⚡ {side}")
    symbol = sig.get("symbol", "UNKNOWN")
    strategy = sig.get("strategy")
    entry_label = sig.get("entry_label")
    status = sig.get("status", "UNKNOWN")
    entry_price = format_price(sig.get("entry_price"))
    stop_loss = format_price(sig.get("stop_loss"))
    tp_str = format_tp_list(sig.get("take_profit_json"))
    created_vn = format_vn_time(sig.get("created_at"))
    sig_id = sig.get("signal_id", "")

    lines = [
        f"{header}",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"💎 Cặp: {symbol} ({side_icon})",
        f"🎯 Entry: {entry_price}",
        f"🛑 Stop Loss: {stop_loss}",
        f"💰 Take Profit: {tp_str}",
        f"📊 Trạng thái: {status}",
    ]
    if entry_label:
        lines.append(f"🏷️ Nhãn Entry: {entry_label}")
    if strategy:
        lines.append(f"🧠 Chiến lược: {strategy}")
    lines.extend([
        f"🕒 Thời gian: {created_vn}",
        f"🆔 Mã: {sig_id}",
        f"🔗 https://eztradingai.com/vi/signals",
    ])
    return "\n".join(lines)


def handle_user_command(cfg, cid, text, name):
    """Phản hồi khi người dùng nhắn tin cho Bot bao token trên Zalo."""
    first_time = (cfg.get("ZALO_CHAT_ID") != cid)
    if first_time:
        cfg["ZALO_CHAT_ID"] = cid
        save_secrets(cfg)
        log(f"Đã tự động bắt và lưu ZALO_CHAT_ID={cid} ({name})")

    signals = fetch_signals(cfg, limit=20)
    if signals is None:
        send_zalo(
            cfg["ZALO_BOT_TOKEN"],
            cid,
            "⚠️ Hiện không lấy được dữ liệu từ eztradingai.com, bot đang thử kết nối lại...",
        )
        return

    open_sigs = [s for s in signals if s.get("status") not in ("CLOSED", "CANCELLED")]

    if first_time:
        welcome = (
            f"✅ Đã kết nối thành công với {name}!\n"
            f"🤖 Bot: Bot bao token (EZTrading AI - Cloud 24/7)\n"
            f"📊 Hiện có {len(open_sigs)} tín hiệu đang mở trên https://eztradingai.com/vi/signals.\n"
            f"Từ giờ hễ web có tín hiệu mới hoặc cập nhật SL/TP, bot sẽ bắn về đây ngay lập tức!"
        )
        send_zalo(cfg["ZALO_BOT_TOKEN"], cid, welcome)
        for s in open_sigs[:4]:
            send_zalo(cfg["ZALO_BOT_TOKEN"], cid, format_signal_message(s, header="📌 TÍN HIỆU ĐANG MỞ TRÊN WEB"))
        return

    low = (text or "").lower().strip()
    if any(k in low for k in ["all", "tất cả", "tat ca", "open", "đang mở", "dang mo"]):
        if not open_sigs:
            send_zalo(cfg["ZALO_BOT_TOKEN"], cid, "📭 Hiện không có tín hiệu nào đang mở (OPEN) trên EZTrading AI.")
        else:
            for s in open_sigs[:5]:
                send_zalo(cfg["ZALO_BOT_TOKEN"], cid, format_signal_message(s, header="📌 TÍN HIỆU ĐANG MỞ (OPEN)"))
        return

    if signals:
        latest = signals[0]
        summary = (
            f"🤖 Bot EZTrading AI (Cloud 24/7) vẫn đang canh tín hiệu!\n"
            f"📊 Số tín hiệu đang mở: {len(open_sigs)} (nhắn 'all' để xem tất cả)\n\n"
            + format_signal_message(latest, header="📌 TÍN HIỆU MỚI NHẤT")
        )
        send_zalo(cfg["ZALO_BOT_TOKEN"], cid, summary)
    else:
        send_zalo(cfg["ZALO_BOT_TOKEN"], cid, "🤖 Bot đang chạy bình thường! Hiện chưa có tín hiệu nào trên hệ thống.")


def run_monitor_loop(max_duration_seconds=None):
    cfg = load_secrets()
    state = load_state()
    poll_interval = max(5, int(cfg.get("POLL_INTERVAL", 10)))
    start_ts = time.time()
    mode_str = f"Cloud Loop ({max_duration_seconds}s)" if max_duration_seconds else "24/7 Daemon"
    log(f"Bắt đầu theo dõi https://eztradingai.com/vi/signals [{mode_str}, chu kỳ {poll_interval}s]...")

    while not STOP_REQUESTED:
        if max_duration_seconds and (time.time() - start_ts) >= max_duration_seconds:
            log(f"Đã hoàn thành chu kỳ Cloud ({max_duration_seconds}s), chuẩn bị chuyển sang phiên tiếp theo.")
            break

        try:
            # 1. Kiểm tra tin nhắn tới Bot bao token
            wait_t = 4 if not cfg.get("ZALO_CHAT_ID") else 2
            cid, user_txt, sender_name = poll_zalo_updates(cfg.get("ZALO_BOT_TOKEN"), wait_timeout=wait_t)
            if cid:
                log(f"Nhận tin nhắn Zalo từ {sender_name} ({cid}): {user_txt}")
                handle_user_command(cfg, cid, user_txt, sender_name)

            # 2. Quét danh sách tín hiệu từ EZTrading AI
            signals = fetch_signals(cfg, limit=50)
            if isinstance(signals, list):
                known = state.get("signals", {})

                if not state.get("initialized"):
                    for s in signals:
                        sid = str(s.get("signal_id") or s.get("id"))
                        known[sid] = {
                            "status": s.get("status"),
                            "stop_loss": s.get("stop_loss"),
                            "updated_at": s.get("updated_at"),
                        }
                    state["initialized"] = True
                    state["signals"] = known
                    save_state(state)
                    log(f"Khởi tạo bộ nhớ trạng thái với {len(known)} tín hiệu hiện có.")
                else:
                    changed = False
                    for s in reversed(signals):
                        sid = str(s.get("signal_id") or s.get("id"))
                        cur_status = s.get("status")
                        cur_sl = s.get("stop_loss")

                        if sid not in known:
                            changed = True
                            msg = format_signal_message(s, header="🚨 TÍN HIỆU MỚI TỪ EZTRADING AI")
                            log(f"Phát hiện tín hiệu mới: {sid} ({s.get('symbol')} {s.get('side')})")
                            if cfg.get("ZALO_CHAT_ID"):
                                send_zalo(cfg["ZALO_BOT_TOKEN"], cfg["ZALO_CHAT_ID"], msg)
                            known[sid] = {
                                "status": cur_status,
                                "stop_loss": cur_sl,
                                "updated_at": s.get("updated_at"),
                            }
                        else:
                            prev = known[sid]
                            if prev.get("status") != cur_status:
                                changed = True
                                msg = format_signal_message(
                                    s,
                                    header=f"🔔 CẬP NHẬT TRẠNG THÁI ({prev.get('status')} ➡️ {cur_status})",
                                )
                                log(f"Tín hiệu {sid} đổi trạng thái: {prev.get('status')} -> {cur_status}")
                                if cfg.get("ZALO_CHAT_ID"):
                                    send_zalo(cfg["ZALO_BOT_TOKEN"], cfg["ZALO_CHAT_ID"], msg)
                                prev["status"] = cur_status
                                prev["updated_at"] = s.get("updated_at")
                            elif str(prev.get("stop_loss")) != str(cur_sl):
                                changed = True
                                msg = format_signal_message(
                                    s,
                                    header=f"🛡️ DỜI STOP LOSS ({format_price(prev.get('stop_loss'))} ➡️ {format_price(cur_sl)})",
                                )
                                log(f"Tín hiệu {sid} dời SL: {prev.get('stop_loss')} -> {cur_sl}")
                                if cfg.get("ZALO_CHAT_ID"):
                                    send_zalo(cfg["ZALO_BOT_TOKEN"], cfg["ZALO_CHAT_ID"], msg)
                                prev["stop_loss"] = cur_sl
                                prev["updated_at"] = s.get("updated_at")

                    if changed:
                        state["signals"] = known
                        save_state(state)

        except Exception as e:
            log(f"Lỗi trong vòng lặp chính: {e}")

        if cfg.get("ZALO_CHAT_ID"):
            for _ in range(max(1, poll_interval - 2)):
                if STOP_REQUESTED:
                    break
                if max_duration_seconds and (time.time() - start_ts) >= max_duration_seconds:
                    break
                time.sleep(1)


if __name__ == "__main__":
    duration = None
    if "--cloud-loop" in sys.argv:
        idx = sys.argv.index("--cloud-loop")
        if idx + 1 < len(sys.argv):
            duration = int(sys.argv[idx + 1])
        else:
            duration = 3000  # Mặc định 50 phút mỗi phiên GitHub Actions
    run_monitor_loop(max_duration_seconds=duration)
