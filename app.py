# -*- coding: utf-8 -*-
"""CodeBuddy2API 桌面启动器：卡通渐变风格界面，一键拉起本地积分转换服务。

界面是一张 562x792 的位图，贴在 tkinter Canvas 上显示：卡片圆角、角色遮挡、
按钮立体感都来自 assets/ui/base.png 这张静态原画，ui_render 只把实时数据
（URL、账号行、数值、账单）按实测坐标画上去。之所以不用现成的 GUI 组件，
是因为要做出「压在渐变背景上的半透明卡片」，而常规控件只能填纯色。
按钮不做成控件，而是用坐标做命中测试。

素材可用 tools/build_ui_assets.py 从设计稿重新生成。
服务在进程内以线程方式托管，关窗即停。
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

import tkinter as tk

from PIL import ImageTk

import ui_render

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


def _short_event(msg):
    """把事件类日志（签到、账号切换等）压成一行可读文案。"""
    s = (msg or "").strip().splitlines()[0] if (msg or "").strip() else ""
    for pfx in ("💰 ", "【CN自动同步】", "[CN自动同步]"):
        if s.startswith(pfx):
            s = s[len(pfx):]
    return s[:28]


def _parse_tokens(msg):
    """从计费日志里抽出 token 明细，格式如「tok 337226+579=337805」。"""
    try:
        seg = msg.split("tok", 1)[1].split("|")[0].strip()
        return "tok %s" % seg
    except Exception:
        return ""


def fit_window(w, h, margin_w=48, margin_h=72):
    """把窗口尺寸收进系统工作区，并在超宽屏上居中。

    默认 620x880 在 1080p 上刚好，但 1366x768 这类小屏会超出屏幕底部，
    导致按钮点不到——所以先按工作区裁剪，再计算居中偏移。
    """
    try:
        import ctypes
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
        try:
            rect = ctypes.wintypes.RECT()
            ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(rect), 0)
            aw, ah = rect.right - rect.left, rect.bottom - rect.top
            ox, oy = rect.left, rect.top
        except Exception:
            aw = ctypes.windll.user32.GetSystemMetrics(0)
            ah = ctypes.windll.user32.GetSystemMetrics(1)
            ox = oy = 0
        if aw <= 0 or ah <= 0:
            return w, h, None
        w = max(420, min(w, aw - margin_w))
        h = max(560, min(h, ah - margin_h))
        pos = "%d+%d" % (ox + max(0, (aw - w) // 2), oy + max(0, (ah - h) // 2))
        return w, h, pos
    except Exception:
        return w, h, None


def detect_scale():
    """当前屏幕的 DPI 缩放系数（96 DPI = 1.0）。

    自绘界面按逻辑坐标布局、按这个系数放大落笔，高分屏下才不会发虚。
    读不到系统 DPI 时退回 1.0。
    """
    try:
        import ctypes
        try:                                    # Win10 1607+ per-monitor aware
            dpi = ctypes.windll.user32.GetDpiForWindow(
                ctypes.windll.kernel32.GetConsoleWindow() or 0)
        except Exception:
            dpi = 0
        if not dpi:
            dpi = ctypes.windll.user32.GetDpiForSystem()
        if dpi:
            return max(1.0, min(2.5, dpi / 96.0))
    except Exception:
        pass
    return 1.0


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

# 窗口圆角外的透明键色。素材 base.png 的四角已打上这个颜色，
# 配合 -transparentcolor，窗口四角就是真正的圆角，
# 而不是露出截图里的白底（会出现"白色尖角"）。
TRANSPARENT_KEY = "#FF00FE"

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
        # 自绘界面状态
        self._resizing = False
        self._resize_job = None
        self._last_size = None
        self._last_redraw = 0.0
        self._redraw_job = None
        self._build()
        self._start_service()
        threading.Thread(target=self._poll, daemon=True).start()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        # 轮询线程把消息投进队列，这里周期性消费并重绘。
        # 必须在 __init__ 里注册：放在 main() 里的话，任何别的入口
        # （测试、被别的模块拉起）都会导致界面永远停在初始画面。
        self.root.after(500, self._drain)

    def _build(self):
        self.root.title("CodeBuddy2API · 积分池网关")

        # 界面素材是固定构图的整幅画（562x792）：天空渐变、云朵、6 个 Q 版角色、
        # 卡片、按钮、图标都取自参考图原画，不存在用控件拼装的可能，因此整窗就是
        # 一张 Canvas 位图。窗口按素材长宽比呈现，只随 DPI 缩放。
        self._scale = detect_scale()
        self._renderer = ui_render.Renderer(scale=self._scale)
        win_w, win_h = self._renderer.W, self._renderer.H
        win_w, win_h, win_pos = fit_window(win_w, win_h,
                                           margin_w=40, margin_h=80)
        self.root.geometry("%dx%d%s" % (win_w, win_h,
                                        ("+" + win_pos) if win_pos else ""))
        # 常规尺寸，供「最大化」还原时兜底（见 _restore_from_max）。
        # 此刻窗口尚未映射，位置可能是 0,0；_apply_transparency 里会再记一次真实值。
        self._normal_geo = (win_w, win_h,
                            self.root.winfo_x(), self.root.winfo_y())
        self.root.minsize(int(win_w * 0.75), int(win_h * 0.75))
        self.root.configure(bg=TRANSPARENT_KEY)
        set_window_icon(self.root)
        # 素材自带标题栏与窗口按钮，因此隐藏系统标题栏，避免出现两条。
        try:
            self.root.overrideredirect(True)
            self._borderless = True
        except Exception:
            self._borderless = False
        # 让素材四角的键色真正透明 —— 窗口获得圆角。
        # 放在 overrideredirect 之后设置，部分 Tk 版本需要窗口先无边框才生效。
        try:
            self.root.attributes("-transparentcolor", TRANSPARENT_KEY)
        except Exception:
            pass
        # 无边框窗口默认不进任务栏，最小化后就找不回来了；
        # 这里补上任务栏入口（见 _enable_taskbar）。放在透明色之后，
        # 因为它内部会 hide/show 一次窗口。
        self._enable_taskbar()

        self.canvas = tk.Canvas(self.root, highlightthickness=0, bd=0,
                                bg=TRANSPARENT_KEY, cursor="arrow")
        self.canvas.pack(fill="both", expand=True)
        self._canvas_img = None      # 必须保持引用，否则被 GC 回收会白屏
        self._photo = None
        self._frame = None           # 未贴按钮的底帧，供 hover 局部重绘
        self._hover_key = None
        self._rects = []
        self._state = {
            "status": "starting",
            "subtitle": "正在等待后端就绪",
            "addr": "—", "key": "—",
            "pool": {"ok": 0, "total": 0, "cooldown": 0, "active": "—"},
            "accounts": [], "bill": [],
            "today_req": 0, "today_credit": 0,
        }

        # 注意：<Button-1> 与 <ButtonPress-1> 在 Tk 里是**同一个事件**，
        # 而 bind() 默认是「替换」不是「追加」——早先分别绑定
        # _on_click（分派按钮）与 _on_press（拖动起点），后者把前者覆盖了，
        # 结果所有按钮都点不动。现在统一由 _on_press 处理：先做命中分派，
        # 未命中按钮且在标题栏时才进入拖动。
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", lambda e: self._set_hover(None))
        # 账号列表支持滚轮（账号多于可视行数时）
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self._acct_scroll = 0
        self._bill_scroll = 0
        self._state_scroll_reset = True
        # 无边框窗口：标题栏区域拖动、右上角三个按钮自绘实现
        self.canvas.bind("<B1-Motion>", self._on_drag)
        # 双击标题栏切换最大化 —— 桌面软件的通用手势
        self.canvas.bind("<Double-Button-1>", self._on_double_click)
        self._drag_off = None
        self._dragging = False
        self._drag_pending = False
        self._drag_job = None
        self._drag_x = None
        self._drag_y = None
        self.root.bind("<ButtonRelease-1>", self._on_release)
        self.root.bind("<Configure>", self._on_resize)
        self.root.bind("<Map>", self._on_map)
        self.root.bind("<Escape>", lambda e: self._on_close())
        self._restore_borderless_pending = False
        self._maxed = False
        # 首帧渲染要几百毫秒。放到事件循环里做，让窗口先出现再出画面。
        self.root.after(1, self._redraw)
        # 窗口映射后再设一次透明色：部分 Tk 版本只在映射后应用才生效
        self.root.after(60, self._apply_transparency)

    def _apply_transparency(self):
        """窗口显示后再次应用透明色，确保四角圆角生效。"""
        try:
            self.root.attributes("-transparentcolor", TRANSPARENT_KEY)
            self.canvas.configure(bg=TRANSPARENT_KEY)
        except Exception:
            pass
        # 窗口已映射，此时的位置才是真实的：更新常规几何用于最大化还原兜底
        try:
            self._normal_geo = (self.root.winfo_width(),
                                self.root.winfo_height(),
                                self.root.winfo_x(), self.root.winfo_y())
        except Exception:
            pass

    # -- 自绘界面：渲染 / 命中测试 / 交互 ------------------------------
    def _redraw(self):
        """把当前状态渲染成整窗位图。渲染较重，只在数据或尺寸变化时调用。

        拖动窗口期间直接返回：此时任何整窗重绘都会与窗口移动叠加成频闪，
        数据变更由 _set_state 打标记，松手后 _on_release 会补一次。
        """
        if getattr(self, "_dragging", False):
            self._drag_pending = True
            return
        try:
            img = self._renderer.render(self._state)
        except Exception:
            import traceback
            try:
                with open(os.path.join(APP_DIR, "ui_render.log"), "w",
                          encoding="utf-8") as f:
                    f.write(traceback.format_exc())
            except Exception:
                pass
            return
        self._frame = img.copy()
        self._blit(img)
        # 按钮热区（物理像素），点击时用同一份版面数据做命中测试
        self._rects = [(k, x * self._scale, y * self._scale,
                        bw * self._scale, bh * self._scale)
                       for (k, _lb, x, y, bw, bh, _ic)
                       in self._renderer.button_rects()]
        self._hover_key = None

    def _draw_button_labels(self, img):
        """把动态按钮文案画到原画按钮上。

        按钮底图（含图标、渐变、描边）是参考图原画，不能重画；
        只有「停止服务/启动服务」这类文案会变，所以按版面坐标覆盖文字。
        """
        from PIL import ImageDraw, ImageFont
        import ui_render as _ur
        d = ImageDraw.Draw(img, "RGBA")
        L = self._renderer.layout
        s = self._scale
        # 参考图原画里按钮文字已经画好；只有文案**确实不同**时才覆盖，
        # 否则会出现「停止服务」和「停止服务」叠在一起的重影。
        want = {
            "stop": self._state.get("toggle_label", "停止服务"),
            "refresh": self._state.get("refresh_label", "刷新"),
            "checkin": self._state.get("checkin_label", "立即签到"),
        }
        origin = {"stop": "停止服务", "refresh": "刷新", "checkin": "立即签到"}
        labels = {k: v for k, v in want.items() if v != origin[k]}
        # 各按钮文字的左边界与中心（实测自参考图，扣掉左侧图标占位）
        TXT = {"stop": (96, 711), "panel": (214, 711), "refresh": (340, 711),
               "copy": (447, 711), "checkin": (250, 770)}
        for b in L["buttons"]:
            k = b["key"]
            tx, ty = TXT.get(k, ((b["x0"] + b["x1"]) / 2, (b["y0"] + b["y1"]) / 2))
            size = 17 if k != "checkin" else 19
            f = _ur.load_font(max(9, int(size * s)), True)
            d.text((tx * s, ty * s), labels.get(k, b["label"]), font=f,
                   fill=(255, 255, 255, 255), anchor="lm")

    def _blit(self, img):
        """把一张位图显示到画布上（维持引用，否则会被 GC 回收）。"""
        self._photo = ImageTk.PhotoImage(img)
        if self._canvas_img is None:
            self._canvas_img = self.canvas.create_image(0, 0, anchor="nw",
                                                        image=self._photo)
        else:
            self.canvas.itemconfigure(self._canvas_img, image=self._photo)
            self.canvas.coords(self._canvas_img, 0, 0)

    def _hit(self, px, py):
        for k, x, y, w, h in self._rects:
            if x <= px <= x + w and y <= py <= y + h:
                return k
        return None

    def _on_click(self, ev):
        k = self._hit(ev.x, ev.y)
        if k == "stop":
            self._on_toggle()
        elif k == "panel":
            self._open_ui()
        elif k == "refresh":
            self._refresh_now()
        elif k == "copy":
            self._copy()
        elif k == "checkin":
            self._checkin_now()
        elif k == "win_min":
            self._win_minimize()
        elif k == "win_max":
            self._win_toggle_max()
        elif k == "win_close":
            self._on_close()

    # -- 无边框窗口的三个标题栏按钮（原画里画着，但系统不会代理点击）--
    # Win32 常量（避免为几个数字引入额外的模块级依赖）
    _GWL_EXSTYLE = -20
    _WS_EX_APPWINDOW = 0x00040000
    _WS_EX_TOOLWINDOW = 0x00000080
    _SW_HIDE, _SW_SHOW, _SW_MINIMIZE, _SW_RESTORE = 0, 5, 6, 9
    _SPI_GETWORKAREA = 48

    def _win_hwnd(self):
        """取顶层窗口的 Win32 HWND。

        Tk 的 winfo_id() 给的是 Tk 自己的窗口，真正的顶层框架是它的父窗口
        （无边框窗口也不例外），所以这里用 GetParent 上溯一层；
        上溯结果为空说明本身就是顶层，直接用原值。
        """
        try:
            import ctypes
            h = self.root.winfo_id()
            parent = ctypes.windll.user32.GetParent(h)
            return parent or h
        except Exception:
            return None

    def _enable_taskbar(self):
        """给无边框窗口补上任务栏入口。

        overrideredirect 窗口是 WS_POPUP，系统默认不把它登记到任务栏——
        于是「最小化」之后用户再也找不回来（只能靠任务管理器）。
        补一个 WS_EX_APPWINDOW 正是该扩展样式的用途；改完必须 hide/show
        一次，任务栏才会重新登记。
        """
        h = self._win_hwnd()
        if not h:
            return
        try:
            import ctypes
            u = ctypes.windll.user32
            ex = u.GetWindowLongW(h, self._GWL_EXSTYLE)
            u.SetWindowLongW(h, self._GWL_EXSTYLE,
                             (ex | self._WS_EX_APPWINDOW)
                             & ~self._WS_EX_TOOLWINDOW)
            u.ShowWindow(h, self._SW_HIDE)
            u.ShowWindow(h, self._SW_SHOW)
        except Exception:
            pass

    def _win_minimize(self):
        """最小化到任务栏（与普通桌面软件一致，点任务栏图标即可还原）。"""
        h = self._win_hwnd()
        if h:
            try:
                import ctypes
                ctypes.windll.user32.ShowWindow(h, self._SW_MINIMIZE)
                return
            except Exception:
                pass
        try:
            self.root.iconify()
        except Exception:
            pass

    def _work_area(self):
        """系统工作区（已排除任务栏），返回 (x, y, w, h)。"""
        try:
            import ctypes
            from ctypes import wintypes
            u = ctypes.windll.user32
            try:
                u.SetProcessDPIAware()
            except Exception:
                pass
            r = wintypes.RECT()
            u.SystemParametersInfoW(self._SPI_GETWORKAREA, 0, ctypes.byref(r), 0)
            w, h = r.right - r.left, r.bottom - r.top
            if w > 0 and h > 0:
                return r.left, r.top, w, h
        except Exception:
            pass
        return 0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight()

    def _max_target(self):
        """最大化的目标尺寸与位置：等比铺满工作区并居中。

        用素材的**逻辑长宽比**计算，与当前 scale 无关。
        """
        ox, oy, aw, ah = self._work_area()
        ratio = self._renderer.lw / float(self._renderer.lh)
        h = ah
        w = int(round(h * ratio))
        if w > aw:
            w = aw
            h = int(round(w / ratio))
        return w, h, ox + (aw - w) // 2, oy + (ah - h) // 2

    def _is_maxed_now(self):
        """按「当前尺寸是否等于最大化目标尺寸」判断，而不是只看标志位。

        标志位可能因为外部还原（任务栏还原、显示器切换）而与实际尺寸失配，
        只信标志位会出现「点最大化没反应」。
        """
        w, h, _x, _y = self._max_target()
        return (abs(self.root.winfo_width() - w) <= 4
                and abs(self.root.winfo_height() - h) <= 4)

    def _restore_from_max(self):
        """从最大化还原到之前的位置与尺寸。"""
        nw, nh, nx, ny = getattr(self, "_normal_geo",
                                 (self._renderer.W, self._renderer.H, 0, 0))
        w = getattr(self, "_rest_w", None) or nw
        h = getattr(self, "_rest_h", None) or nh
        x = getattr(self, "_rest_x", None)
        y = getattr(self, "_rest_y", None)
        if x is None or y is None:
            x, y = nx, ny
        # 记录的尺寸若本身就是最大化尺寸（状态失配），退回启动时的常规尺寸
        tw, th, _tx, _ty = self._max_target()
        if abs(w - tw) <= 4 and abs(h - th) <= 4:
            w, h = nw, nh
        try:
            self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))
        except Exception:
            pass
        self._maxed = False

    def _on_map(self, ev=None):
        """从任务栏还原后，重新套用无边框 + 四角透明。"""
        if not getattr(self, "_restore_borderless_pending", False):
            return
        self._restore_borderless_pending = False
        try:
            self.root.overrideredirect(True)
            self._borderless = True
        except Exception:
            pass
        self.root.after(30, self._apply_transparency)
        self.root.after(60, self._redraw)

    def _win_toggle_max(self):
        """最大化 / 还原。

        素材是固定长宽比的整幅插画，拉伸会变形，所以「最大化」取
        **能塞进系统工作区的最大等比尺寸**并居中 —— 这与看图软件/播放器的
        「适应窗口」一致，纵向正好顶到工作区的上下边，不再留之前那种
        「94% 高度 + 整体上移 12px」的怪偏移。
        """
        # 已最大化（按尺寸判定，标志位可能失配）→ 还原
        if self._is_maxed_now():
            self._restore_from_max()
            return
        try:
            # 记录当前常规尺寸，供还原使用
            self._rest_w = self.root.winfo_width()
            self._rest_h = self.root.winfo_height()
            self._rest_x = self.root.winfo_x()
            self._rest_y = self.root.winfo_y()
            w, h, x, y = self._max_target()
            self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))
            self._maxed = True
        except Exception:
            pass

    def _set_hover(self, key):
        if key == self._hover_key:
            return
        self._hover_key = key
        self.canvas.configure(cursor="hand2" if key else "arrow")
        self._paint_buttons()

    def _paint_buttons(self, base=None):
        """只重绘按钮区域，供 hover 即时响应使用。

        整窗重绘一次要几十毫秒，鼠标划过时必须避免。这里从上一帧已合成的
        整窗图（_frame）出发，把高亮/常态按钮图贴到各自的矩形上。按钮彼此
        不重叠，所以逐个覆盖互不影响。
        """
        base = base if base is not None else self._frame
        if base is None:
            return
        from PIL import Image, ImageDraw
        img = base.copy()
        d = ImageDraw.Draw(img, "RGBA")
        s = self._scale
        for k, _lb, x, y, bw, bh, _ic in self._renderer.button_rects():
            if k != self._hover_key:
                continue
            # 悬停：叠一层白色高光 + 轻微上移的亮边，保持与底图同样的圆角
            d.rounded_rectangle([x * s, y * s, (x + bw) * s, (y + bh) * s],
                                radius=int(11 * s), fill=(255, 255, 255, 46))
        self._blit(img)

    def _on_press(self, ev):
        """鼠标左键按下：先分派按钮点击，否则在标题栏进入拖动。

        合并的原因见 _build 里的说明：<Button-1> 与 <ButtonPress-1> 是同一
        事件，只能绑定一个处理器，否则后者会把前者覆盖掉（按钮将全部失效）。
        """
        k = self._hit(ev.x, ev.y)
        if k:
            # 命中按钮：走点击逻辑，不进入拖动
            self._on_click(ev)
            return
        if self._borderless and ev.y < 34 * self._scale:
            self._drag_off = (ev.x, ev.y)
            self._win_off = (self.root.winfo_x(), self.root.winfo_y())
            # 进入拖动状态：期间暂停一切整窗重绘（见 _on_drag 说明）
            self._dragging = True

    def _on_double_click(self, ev):
        """双击标题栏空白处切换最大化（与按最大化按钮等效）。"""
        if not self._borderless:
            return
        if self._hit(ev.x, ev.y):
            return                    # 落在按钮上：交给按钮处理
        if ev.y < 34 * self._scale:
            self._win_toggle_max()

    def _on_release(self, _ev=None):
        """松开鼠标：结束拖动状态，并把拖动期间压下的重绘补上。"""
        if not getattr(self, "_dragging", False):
            return
        self._dragging = False
        self._drag_off = None
        self._drag_pending = False
        # 拖动结束后强制刷新一次，保证界面回到最新状态
        self._last_redraw = 0.0
        self._redraw()

    def _on_drag(self, ev):
        """拖动窗口。

        频闪的根因在这段与重绘的叠加：
          1) <B1-Motion> 的频率可达 100+ 次/秒，每次都 geometry() 会让
             窗口在移动中不断重排；
          2) 同一时刻 _on_motion 仍会把 hover 变化转成 _paint_buttons()，
             而它要**重建整窗 PhotoImage（约 1.8MB）**再 itemconfigure——
             这个"移动窗口 + 换整窗位图"的组合就是肉眼看到的频闪；
          3) 后台 _drain 每 250ms 的整窗 PIL 渲染（几十毫秒）会落在拖动中途。
        因此：拖动期间不响应 hover、不做整窗重绘，并把 geometry 节流到
        约 60fps；拖动结束再统一刷新一次。
        """
        if not (self._drag_off and getattr(self, "_win_off", None)):
            return
        # 最大化状态下拖标题栏：先还原成普通尺寸再跟手 —— 与 Windows 一致。
        if getattr(self, "_maxed", False):
            self._restore_from_max()
            self._drag_off = (ev.x, ev.y)
            self._win_off = (self.root.winfo_x(), self.root.winfo_y())
        self._drag_x = self._win_off[0] + ev.x - self._drag_off[0]
        self._drag_y = self._win_off[1] + ev.y - self._drag_off[1]
        # 合并同一轮事件循环里的多次移动，只提交最后一次
        if self._drag_job is None:
            self._drag_job = self.root.after(16, self._flush_drag)

    def _flush_drag(self):
        self._drag_job = None
        if not getattr(self, "_dragging", False):
            return
        if getattr(self, "_drag_x", None) is None:
            return
        try:
            self.root.geometry("+%d+%d" % (self._drag_x, self._drag_y))
        except Exception:
            pass

    def _on_wheel(self, ev):
        """滚轮分别滚动账号列表与实时账单。

        指针落在哪个区域就滚哪个；两个区域都只在内容超出可视行数时响应，
        避免误吞其它地方的滚动事件。
        """
        x, y = ev.x / self._scale, ev.y / self._scale
        step = -1 if getattr(ev, "delta", 0) > 0 else 1

        # --- 账号区 ---
        scfg = self._renderer.layout["svc"]
        rows = self._state.get("accounts") or []
        cap = scfg["rows_max"]
        if (len(rows) > cap and 20 <= x <= scfg["col_end"] + 12
                and scfg["row0_cy"] - 14 <= y
                <= scfg["row0_cy"] + cap * scfg["row_h"]):
            off = max(0, min(self._acct_scroll + step, len(rows) - cap))
            if off != self._acct_scroll:
                self._acct_scroll = off
                self._state["acct_scroll"] = off
                self._redraw()
            return

        # --- 账单区 ---
        bcfg = self._renderer.layout["bill"]
        bills = self._state.get("bill") or []
        bcap = bcfg["rows_max"]
        if (len(bills) > bcap
                and bcfg["row0_cy"] - 14 <= y
                <= bcfg["row0_cy"] + bcap * bcfg["row_h"]
                and 20 <= x <= bcfg.get("col_end", 466) + 80):
            off = max(0, min(self._bill_scroll + step, len(bills) - bcap))
            if off != self._bill_scroll:
                self._bill_scroll = off
                self._state["bill_scroll"] = off
                self._redraw()

    def _on_motion(self, ev):
        # 拖动窗口时不处理悬停：hover 一变就要重建整窗位图，
        # 与窗口移动叠加会明显频闪（见 _on_drag 注释）。
        if getattr(self, "_dragging", False):
            return
        self._set_hover(self._hit(ev.x, ev.y))

    def _on_resize(self, ev):
        """窗口尺寸变化后重算缩放系数。

        素材是固定构图的整幅画，不能拉伸变形——按「窗口宽度 / 素材宽度」
        等比缩放，这样任意窗口尺寸下版面比例都与参考图一致。
        """
        if ev.widget is not self.root:
            return
        if ev.width < 40 or ev.height < 40:
            return
        if (ev.width, ev.height) == self._last_size:
            return
        self._last_size = (ev.width, ev.height)
        # 防抖：拖拽窗口时 Configure 会高频触发，而重渲染开销不小
        if self._resize_job:
            try:
                self.root.after_cancel(self._resize_job)
            except Exception:
                pass
        self._resize_job = self.root.after(140, self._apply_resize)

    def _apply_resize(self):
        self._resize_job = None
        w, h = self._last_size or (self._renderer.W, self._renderer.H)
        base_w, base_h = self._renderer.lw, self._renderer.lh
        sc = min(w / base_w, h / base_h) * detect_scale()
        sc = max(0.55, min(2.5, sc))
        if abs(sc - self._scale) > 0.02 and self._renderer.resize(None, None, sc):
            self._scale = sc
            self._redraw()

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
        self._set_state(refresh_label="刷新中…")
        # 立刻给出可见反馈，并立刻用闪烁副本覆盖 1 秒的轮询文案；
        # 不依赖轮询成功与否，否则后端异常时按钮像是没反应。
        stamp = time.strftime("%H:%M:%S")
        self._refresh_flash_until = time.time() + 3.0
        # 立刻把文案切到「已刷新」，不要等下一轮轮询，否则按钮像是没反应
        self._set_state(subtitle="✔ 已刷新 · " + stamp)
        self._refresh_evt.set()
        self.root.after(1200, self._refresh_done)

    def _refresh_done(self):
        # 无论轮询是否成功，按钮文案都要复位，否则会一直停在「刷新中…」
        self._set_state(refresh_label="刷新", redraw=True)

    def _copy(self):
        txt = ("Base URL : %s/v1\nAPI Key  : %s\n"
               "Models   : deepseek-v4-pro, deepseek-v4-flash, glm-5.2, "
               "kimi-k2.7, kimi-k3-1, minimax-m3, hunyuan-2.0-instruct, auto"
               % (self.base, self.key))
        self.root.clipboard_clear()
        self.root.clipboard_append(txt)

    def _checkin_now(self):
        """手动立即签到：调用后端 /api/billing/checkin（后台线程，不卡 UI）。"""
        self._set_state(checkin_label="签到中…")
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
            self.q.put(("state", "reset_checkin"))
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
        """消费轮询线程投递的消息，更新状态字典后触发一次重绘。"""
        kind = item[0]
        if kind == "state":
            if item[1] == "reset_checkin":
                self._set_state(checkin_label="立即签到")
            else:
                self._render_state(item[1])
        elif kind == "addresult":
            import tkinter.messagebox as mb
            self._set_state(checkin_label="立即签到")
            title, body = item[1]
            mb.showinfo(title, body)
        elif kind == "wait":
            self._set_state(status="starting", subtitle="正在等待后端就绪",
                            toggle_label="停止服务")
        elif kind == "down":
            self._set_state(status="stopped", subtitle="点击下方「启动服务」",
                            toggle_label="启动服务")
        elif kind == "ok":
            _, alive, st, accts, stats, logs, bill = item
            self._render_ok(st, accts, stats, logs, bill)

    def _set_state(self, redraw=True, **kw):
        # 按钮文案的临时态（如「刷新中…」）要有反馈，见 _refresh_now/_checkin_now
        """更新状态；未显式要求时合并本轮的多次更新，只重绘一次。"""
        self._state.update(kw)
        if not redraw:
            return
        # 拖动窗口期间不重绘：整窗渲染要几十毫秒，与窗口移动叠加会频闪。
        # 数据照常更新，只置一个标记，松手后由 _on_release 统一补一次。
        if getattr(self, "_dragging", False):
            self._drag_pending = True
            return
        now = time.time()
        if self._redraw_job:
            return
        # 轮询频率 1s，渲染节流到 4 次/秒以内即可，避免无谓的整窗重绘
        delay = max(0, int((0.25 - (now - self._last_redraw)) * 1000))
        self._redraw_job = self.root.after(delay, self._flush_redraw)

    def _flush_redraw(self):
        self._redraw_job = None
        self._last_redraw = time.time()
        self._redraw()

    def _render_state(self, s):
        if s == "running":
            self._set_state(status="starting", subtitle="正在等待后端就绪",
                            toggle_label="停止服务")
        elif s == "stopped":
            self._set_state(status="stopped", subtitle="点击下方「启动服务」",
                            addr="—", key="—",
                            pool={"ok": 0, "total": 0, "cooldown": 0, "active": "—"},
                            accounts=[], bill=[], toggle_label="启动服务")
        elif s.startswith("error"):
            self._set_state(status="error", subtitle=s[6:] or "启动失败",
                            toggle_label="重试启动")

    def _render_ok(self, st, accts, stats, logs, bill=None):
        sub = time.strftime("已运行 %Hh%Mm", time.gmtime(st.get("uptime", 0)))
        if bill and bill.get("scheduler_running"):
            lr = bill.get("last_run") or ""
            if lr:
                sub += " · 每日签到 %s" % lr
        # 手动刷新的反馈要压住轮询文案，否则提示会在 1 秒内被覆盖
        if time.time() < self._refresh_flash_until:
            sub = "✔ 已刷新 · " + time.strftime("%H:%M:%S")

        ps = (accts or {}).get("pool_stats", {}) or {}
        active_id = accts.get("active_id")
        accounts = []
        for a in (accts.get("accounts") or []):
            nm = a.get("name", "?")
            # 过长的名字（企业号常是完整邮箱）压到 10 字符内，避免撑乱列。
            # 注意保留「尾部」——多个企业号往往只有结尾数字不同
            # （WeChatGame8/9/10），截头去尾会全部变成同一个名字。
            if len(nm) > 10:
                local = nm.split("@", 1)[0] if "@" in nm else nm
                nm = local[:2] + "…" + local[-5:] if len(local) > 7 else nm[:9] + "…"
            rc = a.get("real_credit")
            if rc is not None:
                bal = "真实 %s" % rc
            elif a.get("real_credit_note"):
                bal = "企业版"
            else:
                bal = "累计 %s" % a.get("credit_spent", 0)
            # 养虾数据：企业账号或尚未采集时不显示
            g = a.get("growth") or {}
            has_g = bool(g) and not g.get("note")
            hh = a.get("health", "ok")
            accounts.append({
                "name": nm,
                "health": hh,
                # 参考图里状态列显示的是「ok」，对勾由图标位承载
                "state": {"ok": "ok", "exhausted": "耗尽",
                          "cooldown": "冷却"}.get(hh, hh),
                "bal": bal,
                "active": a.get("id") == active_id,
                "growth": has_g,
                "energy": g.get("energy", 0) if has_g else "",
                "buddies": g.get("buddies", 0) if has_g else "",
                "streak": ("连%s天" % g.get("streak_days")) if has_g
                          and g.get("streak_days") is not None else "",
            })

        pool = {"ok": ps.get("ok", 0), "total": ps.get("total", 0),
                "cooldown": ps.get("cooldown", 0),
                "active": accts.get("active_name", "?")}

        # 账单流水。注意 level="credit" 同时也被签到等事件复用（model 为空、
        # 积分为 0），这类不是计费行——按有无 model 区分，否则账单里会混进
        # 一堆 "0.0000 分" 的干扰行。
        items = []
        for it in logs:
            lvl = it.get("level", "info")
            if lvl not in ("credit", "error", "warn"):
                continue
            model = (it.get("model") or "").strip()
            c = float(it.get("credit") or 0)
            billed = bool(model) and c > 0
            if lvl == "credit" and not billed:
                # 事件类（签到、账号切换…）：没有模型与分值，按要求归入告警列
                items.append({
                    "time": it.get("time", ""),
                    "model": _short_event(it.get("msg", "")),
                    "meta": _short_event(it.get("msg", ""))[:20],
                    "tokens": "",
                    "credit": "事件",
                    "level": "warn" if lvl == "credit" else lvl,
                })
                continue
            items.append({
                "time": it.get("time", ""),
                "rid": it.get("rid", ""),
                "model": model or "-",
                # 渲染器单独画「tok」标签，这里只给数字，避免重复
                "tokens": _parse_tokens(it.get("msg", "")).replace("tok ", ""),
                "credit": "%.4f 分" % c if c else lvl,
                "level": lvl,
            })
        # 总线是时间序，界面要新记录在最上面
        items.reverse()

        self._set_state(
            status="running", subtitle=sub,
            addr="%s/v1" % self.base, key=self.key,
            pool=pool,
            accounts=accounts,
            today_req=(stats or {}).get("today_requests", 0),
            today_credit=(stats or {}).get("today_credit", 0),
            bill=items or self._state.get("bill") or [],
            toggle_label="停止服务",
        )

    def _on_close(self):
        self.stop_evt.set()
        for job in (self._redraw_job, self._resize_job):
            if job:
                try:
                    self.root.after_cancel(job)
                except Exception:
                    pass
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
        root = tk.Tk()
        app = App(root)
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
