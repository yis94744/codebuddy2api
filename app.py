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

# CA 证书兜底：必须在任何 httpx.Client 构造之前完成。
# 打包漏收 certifi 的 cacert.pem 时，这里会自动换用其它可用证书，
# 否则所有 httpx 请求都会以 Errno 2 失败（详见 ssl_bootstrap 模块注释）。
try:
    import ssl_bootstrap
    ssl_bootstrap.install()
except Exception:
    pass


def _run_selfcheck():
    """打包自检入口：结果写 selfcheck.log。

    放在 customtkinter 导入之前，这样即使 GUI 依赖本身没打进包，
    自检也能跑完并落盘。--noconsole 模式下 stdout 是黑洞，所以写文件。
    """
    base = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
            else os.path.dirname(os.path.abspath(__file__)))
    try:
        import selfcheck
        return selfcheck.main()
    except Exception:
        import traceback as _tb
        try:
            with open(os.path.join(base, "selfcheck.log"), "w",
                      encoding="utf-8") as f:
                f.write("selfcheck 自身异常:\n" + _tb.format_exc())
        except Exception:
            pass
        return 2


if "--selfcheck" in sys.argv:
    raise SystemExit(_run_selfcheck())

import customtkinter as ctk

# PyInstaller 打包后 __file__ 指向临时解压目录，配置文件须放 exe 所在目录
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
# 允许用 CB2A_APPDIR 指定配置目录（多实例并行/测试时用，避免互相抢端口）
if os.environ.get("CB2A_APPDIR"):
    APP_DIR = os.environ["CB2A_APPDIR"]
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
LOG_PATH = os.path.join(APP_DIR, "server.log")


def asset_path(name):
    """定位随包资源。PyInstaller onefile 下资源解压到 sys._MEIPASS。"""
    for base in (getattr(sys, "_MEIPASS", None), APP_DIR,
                 os.path.dirname(os.path.abspath(__file__))):
        if not base:
            continue
        p = os.path.join(base, "assets", name)
        if os.path.exists(p):
            return p
    return None


def set_window_icon(root):
    """设置窗口/任务栏图标。

    PyInstaller 的 --icon 只改 exe 文件本身的图标资源，**不会**改运行时窗口
    图标——tkinter 默认显示自带的羽毛图标。之前桌面快捷方式图标对了、但
    任务栏和标题栏还是羽毛，就是这个原因。这里显式加载 assets/icon.ico。
    """
    ico = asset_path("icon.ico")
    if not ico:
        return False
    # 注意：必须用位置参数 iconbitmap(path) 才会作用于**本窗口**。
    # 写成 iconbitmap(default=path) 只是设置子窗口的默认图标，主窗口
    # 任务栏图标不会变——这是很容易踩的坑。
    try:
        root.iconbitmap(ico)
        # 再设一次默认值，让后续弹出的 Toplevel（消息框等）继承同一图标
        try:
            root.iconbitmap(default=ico)
        except Exception:
            pass
        return True
    except Exception:
        pass
    # 兜底：iconbitmap 对非标准 ICO 可能失败，改用 PhotoImage
    try:
        png = asset_path("icon.png")
        if png:
            import tkinter as tk
            img = tk.PhotoImage(file=png)
            root.iconphoto(True, img)
            root._icon_ref = img      # 防止被 GC 回收导致图标消失
            return True
    except Exception:
        pass
    return False

# 单实例互斥：Windows 全局 Mutex 名（固定字符串，跨实例识别）
MUTEX_NAME = "Global\\CodeBuddy2API_RunMutex_8f3a"

