"""
Plaud Auto Registration Tool
基于HAR接口分析自动注册 Plaud 账号，支持 Guerrilla Mail 和 mail.tm 临时邮箱
"""

import os
import sys

# ── Fix Windows console UTF-8 encoding ──────────────────────────────────────
if sys.platform == "win32":
    import io
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass

import time
import json
import re
import base64
import random
import string
import hashlib
import hmac as hmac_module
import os as _os
import requests
import warnings
warnings.filterwarnings("ignore")

from datetime import datetime
from typing import Optional, Tuple, List

# ─────────────────────────────────── Rich UI ──────────────────────────────────
try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.rule import Rule
    from rich.text import Text
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn
    from rich.logging import RichHandler
    RICH = True
    console = Console()
except ImportError:
    RICH = False
    console = None

# ─────────────────────────────── Crypto Imports ───────────────────────────────
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

# ═══════════════════════════════════ CONFIG ═══════════════════════════════════

PASSWORD = "Abc123456"

ENVIRONMENTS = {
    "1": ("测试环境", "https://api-dev.plaud.ai"),
    "2": ("正式环境", "https://api.plaud.ai"),
}

# Logs buffer for final display
_LOGS: List[dict] = []


# ══════════════════════════════ Logging Helpers ═══════════════════════════════

def _log(level: str, msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    _LOGS.append({"time": ts, "level": level, "msg": msg})
    if RICH:
        color = {"INFO": "dim white", "OK": "green", "ERR": "bold red", "WARN": "yellow"}.get(level, "white")
        console.print(f"[dim]{ts}[/dim]  [{color}]{level:4s}[/{color}]  {msg}")
    else:
        print(f"{ts}  [{level:4s}]  {msg}")


def info(msg):  _log("INFO", msg)
def ok(msg):    _log("OK", msg)
def err(msg):   _log("ERR", msg)
def warn(msg):  _log("WARN", msg)


# ═══════════════════════════════════ Crypto ═══════════════════════════════════

def _random_str(n: int = 11) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))

def _device_id() -> str:
    return "".join(random.choices("0123456789abcdef", k=16))


def encrypt_password_eccrypto(password: str, pub_key_hex: str) -> str:
    """
    ECIES encryption compatible with the eccrypto Node.js library:
      ephemPubKey(65 uncompressed) + IV(16) + AES-256-CBC-ciphertext + HMAC-SHA256(32)

    HAR observation: server always receives ~135-byte output.
    This implementation produces 129 bytes (16-byte ciphertext for 9-char password).
    If the server rejects it, fall back to plain-text mode below.
    """
    if not COINCURVE or not PYCRYPTO:
        raise RuntimeError("Missing crypto libs (coincurve / pycryptodome)")

    # Parse recipient compressed public key (33 bytes)
    pub_key_bytes = bytes.fromhex(pub_key_hex)

    # Generate ephemeral EC key pair on secp256k1
    ephem_priv = ECPrivateKey()
    ephem_pub_uncompressed = ephem_priv.public_key.format(compressed=False)  # 65 bytes, starts with 04

    # ECDH: compute shared point, take x-coordinate (32 bytes)
    recipient_pub = ECPublicKey(pub_key_bytes)
    shared_point = recipient_pub.multiply(ephem_priv.secret)
    shared_x = shared_point.format(compressed=True)[1:]  # strip 02/03 prefix, keep 32-byte x

    # KDF: SHA-512 → enc_key (32 bytes) + mac_key (32 bytes)
    derived = hashlib.sha512(shared_x).digest()
    enc_key = derived[:32]
    mac_key = derived[32:]

    # AES-256-CBC encryption with PKCS7 padding
    iv = _os.urandom(16)
    cipher = AES.new(enc_key, AES.MODE_CBC, iv)
    ciphertext = cipher.encrypt(pad(password.encode("utf-8"), AES.block_size))

    # HMAC-SHA256 over (IV ‖ ephemPubKey ‖ ciphertext)
    mac_data = iv + ephem_pub_uncompressed + ciphertext
    mac = hmac_module.new(mac_key, mac_data, hashlib.sha256).digest()

    # Serialise: ephemPubKey(65) | IV(16) | ciphertext | MAC(32)
    result = ephem_pub_uncompressed + iv + ciphertext + mac
    return base64.b64encode(result).decode()


