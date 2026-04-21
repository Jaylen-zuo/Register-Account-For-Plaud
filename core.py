"""Core logic: crypto, email providers, PlaudRegistrar."""

import os, re, time, base64, random, string, hashlib
import hmac as hmac_module
import requests
from typing import Optional, Tuple

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

PASSWORD = "Abc123456"
ENVS = {
    "test": ("测试环境", "https://api-dev.plaud.ai"),
    "prod": ("正式环境", "https://api.plaud.ai"),
}

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36 Edg/147.0.0.0",
    "Origin": "https://test.theplaud.com",
    "Referer": "https://test.theplaud.com/",
    "app-platform": "web", "edit-from": "web",
    "app-language": "zh-cn", "timezone": "Asia/Shanghai",
    "Accept": "application/json, text/plain, */*",
}

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
        d = self.s.get(self.BASE, params={"f": "get_email_address"}, timeout=20).json()
        self.sid, self.email = d["sid_token"], d["email_addr"]
        return self.email

    def wait_for_code(self, timeout=120, log=None, stop_fn=None) -> Optional[str]:
        start, seen, seq = time.time(), set(), 0
        while time.time() - start < timeout:
            if stop_fn and stop_fn(): return None
            if log: log("INFO", f"等待验证码中… ({int(time.time()-start)}s/{timeout}s)")
            try:
                items = self.s.get(self.BASE, params={"f": "check_email", "seq": seq, "sid_token": self.sid}, timeout=15).json().get("list", [])
                for it in items:
                    mid = str(it.get("mail_id", ""))
                    if mid and mid not in seen:
                        seen.add(mid)
                        code = _find6(it.get("mail_excerpt", "") + it.get("mail_subject", ""))
                        if not code:
                            try:
                                fd = self.s.get(self.BASE, params={"f": "fetch_email", "email_id": mid, "sid_token": self.sid}, timeout=15).json()
                                body = re.sub(r"<[^>]+>", " ", fd.get("mail_body", "") + fd.get("mail_subject", ""))
                                code = _find6(body)
                            except Exception:
                                pass
                        if code: return code
                        try: seq = max(seq, int(mid))
                        except Exception: pass
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
        self.email = "".join(random.choices(string.ascii_lowercase + string.digits, k=12)) + "@" + dom
        self.s.post(f"{self.BASE}/accounts", json={"address": self.email, "password": self._pw}, timeout=20).raise_for_status()
        tok = self.s.post(f"{self.BASE}/token", json={"address": self.email, "password": self._pw}, timeout=20).json()["token"]
        self.s.headers["Authorization"] = f"Bearer {tok}"
        return self.email

    def wait_for_code(self, timeout=120, log=None, stop_fn=None) -> Optional[str]:
        start, seen = time.time(), set()
        while time.time() - start < timeout:
            if stop_fn and stop_fn(): return None
            if log: log("INFO", f"等待验证码中… ({int(time.time()-start)}s/{timeout}s)")
            try:
                for msg in self.s.get(f"{self.BASE}/messages", timeout=15).json().get("hydra:member", []):
                    if msg["id"] not in seen:
                        seen.add(msg["id"])
                        full = self.s.get(f"{self.BASE}/messages/{msg['id']}", timeout=15).json()
                        html = full.get("html", "")
                        if isinstance(html, list): html = " ".join(str(h) for h in html)
                        code = _find6(full.get("text", "") + re.sub(r"<[^>]+>", " ", html))
                        if code: return code
            except Exception as e:
                if log: log("WARN", f"mail.tm 异常: {e}")
            time.sleep(5)
        return None

