"""
Plaud Auto Registration Tool — Web UI
基于 Flask + SSE 的网页版自动注册工具
"""

import os, sys, io, warnings
warnings.filterwarnings("ignore")

# ── Windows UTF-8 fix ─────────────────────────────────────────────────────────
if sys.platform == "win32":
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    except Exception: pass
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception: pass

import time, json, re, base64, random, string, hashlib, threading, queue, socket
import hmac as hmac_module
import requests
from datetime import datetime
from typing import Optional, Tuple, List, Dict
from flask import Flask, Response, request, jsonify

# ─────────────────────────── Crypto ──────────────────────────────────────────
try:
    from coincurve import PrivateKey as ECPrivateKey, PublicKey as ECPublicKey
    COINCURVE = True
except ImportError:
    COINCURVE = False

try:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad
    PYCRYPTO = True
except ImportError:
    PYCRYPTO = False

# ─────────────────────────── Constants ───────────────────────────────────────
PASSWORD = "Abc123456"
ENVS = {
    "test": ("测试环境", "https://api-dev.plaud.ai"),
    "prod": ("正式环境", "https://api.plaud.ai"),
}

# ═══════════════════════════════ Crypto Logic ═════════════════════════════════

def _rstr(n=11):
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))

def _devid():
    return "".join(random.choices("0123456789abcdef", k=16))

def encrypt_password(password: str, pub_key_hex: str) -> Tuple[str, bool]:
    try:
        if not COINCURVE or not PYCRYPTO:
            raise RuntimeError("crypto libs missing")
        pub_bytes = bytes.fromhex(pub_key_hex)
        ep = ECPrivateKey()
        epk = ep.public_key.format(compressed=False)
        sp = ECPublicKey(pub_bytes).multiply(ep.secret)
        sx = sp.format(compressed=True)[1:]
        derived = hashlib.sha512(sx).digest()
        iv = os.urandom(16)
        cipher = AES.new(derived[:32], AES.MODE_CBC, iv)
        ct = cipher.encrypt(pad(password.encode(), AES.block_size))
        mac = hmac_module.new(derived[32:], iv + epk + ct, hashlib.sha256).digest()
        return base64.b64encode(epk + iv + ct + mac).decode(), True
    except Exception:
        return password, False

# ═══════════════════════ Email Providers ══════════════════════════════════════

def _find6(text: str) -> Optional[str]:
    for pat in [r"(?:code|验证码|verification)[^\d]{0,10}(\d{6})", r"\b(\d{6})\b"]:
        m = re.search(pat, text, re.IGNORECASE)
        if m: return m.group(1)
    return None

class GuerrillaMailProvider:
    BASE = "https://api.guerrillamail.com/ajax.php"
    def __init__(self):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "Mozilla/5.0"
        self.sid = self.email = None
    def get_email(self) -> str:
        d = self.s.get(self.BASE, params={"f":"get_email_address"}, timeout=20).json()
        self.sid, self.email = d["sid_token"], d["email_addr"]
        return self.email
    def wait_for_code(self, timeout=120, log=None) -> Optional[str]:
        start, seen, seq = time.time(), set(), 0
        while time.time()-start < timeout:
            if log: log("INFO", f"等待验证码中… ({int(time.time()-start)}s/{timeout}s)")
            try:
                items = self.s.get(self.BASE, params={"f":"check_email","seq":seq,"sid_token":self.sid}, timeout=15).json().get("list",[])
                for it in items:
                    mid = str(it.get("mail_id",""))
                    if mid and mid not in seen:
                        seen.add(mid)
                        code = _find6(it.get("mail_excerpt","")+it.get("mail_subject",""))
                        if not code:
                            try:
                                fd = self.s.get(self.BASE, params={"f":"fetch_email","email_id":mid,"sid_token":self.sid}, timeout=15).json()
                                body = re.sub(r"<[^>]+>"," ", fd.get("mail_body","")+fd.get("mail_subject",""))
                                code = _find6(body)
                            except: pass
                        if code: return code
                        try: seq = max(seq, int(mid))
                        except: pass
            except Exception as e:
                if log: log("WARN", f"Guerrilla Mail 异常: {e}")
            time.sleep(5)
        return None

