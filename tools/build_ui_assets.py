# -*- coding: utf-8 -*-
"""从参考图一次性生成全部 UI 素材（可重现，取代此前逐次打补丁的做法）。

用法：
    python tools/build_ui_assets.py <参考图路径>

产物（写入 assets/ui/）：
    base.png          界面基底：保留全部原画（背景/云/角色/卡片/按钮/图标），
                      仅抹除会变的数据（URL、账号行、数值、账单行、按钮文案）
    layout.json       所有元素的实测坐标与字号
    icon_*.png        从原画裁出的图标（对勾/闪电/虾/账单行的三个图标）

为什么以参考图为基底
    参考图的角色、按钮、卡片都是 AI 生成的完整插画，用代码画简笔图形无法对齐。
    把它作为基底、只抹掉动态数据，就能在保留原画品质的前提下显示真实数据。
"""
from __future__ import annotations

import json
import os
import sys
import statistics
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ASSETS = os.path.join(ROOT, "assets", "ui")

# ---------------------------------------------------------------- 常量
SVC_BG = (250, 245, 246)      # 服务卡 / 账单卡底色 #FAF5F6
STAT1_BG = (252, 242, 244)    # 今日请求卡 #FCF2F4
STAT2_BG = (253, 244, 231)    # 积分消耗卡 #FDF4E7

# 账号行起点与行高（实测：用账号名列 x=43..62 扫描，避开角色与卡片边框）
# 早先误用 218.5/19.1 —— 那其实是第二行的中心，且行距偏大，
# 结果第 9 个账号会被下面的统计卡压住。
ROW0_CY, ROW_H = 201.0, 17.5
BILL0_CY, BILL_H = 484.5, 16.6


# 每个擦除矩形都已实测校准，边界避开原画角色。
# 早期用"角色包围盒 + 矩形裁剪"的做法不可行：角色轮廓是非矩形的
# （左上少女在 y>188 时只占 x 0..23，但包围盒宽到 140），
# 裁剪会把账号名所在的整块区域判为"角色"从而漏擦。
# 现在直接给出精确矩形，简单可验证。
#
# 各角色的实际占位（实测，供后续调整参考）：
#   左上少女 x 0..23（y>188 时）  猫耳娘 x 421..553, y 179..339
#   左中少女 x 6..100, y 356..444   粉发少女 x 430..551, y 355..468
#   白兔 x 3..144 / 柴犬 x 400..561（均在 y > 657 才出现）


def erase(img, box, fill=None, seed=None):
    """擦除一块区域，并**保住原画的渐变**。

    早期直接填一个常量色（如 #FAF5F6），在纯色区域没问题，但参考图里
    若干位置是渐变或有淡装饰（实测：积分卡 x=334 已过渡到 #FDFDFE、
    x=335 是星光的 #FAEBCF；按钮底色横向也有渐变）。常量填充会留下一块
    方方正正的色斑，正是用户截图里"贴图有瑕疵"的来源。

    做法：先用常量色打底，再逐行做**边界两侧向外插值**——
      每个像素取该行左右两个锚点（擦除区外最近的未被擦像素）的线性混合，
      纵向再做同样的插值，取两者中与底色更接近的一个。
    这样渐变得以延续，矩形边缘不会出现硬色差。
    """
    x0, y0, x1, y1 = box
    if x1 <= x0 or y1 <= y0:
        return
    x0, y0 = max(0, x0), max(0, y0)
    x1 = min(img.width, x1)
    y1 = min(img.height, y1)
    if x1 <= x0 or y1 <= y0:
        return

    W, H = img.size
    src = img.copy()
    sp = src.load()
    px = img.load()

    for y in range(y0, y1):
        # 左右锚点：擦除区外最近的像素（最多向外找 4 像素）
        lx = x0 - 1
        rx = x1
        lv = None
        rv = None
        for k in range(4):
            if lv is None and lx - k >= 0:
                lv = sp[lx - k, y]
            if rv is None and rx + k < W:
                rv = sp[rx + k, y]
        if lv is None:
            lv = rv
        if rv is None:
            rv = lv
        if lv is None:
            continue
        for x in range(x0, x1):
            if x1 - x0 <= 1:
                px[x, y] = lv
            else:
                t = (x - x0 + 0.5) / float(x1 - x0)
                px[x, y] = (int(round(lv[0] + (rv[0] - lv[0]) * t)),
                            int(round(lv[1] + (rv[1] - lv[1]) * t)),
                            int(round(lv[2] + (rv[2] - lv[2]) * t)))

    # 纵向同理：对每一列在擦除区外取上下锚点再插值，抑制横向插值留下的竖条
    mid = img.copy()
    mp = mid.load()
    for x in range(x0, x1):
        ty = y0 - 1
        by = y1
        tv = sp[x, ty] if ty >= 0 else None
        bv = sp[x, by] if by < H else None
        if tv is None:
            tv = bv
        if bv is None:
            bv = tv
        if tv is None:
            continue
        for y in range(y0, y1):
            t = (y - y0 + 0.5) / float(max(1, y1 - y0))
            mp[x, y] = (int(round(tv[0] + (bv[0] - tv[0]) * t)),
                        int(round(tv[1] + (bv[1] - tv[1]) * t)),
                        int(round(tv[2] + (bv[2] - tv[2]) * t)))

    # 融合：横向插值更贴合"横向渐变"，纵向更能保住"上下不同的底色"，
    # 取与常量底色（fill）更接近的一侧，避免把边框色带进区域内部。
    if fill is not None:
        for y in range(y0, y1):
            for x in range(x0, x1):
                a = px[x, y]
                b = mp[x, y]
                da = sum(abs(a[i] - fill[i]) for i in range(3))
                db = sum(abs(b[i] - fill[i]) for i in range(3))
                if db < da:
                    px[x, y] = b