class PlaudRegistrar:
    def __init__(self, base_url: str, password: str = PASSWORD, country_override: str = None, log_fn=None):
        self.base = base_url.rstrip("/")
        self.password = password or PASSWORD
        self.country_override = country_override or None
        self.log = log_fn or (lambda lvl, msg: None)
        self.s = requests.Session()
        did = _devid()
        self.s.headers.update({**_HEADERS, "x-device-id": did, "x-pld-tag": did})
        self.pub_key = self.access_token = None
        self.country, self.pv = "SG", 1
        self._pw_val, self._pw_enc = self.password, True

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
        if resp.get("status") != -302:
            return False
        new_api = resp.get("data", {}).get("domains", {}).get("api", "")
        if not new_api:
            return False
        self.log("WARN", f"区域重定向 → {new_api}")
        self.base = new_api.rstrip("/")
        d = self._get("/config/security")
        self.pub_key = d["data"]["pass_pub_key"]
        d2 = self._get("/user/privacy/location")
        self.country = d2["data"].get("cf_country", self.country)
        self.pv = d2["data"].get("privacy_version", self.pv)
        if self.country_override:
            self.country = self.country_override
        self.log("INFO", f"已切换至区域节点，国家: {self.country}")
        return True

    def register(self, email: str, provider, stop_fn=None) -> dict:
        result = {
            "email": email, "password": self.password, "token": None,
            "country": "N/A", "env": "测试" if "dev" in self.base else "正式",
            "status": "FAILED", "error": None,
        }
        try:
            self.log("INFO", "Step1 获取安全配置…")
            d = self._get("/config/security")
            self.pub_key = d["data"]["pass_pub_key"]

            self.log("INFO", "Step2 获取地理位置…")
            d2 = self._get("/user/privacy/location")
            self.country = d2["data"].get("cf_country", "SG")
            self.pv = d2["data"].get("privacy_version", 1)
            if self.country_override:
                self.country = self.country_override
                self.log("INFO", f"使用指定国家: {self.country}")
            result["country"] = self.country

            self.log("INFO", f"Step3 发送验证码到 {email}…")
            d3 = self._post("/auth/send-code", json={"username": email, "type": "signup", "user_area": self.country, "r": random.random()})
            if self._follow_region_redirect(d3):
                result["env"] = "测试" if "dev" in self.base else "正式"
                d3 = self._post("/auth/send-code", json={"username": email, "type": "signup", "user_area": self.country, "r": random.random()})
            if d3.get("status") != 0: raise RuntimeError(f"send-code: {d3}")
            jwt = d3["token"]
            self.log("OK", "验证码已发送，等待邮件…")

            self.log("INFO", "Step4 等待验证码（最多120秒）…")
            code = provider.wait_for_code(timeout=120, log=self.log, stop_fn=stop_fn)
            if not code:
                raise RuntimeError("任务已停止" if stop_fn and stop_fn() else "等待验证码超时")
            self.log("OK", f"收到验证码: {code}")

            self.log("INFO", "Step5 提交验证码完成注册…")
            enc_pw, is_enc = encrypt_password(self.password, self.pub_key)
            try:
                d5 = self._post("/auth/verify-code", json={"code": code, "token": jwt, "password": enc_pw, "password_encrypted": is_enc, "user_area": self.country, "r": random.random()})
                if d5.get("status") == 0:
                    self._pw_val, self._pw_enc = enc_pw, is_enc
                else:
                    raise RuntimeError(str(d5))
            except Exception as e:
                self.log("WARN", f"加密密码失败({e})，尝试明文…")
                d5 = self._post("/auth/verify-code", json={"code": code, "token": jwt, "password": self.password, "password_encrypted": False, "user_area": self.country, "r": random.random()})
                if d5.get("status") != 0: raise RuntimeError(f"verify-code: {d5}")
                self._pw_val, self._pw_enc = self.password, False
            self.log("OK", "注册成功！")

            self.log("INFO", "Step6 提交隐私协议（注册前）…")
            try:
                self._post("/user/privacy/agreement", json={"country": "other", "privacy_version": "", "is_marketing_agreement": False, "source": "web", "email": email, "password": self.password, "password_encrypted": True, "r": random.random()})
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