def encrypt_password(password: str, pub_key_hex: str) -> Tuple[str, bool]:
    """
    Returns (encoded_password, is_encrypted).
    Tries ECIES first; falls back to plain text.
    """
    try:
        enc = encrypt_password_eccrypto(password, pub_key_hex)
        return enc, True
    except Exception as e:
        warn(f"ECIES加密失败({e})，将使用明文密码")
        return password, False


# ═══════════════════════════ Guerrilla Mail Provider ══════════════════════════

class GuerrillaMailProvider:
    """Guerrilla Mail API — 无限速，稳定（主力）"""

    BASE = "https://api.guerrillamail.com/ajax.php"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "Mozilla/5.0"
        self.sid_token: Optional[str] = None
        self.email: Optional[str] = None

    def get_email(self) -> str:
        resp = self.session.get(self.BASE, params={"f": "get_email_address"}, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        self.sid_token = data["sid_token"]
        self.email = data["email_addr"]
        return self.email

    def wait_for_code(self, timeout: int = 120) -> Optional[str]:
        start = time.time()
        seen: set = set()
        seq = 0
        while time.time() - start < timeout:
            elapsed = int(time.time() - start)
            info(f"  等待验证码中… ({elapsed}s / {timeout}s)")
            try:
                resp = self.session.get(
                    self.BASE,
                    params={"f": "check_email", "seq": seq, "sid_token": self.sid_token},
                    timeout=15,
                )
                emails = resp.json().get("list", [])
                for item in emails:
                    mid = str(item.get("mail_id", ""))
                    if mid and mid not in seen:
                        seen.add(mid)
                        code = self._extract_from_item(item) or self._fetch_full(mid)
                        if code:
                            return code
                        try:
                            seq = max(seq, int(mid))
                        except ValueError:
                            pass
            except Exception as exc:
                warn(f"  Guerrilla Mail 查询异常: {exc}")
            time.sleep(5)
        return None

    def _extract_from_item(self, item: dict) -> Optional[str]:
        text = item.get("mail_excerpt", "") + " " + item.get("mail_subject", "")
        return _find_6digit(text)

    def _fetch_full(self, mail_id: str) -> Optional[str]:
        try:
            resp = self.session.get(
                self.BASE,
                params={"f": "fetch_email", "email_id": mail_id, "sid_token": self.sid_token},
                timeout=15,
            )
            data = resp.json()
            body = data.get("mail_body", "") + " " + data.get("mail_subject", "")
            return _find_6digit(re.sub(r"<[^>]+>", " ", body))
        except Exception:
            return None


# ═════════════════════════════ mail.tm Provider ═══════════════════════════════

class MailTMProvider:
    """mail.tm API — 备用（有并发速率限制）"""

    BASE = "https://api.mail.tm"

    def __init__(self):
        self.session = requests.Session()
        self._acct_pw = "MT@123456!"
        self.email: Optional[str] = None
        self.token: Optional[str] = None

    def get_email(self) -> str:
        domains = self.session.get(f"{self.BASE}/domains", timeout=20).json()
        domain = domains["hydra:member"][0]["domain"]
        username = "".join(random.choices(string.ascii_lowercase + string.digits, k=12))
        self.email = f"{username}@{domain}"
        self.session.post(
            f"{self.BASE}/accounts",
            json={"address": self.email, "password": self._acct_pw},
            timeout=20,
        ).raise_for_status()
        r = self.session.post(
            f"{self.BASE}/token",
            json={"address": self.email, "password": self._acct_pw},
            timeout=20,
        )
        r.raise_for_status()
        self.token = r.json()["token"]
        self.session.headers["Authorization"] = f"Bearer {self.token}"
        return self.email

    def wait_for_code(self, timeout: int = 120) -> Optional[str]:
        start = time.time()
        seen: set = set()
        while time.time() - start < timeout:
            elapsed = int(time.time() - start)
            info(f"  等待验证码中… ({elapsed}s / {timeout}s)")
            try:
                msgs = self.session.get(f"{self.BASE}/messages", timeout=15).json().get("hydra:member", [])
                for msg in msgs:
                    if msg["id"] not in seen:
                        seen.add(msg["id"])
                        full = self.session.get(f"{self.BASE}/messages/{msg['id']}", timeout=15).json()
                        body = full.get("text", "")
                        html = full.get("html", "")
                        if isinstance(html, list):
                            html = " ".join(h.get("body", "") if isinstance(h, dict) else str(h) for h in html)
                        combined = body + " " + re.sub(r"<[^>]+>", " ", str(html))
                        code = _find_6digit(combined)
                        if code:
                            return code
            except Exception as exc:
                warn(f"  mail.tm 查询异常: {exc}")
            time.sleep(5)
        return None


def _find_6digit(text: str) -> Optional[str]:
    """Extract a 6-digit verification code from text."""
    # Look for common patterns: "code is 123456", "验证码：123456", standalone "123456"
    # Prefer codes after keywords
    for pat in [
        r"(?:code|验证码|verification)[^\d]{0,10}(\d{6})",
        r"\b(\d{6})\b",
    ]:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


# ═════════════════════════════ Plaud Registrar ════════════════════════════════

class PlaudRegistrar:
    """Handles the full Plaud account registration flow."""

    _COMMON_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36 Edg/147.0.0.0"
        ),
        "Origin": "https://test.theplaud.com",
        "Referer": "https://test.theplaud.com/",
        "app-platform": "web",
        "edit-from": "web",
        "app-language": "zh-cn",
        "timezone": "Asia/Shanghai",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")
        self.device_id = _device_id()
        self.session = requests.Session()
        self.session.headers.update(
            {
                **self._COMMON_HEADERS,
                "x-device-id": self.device_id,
                "x-pld-tag": self.device_id,
            }
        )
        # State populated during registration
        self.pub_key: Optional[str] = None
        self.country: str = "SG"
        self.privacy_version: int = 1
        self.access_token: Optional[str] = None
        # Track which password mode succeeded (encrypted or plain)
        self._pw_encrypted: bool = True   # default: try encrypted first
        self._pw_value: str = PASSWORD    # will be set after first successful attempt

    # ── helpers ──────────────────────────────────────────────────────────────

    def _xid(self) -> dict:
        return {"X-Request-ID": _random_str(11)}

    def _r(self) -> float:
        return random.random()

    def _get(self, path: str, **kw) -> dict:
        r = self.session.get(f"{self.base}{path}", headers=self._xid(), timeout=15, **kw)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, *, auth: bool = False, **kw) -> dict:
        h = self._xid()
        if auth and self.access_token:
            h["Authorization"] = f"bearer {self.access_token}"
        r = self.session.post(f"{self.base}{path}", headers=h, timeout=20, **kw)
        r.raise_for_status()
        return r.json()

    # ── API calls ─────────────────────────────────────────────────────────────

    def fetch_security_config(self):
        data = self._get("/config/security")
        if data.get("status") == 0:
            self.pub_key = data["data"]["pass_pub_key"]
        else:
            raise RuntimeError(f"/config/security 失败: {data}")

    def fetch_location(self):
        data = self._get("/user/privacy/location")
        if data.get("status") == 0:
            self.country = data["data"].get("cf_country", "SG")
            self.privacy_version = data["data"].get("privacy_version", 1)
        else:
            warn(f"/user/privacy/location 返回非0: {data}")

    def _follow_region_redirect(self, resp: dict) -> bool:
        """Handle -302 region mismatch. Updates self.base and re-fetches config. Returns True if redirected."""
        if resp.get("status") != -302:
            return False
        new_api = resp.get("data", {}).get("domains", {}).get("api", "")
        if not new_api:
            return False
        warn(f"区域重定向 → {new_api}")
        self.base = new_api.rstrip("/")
        self.fetch_security_config()
        self.fetch_location()
        info(f"已切换至区域节点，国家: {self.country}")
        return True

    def send_code(self, email: str) -> str:
        """Returns the JWT token from send-code response."""
        data = self._post(
            "/auth/send-code",
            json={"username": email, "type": "signup", "user_area": self.country, "r": self._r()},
        )
        # Handle -302 region mismatch: switch to regional endpoint and retry
        if self._follow_region_redirect(data):
            data = self._post(
                "/auth/send-code",
                json={"username": email, "type": "signup", "user_area": self.country, "r": self._r()},
            )
        if data.get("status") != 0:
            raise RuntimeError(f"send-code 失败: {data}")
        return data["token"]

    def verify_code(self, code: str, jwt_token: str) -> None:
        enc_pw, is_enc = encrypt_password(PASSWORD, self.pub_key)
        # Try encrypted first
        try:
            data = self._post(
                "/auth/verify-code",
                json={
                    "code": code,
                    "token": jwt_token,
                    "password": enc_pw,
                    "password_encrypted": is_enc,
                    "user_area": self.country,
                    "r": self._r(),
                },
            )
            if data.get("status") == 0:
                self._pw_value = enc_pw
                self._pw_encrypted = is_enc
                return
            warn(f"加密密码注册失败(status={data.get('status')})，尝试明文密码…")
        except Exception as e:
            warn(f"加密密码请求异常({e})，尝试明文密码…")

        # Fallback: plain text
        data = self._post(
            "/auth/verify-code",
            json={
                "code": code,
                "token": jwt_token,
                "password": PASSWORD,
                "password_encrypted": False,
                "user_area": self.country,
                "r": self._r(),
            },
        )
        if data.get("status") != 0:
            raise RuntimeError(f"verify-code 失败 (加密+明文均失败): {data}")
        self._pw_value = PASSWORD
        self._pw_encrypted = False

    def privacy_agreement_pre_login(self, email: str) -> None:
        """第一次隐私协议 (注册完成后、登录前)"""
        # HAR shows this sends plaintext password with password_encrypted=true
        try:
            self._post(
                "/user/privacy/agreement",
                json={
                    "country": "other",
                    "privacy_version": "",
                    "is_marketing_agreement": False,
                    "source": "web",
                    "email": email,
                    "password": PASSWORD,
                    "password_encrypted": True,
                    "r": self._r(),
                },
            )
        except Exception as e:
            warn(f"隐私协议(注册前)异常(可忽略): {e}")

    def get_access_token(self, email: str) -> None:
        # Use the same password mode that succeeded in verify_code
        pw = self._pw_value
        is_enc = self._pw_encrypted
        data = self._post(
            "/auth/access-token",
            files={
                "username": (None, email),
                "password": (None, pw),
                "client_id": (None, "web"),
                "password_encrypted": (None, str(is_enc).lower()),
            },
        )
        if data.get("status") != 0:
            raise RuntimeError(f"access-token 失败: {data}")
        self.access_token = data["access_token"]

    def privacy_agreement_post_login(self) -> None:
        """第二次隐私协议 (登录后，带 Bearer token)"""
        try:
            self._post(
                "/user/privacy/agreement",
                auth=True,
                json={
                    "country": self.country,
                    "privacy_version": self.privacy_version,
                    "is_marketing_agreement": True,
                    "source": "web",
                },
            )
        except Exception as e:
            warn(f"隐私协议(登录后)异常(可忽略): {e}")

    # ── full registration flow ────────────────────────────────────────────────

    def register(self, email: str, mail_provider) -> dict:
        result = {
            "email": email,
            "password": PASSWORD,
            "token": None,
            "country": "N/A",
            "env": "测试" if "dev" in self.base else "正式",
            "env_url": self.base,
            "status": "FAILED",
            "error": None,
        }
        try:
            info(f"[{email}] Step1: 获取安全配置…")
            self.fetch_security_config()

            info(f"[{email}] Step2: 获取地理位置…")
            self.fetch_location()
            result["country"] = self.country

            info(f"[{email}] Step3: 发送验证码…")
            jwt_token = self.send_code(email)
            ok(f"[{email}] 验证码已发送，等待邮件…")

            info(f"[{email}] Step4: 等待验证码（最多120秒）…")
            code = mail_provider.wait_for_code(timeout=120)
            if not code:
                raise RuntimeError("等待验证码超时（120秒）")
            ok(f"[{email}] 收到验证码: {code}")

            info(f"[{email}] Step5: 提交验证码完成注册…")
            self.verify_code(code, jwt_token)
            ok(f"[{email}] 注册成功！")

            info(f"[{email}] Step6: 提交隐私协议(注册前)…")
            self.privacy_agreement_pre_login(email)

            info(f"[{email}] Step7: 登录获取AccessToken…")
            self.get_access_token(email)
            ok(f"[{email}] 登录成功，Token已获取")

            info(f"[{email}] Step8: 提交隐私协议(登录后)…")
            self.privacy_agreement_post_login()

            result["token"] = self.access_token
            result["status"] = "SUCCESS"

        except Exception as exc:
            result["error"] = str(exc)
            err(f"[{email}] 注册失败: {exc}")

        return result