class MailTMProvider:
    BASE = "https://api.mail.tm"
    def __init__(self):
        self.s = requests.Session()
        self._pw = "MT@123456!"
        self.email = None
    def get_email(self) -> str:
        dom = self.s.get(f"{self.BASE}/domains", timeout=20).json()["hydra:member"][0]["domain"]
        self.email = "".join(random.choices(string.ascii_lowercase+string.digits, k=12)) + "@" + dom
        self.s.post(f"{self.BASE}/accounts", json={"address":self.email,"password":self._pw}, timeout=20).raise_for_status()
        tok = self.s.post(f"{self.BASE}/token", json={"address":self.email,"password":self._pw}, timeout=20).json()["token"]
        self.s.headers["Authorization"] = f"Bearer {tok}"
        return self.email
    def wait_for_code(self, timeout=120, log=None) -> Optional[str]:
        start, seen = time.time(), set()
        while time.time()-start < timeout:
            if log: log("INFO", f"等待验证码中… ({int(time.time()-start)}s/{timeout}s)")
            try:
                for msg in self.s.get(f"{self.BASE}/messages", timeout=15).json().get("hydra:member",[]):
                    if msg["id"] not in seen:
                        seen.add(msg["id"])
                        full = self.s.get(f"{self.BASE}/messages/{msg['id']}", timeout=15).json()
                        html = full.get("html","")
                        if isinstance(html, list): html = " ".join(str(h) for h in html)
                        code = _find6(full.get("text","")+re.sub(r"<[^>]+"," ",html))
                        if code: return code
            except Exception as e:
                if log: log("WARN", f"mail.tm 异常: {e}")
            time.sleep(5)
        return None

# ══════════════════════════ Plaud Registrar ═══════════════════════════════════

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36 Edg/147.0.0.0",
    "Origin": "https://test.theplaud.com",
    "Referer": "https://test.theplaud.com/",
    "app-platform": "web", "edit-from": "web",
    "app-language": "zh-cn", "timezone": "Asia/Shanghai",
    "Accept": "application/json, text/plain, */*",
}