def erase_sky(img, src, box, stroke_thr=430, grow=3, iters=140):
    """抹掉天空上的标题文字，同时**保留云朵**。

    关键：不能用"整块横向插值 + 模糊"——那会把标题后面的云一起抹平，
    留下一片死板的渐变（实测白云像素 1324 -> 0，肉眼一眼能看出不对）。

    做法：
      1. 用深蓝描边定位文字像素；
      2. 把描边掩码向外膨胀若干像素，覆盖住字形内部的白色填充；
      3. 只在这些像素上做扩散修补（从周围天空/云取色），其余像素原样保留。
    这样云的结构完整，字形位置由邻近的天空与云自然补上。
    """
    from PIL import ImageFilter
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0

    # 1) 文字掩码 = 深色描边
    mask = Image.new("L", (w, h), 0)
    mp = mask.load()
    for y in range(y0, y1):
        for x in range(x0, x1):
            if sum(src.getpixel((x, y))) < stroke_thr:
                mp[x - x0, y - y0] = 255

    # 2) 膨胀到覆盖字形内部（描边上下各有约 10px 宽的白色填充）
    side = 2 * grow + 1
    mask = mask.filter(ImageFilter.MaxFilter(side))

    # 3) 只对掩码内做扩散修补
    region = img.crop(box).convert("RGB")
    for _ in range(iters):
        blurred = region.filter(ImageFilter.GaussianBlur(2.5))
        region = Image.composite(blurred, region, mask)
    img.paste(region, (x0, y0))


# 各按钮的实测底色（取自文字区正上方/下方的干净带，见开发时的采样记录）。
# 用常量而不是运行时估计：按钮样式是固定的，实测值最可靠，
# 也避免"自动选干净行"在按钮高度不足时误选到高光边（曾把签到按钮填成深蓝块）。
BTN_BASE = {
    "停止服务": (0xFB, 0x71, 0x7A),
    "管理面板": (0x6F, 0xC2, 0x8D),
    "刷新":     (0x6D, 0xA4, 0xFA),
    "复制配置": (0xA5, 0x74, 0xEB),
    "立即签到": (0x7F, 0xC7, 0x86),
}