DEFAULTS = {
    "host": "127.0.0.1",
    "port": 8787,
    "api_key": "123456",
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
        self._refresh_evt = threading.Event()
        self._refresh_flash_until = 0.0
        self.seen = set()
        self._build()
        self._start_service()
        threading.Thread(target=self._poll, daemon=True).start()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build(self):
        self.root.title("CodeBuddy2API · 积分池网关")
        # 加宽加高：账号行现在带养虾数据（⚡能量 🦐虾 连续天数），且不再截断到 6 个
        self.root.geometry("560x760")
        self.root.minsize(520, 640)
        set_window_icon(self.root)

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
        # 用等宽字体，账号名/状态/余额/养虾数据列能对齐，扫一眼就看清
        self.lbl_pool = ctk.CTkLabel(card, text="", anchor="w", justify="left",
                                     font=ctk.CTkFont(family="Consolas", size=11),
                                     text_color="gray80")
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
        btns.grid_columnconfigure((0, 1, 2, 3), weight=1)
        self.btn_toggle = ctk.CTkButton(
            btns, text="停止服务", command=self._on_toggle,
            fg_color="#ff453a", hover_color="#cc3a30", corner_radius=8,
            font=ctk.CTkFont(size=12))
        self.btn_toggle.grid(row=0, column=0, sticky="ew", padx=(0, 4), pady=(0, 4))
        self.btn_ui = ctk.CTkButton(
            btns, text="管理面板", command=self._open_ui,
            corner_radius=8, font=ctk.CTkFont(size=12))
        self.btn_ui.grid(row=0, column=1, sticky="ew", padx=4, pady=(0, 4))
        self.btn_refresh = ctk.CTkButton(
            btns, text="↻ 刷新", command=self._refresh_now,
            corner_radius=8, font=ctk.CTkFont(size=12),
            fg_color="#2c6e49", hover_color="#22553a")
        self.btn_refresh.grid(row=0, column=2, sticky="ew", padx=4, pady=(0, 4))
        self.btn_copy = ctk.CTkButton(
            btns, text="复制配置", command=self._copy,
            corner_radius=8, font=ctk.CTkFont(size=12))
        self.btn_copy.grid(row=0, column=3, sticky="ew", padx=(4, 0), pady=(0, 4))
        self.btn_checkin = ctk.CTkButton(
            btns, text="🎁 立即签到", command=self._checkin_now,
            fg_color="#3a6f3a", hover_color="#2d5a2d", corner_radius=8,
            font=ctk.CTkFont(size=12))
        self.btn_checkin.grid(row=1, column=0, columnspan=4, sticky="ew", padx=0)

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

    def _refresh_now(self):
        """立即刷新界面数据，不用等轮询周期（不改动服务进程）。

        只触发轮询线程立刻再跑一轮；服务本身不动，所以不会中断正在处理的请求。
        """
        self.btn_refresh.configure(text="刷新中…", state="disabled")
        # 立刻给出可见反馈，并立刻用闪烁副本覆盖 1 秒的轮询文案；
        # 不依赖轮询成功与否，否则后端异常时按钮像是没反应。
        stamp = time.strftime("%H:%M:%S")
        self._refresh_flash_until = time.time() + 3.0
        self.lbl_sub.configure(text="✔ 已刷新 · " + stamp)
        self._refresh_evt.set()
        self.root.after(1200, self._refresh_done)

    def _refresh_done(self):
        self.btn_refresh.configure(text="↻ 刷新", state="normal")

    def _copy(self):
        txt = ("Base URL : %s/v1\nAPI Key  : %s\n"
               "Models   : glm-5.2, kimi-k2.7, deepseek-v4-pro, auto"
               % (self.base, self.key))
        self.root.clipboard_clear()
        self.root.clipboard_append(txt)

    def _checkin_now(self):
        """手动立即签到：调用后端 /api/billing/checkin（后台线程，不卡 UI）。"""
        self.btn_checkin.configure(state="disabled", text="签到中…")
        threading.Thread(target=self._checkin_worker, daemon=True).start()

    def _checkin_worker(self):
        import tkinter.messagebox as mb
        try:
            req = urllib.request.Request(
                self.base + "/api/billing/checkin", data=b"{}",
                headers={"Authorization": "Bearer " + self.key,
                         "Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=20) as resp:
                r = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:
            self.q.put(("addresult", ("签到", "签到失败：%s" % e)))
            return
        lines = []
        for x in (r.get("results") or []):
            nm = x.get("nickname") or x.get("name", "?")
            if x.get("already"):
                lines.append("· %s：今日已签到" % nm)
            elif x.get("checked"):
                lines.append("· %s：+%s 积分（连签 %s 天）" % (
                    nm, x.get("awarded", ""), x.get("streak", "")))
            elif x.get("message"):
                lines.append("· %s：%s" % (nm, x.get("message")))
        msg = "\n".join(lines) or (r.get("message") or "签到完成")
        self.q.put(("addresult", ("签到", "签到结果：\n" + msg)))

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
            # 手动刷新：立即进入下一轮，而不是等满 1 秒
            if self._refresh_evt.is_set():
                self._refresh_evt.clear()
                continue
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
            self.btn_checkin.configure(state="normal", text="🎁 立即签到")
            title, body = item[1]
            mb.showinfo(title, body)
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
        # 手动刷新的反馈要压住轮询文案，否则提示会在 1 秒内被覆盖掉
        if time.time() < getattr(self, "_refresh_flash_until", 0):
            sub = "✔ 已刷新 · " + time.strftime("%H:%M:%S")
        self.lbl_sub.configure(text=sub)
        self.btn_toggle.configure(text="停止服务")
        ps = (accts or {}).get("pool_stats", {}) or {}
        active = accts.get("active_name", "?")
        self.lbl_addr.configure(text="地址  %s/v1\nKey   %s" % (self.base, self.key))
        ok = ps.get("ok", 0)
        tot = ps.get("total", 0)
        cool = ps.get("cooldown", 0)
        # 展示全部账号（原先硬编码 [:6] 会截断，账号一多就看不到后面的）。
        # 每行附带养虾数据：⚡能量 / 🦐虾数 / 连续签到天数。
        rows = []
        all_accts = accts.get("accounts") or []
        for a in all_accts:
            nm = a.get("name", "?")
            # 过长的名字（企业号常是完整邮箱）压到 10 字符内，避免撑乱等宽列。
            # 注意保留「尾部」——多个企业号往往只有结尾数字不同
            # （WeChatGame8/9/10），截头去尾会全部变成同一个名字。
            if len(nm) > 10:
                local = nm.split("@", 1)[0] if "@" in nm else nm
                nm = local[:2] + "…" + local[-5:] if len(local) > 7 else nm[:9] + "…"
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
            # 养虾数据：企业账号 or 尚未采集时不显示
            g = a.get("growth") or {}
            if g and not g.get("note"):
                bal += "  ⚡%s 🦐%s" % (g.get("energy", 0), g.get("buddies", 0))
                sd = g.get("streak_days")
                if sd is not None:
                    bal += " 连%s天" % sd
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
        # 注意：必须用 use_last_error=True + ctypes.get_last_error() 取错误码。
        # 默认的 ctypes.windll 不保存线程 last-error，CreateMutexW 与 GetLastError
        # 之间 ctypes 自身的调用会把错误码冲掉，单实例判断可能被误判成
        # 「已在运行」——表现为窗口一闪即退、连崩溃日志都没有。
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        mutex = k32.CreateMutexW(None, False, MUTEX_NAME)
        if mutex and ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            # 双重校验：只有端口确实被占用才认为是真的已有实例在跑，
            # 避免残留互斥体导致启动被静默拦下。
            _port = int(DEFAULTS["port"])
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as _f:
                    _port = int(json.load(_f).get("port", _port))
            except Exception:
                pass
            _busy = False
            try:
                import socket as _sk
                with _sk.create_connection(("127.0.0.1", _port), timeout=1):
                    _busy = True
            except Exception:
                _busy = False
            if _busy:
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
