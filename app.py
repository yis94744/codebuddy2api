# -*- coding: utf-8 -*-
"""CodeBuddy2API 桌面启动器：现代化深色 UI，一键拉起本地积分转换服务。

使用 customtkinter 实现圆角卡片式界面。
服务以子进程方式托管，关窗即停。
"""
import json
import os
import queue
import sys
import threading
import time
import urllib.request
import webbrowser

import customtkinter as ctk

# PyInstaller 打包后 __file__ 指向临时解压目录，配置文件须放 exe 所在目录
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
LOG_PATH = os.path.join(APP_DIR, "server.log")

# 单实例互斥：Windows 全局 Mutex 名（固定字符串，跨实例识别）
MUTEX_NAME = "Global\\CodeBuddy2API_RunMutex_8f3a"

DEFAULTS = {
    "host": "127.0.0.1",
    "port": 8000,
    "api_key": "sk-cb2a-local",
    "strategy": "failover",
}


def load_cfg():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return cfg


def _get(path, base, key):
    req = urllib.request.Request(base + path, headers={"Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=3) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


class App:
    def __init__(self, root):
        self.root = root
        self.cfg = load_cfg()
        self.base = "http://%s:%s" % (self.cfg["host"], self.cfg["port"])
        self.key = self.cfg["api_key"]
        self.proc = None
        self._thread = None
        self.q = queue.Queue()
        self.stop_evt = threading.Event()
        self.seen = set()
        self._build()
        self._start_service()
        threading.Thread(target=self._poll, daemon=True).start()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build(self):
        self.root.title("CodeBuddy2API · 积分池网关")
        self.root.geometry("480x680")
        self.root.minsize(420, 600)

        # 主容器
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(3, weight=1)

        # ---- 状态头 ----
        hdr = ctk.CTkFrame(self.root, corner_radius=0, fg_color="transparent")
        hdr.grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 8))
        self.lbl_dot = ctk.CTkLabel(hdr, text="●", text_color="#ffd60a",
                                    font=ctk.CTkFont(size=20))
        self.lbl_dot.pack(side="left")
        self.lbl_state = ctk.CTkLabel(hdr, text="启动中…",
                                      font=ctk.CTkFont(size=18, weight="bold"))
        self.lbl_state.pack(side="left", padx=(4, 0))
        self.lbl_sub = ctk.CTkLabel(hdr, text="",
                                    font=ctk.CTkFont(size=11),
                                    text_color="gray60")
        self.lbl_sub.pack(side="left", padx=(8, 0), pady=(4, 0))

        # ---- 服务信息卡 ----
        card = ctk.CTkFrame(self.root, corner_radius=14)
        card.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 8))
        card.grid_columnconfigure(0, weight=1)
        self.lbl_addr = ctk.CTkLabel(card, text="", anchor="w",
                                     font=ctk.CTkFont(family="Consolas", size=11))
        self.lbl_addr.grid(row=0, column=0, sticky="ew", padx=14, pady=(10, 4))
        self.lbl_pool = ctk.CTkLabel(card, text="", anchor="w", justify="left",
                                     font=ctk.CTkFont(size=11), text_color="gray80")
        self.lbl_pool.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 10))

        # ---- 统计双卡 ----
        stat = ctk.CTkFrame(self.root, fg_color="transparent")
        stat.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 8))
        stat.grid_columnconfigure((0, 1), weight=1)

        c1 = ctk.CTkFrame(stat, corner_radius=10)
        c1.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        ctk.CTkLabel(c1, text="今日请求", text_color="gray60",
                     font=ctk.CTkFont(size=10)).pack(padx=12, pady=(8, 0))
        self.lbl_req = ctk.CTkLabel(c1, text="0",
                                    font=ctk.CTkFont(size=22, weight="bold"))
        self.lbl_req.pack(padx=12, pady=(0, 8))

        c2 = ctk.CTkFrame(stat, corner_radius=10)
        c2.grid(row=0, column=1, sticky="ew", padx=(4, 0))
        ctk.CTkLabel(c2, text="积分消耗", text_color="gray60",
                     font=ctk.CTkFont(size=10)).pack(padx=12, pady=(8, 0))
        self.lbl_credit = ctk.CTkLabel(c2, text="0", text_color="#ffd60a",
                                       font=ctk.CTkFont(size=22, weight="bold"))
        self.lbl_credit.pack(padx=12, pady=(0, 8))

        # ---- 账单流水 ----
        ttl = ctk.CTkFrame(self.root, fg_color="transparent")
        ttl.grid(row=3, column=0, sticky="nsew", padx=16, pady=(0, 8))
        ttl.grid_columnconfigure(0, weight=1)
        ttl.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(ttl, text="实时账单", text_color="gray60",
                     font=ctk.CTkFont(size=10, weight="bold")).grid(
            row=0, column=0, sticky="w")
        self.txt = ctk.CTkTextbox(ttl, corner_radius=10, font=ctk.CTkFont(
            family="Consolas", size=11), wrap="word")
        self.txt.grid(row=1, column=0, sticky="nsew")
        self.txt.configure(state="disabled")

        # ---- 按钮 ----
        btns = ctk.CTkFrame(self.root, fg_color="transparent")
        btns.grid(row=4, column=0, sticky="ew", padx=16, pady=(0, 16))
        btns.grid_columnconfigure((0, 1, 2), weight=1)
        self.btn_toggle = ctk.CTkButton(
            btns, text="停止服务", command=self._on_toggle,
            fg_color="#ff453a", hover_color="#cc3a30", corner_radius=8,
            font=ctk.CTkFont(size=12))
        self.btn_toggle.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.btn_ui = ctk.CTkButton(
            btns, text="管理面板", command=self._open_ui,
            corner_radius=8, font=ctk.CTkFont(size=12))
        self.btn_ui.grid(row=0, column=1, sticky="ew", padx=4)
        self.btn_add = ctk.CTkButton(
            btns, text="添加账号", command=self._add_account,
            fg_color="#2d3a5f", hover_color="#3a4a7a", corner_radius=8,
            font=ctk.CTkFont(size=12))
        self.btn_add.grid(row=0, column=2, sticky="ew", padx=4)
        self.btn_copy = ctk.CTkButton(
            btns, text="复制配置", command=self._copy,
            corner_radius=8, font=ctk.CTkFont(size=12))
        self.btn_copy.grid(row=0, column=3, sticky="ew", padx=(4, 0))

    # -- 服务生命周期（进程内启动，适配 PyInstaller 打包） ----------
    def _start_service(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self.q.put(("state", "running"))

    def _run(self):
        try:
            import converter
            converter.start(
                host=self.cfg["host"], port=self.cfg["port"],
                api_key=self.key, strategy=self.cfg["strategy"],
                log_path=LOG_PATH, skip_check=True, with_ui=True,
            )
        except BaseException as e:  # uvicorn 端口占用会抛 SystemExit，须一并捕获
            import traceback
            crash = os.path.join(os.path.expanduser("~"), "cb2a_crash.log")
            with open(crash, "w", encoding="utf-8") as f:
                f.write("_run:\n" + traceback.format_exc())
            self.q.put(("state", "error:" + str(e)[:120]))

    def _stop_service(self):
        try:
            import converter
            converter.stop()
        except Exception:
            pass
        self.q.put(("state", "stopped"))

    def _on_toggle(self):
        if self._thread and self._thread.is_alive():
            self._stop_service()
        else:
            self._start_service()

    def _open_ui(self):
        webbrowser.open(self.base + "/")

    def _copy(self):
        txt = ("Base URL : %s/v1\nAPI Key  : %s\n"
               "Models   : glm-5.2, kimi-k2.7, deepseek-v4-pro, auto"
               % (self.base, self.key))
        self.root.clipboard_clear()
        self.root.clipboard_append(txt)

    def _add_account(self):
        """一键添加账号：打开 CodeBuddy CN 登录，后台轮询检测并自动导入。"""
        import tkinter.messagebox as mb
        try:
            _get("/api/status", self.base, self.key)  # 确保服务在线
        except Exception:
            mb.showinfo("添加账号", "服务未运行，请先启动服务")
            return
        req = urllib.request.Request(
            self.base + "/api/accounts/cn/open", data=b"{}",
            headers={"Authorization": "Bearer " + self.key,
                     "Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                r = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:
            mb.showerror("添加账号", f"打开 CodeBuddy 失败：{e}")
            return
        mb.showinfo("添加账号", (r.get("message") or "已打开 CodeBuddy CN") +
                    "\n\n请在 CodeBuddy 窗口完成登录。\n登录成功后本窗口会自动检测并导入新账号（每 3 秒检查一次，最多 3 分钟）。")
        self.btn_add.configure(state="disabled", text="等待登录中…")
        threading.Thread(target=self._cn_wait, daemon=True).start()

    def _cn_wait(self):
        """后台轮询检测新账号，发现后自动导入并提示。"""
        import tkinter.messagebox as mb
        deadline = time.time() + 180
        while not self.stop_evt.is_set() and time.time() < deadline:
            try:
                d = _get("/api/accounts/cn/detect", self.base, self.key)
                if d.get("found"):
                    imp = _get("/api/accounts/cn/import", self.base, self.key)
                    msg = (imp.get("message") or "导入完成") if imp.get("ok") \
                        else (imp.get("message") or "导入失败")
                    self.q.put(("addresult", msg))
                    return
            except Exception:
                pass
            time.sleep(3)
        self.q.put(("addresult", "等待超时（3 分钟），未检测到新账号。"
                                   "可在管理面板 → 添加账号 里重试。"))

    # -- 轮询 ----------------------------------------------------------
    def _poll(self):
        while not self.stop_evt.is_set():
            alive = self._thread is not None and self._thread.is_alive()
            try:
                st = _get("/api/status", self.base, self.key)
                accts = _get("/api/accounts", self.base, self.key)
                stats = _get("/api/logs/stats", self.base, self.key)
                logs = _get("/api/logs?limit=80", self.base, self.key).get("items", [])
                try:
                    bill = _get("/api/billing/status", self.base, self.key)
                except Exception:
                    bill = None
                self.q.put(("ok", alive, st, accts, stats, logs, bill))
            except Exception:
                self.q.put(("wait" if alive else "down",))
            self.stop_evt.wait(1.0)
        # 线程退出前确保服务已停
        try:
            import converter
            converter.stop()
        except Exception:
            pass

    def _drain(self):
        while True:
            try:
                item = self.q.get_nowait()
            except queue.Empty:
                break
            self._apply(item)
        self.root.after(500, self._drain)

    def _apply(self, item):
        kind = item[0]
        if kind == "state":
            self._render_state(item[1])
        elif kind == "addresult":
            import tkinter.messagebox as mb
            self.btn_add.configure(state="normal", text="添加账号")
            mb.showinfo("添加账号", item[1])
        elif kind == "wait":
            self.lbl_dot.configure(text_color="#ffd60a")
            self.lbl_state.configure(text="服务启动中…")
            self.lbl_sub.configure(text="正在等待后端就绪")
            self.btn_toggle.configure(text="停止服务")
        elif kind == "down":
            self.lbl_dot.configure(text_color="#ff453a")
            self.lbl_state.configure(text="服务未运行")
            self.lbl_sub.configure(text="")
            self.btn_toggle.configure(text="启动服务")
        elif kind == "ok":
            _, alive, st, accts, stats, logs, bill = item
            self._render_ok(st, accts, stats, logs, bill)

    def _render_state(self, s):
        if s == "running":
            self.lbl_dot.configure(text_color="#ffd60a")
            self.lbl_state.configure(text="服务启动中…")
            self.lbl_sub.configure(text="正在等待后端就绪")
            self.btn_toggle.configure(text="停止服务")
        elif s == "stopped":
            self.lbl_dot.configure(text_color="#ff453a")
            self.lbl_state.configure(text="服务已停止")
            self.lbl_sub.configure(text="点击下方「启动服务」")
            self.lbl_addr.configure(text="")
            self.lbl_pool.configure(text="")
            self.btn_toggle.configure(text="启动服务")
        elif s.startswith("error"):
            self.lbl_dot.configure(text_color="#ff453a")
            self.lbl_state.configure(text="启动失败")
            self.lbl_addr.configure(text=s[6:])
            self.btn_toggle.configure(text="重试启动")

    def _render_ok(self, st, accts, stats, logs, bill=None):
        self.lbl_dot.configure(text_color="#34c759")
        self.lbl_state.configure(text="服务运行中")
        sub = time.strftime("已运行 %Hh%Mm", time.gmtime(st.get("uptime", 0)))
        if bill and bill.get("scheduler_running"):
            lr = bill.get("last_run") or ""
            if lr:
                sub += " · 每日签到 %s" % lr
        self.lbl_sub.configure(text=sub)
        self.btn_toggle.configure(text="停止服务")
        ps = (accts or {}).get("pool_stats", {}) or {}
        active = accts.get("active_name", "?")
        self.lbl_addr.configure(text="地址  %s/v1\nKey   %s" % (self.base, self.key))
        ok = ps.get("ok", 0)
        tot = ps.get("total", 0)
        cool = ps.get("cooldown", 0)
        rows = []
        for a in (accts.get("accounts") or [])[:6]:
            nm = a.get("name", "?")
            h = a.get("health", "?")
            sp = a.get("credit_spent", 0)
            rc = a.get("real_credit")
            star = "★ " if a.get("id") == accts.get("active_id") else "   "
            hicon = {"ok": "✓", "exhausted": "✗", "cooldown": "⏳"}.get(h, "•")
            if rc is not None:
                bal = "真实 %s" % rc
            elif a.get("real_credit_note"):
                bal = "企业版"
            else:
                bal = "累计 %s" % sp
            rows.append("%s%-10s %s %-9s %s" % (star, nm, hicon, h, bal))
        self.lbl_pool.configure(text="账号池 %s/%s（冷却%s） · 主力 %s\n%s" % (
            ok, tot, cool, active, "\n".join(rows)))
        self.lbl_req.configure(text=str((stats or {}).get("today_requests", 0)))
        self.lbl_credit.configure(text=str((stats or {}).get("today_credit", 0)))

        # 账单流水
        new = []
        for it in logs:
            lvl = it.get("level", "info")
            if lvl not in ("credit", "error", "warn"):
                continue
            k = "%s|%s|%s" % (it.get("ts"), lvl, it.get("msg", "")[:90])
            if k in self.seen:
                continue
            self.seen.add(k)
            if len(self.seen) > 4000:
                self.seen = set(list(self.seen)[-2000:])
            icon = {"credit": "💰", "error": "❌", "warn": "⚠️"}.get(lvl, "")
            new.append("%s  %s  %s" % (it.get("time", ""), icon,
                                       it.get("msg", "").split("\n")[0]))
        if new:
            self.txt.configure(state="normal")
            for line in new:
                self.txt.insert("end", line + "\n")
            try:
                ln = int(self.txt.index("end-1c").split(".")[0])
                if ln > 300:
                    self.txt.delete("1.0", "%d.0" % (ln - 300))
            except Exception:
                pass
            self.txt.see("end")
            self.txt.configure(state="disabled")

    def _on_close(self):
        self.stop_evt.set()
        try:
            import converter
            converter.stop()
        except Exception:
            pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        self.root.destroy()


def main():
    import traceback
    # PyInstaller --noconsole 模式下 sys.stdout/stderr 可能为 None，
    # uvicorn 的日志 formatter 会调 sys.stdout.isatty() 崩溃，这里兜底。
    import io as _io
    if sys.stdout is None:
        sys.stdout = _io.StringIO()
    if sys.stderr is None:
        sys.stderr = _io.StringIO()
    crash = os.path.join(os.path.expanduser("~"), "cb2a_crash.log")
    # 单实例互斥：已有实例在运行则弹提示并退出，避免端口冲突。
    mutex = None
    try:
        import ctypes
        mutex = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
        if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            ctypes.windll.user32.MessageBoxW(
                0, "CodeBuddy2API 已在运行中。\n请查看任务栏或系统托盘中的现有窗口。",
                "已在运行", 0x40)
            return
    except Exception:
        pass  # 非 Windows 或创建失败时退化为多实例，不强求
    try:
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("green")
        root = ctk.CTk()
        app = App(root)
        root.after(500, app._drain)
        root.mainloop()
    except Exception:
        with open(crash, "w", encoding="utf-8") as f:
            f.write("main:\n" + traceback.format_exc())
    finally:
        if mutex:
            try:
                ctypes.windll.kernel32.ReleaseMutex(mutex)
                ctypes.windll.kernel32.CloseHandle(mutex)
            except Exception:
                pass


if __name__ == "__main__":
    main()