def fill_btn_text(img, src, box, tbox, base=None):
    """抹掉按钮上的文案，保留图标与按钮底图。

    box  —— 按钮的完整矩形 (x0, y0, x1, y1)
    tbox —— 该按钮文字所占的区间 (tx0, ty0, tx1, ty1)

    做法：**逐行从文字区左右两侧的按钮本体取色并插值**。

    早期版本用单一基准色（如 #7FC786）直接填矩形，问题有二：
      1. 按钮横向其实有渐变（实测签到按钮在 y=750 上从 #80C688 过渡到
         #7DC68A，纵向也随高光变化），常量填充会留下一块死板的方色斑；
      2. 矩形一旦超出按钮内边界（签到按钮文字只到 x=313，旧代码却填到
         332），就会在按钮外面糊出一块绿色，正是用户截图指出的瑕疵。
    现在两侧锚点取自按钮本体，且矩形由调用方收紧到文字墨迹 + 2px，
    既接住了渐变，也不会溢出按钮。
    """
    bx0, by0, bx1, by1 = box
    tx0, ty0, tx1, ty1 = tbox

    c = base or (0x7F, 0xC7, 0x86)
    src_px = src.load()
    px = img.load()

    x0 = max(tx0, bx0 + 1)
    x1 = min(tx1, bx1)
    y0 = max(ty0, by0)
    y1 = min(ty1, by1)

    for y in range(y0, y1):
        # 左锚点：文字区左侧仍属按钮本体的像素
        lv = rv = None
        for k in range(1, 8):
            if lv is None and x0 - k >= bx0:
                lv = src_px[x0 - k, y]
            if rv is None and x1 + k - 1 <= bx1:
                rv = src_px[x1 + k - 1, y]
        if lv is None:
            lv = rv
        if rv is None:
            rv = lv
        if lv is None:
            lv = rv = c
        span = max(1, x1 - x0)
        for x in range(x0, x1):
            t = (x - x0 + 0.5) / span
            px[x, y] = (int(round(lv[0] + (rv[0] - lv[0]) * t)),
                        int(round(lv[1] + (rv[1] - lv[1]) * t)),
                        int(round(lv[2] + (rv[2] - lv[2]) * t)))

    # 二次清理：文字的抗锯齿边缘不是纯白（例如 #F6C9CB），插值后可能留下
    # 浅浅的字形残影。把明显"偏亮/偏白"的像素按同一行的插值结果拉回。
    for y in range(y0, y1):
        lv = src_px[max(bx0, x0 - 1), y]
        rv = src_px[min(bx1, x1), y]
        for x in range(x0, x1):
            p = px[x, y]
            if min(p) > 150 and (max(p) - min(p)) < 70:
                t = (x - x0 + 0.5) / max(1, x1 - x0)
                px[x, y] = (int(round(lv[0] + (rv[0] - lv[0]) * t)),
                            int(round(lv[1] + (rv[1] - lv[1]) * t)),
                            int(round(lv[2] + (rv[2] - lv[2]) * t)))


def matte_icon(src, box, bg):
    """裁一个图标并按「到底色距离」做柔边去背。"""
    c = src.crop(box).convert("RGBA")
    px = c.load()
    for yy in range(c.height):
        for xx in range(c.width):
            r, g, b, _ = px[xx, yy]
            d = max(abs(r-bg[0]), abs(g-bg[1]), abs(b-bg[2]))
            a = 0 if d <= 6 else (255 if d >= 40 else int((d-6)/34.0*255))
            px[xx, yy] = (r, g, b, a)
    return c


# 窗口圆角之外的区域用这个颜色标记，配合 app.py 的 -transparentcolor
# 让窗口呈现真正的圆角（否则四角会露出截图里的白底，形成"白色尖角"）。
# 选纯品红是因为这套粉彩配色里不会出现它，不会误伤画面。
TRANSPARENT_KEY = (255, 0, 254)


def punch_corners(img, size=28):
    """把窗口圆角之外的像素替换成透明键色。

    只在四角的小方块内做泛洪扩散，且只扩散"浅色"像素 ——
    窗口自身的边框是深靛蓝，会把扩散挡住，因此不会误伤卡片内部。
    """
    from collections import deque
    W, H = img.size
    px = img.load()
    seen = set()
    dq = deque()
    boxes = [(0, 0), (W - size, 0), (0, H - size), (W - size, H - size)]
    for (x0, y0) in boxes:
        for y in range(y0, min(H, y0 + size)):
            for x in range(x0, min(W, x0 + size)):
                if (x, y) in seen:
                    continue
                # 只从"贴边且浅色"的像素起头
                edge = (x in (0, W - 1) or y in (0, H - 1))
                if edge and sum(px[x, y]) > 470:
                    seen.add((x, y)); dq.append((x, y))
    n = 0
    while dq:
        x, y = dq.popleft()
        # 仍限制在四角方块内，双保险
        if not any(x0 <= x < x0 + size and y0 <= y < y0 + size
                   for (x0, y0) in boxes):
            continue
        px[x, y] = TRANSPARENT_KEY
        n += 1
        for nx, ny in ((x+1, y), (x-1, y), (x, y+1), (x, y-1)):
            if (0 <= nx < W and 0 <= ny < H and (nx, ny) not in seen
                    and sum(px[nx, ny]) > 470):
                seen.add((nx, ny)); dq.append((nx, ny))
    return n


