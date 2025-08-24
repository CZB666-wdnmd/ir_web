#!/usr/bin/env python3
"""
ir_web.py — 服务端控制重复（修正版，修复模板花括号导致的 IndexError）

用法:
  sudo python3 ir_web.py [--host HOST] [--port PORT] [--device /dev/lirc0] [--keyfile key.json] [--repeat-interval 100] [--max-hold 5]
"""
import http.server
import socketserver
import json
import os
import argparse
import subprocess
import threading
import time
import uuid
from urllib.parse import urlparse

parser = argparse.ArgumentParser()
parser.add_argument("--host", default="0.0.0.0", help="监听地址，默认 0.0.0.0")
parser.add_argument("--port", type=int, default=8000, help="监听端口，默认 8000")
parser.add_argument("--device", default="/dev/lirc0", help="ir device，默认 /dev/lirc0")
parser.add_argument("--keyfile", default="key.json", help="按键 JSON 文件，默认 key.json")
parser.add_argument("--repeat-interval", type=int, default=100, help="重复间隔毫秒 (默认100)")
parser.add_argument("--max-hold", type=float, default=5.0, help="最长按住秒数（超过则服务端自动触发 up），默认5秒")
parser.add_argument("--template", default="template.html",
                    help="HTML 模板文件（包含占位符 __KEYMAP_JSON__, __REPEAT_INTERVAL__, __MAX_HOLD__, __GEN_UUID__），默认 template.html")
parser.add_argument("--keylayout", default="key_layout.json",
                    help="按键布局文件，默认 key_layout.json（可选）")
args = parser.parse_args()

KEYFILE = args.keyfile
IR_DEVICE = args.device
REPEAT_INTERVAL_MS = max(1, args.repeat_interval)
MAX_HOLD_S = max(0.1, args.max_hold)

if not os.path.exists(KEYFILE):
    raise SystemExit(f"找不到 {KEYFILE}，请把 key.json 放在同目录或用 --keyfile 指定路径。")

with open(KEYFILE, "r", encoding="utf-8") as f:
    try:
        KEYMAP = json.load(f)
    except Exception as e:
        raise SystemExit(f"解析 {KEYFILE} 出错: {e}")

# 读取 HTML 模板文件
TEMPLATE_FILE = args.template
if not os.path.exists(TEMPLATE_FILE):
    raise SystemExit(f"找不到 HTML 模板文件 {TEMPLATE_FILE}，请把 template.html 放在同目录或用 --template 指定路径。")
with open(TEMPLATE_FILE, "r", encoding="utf-8") as tf:
    HTML_TEMPLATE = tf.read()

if not isinstance(KEYMAP, dict):
    raise SystemExit("key.json 必须是一个对象 (map)。")

KEY_LAYOUT_FILE = args.keylayout
if os.path.exists(KEY_LAYOUT_FILE):
    with open(KEY_LAYOUT_FILE, "r", encoding="utf-8") as f:
        try:
            KEY_LAYOUT = json.load(f)
        except Exception as e:
            raise SystemExit(f"解析 {KEY_LAYOUT_FILE} 出错: {e}")
else:
    KEY_LAYOUT = None

# 活动按键跟踪结构
active_presses = {}
active_lock = threading.Lock()

def send_scancodes_for_key(key_name):
    if key_name not in KEYMAP:
        return False, f"Unknown key: {key_name}"

    scs = KEYMAP[key_name]
    if not isinstance(scs, list) or not scs:
        return False, f"Key {key_name} has no scancodes"

    for sc in scs:
        cmd = ["ir-ctl", "-d", IR_DEVICE, "--scancode", f"nec:{sc}"]
        try:
            subprocess.run(cmd, check=True)
        except subprocess.CalledProcessError as e:
            return False, f"Command failed: {' '.join(cmd)} -> {e.returncode}"
        except FileNotFoundError:
            return False, "ir-ctl not found (请安装 v4l-utils 或确保 ir-ctl 在 PATH)"
        except Exception as e:
            return False, f"执行出错: {e}"
    return True, "OK"