class PlaudRegistrar:
    def __init__(self, base_url: str, log_fn=None):
        self.base = base_url.rstrip("/")
        self.log = log_fn or (lambda lvl, msg: None)
        self.s = requests.Session()
        did = _devid()
        self.s.headers.update({**_HEADERS, "x-device-id": did, "x-pld-tag": did})
        self.pub_key = self.access_token = None
        self.country, self.pv = "SG", 1
        self._pw_val, self._pw_enc = PASSWORD, True

    def _xid(self): return {"X-Request-ID": _rstr(11)}

    def _get(self, p):
        r = self.s.get(f"{self.base}{p}", headers=self._xid(), timeout=15)
        r.raise_for_status(); return r.json()

    def _post(self, p, auth=False, **kw):
        h = self._xid()
        if auth and self.access_token: h["Authorization"] = f"bearer {self.access_token}"
        r = self.s.post(f"{self.base}{p}", headers=h, timeout=20, **kw)
        r.raise_for_status(); return r.json()

    def _follow_region_redirect(self, resp: dict) -> bool:
        """Handle -302 region mismatch: update base URL and re-fetch config. Returns True if redirected."""
        if resp.get("status") != -302:
            return False
        new_api = resp.get("data", {}).get("domains", {}).get("api", "")
        if not new_api:
            return False
        self.log("WARN", f"区域重定向 → {new_api}")
        self.base = new_api.rstrip("/")
        # Re-fetch security config and location from the new regional endpoint
        d = self._get("/config/security")
        self.pub_key = d["data"]["pass_pub_key"]
        d2 = self._get("/user/privacy/location")
        self.country = d2["data"].get("cf_country", self.country)
        self.pv = d2["data"].get("privacy_version", self.pv)
        self.log("INFO", f"已切换至区域节点，国家: {self.country}")
        return True

    def register(self, email: str, provider) -> dict:
        result = {"email": email, "password": PASSWORD, "token": None,
                  "country": "N/A", "env": "测试" if "dev" in self.base else "正式", "status": "FAILED", "error": None}
        try:
            self.log("INFO", f"Step1 获取安全配置…")
            d = self._get("/config/security")
            self.pub_key = d["data"]["pass_pub_key"]

            self.log("INFO", f"Step2 获取地理位置…")
            d2 = self._get("/user/privacy/location")
            self.country = d2["data"].get("cf_country", "SG")
            self.pv = d2["data"].get("privacy_version", 1)
            result["country"] = self.country

            self.log("INFO", f"Step3 发送验证码到 {email}…")
            d3 = self._post("/auth/send-code", json={"username": email, "type": "signup", "user_area": self.country, "r": random.random()})
            # Handle regional redirect (-302)
            if self._follow_region_redirect(d3):
                result["env"] = "测试" if "dev" in self.base else "正式"
                d3 = self._post("/auth/send-code", json={"username": email, "type": "signup", "user_area": self.country, "r": random.random()})
            if d3.get("status") != 0: raise RuntimeError(f"send-code: {d3}")
            jwt = d3["token"]
            self.log("OK", f"验证码已发送，等待邮件…")

            self.log("INFO", f"Step4 等待验证码（最多120秒）…")
            code = provider.wait_for_code(timeout=120, log=self.log)
            if not code: raise RuntimeError("等待验证码超时")
            self.log("OK", f"收到验证码: {code}")

            self.log("INFO", f"Step5 提交验证码完成注册…")
            enc_pw, is_enc = encrypt_password(PASSWORD, self.pub_key)
            try:
                d5 = self._post("/auth/verify-code", json={"code": code, "token": jwt, "password": enc_pw, "password_encrypted": is_enc, "user_area": self.country, "r": random.random()})
                if d5.get("status") == 0:
                    self._pw_val, self._pw_enc = enc_pw, is_enc
                else:
                    raise RuntimeError(str(d5))
            except Exception as e:
                self.log("WARN", f"加密密码失败({e})，尝试明文…")
                d5 = self._post("/auth/verify-code", json={"code": code, "token": jwt, "password": PASSWORD, "password_encrypted": False, "user_area": self.country, "r": random.random()})
                if d5.get("status") != 0: raise RuntimeError(f"verify-code: {d5}")
                self._pw_val, self._pw_enc = PASSWORD, False
            self.log("OK", "注册成功！")

            self.log("INFO", "Step6 提交隐私协议（注册前）…")
            try:
                self._post("/user/privacy/agreement", json={"country": "other", "privacy_version": "", "is_marketing_agreement": False, "source": "web", "email": email, "password": PASSWORD, "password_encrypted": True, "r": random.random()})
            except Exception as e:
                self.log("WARN", f"隐私协议(注册前)异常(可忽略): {e}")

            self.log("INFO", "Step7 登录获取 AccessToken…")
            d7 = self._post("/auth/access-token", files={"username": (None, email), "password": (None, self._pw_val), "client_id": (None, "web"), "password_encrypted": (None, str(self._pw_enc).lower())})
            if d7.get("status") != 0: raise RuntimeError(f"access-token: {d7}")
            self.access_token = d7["access_token"]
            self.log("OK", "登录成功，Token 已获取")

            self.log("INFO", "Step8 提交隐私协议（登录后）…")
            try:
                self._post("/user/privacy/agreement", auth=True, json={"country": self.country, "privacy_version": self.pv, "is_marketing_agreement": True, "source": "web"})
            except Exception as e:
                self.log("WARN", f"隐私协议(登录后)异常(可忽略): {e}")

            result["token"] = self.access_token
            result["status"] = "SUCCESS"
        except Exception as e:
            result["error"] = str(e)
            self.log("ERR", f"注册失败: {e}")
        return result

# ═══════════════════════════ Task Manager ════════════════════════════════════

_tasks: Dict[str, dict] = {}