# ═══════════════════════════════════ Output ═══════════════════════════════════

def print_summary(results: List[dict], env_name: str):
    if RICH:
        console.print()
        console.print(Rule("[bold cyan]执行结果汇总[/bold cyan]"))
        tbl = Table(
            title=f"环境: {env_name}  |  共 {len(results)} 个账号",
            show_header=True,
            header_style="bold cyan",
            border_style="blue",
            expand=True,
        )
        tbl.add_column("邮箱", style="cyan", no_wrap=True, min_width=30)
        tbl.add_column("密码", style="yellow", no_wrap=True)
        tbl.add_column("Token (前50字符)", style="green")
        tbl.add_column("国家", style="blue", justify="center")
        tbl.add_column("运行环境", style="magenta", justify="center")
        tbl.add_column("状态", justify="center")

        for r in results:
            tok = r.get("token") or ""
            tok_disp = (tok[:50] + "…") if len(tok) > 50 else (tok or "N/A")
            status_txt = Text("SUCCESS", style="bold green") if r["status"] == "SUCCESS" else Text("FAILED", style="bold red")
            tbl.add_row(
                r["email"],
                r["password"],
                tok_disp,
                r.get("country", "N/A"),
                r.get("env", "N/A"),
                status_txt,
            )
        console.print(tbl)
    else:
        print(f"\n{'='*70}")
        print(f"执行结果汇总 — {env_name}")
        print(f"{'='*70}")
        for r in results:
            tok = r.get("token") or "N/A"
            print(
                f"[{r['status']:7s}] {r['email']:40s} | pw={r['password']} | "
                f"country={r.get('country','N/A')} | env={r.get('env','N/A')} | "
                f"token={tok[:40]}{'...' if len(tok)>40 else ''}"
            )