def repeat_thread_func(client_id, key_name, stop_event):
    start = time.monotonic()
    ok, msg = send_scancodes_for_key(key_name)
    print(f"[{client_id}] {key_name} initial send -> {ok}, {msg}")
    interval = REPEAT_INTERVAL_MS / 1000.0
    while not stop_event.is_set():
        elapsed = time.monotonic() - start
        if elapsed >= MAX_HOLD_S:
            print(f"[{client_id}] {key_name} reached max hold {MAX_HOLD_S}s, auto stopping.")
            break
        if stop_event.wait(interval):
            break
        ok, msg = send_scancodes_for_key(key_name)
        print(f"[{client_id}] {key_name} repeat send -> {ok}, {msg}")
    with active_lock:
        active_presses.pop((client_id, key_name), None)
    print(f"[{client_id}] {key_name} thread exit.")

class Handler(http.server.BaseHTTPRequestHandler):
    def _set_json_headers(self, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _set_html_headers(self, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/" or path == "/index.html":
            # 使用占位符替换，避免 .format() 对大量 {} 的误解析
            page = HTML_TEMPLATE \
                .replace("__KEYMAP_JSON__", json.dumps(KEYMAP, ensure_ascii=False)) \
                .replace("__KEY_LAYOUT_JSON__", json.dumps(KEY_LAYOUT, ensure_ascii=False)) \
                .replace("__REPEAT_INTERVAL__", str(REPEAT_INTERVAL_MS)) \
                .replace("__MAX_HOLD__", str(MAX_HOLD_S)) \
                .replace("__GEN_UUID__", str(uuid.uuid4()))
            self._set_html_headers(200)
            self.wfile.write(page.encode("utf-8"))
            return
        elif path == "/key.json":
            self._set_json_headers(200)
            self.wfile.write(json.dumps(KEYMAP, ensure_ascii=False).encode("utf-8"))
            return
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not found")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/action":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b""
            try:
                data = json.loads(raw.decode("utf-8") if raw else "{}")
                action = data.get("action")
                key = data.get("key")
                client_id = data.get("client_id")
                if not action or not key or not client_id:
                    self._set_json_headers(400)
                    self.wfile.write(json.dumps({"ok": False, "error": "missing action/key/client_id"}).encode("utf-8"))
                    return
            except Exception as e:
                self._set_json_headers(400)
                self.wfile.write(json.dumps({"ok": False, "error": f"invalid json: {e}"}).encode("utf-8"))
                return

            if action == "click":
                ok, msg = send_scancodes_for_key(key)
                if ok:
                    self._set_json_headers(200)
                    self.wfile.write(json.dumps({"ok": True, "msg": "sent"}).encode("utf-8"))
                else:
                    self._set_json_headers(500)
                    self.wfile.write(json.dumps({"ok": False, "error": msg}).encode("utf-8"))
                return

            elif action == "down":
                with active_lock:
                    k = (client_id, key)
                    if k in active_presses:
                        self._set_json_headers(200)
                        self.wfile.write(json.dumps({"ok": True, "msg": "already down"}).encode("utf-8"))
                        return
                    stop_event = threading.Event()
                    th = threading.Thread(target=repeat_thread_func, args=(client_id, key, stop_event), daemon=True)
                    active_presses[k] = {"thread": th, "stop_event": stop_event, "start_time": time.monotonic()}
                    th.start()
                self._set_json_headers(200)
                self.wfile.write(json.dumps({"ok": True, "msg": "started"}).encode("utf-8"))
                return

            elif action == "up":
                with active_lock:
                    k = (client_id, key)
                    info = active_presses.get(k)
                    if not info:
                        self._set_json_headers(200)
                        self.wfile.write(json.dumps({"ok": True, "msg": "not active"}).encode("utf-8"))
                        return
                    info["stop_event"].set()
                self._set_json_headers(200)
                self.wfile.write(json.dumps({"ok": True, "msg": "stopping"}).encode("utf-8"))
                return
            else:
                self._set_json_headers(400)
                self.wfile.write(json.dumps({"ok": False, "error": "unknown action"}).encode("utf-8"))
                return
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not found")

    def log_message(self, format, *args):
        print("%s - - [%s] %s" % (self.client_address[0], self.log_date_time_string(), format%args))

class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    server = ThreadedHTTPServer((args.host, args.port), Handler)
    print(f"Starting server on http://{args.host}:{args.port}/")
    print(f"Using key file: {KEYFILE}")
    print(f"IR device: {IR_DEVICE}")
    print(f"Repeat interval: {REPEAT_INTERVAL_MS} ms, Max hold: {MAX_HOLD_S} s")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Shutting down...")
    finally:
        with active_lock:
            for k, info in list(active_presses.items()):
                info["stop_event"].set()
        server.shutdown()
        server.server_close()