def run_task(task_id: str, cfg: dict):
    q: queue.Queue = _tasks[task_id]["queue"]
    results = []

    def send(t, **kw): q.put({"type": t, **kw})
    def log(lvl, msg):
        send("log", level=lvl, msg=msg, time=datetime.now().strftime("%H:%M:%S"))

    env_label, base_url = ENVS.get(cfg["env"], ENVS["test"])
    count = max(1, int(cfg.get("count", 1)))
    use_guerrilla = cfg.get("provider", "guerrilla") != "mailtm"

    send("start", total=count, env=env_label, provider="Guerrilla Mail" if use_guerrilla else "mail.tm")
    log("INFO", f"开始注册 {count} 个账号 | 环境: {env_label}")

    for idx in range(count):
        if _tasks[task_id].get("stop"):
            log("WARN", "任务已被停止")
            break

        send("progress", current=idx+1, total=count)
        log("INFO", f"── 账号 {idx+1}/{count} ─────────────────────")

        provider = GuerrillaMailProvider() if use_guerrilla else MailTMProvider()
        registrar = PlaudRegistrar(base_url, log_fn=log)

        try:
            log("INFO", "获取临时邮箱地址…")
            email = provider.get_email()
            log("OK", f"临时邮箱: {email}")
        except Exception as e:
            log("ERR", f"获取邮箱失败: {e}")
            result = {"email":"N/A","password":PASSWORD,"token":None,"country":"N/A","env":"测试" if "dev" in base_url else "正式","status":"FAILED","error":str(e)}
            results.append(result)
            send("result", result=result)
            continue

        result = registrar.register(email, provider)
        results.append(result)
        send("result", result=result)

        if idx < count - 1:
            time.sleep(2)

    success = sum(1 for r in results if r["status"] == "SUCCESS")
    log("INFO" if success < count else "OK", f"完成：{success}/{count} 成功")
    send("done", success=success, total=count, results=results)
    _tasks[task_id]["done"] = True
    q.put(None)  # sentinel

# ══════════════════════════════ Flask App ════════════════════════════════════

app = Flask(__name__)