def build(ref_path):
    os.makedirs(ASSETS, exist_ok=True)
    src = Image.open(ref_path).convert("RGB")
    W, H = src.size
    img = src.copy()

    # 按钮矩形与各自文字的实测区间（擦文案、做微模糊都要用）
    # 文字区间按「列分段」实测得出：图标段与文字段之间有明确空隙，
    # 因此可精确切分，既不碰图标，也不碰按钮两侧的高光边（x=167/508 等）。
    #   停止服务：图标 74..85        文字 93..138
    #   管理面板：图标 185..202      文字 213..259
    #   刷新：    图标 314..329      文字 338..360
    #   复制配置：图标 421..433      文字 443..489
    #   立即签到：图标 128..179      文字 260..327
    # tbox 由「文字墨迹 ± 2..3px」实测收紧，**绝不能碰按钮内边界**：
    # 旧值（如签到按钮 x 到 332、y 到 786）会切进按钮描边并在按钮外糊出色块。
    # 实测白字墨迹：
    #   停止服务 x 93..138  y 704..716      管理面板 x 213..259 y 705..717
    #   刷新     x 338..359 y 706..717      复制配置 x 444..512 y 706..718
    #   立即签到 x 260..313 y 754..768（礼物图标在 x 238..252，必须避开）
    BTN_TEXT = [
        ((68, 693, 173, 730),   (90, 701, 142, 721),   "停止服务"),
        ((176, 693, 278, 730),  (210, 702, 263, 721),  "管理面板"),
        ((289, 693, 390, 730),  (335, 703, 363, 721),  "刷新"),
        ((415, 693, 521, 730),  (441, 703, 516, 722),  "复制配置"),
        ((68, 752, 502, 789),   (257, 751, 317, 772),  "立即签到"),
    ]

    # ---------------- 1. 抹除动态数据 ----------------
    # 所有矩形都**必须停在卡片边框内侧**——边框是原画的一部分，越界会把它切断，
    # 表现为"边框缺了一段"。实测边框位置：
    #   账号卡 上边 y=182..183 / 下边 y=352..353 / 左 x=24..25 / 右 x=417..418
    #   账单卡 上边 y=470..471 / 下边 y=650..651 / 左 x=24..25 / 右 x=470..471
    #   今日请求卡 左 x=96..97 / 右 x=273..278
    #   积分卡     左 x=333..339（星光装饰在此）/ 右 x=440..444
    # 地址值：右边界 480（再往右 x≈482 有原画淡色装饰）
    erase(img, [205, 116, 480, 139], SVC_BG)
    erase(img, [205, 139, 302, 158], SVC_BG)          # Key 值
    # 账号池汇总行：文字实测 y 172..189，左右以卡片内边为准
    erase(img, [148, 172, 440, 188], SVC_BG)
    # 9 条账号行：第 1 行中心 201，行距 17.5，末行中心 341。
    # 账号文字实测 x 24..401、y 192..349；
    # 左 26（左上少女只占 x 0..23）、右 415（猫耳娘从 421 起）、
    # 下 350（**必须小于卡片下边框 y=352**，原来的 354 会切掉边框）。
    erase(img, [26, 190, 415, 350], SVC_BG)
    # 今日请求数值实测 x 96..201（含左侧的浅色描边）、y 399..443；
    # 左 102（左中少女占 x 6..100，极限贴边）、右 272（卡右边框 273）、
    # 上 396、下 442（卡下边 443）。
    erase(img, [102, 396, 272, 442], STAT1_BG)
    # 积分消耗数值「88.27」橙色墨迹实测 x 334..417、y 404..433；
    # 左 330（数字左侧的 #FDFDFE 高光在 x=334，留 4px 余量）、
    # 右 432（卡右边框 440）、
    # 上下收到 402..438（卡上下边 390/443 之间）。
    # 注意：x=333..339 那条深色其实是数字「8」的左笔，不是装饰 ——
    # 早先把左边界设成 341 会留下一条橙色竖线。
    erase(img, [330, 402, 432, 438], STAT2_BG)
    # 11 条账单行：文字实测 x 36..462、y 479..657；
    # 右 466（右边框 470）。
    # 下界取 658：实测空白列 x=150 在 y 644..662 全是卡片底色，
    # 即 y 649..655 那片"连续"内容其实是第 11 行的「0.3800 分」，
    # 收得太紧（646）会留下最后一行没擦掉（实机截图可见）。
    erase(img, [34, 476, 466, 658], SVC_BG)
    # 状态标题：只修补文字像素，保留标题后面的云。
    # 左边界 142（含状态圆点 x 146..162 —— 它的绿色也会被"深色描边"判据命中，
    # 一并擦掉是对的：圆点颜色随服务状态变，必须由运行时绘制）；
    # 右边界收在文字之后（title 墨迹到 x=305、sub 到 359）——
    # 早先写到 500 会把 x≈440..459 的**云影**当成文字修补，抹掉一块云。
    erase_sky(img, src, (142, 34, 366, 106), grow=4)

    # 按钮文案（保留图标与按钮底图）
    for box, tbox, name in BTN_TEXT:
        fill_btn_text(img, src, box, tbox, BTN_BASE[name])

    # 按钮区做一次极轻微的重采样，抹掉插值可能留下的细纹（不影响按钮轮廓）
    from PIL import ImageFilter
    for _b, tbox, _n in BTN_TEXT:
        # 只在文字区做极轻微模糊，避免影响图标与按钮高光
        img.paste(img.crop(tbox).filter(ImageFilter.GaussianBlur(0.4)),
                  (tbox[0], tbox[1]))

    # 四角打透明孔（必须在所有绘制之后做，否则后续填充会盖掉键色）
    n = punch_corners(img)
    print("  四角透明像素: %d" % n)

    img.save(os.path.join(ASSETS, "base.png"))

    # ---------------- 2. 图标 ----------------
    # 图标取自第 3 行（中心 236），避免第 1 行可能被角色遮挡
    ICON_CY = ROW0_CY + 2 * ROW_H
    y0, y1 = int(ICON_CY - 9), int(ICON_CY + 9)
    icons = {
        "icon_check.png":  (144, 162),
        "icon_bolt.png":   (316, 330),
        "icon_shrimp.png": (345, 360),
    }
    for name, (a, b) in icons.items():
        matte_icon(src, (a, y0, b, y1), SVC_BG).save(os.path.join(ASSETS, name))

    # 活跃账号标记：只有第 3 行有（x=30..38，在名字左侧）。实测确认它不是 ★ 字形，
    # 而是原画里的一个小徽标，因此按原样裁出。
    ROW3_CY = ROW0_CY + 2 * ROW_H
    matte_icon(src, (27, int(ROW3_CY - 9), 41, int(ROW3_CY + 9)),
               SVC_BG).save(os.path.join(ASSETS, "icon_active.png"))
    # 账单行图标（第 1 行两个，第 2 行起一个带绿）
    matte_icon(src, (93, int(BILL0_CY)-8, 107, int(BILL0_CY)+8), SVC_BG).save(
        os.path.join(ASSETS, "bill_icon_a.png"))
    matte_icon(src, (111, int(BILL0_CY)-8, 124, int(BILL0_CY)+8), SVC_BG).save(
        os.path.join(ASSETS, "bill_icon_b.png"))
    matte_icon(src, (93, int(BILL0_CY+BILL_H)-8, 107, int(BILL0_CY+BILL_H)+8),
               SVC_BG).save(os.path.join(ASSETS, "bill_icon_c.png"))

    # ---------------- 3. 坐标表 ----------------
    layout = {
        "size": [W, H],
        "titlebar": {"h": 34, "color": "#35379B"},
        "status": {
            # 参考图实测：绿色圆点中心 (153.5, 66)、半径约 7；
            # 外面还有一圈白色底环到 x 146..162（半径约 9）。
            # 圆点由运行时绘制（颜色随状态变），所以基底里要擦掉。
            "dot":   {"cx": 153, "cy": 66, "r": 7, "ring": 2},
            # 字体/字号/描边由 tools 里的自动比对选出：
            # 等线 Bold 27/描边3 与参考图标题的墨迹掩码差异最小
            "title": {"x": 172, "cy": 67, "size": 27, "stroke": 3,
                      "font": "Dengb.ttf"},
            # 副标题：参考图实测墨迹 169x10、深色像素 506。原 13px+描边2 会渲染成
            # 198x15 / 1550 像素，明显偏大偏粗；换等线常规 10px+描边1 后贴合。
            "sub":   {"x": 173, "cy": 97, "size": 10, "stroke": 1,
                      "font": "Deng.ttf"},
        },
        "svc": {
            "label_x": 157, "value_x": 207, "value_size": 12,
            "addr_cy": 131, "key_cy": 148,
            "pool_cy": 178, "pool_size": 11,
            "row0_cy": ROW0_CY, "row_h": ROW_H, "rows_max": 9, "size": 10,
            "col_name": 43, "col_star": 29,
            "col_check": 147, "col_state": 164,
            # 企业号（We_Game8/9）的状态列实测左移约 9px
            "col_check_ent": 138, "col_state_ent": 152,
            "col_bal": 226, "col_num": 259,
            "col_energy": 317, "col_energy_v": 334,
            "col_buddy": 346, "col_buddy_v": 364,
            "col_streak": 377, "col_end": 434,
            # 滚动条位置：在账号文字（≤401）与猫耳角色（≥421）之间的空白带
            "scrollbar_x": 412, "scrollbar_w": 3,
        },
        "stat": {
            # 统计数值：实测参考图的数字中心是 (156,413) 与 (377,415)，
            # 字高约 27~29px（不是最初估的 53/58 —— 那会盖住两侧的角色）。
            "req_value":    {"cx": 156, "cy": 413, "size": 36},
            "credit_value": {"cx": 376, "cy": 418, "size": 37},
        },
        "bill": {
            "row0_cy": BILL0_CY, "row_h": BILL_H, "rows_max": 11,
            "size": 11, "id_size": 9, "model_size": 9,
            "tok_size": 8, "credit_size": 9, "meta_size": 9,
            "col_time": 36,
            "col_icon_a": 92, "col_icon_b": 110, "col_icon_c": 92,
            "col_id": 128, "col_meta": 106, "col_model": 184,
            "col_tok_label": 296, "col_tokens": 316, "col_credit": 463,
            "seps": [82.5, 284.5, 413.5, 470.5], "sep_half": 5,
            # 滚轮命中的右边界（账单文字最右到 462，卡片右边框 470）
            "col_end": 466,
            # 参考图右侧原画就画着一条竖向轨道（x 528..531 的 4px 柔边细线）
            # 与一枚画死在固定位置的滑块胶囊（紫边 x 524..525 / 534，y 607..644）。
            # 原画滑块滚动时不动，故由渲染器按偏移重画；
            # 轨道本身是原画的一部分，渲染时**必须原样保留**（重画会变粗一倍）。
            # 轨道下方 y≥653 是原画的爪子装饰，同样不能碰。
            "scroll": {"track_x": 528, "track_w": 4, "track_y0": 480,
                       "track_y1": 650, "thumb_x": 524, "thumb_w": 12,
                       "thumb_h": 38, "art_thumb_y0": 606, "art_thumb_y1": 646,
                       "clean_y": 500},
        },
        "buttons": [
            {"key": "stop",    "x0": 68,  "x1": 173, "y0": 693, "y1": 729, "label": "停止服务"},
            {"key": "panel",   "x0": 176, "x1": 278, "y0": 693, "y1": 729, "label": "管理面板"},
            {"key": "refresh", "x0": 289, "x1": 390, "y0": 693, "y1": 729, "label": "刷新"},
            {"key": "copy",    "x0": 415, "x1": 521, "y0": 693, "y1": 729, "label": "复制配置"},
            {"key": "checkin", "x0": 68,  "x1": 502, "y0": 752, "y1": 788, "label": "立即签到"},
        ],
        # 按钮文案：字号按参考图实测宽度反推（小按钮 11~12、签到 17）。
        # 早先用 17/19 会超出按钮范围，看起来"不居中"。
        "button_text": {
            "stop":    [ 93, 710, 12], "panel":   [213, 711, 12],
            "refresh": [338, 711, 12], "copy":    [443, 711, 12],
            "checkin": [260, 762, 15],
        },
        "colors": {
            "ink": "#3B3358", "name": "#4A4A96", "dim": "#7A76A8",
            "value": "#3A3A80", "blue": "#5D7FD4", "orange": "#EC9204",
            "green": "#22A05A", "warn": "#E0A031", "red": "#E05462",
            "model": "#4A4A9E",
            "title_fill": "#FFFFFF", "title_stroke": "#31337F",
            "sub_fill": "#FFFFFF", "sub_stroke": "#3A3E80",
        },
    }
    with open(os.path.join(ASSETS, "layout.json"), "w", encoding="utf-8") as f:
        json.dump(layout, f, ensure_ascii=False, indent=1)

    # ---------------- 4. 核验 ----------------
    print("生成完成 -> %s" % ASSETS)
    for n in sorted(os.listdir(ASSETS)):
        p = os.path.join(ASSETS, n)
        print("  %-20s %7d B" % (n, os.path.getsize(p)))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python tools/build_ui_assets.py <参考图路径>")
        raise SystemExit(1)
    build(sys.argv[1])
