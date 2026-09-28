# -*- coding: utf-8 -*-
"""ui_render.py — 1:1 复刻参考图界面的渲染器。

设计（与前版的关键区别）
    参考界面里的角色、按钮、卡片、图标都是**手绘插画**，用 PIL 现画简笔画
    不可能对齐。因此改为：
        以 assets/ui/base.png（由参考图加工而来，已抹掉动态数据）为底，
        运行时只把真实数据画上去。
    这样背景、云、6 个 Q 版角色、按钮、图标、卡片阴影全部是原画品质，
    1:1 一致；只有数字与文字是活的。

坐标
    全部取自对参考图的逐像素实测，见 assets/ui/layout.json。
    绘制时统一乘 scale 以适配高 DPI。
"""
from __future__ import annotations

import json
import os
from PIL import Image, ImageDraw, ImageFont

# ------------------------------------------------------------------ 资源
def _app_dir():
    import sys
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _asset_path(name):
    import sys
    for base in (getattr(sys, "_MEIPASS", None), _app_dir()):
        if not base:
            continue
        p = os.path.join(base, "assets", "ui", name)
        if os.path.exists(p):
            return p
    return os.path.join(_app_dir(), "assets", "ui", name)


_WIN_FONTS = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
_FONT_FILES = {
    False: ["msyhui.ttc", "msyh.ttc", "msyh.ttf", "simhei.ttf", "segoeui.ttf"],
    True:  ["msyhbd.ttc", "msyhui.ttc", "msyh.ttc", "simhei.ttf", "segoeuib.ttf"],
}
_FONTS = {}


def load_font(size, bold=False, prefer=None):
    """加载字体。

    prefer —— 指定优先使用的字体文件名（如 "Dengb.ttf"）。
    标题这类对字形敏感的文本，会自动比对选出最接近原画的字体，
    在该参数里写死，避免不同机器上回退到别的字体导致观感变化。
    """
    key = (size, bold, prefer)
    if key in _FONTS:
        return _FONTS[key]
    f = None
    names = ([prefer] if prefer else []) + _FONT_FILES[bold]
    for nm in names:
        p = os.path.join(_WIN_FONTS, nm)
        if os.path.exists(p):
            try:
                f = ImageFont.truetype(p, size, index=0)
                break
            except Exception:
                continue
    if f is None:
        try:
            f = ImageFont.truetype("msyh.ttc", size)
        except Exception:
            f = ImageFont.load_default()
    _FONTS[key] = f
    return f


def rgb(s):
    s = s.lstrip("#")
    return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