def print_log_section():
    if not RICH:
        return
    console.print()
    console.print(Rule("[bold]执行日志[/bold]"))
    for entry in _LOGS:
        color = {"INFO": "dim", "OK": "green", "ERR": "bold red", "WARN": "yellow"}.get(entry["level"], "white")
        console.print(f"[dim]{entry['time']}[/dim]  [{color}]{entry['level']:4s}[/{color}]  {entry['msg']}")


def save_results(results: List[dict]) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"plaud_accounts_{ts}.json"
    path = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), fname)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    return path


# ═══════════════════════════════════ Main ════════════════════════════════════

def _input(prompt: str) -> str:
    # Strip Rich markup tags for the actual input() call on plain terminals
    clean = re.sub(r"\[/?[^\]]+\]", "", prompt)
    if RICH:
        console.print(prompt, end="")
        return input()
    else:
        return input(clean)


def main():
    if RICH:
        console.print(
            Panel.fit(
                "[bold cyan]Plaud Auto Registration Tool[/bold cyan]\n"
                "[dim]自动注册 Plaud 账号 · 支持测试/正式环境 · Guerrilla Mail / mail.tm[/dim]",
                border_style="cyan",
            )
        )
    else:
        print("=" * 60)
        print("  Plaud Auto Registration Tool")
        print("=" * 60)

    # ── Environment selection ────────────────────────────────────────────────
    if RICH:
        console.print("\n[bold]请选择运行环境：[/bold]")
        for k, (name, url) in ENVIRONMENTS.items():
            console.print(f"  [yellow]{k}[/yellow]. {name}  ({url})")
    else:
        print("\n请选择运行环境：")
        for k, (name, url) in ENVIRONMENTS.items():
            print(f"  {k}. {name}  ({url})")

    env_choice = _input("[bold cyan]输入选项 (1/2) [默认:1]: [/bold cyan]").strip() or "1"
    if env_choice not in ENVIRONMENTS:
        env_choice = "1"
    env_name, base_url = ENVIRONMENTS[env_choice]

    # ── Email provider selection ─────────────────────────────────────────────
    if RICH:
        console.print("\n[bold]请选择临时邮箱提供商：[/bold]")
        console.print("  [yellow]1[/yellow]. Guerrilla Mail  (主力，稳定无限速)")
        console.print("  [yellow]2[/yellow]. mail.tm          (备用，有并发速率限制)")
    else:
        print("\n请选择临时邮箱提供商：")
        print("  1. Guerrilla Mail  (主力，稳定无限速)")
        print("  2. mail.tm          (备用，有并发速率限制)")

    prov_choice = _input("[bold cyan]输入选项 (1/2) [默认:1]: [/bold cyan]").strip() or "1"
    use_guerrilla = prov_choice != "2"
    prov_label = "Guerrilla Mail" if use_guerrilla else "mail.tm"

    # ── Number of accounts ───────────────────────────────────────────────────
    count_str = _input("[bold cyan]注册账号数量 [默认:1]: [/bold cyan]").strip() or "1"
    try:
        count = max(1, int(count_str))
    except ValueError:
        count = 1

    # ── Summary ──────────────────────────────────────────────────────────────
    if RICH:
        console.print()
        console.print(
            Panel(
                f"[cyan]环境[/cyan]: {env_name} ({base_url})\n"
                f"[cyan]邮箱提供商[/cyan]: {prov_label}\n"
                f"[cyan]注册数量[/cyan]: {count}\n"
                f"[cyan]固定密码[/cyan]: {PASSWORD}",
                title="任务配置",
                border_style="green",
            )
        )
    else:
        print(f"\n配置: env={env_name}, provider={prov_label}, count={count}, pw={PASSWORD}\n")

    results = []

    for idx in range(count):
        if RICH:
            console.print()
            console.print(Rule(f"[bold]账号 {idx + 1} / {count}[/bold]"))
        else:
            print(f"\n--- 账号 {idx + 1} / {count} ---")

        # Create provider and registrar
        provider = GuerrillaMailProvider() if use_guerrilla else MailTMProvider()
        registrar = PlaudRegistrar(base_url)

        try:
            info("获取临时邮箱地址…")
            email = provider.get_email()
            ok(f"临时邮箱: [bold]{email}[/bold]" if RICH else f"临时邮箱: {email}")
            result = registrar.register(email, provider)
        except Exception as exc:
            err(f"发生意外错误: {exc}")
            result = {
                "email": "N/A",
                "password": PASSWORD,
                "token": None,
                "country": "N/A",
                "env": "测试" if "dev" in base_url else "正式",
                "env_url": base_url,
                "status": "FAILED",
                "error": str(exc),
            }

        results.append(result)

        if idx < count - 1:
            time.sleep(2)  # small delay between accounts

    # ── Output ───────────────────────────────────────────────────────────────
    print_summary(results, env_name)

    saved = save_results(results)
    success = sum(1 for r in results if r["status"] == "SUCCESS")

    if RICH:
        console.print()
        console.print(
            Panel(
                f"[green]成功[/green]: {success} / {count}\n"
                f"[red]失败[/red]: {count - success} / {count}\n"
                f"[dim]结果已保存: {saved}[/dim]",
                title="执行统计",
                border_style="cyan",
            )
        )
    else:
        print(f"\n成功: {success}/{count}")
        print(f"结果已保存: {saved}")

    # Keep window open when run as exe
    if getattr(sys, "frozen", False):
        input("\n按 Enter 键退出…")


if __name__ == "__main__":
    main()
