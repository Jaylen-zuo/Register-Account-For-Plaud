"""Plaud Auto Registration Tool — Web UI (Flask entry point)."""

import os, sys, io, warnings
warnings.filterwarnings("ignore")

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

import time, json, queue, threading
from flask import Flask, Response, request, jsonify, render_template
from tasks import _tasks, run_task


def resource_path(rel: str) -> str:
    """Resolve path to bundled resource (works both in dev and PyInstaller exe)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


app = Flask(
    __name__,
    template_folder=resource_path("templates"),
    static_folder=resource_path("static"),
)


@app.route("/")
def index():
    return render_template("index.html")


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
            yield f"data: {json.dumps({'type': 'error', 'msg': 'task not found'})}\n\n"
            return
        q = _tasks[task_id]["queue"]
        while True:
            try:
                msg = q.get(timeout=30)
            except queue.Empty:
                yield f"data: {json.dumps({'type': 'ping'})}\n\n"
                continue
            if msg is None:
                break
            yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/stop/<task_id>", methods=["POST"])
def api_stop(task_id):
    if task_id in _tasks:
        _tasks[task_id]["stop"] = True
    return jsonify({"ok": True})



def open_browser(port: int):
    import webbrowser
    time.sleep(1.0)
    webbrowser.open(f"http://127.0.0.1:{port}")


if __name__ == "__main__":
    port = 5000
    print(f"\n Plaud 自动注册工具 — Web UI")
    print(f" 浏览器访问: http://127.0.0.1:{port}")
    print(f" 按 Ctrl+C 退出\n")
    threading.Thread(target=open_browser, args=(port,), daemon=True).start()
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
