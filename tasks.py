"""Background task runner for batch registration jobs."""

import time, queue, threading
from datetime import datetime
from typing import Dict

from core import ENVS, PASSWORD, GuerrillaMailProvider, MailTMProvider, PlaudRegistrar

_tasks: Dict[str, dict] = {}

def run_task(task_id: str, cfg: dict):
    q: queue.Queue = _tasks[task_id]["queue"]
    results = []

    def send(t, **kw): q.put({"type": t, **kw})
    def log(lvl, msg): send("log", level=lvl, msg=msg, time=datetime.now().strftime("%H:%M:%S"))

    env_label, base_url = ENVS.get(cfg["env"], ENVS["prod"])
    count = max(1, int(cfg.get("count", 1)))
    use_guerrilla = cfg.get("provider", "guerrilla") != "mailtm"
    password = cfg.get("password", PASSWORD) or PASSWORD
    country_override = cfg.get("country") or None

    def is_stopped(): return bool(_tasks[task_id].get("stop"))

    send("start", total=count, env=env_label, provider="Guerrilla Mail" if use_guerrilla else "mail.tm")
    log("INFO", f"开始注册 {count} 个账号 | 环境: {env_label}")

    for idx in range(count):
        if is_stopped():
            log("WARN", "任务已被停止")
            break

        send("progress", current=idx + 1, total=count)
        log("INFO", f"── 账号 {idx+1}/{count} ─────────────────────")

        provider = GuerrillaMailProvider() if use_guerrilla else MailTMProvider()
        registrar = PlaudRegistrar(base_url, password=password, country_override=country_override, log_fn=log)

        try:
            log("INFO", "获取临时邮箱地址…")
            email = provider.get_email()
            log("OK", f"临时邮箱: {email}")
        except Exception as e:
            log("ERR", f"获取邮箱失败: {e}")
            result = {
                "email": "N/A", "password": password, "token": None,
                "country": "N/A", "env": "测试" if "dev" in base_url else "正式",
                "status": "FAILED", "error": str(e),
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            results.append(result)
            send("result", result=result)
            continue

        result = registrar.register(email, provider, stop_fn=is_stopped)
        result["time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        results.append(result)
        send("result", result=result)

        if idx < count - 1 and not is_stopped():
            time.sleep(2)

    success = sum(1 for r in results if r["status"] == "SUCCESS")
    actual = len(results)
    log("INFO" if success < actual else "OK", f"完成：{success}/{actual} 成功")
    send("done", success=success, total=actual, results=results)
    _tasks[task_id]["done"] = True
    q.put(None)