# ------------------------------------------------------------------ 渲染器
class Renderer:
    """把状态字典渲染成界面位图。"""

    def __init__(self, w=None, h=None, scale=1.0):
        self.layout = json.load(open(_asset_path("layout.json"), encoding="utf-8"))
        self.base_src = Image.open(_asset_path("base.png")).convert("RGB")
        self.lw, self.lh = self.base_src.size          # 逻辑尺寸锁定为素材尺寸
        self.scale = max(1.0, float(scale))
        self._base_cache = None
        self._frame = None

    # ---- 尺寸 ----
    @property
    def W(self):
        return int(round(self.lw * self.scale))

    @property
    def H(self):
        return int(round(self.lh * self.scale))

    def resize(self, w, h, scale=None):
        """素材是固定构图，只随 DPI 缩放；窗口尺寸变化不影响版面比例。"""
        sc = max(1.0, float(scale if scale is not None else self.scale))
        if abs(sc - self.scale) < 0.01:
            return False
        self.scale = sc
        self._base_cache = None
        self._btnfont = None      # 字号随 scale 变，必须重建
        return True

    # ---- 基底 ----
    def _base(self):
        if self._base_cache is None:
            if abs(self.scale - 1.0) < 0.01:
                img = self.base_src.copy()
            else:
                img = self.base_src.resize((self.W, self.H), Image.LANCZOS)
                # 缩放会把四角的透明键色与相邻像素混合，产生非纯键色的毛边，
                # 那些像素不会被 -transparentcolor 识别，会露出一圈品红。
                # 因此在缩放后重新打一次透明孔。
                self._repunch_corners(img)
            self._base_cache = img
        return self._base_cache.copy()

    def _repunch_corners(self, img, key=(255, 0, 254)):
        """重新标记四角的透明区域（缩放后调用）。"""
        from collections import deque
        W, H = img.size
        px = img.load()
        size = max(8, int(round(26 * self.scale)))
        boxes = [(0, 0), (W - size, 0), (0, H - size), (W - size, H - size)]
        seen = set()
        dq = deque()
        for (x0, y0) in boxes:
            for y in range(y0, min(H, y0 + size)):
                for x in range(x0, min(W, x0 + size)):
                    if (x, y) in seen:
                        continue
                    if (x in (0, W-1) or y in (0, H-1)) and sum(px[x, y]) > 430:
                        seen.add((x, y)); dq.append((x, y))
        while dq:
            x, y = dq.popleft()
            if not any(x0 <= x < x0 + size and y0 <= y < y0 + size
                       for (x0, y0) in boxes):
                continue
            px[x, y] = key
            for nx, ny in ((x+1, y), (x-1, y), (x, y+1), (x, y-1)):
                if (0 <= nx < W and 0 <= ny < H and (nx, ny) not in seen
                        and sum(px[nx, ny]) > 430):
                    seen.add((nx, ny)); dq.append((nx, ny))

    # ---- 文字工具 ----
    def _text(self, d, x, y, s, size, color, bold=False, anchor="lm",
              stroke=0, stroke_fill=None, prefer=None):
        # 兜底：任何字段都强制成字符串。
        # 上游偶发返回非字符串（实测新版 WorkBuddy 登录态的昵称是加密信封
        # dict {"$wbEncrypted":1,...}），而 ImageDraw.text 要求 str，否则抛
        # AttributeError。渲染器是整个界面的单点，一旦这里抛异常，
        # _redraw 会静默跳过绘制，窗口就停在启动帧——表现为「界面什么都没有」。
        # 单字段显示难看可以接受，整窗空白不行。
        if not isinstance(s, str):
            s = "" if s is None else str(s)
        px = max(8, int(round(size * self.scale)))
        f = load_font(px, bold, prefer)
        d.text((x * self.scale, y * self.scale), s, font=f,
               fill=rgb(color) + (255,), anchor=anchor,
               stroke_width=int(stroke * self.scale),
               stroke_fill=(rgb(stroke_fill) + (255,)) if stroke_fill else None)

    def _text_w(self, s, size, bold=False):
        if not isinstance(s, str):
            s = "" if s is None else str(s)
        f = load_font(max(8, int(round(size * self.scale))), bold)
        try:
            b = f.getbbox(s)
            return (b[2] - b[0]) / self.scale
        except Exception:
            return len(s) * size * 0.55

    # ---- 主渲染 ----
    def render(self, st):
        img = self._base()
        d = ImageDraw.Draw(img, "RGBA")
        self._target = img          # 图标贴图的目标画布
        L = self.layout
        C = L["colors"]
        s = self.scale

        self._status(d, st, L, C)
        self._service(d, st, L, C)
        self._stats(d, st, L, C)
        self._bill(d, st, L, C)
        # 账单滚动条要在账单行之后画：它盖在卡片右内侧的轨道上，
        # 而账单文字最右只到 x=462，不会被滑块压到。
        self._bill_scrollbar(st, L)
        self._buttons(d, st, L)
        self._frame = img.copy()
        return img

    def _bill_icons(self, img, st, L):
        """给每条账单行贴上参考图里的图标组（保持原画品质）。"""
        icon = self._bill_icon()
        if icon is None:
            return
        cfg = L["bill"]
        n = len((st.get("bill") or [])[:cfg["rows_max"]])
        if abs(self.scale - 1.0) > 0.01:
            icon = icon.resize((max(1, int(icon.width * self.scale)),
                                max(1, int(icon.height * self.scale))), Image.LANCZOS)
        x = int(cfg["col_icon"] * self.scale)
        for i in range(n):
            cy = cfg["row0_cy"] + i * cfg["row_h"]
            y = int((cy - icon.height / (2 * self.scale)) * self.scale)
            # img 是 RGB，用 paste + mask 做贴图（alpha_composite 只接受 RGBA）
            img.paste(icon, (x, y), icon)

    # ---- 标题区 ----
    def _status(self, d, st, L, C):
        cfg = L["status"]
        status = st.get("status", "starting")
        col = {"running": C["green"], "starting": C["warn"],
               "stopped": C["red"], "error": C["red"]}.get(status, C["warn"])
        # 状态圆点
        # 位置取自 layout（参考图实测中心 (153.5, 66)、半径约 7）。
        # 早先用「title.x - 34」推导会落到 x=138，比原画偏左 15px，
        # 而且正好撞在 erase_sky 的左边界上被擦掉一半。
        cx, cy, r = cfg["dot"]["cx"], cfg["dot"]["cy"], cfg["dot"]["r"]
        # 参考图的圆点是「白色实心底环 + 绿色实心点」：
        # 绿点 x 147..160（半径 7）、白环到 x 146..162（半径约 9）。
        # PIL 的 ellipse outline 是向内收的，所以白环要单独画成一个更大的实心圆，
        # 否则白色会吃掉绿点的边缘，看着比原画小一圈。
        rw = r + cfg.get("ring", 2)
        d.ellipse([(cx - rw) * self.scale, (cy - rw) * self.scale,
                   (cx + rw) * self.scale, (cy + rw) * self.scale],
                  fill=(255, 255, 255, 255))
        d.ellipse([(cx - r) * self.scale, (cy - r) * self.scale,
                   (cx + r) * self.scale, (cy + r) * self.scale],
                  fill=rgb(col) + (255,))
        title = {"running": "服务运行中", "starting": "服务启动中…",
                 "stopped": "服务已停止", "error": "启动失败"}.get(
            status, "服务启动中…")
        t = cfg["title"]
        # 字体/字号/描边由 tools 里的自动比对选出（等线 Bold 27/描边3 与参考图
        # 标题的墨迹掩码差异最小）。此前用雅黑 33 会宽出约 45px。
        self._text(d, t["x"], t["cy"], title, t["size"], C["title_fill"],
                   bold=True, stroke=t.get("stroke", 3),
                   stroke_fill=C["title_stroke"],
                   prefer=t.get("font", "Dengb.ttf"))
        sub = st.get("subtitle") or ""
        if sub:
            u = cfg["sub"]
            self._text(d, u["x"], u["cy"], sub, u["size"], C["sub_fill"],
                       bold=True, stroke=u.get("stroke", 2),
                       stroke_fill=C["sub_stroke"],
                       prefer=u.get("font", "Dengb.ttf"))

    # ---- 服务卡 ----
    def _service(self, d, st, L, C):
        cfg = L["svc"]
        size = cfg["size"]
        d = d
        self._text(d, cfg["value_x"], cfg["addr_cy"], st.get("addr", "—"),
                   cfg["value_size"], C["value"])
        self._text(d, cfg["value_x"], cfg["key_cy"], st.get("key", "—"),
                   cfg["value_size"], C["value"])
        # 账号池汇总
        ps = st.get("pool") or {}
        pool = "账号池 %s/%s  （冷却%s） ， 主力 %s" % (
            ps.get("ok", 0), ps.get("total", 0), ps.get("cooldown", 0),
            ps.get("active", "?"))
        self._text(d, cfg["label_x"], cfg["pool_cy"], pool,
                   cfg.get("pool_size", size), C["dim"])

        rows = st.get("accounts") or []
        cap = cfg["rows_max"]                    # 一屏能放下的行数
        n = len(rows)
        # 滚动位置：账号多于容量时由调用方（滚轮）给出，这里做一次夹取
        off = max(0, min(int(st.get("acct_scroll", 0)), max(0, n - cap)))
        for i, a in enumerate(rows[off:off + cap]):
            cy = cfg["row0_cy"] + i * cfg["row_h"]
            act = bool(a.get("active"))
            # 当前账号：名字左侧有原画的小徽标（实测只有这一行有）
            if act:
                self._paste_icon(d, "icon_active.png", cfg.get("col_star", 29),
                                 cy - 9)
            self._text(d, cfg["col_name"], cy, a.get("name", "?"), size,
                       C["name"], bold=act)
            # 状态对勾：原画图标 + 健康度着色。
            # 企业号（We_Game8/9）的状态列实测比普通行左移约 9px。
            ent = (a.get("bal") or "").startswith("企业版")
            cx_ = cfg.get("col_check_ent", 138) if ent else cfg["col_check"]
            sx_ = cfg.get("col_state_ent", 152) if ent else cfg["col_state"]
            hh = a.get("health", "ok")
            tint = {"ok": None, "exhausted": (224, 84, 98),
                    "cooldown": (224, 160, 49)}.get(hh)
            self._paste_icon(d, "icon_check.png", cx_, cy - 9, tint=tint)
            self._text(d, sx_, cy, a.get("state", "ok"), size,
                       C["value"], bold=True)
            # 余额：标签与数字分列（参考图里是两列对齐）
            bal = a.get("bal", "")
            if bal.startswith("真实 "):
                self._text(d, cfg["col_bal"], cy, "真实", size, C["value"])
                self._text(d, cfg["col_num"], cy, bal[3:].strip(), size, C["value"])
            elif bal.startswith("累计 "):
                self._text(d, cfg["col_bal"], cy, "累计", size, C["value"])
                self._text(d, cfg["col_num"], cy, bal[3:].strip(), size, C["value"])
            else:
                # 企业版：只有一列，位置与「真实」对齐
                self._text(d, cfg["col_bal"], cy, bal, size, C["value"])
            # 成长数据：原画图标 + 数值
            if a.get("growth"):
                self._paste_icon(d, "icon_bolt.png", cfg["col_energy"], cy - 9)
                self._text(d, cfg["col_energy_v"], cy, str(a.get("energy", "")),
                           size, "#7A6BB8")
                self._paste_icon(d, "icon_shrimp.png", cfg["col_buddy"], cy - 9)
                self._text(d, cfg["col_buddy_v"], cy, str(a.get("buddies", "")),
                           size, "#7A6BB8")
                if a.get("streak"):
                    # 「连X天」与其余列同为深蓝（参考图实测）
                    self._text(d, cfg["col_streak"], cy, a["streak"], size,
                               C["value"])
        # 账号多于容量时，右侧画一条细滚动条提示可滚动
        if n > cap:
            self._scroll_hint(d, cfg, off, n, cap)

    def _scroll_hint(self, d, cfg, off, total, cap):
        """账号列表右侧的细滚动条（只在内容超出时出现）。

        轨道/滑块颜色取自参考图右侧那道原画轨道（x≈418）：
        轨道 #D8D2EC、滑块 #8E88BE，与卡片底色形成自然的淡紫细线。
        """
        s = self.scale
        # 放在账号文字（最远到 x=401）与猫耳角色（x≥421）之间的空白带，
        # 不能贴着 col_end —— 那里会压在角色上。
        x0 = cfg.get("scrollbar_x", 412)
        w = cfg.get("scrollbar_w", 3)
        y0 = cfg["row0_cy"] - 8
        y1 = y0 + cap * cfg["row_h"]
        d.rounded_rectangle([x0 * s, y0 * s, (x0 + w) * s, y1 * s],
                            radius=int(1.5 * s), fill=(216, 210, 236, 200))
        span = (y1 - y0) * cap / float(total)
        ty0 = y0 + (y1 - y0 - span) * off / float(max(1, total - cap))
        d.rounded_rectangle([x0 * s, ty0 * s, (x0 + w) * s, (ty0 + span) * s],
                            radius=int(1.5 * s), fill=(142, 136, 190, 240))

    # ---- 账单滚动条（参考图里原画自带，这里把滑块变成活的）----
    def _bill_scrollbar(self, st, L):
        """按账单滚动位置重画右侧滑块。

        参考图右侧原画本来就有一条竖向轨道（x 528..531）和一枚画死在
        固定位置的滑块（圆角胶囊，实测 x 523..536、y 608..650），
        滚动时它纹丝不动 —— 用户反馈"滑动条没有效果"。

        这里先把原画滑块那段盖掉（**只盖滑块所在的纵向区间**，
        下方的爪子装饰 y≥653 是原画的一部分，必须保留），再按当前
        偏移重新画轨道与滑块。
        """
        cfg = L["bill"]
        rows = st.get("bill") or []
        total = len(rows)
        cap = cfg["rows_max"]
        s = self.scale
        sc = cfg.get("scroll", {})
        tx = sc.get("track_x", 528)
        tw = sc.get("track_w", 4)
        ty0 = sc.get("track_y0", 480)
        ty1 = sc.get("track_y1", 650)          # 停在爪子装饰（y≥653）之前
        txx = sc.get("thumb_x", 523)
        tww = sc.get("thumb_w", 14)
        th_h = sc.get("thumb_h", 43)
        off = int(st.get("bill_scroll", 0) or 0)
        off = max(0, min(off, max(0, total - cap))) if total > cap else 0

        img = self._target
        d = ImageDraw.Draw(img, "RGBA")

        # 1) 抹掉原画那枚「固定不动」的滑块。
        #    不能画纯色：原画的轨道是带柔边的 4px 细线（x 528..531，
        #    两侧还有 #FFFEFC 的高光），用近似的实心紫去补，
        #    渲染出来就是一条比原画粗一倍的棒（实测 5px 实心 vs 原画 4px 柔边）。
        #    这里直接**复制一段干净轨道行的像素**纵向铺开，逐列精确还原。
        # 原画胶囊实测：y 607..644（高 38），紫边在 x 524..525 与 x 534。
        art_y0 = sc.get("art_thumb_y0", 606)
        art_y1 = sc.get("art_thumb_y1", 646)
        clean_y = sc.get("clean_y", 500)
        w = tww + 2
        strip = self._base().crop(
            (int((txx - 1) * s), int(clean_y * s),
             int((txx - 1 + w) * s), int((clean_y + 1) * s)))
        strip = strip.resize((int(w * s), int((art_y1 - art_y0) * s)),
                             Image.NEAREST)
        img.paste(strip, (int((txx - 1) * s), int(art_y0 * s)))

        # 2) 按滚动偏移画滑块；尺寸对齐原画胶囊（宽 12、高 38，白底紫边）。
        travel = (ty1 - ty0) - th_h
        frac = off / float(total - cap) if total > cap else 0.0
        ty = ty0 + travel * frac
        d.rounded_rectangle([txx * s, ty * s, (txx + tww - 1) * s, (ty + th_h) * s],
                            radius=int(min(th_h, tww) * s / 2.0),
                            fill=(0xF7, 0xF2, 0xF8, 255),
                            outline=(0xAF, 0x9F, 0xDD, 255),
                            width=max(1, int(2 * s)))

    # ---- 统计卡 ----
    def _stats(self, d, st, L, C):
        cfg = L["stat"]
        for key, val, color in (
                ("req", str(st.get("today_req", 0)), C["value"]),
                ("credit", str(st.get("today_credit", 0)), C["orange"])):
            c = cfg[key + "_value"]
            self._text(d, c["cx"], c["cy"], val, c["size"], color,
                       bold=True, anchor="mm")
        # 标签是原画的一部分，无需重绘

    # ---- 账单 ----
    # 参考图的账单行有两种形态（实测）：
    #   计费行：时间 | [图标A][图标B] [请求ID] 模型 | tok 明细 | 分值 |
    #   事件行：时间 | [图标C] 累计:x.xx | 流式  模型 | tok 明细 | 分值 |
    # 图标是从参考图裁出的原画，按行类型贴用。
    def _bill(self, d, st, L, C):
        cfg = L["bill"]
        rows_all = st.get("bill") or []
        cap = cfg["rows_max"]
        total = len(rows_all)
        # 滚动偏移由 app.py 的滚轮给出；超出部分裁剪，避免压到卡片边框。
        boff = max(0, min(int(st.get("bill_scroll", 0) or 0),
                          max(0, total - cap))) if total > cap else 0
        items = rows_all[boff:boff + cap]
        sep_h = cfg.get("sep_half", 5)
        for i, it in enumerate(items):
            cy = cfg["row0_cy"] + i * cfg["row_h"]
            size = cfg["size"]
            self._text(d, cfg["col_time"], cy, it.get("time", ""), size, "#7B78B8")

            billed = it.get("level", "credit") == "credit"
            if billed:
                self._paste_icon(d, "bill_icon_a.png", cfg["col_icon_a"],
                                 cy - 8)
                self._paste_icon(d, "bill_icon_b.png", cfg["col_icon_b"],
                                 cy - 8)
                rid = it.get("rid", "")
                if rid:
                    self._text(d, cfg["col_id"], cy, "[%s]" % rid[:10],
                               cfg.get("id_size", size), "#6F6BA8")
            else:
                self._paste_icon(d, "bill_icon_c.png", cfg["col_icon_c"],
                                 cy - 8)
                meta = it.get("meta") or ""
                if meta:
                    self._text(d, cfg["col_meta"], cy, meta[:20],
                               cfg.get("meta_size", size), "#6F6BA8")

            # 模型名一律深蓝（参考图里只有 error 行才用红色，warn 仍是深蓝）。
            # 事件行的内容已画在 meta 列，这里跳过以免同一句出现两次。
            lvl = it.get("level", "credit")
            mc = C["red"] if lvl == "error" else "#4A4A9E"
            if billed:
                self._text(d, cfg["col_model"], cy, it.get("model", "")[:24],
                           cfg.get("model_size", size), mc)
            self._text(d, cfg["col_tok_label"], cy, "tok",
                       cfg.get("tok_size", size), "#6F6BA8")
            self._text(d, cfg["col_tokens"], cy, it.get("tokens", ""),
                       cfg.get("tok_size", size), "#6F6BA8")
            self._text(d, cfg["col_credit"], cy, it.get("credit", ""),
                       cfg.get("credit_size", size), C["orange"], anchor="rm")
            for sx in cfg.get("seps", ()):
                d.line([(sx * self.scale, (cy - sep_h) * self.scale),
                        (sx * self.scale, (cy + sep_h) * self.scale)],
                       fill=(150, 146, 196, 205), width=max(1, int(self.scale)))

    def _paste_icon(self, d, name, x, y, tint=None):
        """贴上从参考图裁出的图标（缓存）。tint 给定时整体着色。"""
        cache = getattr(self, "_icon_cache", None)
        if cache is None:
            cache = self._icon_cache = {}
        img = cache.get((name, tint))
        if img is None:
            p = _asset_path(name)
            try:
                img = Image.open(p).convert("RGBA")
            except Exception:
                img = None
            if img is not None and abs(self.scale - 1.0) > 0.01:
                img = img.resize((max(1, int(img.width * self.scale)),
                                  max(1, int(img.height * self.scale))), Image.LANCZOS)
            if img is not None and tint:
                # 仅给"非底色"像素着色：底色是卡片色，接近白
                px = img.load()
                for yy in range(img.height):
                    for xx in range(img.width):
                        r, g, b, a = px[xx, yy]
                        if a > 40 and not (r > 225 and g > 225 and b > 225):
                            px[xx, yy] = (tint[0], tint[1], tint[2], a)
            cache[(name, tint)] = img
        if img is None:
            return
        self._target.paste(img, (int(x * self.scale), int(y * self.scale)), img)

    def _bill_icon(self):
        """账单行左侧图标组，从参考图裁出，只在首次使用时加载。"""
        if getattr(self, "_icon_img", "unset") == "unset":
            p = _asset_path("bill_icons.png")
            try:
                self._icon_img = Image.open(p).convert("RGBA")
            except Exception:
                self._icon_img = None
        return self._icon_img

    # ---- 按钮文案 ----
    # 位置实测自参考图（见 layout.json 的 button_text）：图标右缘起笔，垂直居中。
    def _buttons_cfg(self, L):
        cf = L.get("button_text")
        if not cf:
            cf = {"stop": [96, 712, 17], "panel": [214, 712, 17],
                  "refresh": [340, 712, 17], "copy": [447, 712, 17],
                  "checkin": [250, 772, 19]}
            L["button_text"] = cf
        return cf

    def _buttons(self, d, st, L):
        """按钮底图（含图标）取自参考图原画，这里只画会变的文案。

        原画里的「停止服务 / 刷新 / 立即签到」已随基底擦除，
        所以三种状态文案（如「启动服务」「刷新中…」「签到中…」）都能正常显示。
        """
        want = {
            "stop": st.get("toggle_label", "停止服务"),
            "panel": "管理面板",
            "refresh": st.get("refresh_label", "刷新"),
            "copy": "复制配置",
            "checkin": st.get("checkin_label", "立即签到"),
        }
        cfg = self._buttons_cfg(L)
        f = self._btn_fonts(cfg)
        for key, pos in cfg.items():
            x, cy, size = pos
            txt = want.get(key, "")
            if not txt:
                continue
            d.text((x * self.scale, cy * self.scale), txt,
                   font=f[(key, size)], fill=(255, 255, 255, 255), anchor="lm")

    def _btn_fonts(self, cfg):
        if getattr(self, "_btnfont", None) is None:
            self._btnfont = {sz: load_font(max(9, int(sz * self.scale)), True)
                             for (_x, _cy, sz) in cfg.values()}
        return {(k, sz): self._btnfont[sz] for k, (_x, _cy, sz) in cfg.items()}

    # ---- 标题栏窗口按钮（最小化/最大化/关闭）----
    # 参考图右上角本来就画着这三个按钮，但窗口是无边框的（overrideredirect），
    # 系统不会代理它们的点击。这里把实测矩形登记成热区，由 app.py 真正执行。
    # 实测：最小化 x 444..473 y 4..28；最大化 x 479..507 y 4..28；
    #       关闭 x 512..545 y 4..30（红色块）。
    WIN_BUTTONS = (
        ("win_min",   (444, 4, 474, 30)),
        ("win_max",   (478, 4, 508, 30)),
        ("win_close", (511, 4, 547, 31)),
    )

    # ---- 命中区域（供 app.py 做点击判定）----
    def account_rects(self, state=None):
        """账号行的点击热区：点一行即把该账号切为当前使用账号。

        只覆盖**当前可见**的行（考虑 acct_scroll 偏移），返回的 key 形如
        "acct:<账号 id>"，这样 app.py 不必了解版面细节就能把点击路由回去。

        行的纵向中心与绘制时一致（row0_cy + i*row_h），横向取账号卡内边，
        这样整行可点，不必精确点到名字上。
        """
        out = []
        if not state:
            return out
        cfg = self.layout.get("svc") or {}
        rows = state.get("accounts") or []
        cap = cfg.get("rows_max", 9)
        if not rows:
            return out
        x0 = cfg.get("row_x0", 26)
        x1 = cfg.get("row_x1", 415)
        rh = cfg.get("row_h", 17.5)
        off = max(0, min(int(state.get("acct_scroll", 0) or 0),
                         max(0, len(rows) - cap)))
        for i, a in enumerate(rows[off:off + cap]):
            aid = a.get("id")
            if not aid:
                continue        # 没有 id 的行不可切换（例如接口降级时的占位）
            cy = cfg.get("row0_cy", 201.0) + i * rh
            out.append(("acct:" + str(aid), a.get("name"), x0,
                        int(round(cy - rh / 2.0)), x1 - x0,
                        max(8, int(round(rh))), None))
        return out

    def button_rects(self, state=None):
        out = []
        for k, (x0, y0, x1, y1) in self.WIN_BUTTONS:
            out.append((k, None, x0, y0, x1 - x0, y1 - y0, None))
        for b in self.layout["buttons"]:
            out.append((b["key"], b["label"], b["x0"], b["y0"],
                        b["x1"] - b["x0"], b["y1"] - b["y0"], None))
        out.extend(self.account_rects(state))
        return out

    def button_image(self, key, label, w, h, icon=None, hover=False):
        """按钮底图取自基底；hover 时叠一层高光。"""
        return None