# ─── HTML (inline) ─────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Plaud 自动注册工具</title>
<style>
:root{
  --bg:#0f1117;--surface:#1a1d27;--surface2:#232636;--border:#2e3248;
  --accent:#5b7ef5;--accent2:#7c5bf5;--green:#3ecf8e;--red:#f56565;
  --yellow:#f6ad55;--text:#e2e8f0;--muted:#64748b;--radius:10px;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;font-size:14px;min-height:100vh}
/* ── Layout ── */
.app{max-width:1280px;margin:0 auto;padding:24px 20px}
header{display:flex;align-items:center;gap:12px;margin-bottom:24px}
header h1{font-size:22px;font-weight:700;background:linear-gradient(135deg,var(--accent),var(--accent2));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
header .badge{background:var(--surface2);border:1px solid var(--border);padding:3px 10px;border-radius:20px;font-size:11px;color:var(--muted)}
.grid{display:grid;grid-template-columns:320px 1fr;gap:16px;align-items:start}
/* ── Card ── */
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:20px}
.card-title{font-size:13px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;margin-bottom:16px;display:flex;align-items:center;gap:8px}
.card-title::before{content:'';display:block;width:3px;height:14px;background:linear-gradient(var(--accent),var(--accent2));border-radius:2px}
/* ── Form ── */
.field{margin-bottom:14px}
label{display:block;font-size:12px;color:var(--muted);margin-bottom:6px;font-weight:500}
select,input[type=number],input[type=text]{width:100%;background:var(--surface2);border:1px solid var(--border);border-radius:7px;color:var(--text);padding:9px 12px;font-size:13px;outline:none;transition:border-color .2s}
select:focus,input:focus{border-color:var(--accent)}
select option{background:var(--surface2)}
.pw-field{display:flex;align-items:center;gap:8px}
.pw-field input{flex:1}
.pw-badge{background:rgba(91,126,245,.15);color:var(--accent);border:1px solid rgba(91,126,245,.3);padding:4px 10px;border-radius:6px;font-size:11px;white-space:nowrap}
/* ── Buttons ── */
.btn{display:flex;align-items:center;justify-content:center;gap:8px;width:100%;padding:11px;border:none;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer;transition:all .2s}
.btn-primary{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff}
.btn-primary:hover:not(:disabled){filter:brightness(1.1);transform:translateY(-1px)}
.btn-primary:disabled{opacity:.45;cursor:not-allowed;transform:none}
.btn-stop{background:rgba(245,101,101,.15);color:var(--red);border:1px solid rgba(245,101,101,.3)}
.btn-stop:hover{background:rgba(245,101,101,.25)}
.btn-sm{width:auto;padding:5px 12px;font-size:12px;border-radius:6px}
.btn-export{background:rgba(91,126,245,.15);color:var(--accent);border:1px solid rgba(91,126,245,.3)}
.btn-export:hover{background:rgba(91,126,245,.25)}
/* ── Progress ── */
.progress-bar{height:4px;background:var(--surface2);border-radius:2px;margin-top:12px;overflow:hidden}
.progress-fill{height:100%;background:linear-gradient(90deg,var(--accent),var(--accent2));border-radius:2px;transition:width .4s ease;width:0%}
.progress-label{font-size:11px;color:var(--muted);margin-top:5px;text-align:center}
/* ── Log panel ── */
.log-panel{display:flex;flex-direction:column;height:440px}
.log-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
#log-box{flex:1;background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:12px 14px;overflow-y:auto;font-family:'Consolas','Courier New',monospace;font-size:12.5px;line-height:1.7}
#log-box::-webkit-scrollbar{width:5px}
#log-box::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}
.log-line{display:flex;gap:10px;align-items:baseline;padding:1px 0}
.log-time{color:var(--muted);font-size:11px;flex-shrink:0;width:62px}
.log-lvl{font-size:11px;font-weight:700;width:34px;flex-shrink:0;text-align:center;border-radius:3px;padding:0 2px}
.lvl-INFO .log-lvl{color:#94a3b8}
.lvl-OK   .log-lvl{color:var(--green)}
.lvl-ERR  .log-lvl{color:var(--red)}
.lvl-WARN .log-lvl{color:var(--yellow)}
.lvl-INFO .log-msg{color:#cbd5e1}
.lvl-OK   .log-msg{color:var(--green)}
.lvl-ERR  .log-msg{color:var(--red)}
.lvl-WARN .log-msg{color:var(--yellow)}
/* ── Stats ── */
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:16px}
.stat-card{background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:12px 14px;text-align:center}
.stat-num{font-size:22px;font-weight:700;line-height:1}
.stat-label{font-size:11px;color:var(--muted);margin-top:4px}
/* ── Table ── */
.table-wrap{overflow-x:auto;border-radius:8px;border:1px solid var(--border)}
table{width:100%;border-collapse:collapse}
thead th{background:var(--surface2);padding:10px 14px;text-align:left;font-size:11px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.05em;white-space:nowrap}
tbody tr{border-top:1px solid var(--border);transition:background .15s}
tbody tr:hover{background:rgba(255,255,255,.03)}
td{padding:10px 14px;font-size:12.5px;vertical-align:middle}
.token-cell{font-family:monospace;font-size:11px;color:var(--muted);max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:pointer}
.token-cell:hover{color:var(--text)}
.badge-ok{background:rgba(62,207,142,.15);color:var(--green);border:1px solid rgba(62,207,142,.3);padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600}
.badge-fail{background:rgba(245,101,101,.15);color:var(--red);border:1px solid rgba(245,101,101,.3);padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600}
/* ── Toast ── */
.toast{position:fixed;bottom:20px;right:20px;background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:10px 16px;font-size:13px;opacity:0;transition:opacity .3s;pointer-events:none;z-index:999}
.toast.show{opacity:1}
</style>
</head>
<body>
<div class="app">
  <!-- Header -->
  <header>
    <h1>⚡ Plaud 自动注册工具</h1>
    <span class="badge">Web UI</span>
    <span class="badge" id="env-badge" style="margin-left:auto">未运行</span>
  </header>

  <!-- Main grid -->
  <div class="grid">
    <!-- Left: Config -->
    <div>
      <div class="card">
        <div class="card-title">运行配置</div>

        <div class="field">
          <label>运行环境</label>
          <select id="env">
            <option value="test">🧪 测试环境 (api-dev.plaud.ai)</option>
            <option value="prod">🚀 正式环境 (api.plaud.ai)</option>
          </select>
        </div>

        <div class="field">
          <label>临时邮箱提供商</label>
          <select id="provider">
            <option value="guerrilla">Guerrilla Mail（主力，稳定无限速）</option>
            <option value="mailtm">mail.tm（备用，有并发速率限制）</option>
          </select>
        </div>

        <div class="field">
          <label>注册账号数量</label>
          <input type="number" id="count" value="1" min="1" max="50">
        </div>

        <div class="field">
          <label>固定密码</label>
          <div class="pw-field">
            <input type="text" value="Abc123456" readonly>
            <span class="pw-badge">已固定</span>
          </div>
        </div>

        <div style="margin-top:20px">
          <button class="btn btn-primary" id="start-btn" onclick="startTask()">
            <span id="btn-icon">▶</span>
            <span id="btn-text">开始注册</span>
          </button>
          <button class="btn btn-stop" id="stop-btn" style="display:none;margin-top:8px" onclick="stopTask()">
            ⏹ 停止任务
          </button>
        </div>

        <div id="progress-wrap" style="display:none;margin-top:16px">
          <div class="progress-bar"><div class="progress-fill" id="progress-fill"></div></div>
          <div class="progress-label" id="progress-label">准备中…</div>
        </div>
      </div>

      <!-- Stats card (hidden until done) -->
      <div class="card" id="stats-card" style="margin-top:16px;display:none">
        <div class="card-title">执行统计</div>
        <div class="stats">
          <div class="stat-card">
            <div class="stat-num" id="s-total" style="color:var(--text)">0</div>
            <div class="stat-label">总计</div>
          </div>
          <div class="stat-card">
            <div class="stat-num" id="s-ok" style="color:var(--green)">0</div>
            <div class="stat-label">成功</div>
          </div>
          <div class="stat-card">
            <div class="stat-num" id="s-fail" style="color:var(--red)">0</div>
            <div class="stat-label">失败</div>
          </div>
          <div class="stat-card">
            <div class="stat-num" id="s-rate" style="color:var(--accent)">0%</div>
            <div class="stat-label">成功率</div>
          </div>
        </div>
        <button class="btn btn-export btn-sm" onclick="exportJSON()">⬇ 导出 JSON</button>
      </div>
    </div>

    <!-- Right: Logs -->
    <div class="card log-panel">
      <div class="log-header">
        <div class="card-title" style="margin-bottom:0">执行日志</div>
        <button class="btn btn-sm" style="background:var(--surface2);color:var(--muted);border:1px solid var(--border)" onclick="clearLogs()">清空</button>
      </div>
      <div id="log-box"><div style="color:var(--muted);font-size:12px;padding:8px 0">等待任务启动…</div></div>
    </div>
  </div>

  <!-- Results table (shown after first result) -->
  <div class="card" id="results-card" style="margin-top:16px;display:none">
    <div class="card-title">注册结果</div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>#</th><th>邮箱</th><th>密码</th><th>Token</th>
            <th>国家</th><th>环境</th><th>状态</th>
          </tr>
        </thead>
        <tbody id="result-body"></tbody>
      </table>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
let taskId = null, es = null, allResults = [], running = false;

async function startTask() {
  const cfg = {
    env:      document.getElementById('env').value,
    provider: document.getElementById('provider').value,
    count:    parseInt(document.getElementById('count').value) || 1
  };
  clearLogs();
  allResults = [];
  document.getElementById('result-body').innerHTML = '';
  document.getElementById('results-card').style.display = 'none';
  document.getElementById('stats-card').style.display = 'none';
  setBusy(true);

  const res = await fetch('/api/start', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(cfg)});
  const {task_id} = await res.json();
  taskId = task_id;

  es = new EventSource(`/api/stream/${task_id}`);
  es.onmessage = e => handleEvent(JSON.parse(e.data));
  es.onerror   = () => { es.close(); setBusy(false); };
}

async function stopTask() {
  if (!taskId) return;
  await fetch(`/api/stop/${taskId}`, {method:'POST'});
  toast('停止请求已发送');
}

function handleEvent(d) {
  if (d.type === 'log') {
    addLog(d.level, d.msg, d.time);
  } else if (d.type === 'progress') {
    const pct = Math.round((d.current / d.total) * 100);
    document.getElementById('progress-fill').style.width = pct + '%';
    document.getElementById('progress-label').textContent = `账号 ${d.current} / ${d.total}`;
  } else if (d.type === 'result') {
    allResults.push(d.result);
    addResultRow(d.result, allResults.length);
    document.getElementById('results-card').style.display = 'block';
  } else if (d.type === 'start') {
    document.getElementById('env-badge').textContent = `${d.env} | ${d.provider}`;
    document.getElementById('progress-wrap').style.display = 'block';
    document.getElementById('progress-label').textContent = `准备中…`;
  } else if (d.type === 'done') {
    es.close(); setBusy(false);
    showStats(d.success, d.total);
    document.getElementById('progress-fill').style.width = '100%';
    document.getElementById('progress-label').textContent = `完成：${d.success}/${d.total} 成功`;
    toast(`注册完成！成功 ${d.success}/${d.total}`);
  }
}

function addLog(level, msg, time) {
  const box = document.getElementById('log-box');
  if (box.querySelector('div[style]')) box.innerHTML = '';  // remove placeholder
  const d = document.createElement('div');
  d.className = `log-line lvl-${level}`;
  d.innerHTML = `<span class="log-time">${time||''}</span><span class="log-lvl">${level}</span><span class="log-msg">${escHtml(msg)}</span>`;
  box.appendChild(d);
  box.scrollTop = box.scrollHeight;
}

function addResultRow(r, idx) {
  const tb = document.getElementById('result-body');
  const tr = document.createElement('tr');
  const tok = r.token || '';
  const tokShort = tok.length > 40 ? tok.slice(0,40)+'…' : (tok || 'N/A');
  tr.innerHTML = `
    <td style="color:var(--muted)">${idx}</td>
    <td>${escHtml(r.email)}</td>
    <td><span style="font-family:monospace">${r.password}</span></td>
    <td class="token-cell" title="${escHtml(tok)}" onclick="copyText('${tok}')">${escHtml(tokShort)}</td>
    <td>${r.country||'N/A'}</td>
    <td>${r.env||'N/A'}</td>
    <td>${r.status==='SUCCESS' ? '<span class="badge-ok">SUCCESS</span>' : `<span class="badge-fail">FAILED</span>`}</td>`;
  tb.appendChild(tr);
}

function showStats(ok, total) {
  const fail = total - ok;
  document.getElementById('s-total').textContent = total;
  document.getElementById('s-ok').textContent    = ok;
  document.getElementById('s-fail').textContent  = fail;
  document.getElementById('s-rate').textContent  = total ? Math.round(ok/total*100)+'%' : '0%';
  document.getElementById('stats-card').style.display = 'block';
}

function setBusy(on) {
  running = on;
  document.getElementById('start-btn').disabled = on;
  document.getElementById('btn-icon').textContent = on ? '⏳' : '▶';
  document.getElementById('btn-text').textContent = on ? '注册中…' : '开始注册';
  document.getElementById('stop-btn').style.display = on ? 'block' : 'none';
  if (!on) document.getElementById('env-badge').textContent = '已完成';
}

function clearLogs() {
  document.getElementById('log-box').innerHTML = '<div style="color:var(--muted);font-size:12px;padding:8px 0">日志已清空</div>';
}

function exportJSON() {
  const blob = new Blob([JSON.stringify(allResults, null, 2)], {type:'application/json'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `plaud_accounts_${Date.now()}.json`;
  a.click();
}

function copyText(t) {
  navigator.clipboard.writeText(t).then(() => toast('Token 已复制'));
}

function toast(msg) {
  const el = document.getElementById('toast');
  el.textContent = msg; el.classList.add('show');
  setTimeout(() => el.classList.remove('show'), 2500);
}

function escHtml(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
</script>
</body>
</html>"""

# ─── Routes ────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return HTML

@app.route("/api/start", methods=["POST"])
def api_start():
    cfg = request.get_json(force=True)
    task_id = f"{time.time():.6f}"
    q: queue.Queue = queue.Queue()
    _tasks[task_id] = {"queue": q, "stop": False, "done": False}
    threading.Thread(target=run_task, args=(task_id, cfg), daemon=True).start()
    return jsonify({"task_id": task_id})

@app.route("/api/stream/<task_id>")
def api_stream(task_id):
    def generate():
        if task_id not in _tasks:
            yield f"data: {json.dumps({'type':'error','msg':'task not found'})}\n\n"
            return
        q = _tasks[task_id]["queue"]
        while True:
            try:
                msg = q.get(timeout=30)
            except queue.Empty:
                yield f"data: {json.dumps({'type':'ping'})}\n\n"
                continue
            if msg is None:
                break
            yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route("/api/stop/<task_id>", methods=["POST"])
def api_stop(task_id):
    if task_id in _tasks:
        _tasks[task_id]["stop"] = True
    return jsonify({"ok": True})

# ─── Startup ───────────────────────────────────────────────────────────────

def find_free_port(start=5000) -> int:
    for port in range(start, start + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return start

def open_browser(port: int):
    import webbrowser, time as _t
    _t.sleep(1.0)
    webbrowser.open(f"http://127.0.0.1:{port}")

if __name__ == "__main__":
    port = find_free_port(5000)
    print(f"\n Plaud 自动注册工具 — Web UI")
    print(f" 浏览器访问: http://127.0.0.1:{port}")
    print(f" 按 Ctrl+C 退出\n")
    threading.Thread(target=open_browser, args=(port,), daemon=True).start()
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
