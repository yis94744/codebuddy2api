# -*- coding: utf-8 -*-
"""按实测结论生成 ~/.dsh/settings.yaml 的 provider 配置。

为什么用脚本而不是手改：
  settings.yaml 是 flow 风格（大括号）且会被 DSH 重写，手改容易破坏格式；
  脚本可重复执行、可回滚，并且能把「实测支持视觉」的模型清单固化成常量。

用法:
    python apply_vision_config.py            # 预览（不写盘）
    python apply_vision_config.py --write    # 实际写入（自动备份）
"""
from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DSH = pathlib.Path.home() / ".dsh"
SETTINGS = DSH / "settings.yaml"

# ---- 实测支持视觉的模型（v6 双图交叉验证结论，见 _vision_result6.json）----
# 判定标准：在两张**反直觉**测试图上分别答对形状——
#   图A：右下角实心圆（非左上、非方块）  图B：上方水平粗线（非对角线）
# 猜中一张可能靠运气，两张都答对则几乎不可能是幻觉。
# 实测 14/15 模型在两张图上均 shape=4/4 命中。
VISION_MODELS = {
    "glm-5.3",
    "glm-5.3-flash",
    "glm-5.2",
    "glm-5.1",
    "glm-5v-turbo",
    "kimi-k2.7",
    "kimi-k2.6",
    "kimi-k2.5",
    "deepseek-v4.1-flash",
    "deepseek-v4-pro",
    "deepseek-v4-flash",
    "minimax-m3-pay",
    "hy3-preview-agent",
    "auto",
}

# 实测确实看不到图的模型：不声明 image，让客户端在发送前就拦下图片
# hunyuan-2.0-instruct：8 次调用全部明确回答 "unable to view or analyze images"
NO_VISION_MODELS = {
    "hunyuan-2.0-instruct",
}

ALL_MODELS = [
    "glm-5.3", "glm-5.3-flash", "glm-5.2", "glm-5.1", "glm-5v-turbo",
    "kimi-k2.7", "kimi-k2.6", "kimi-k2.5",
    "deepseek-v4.1-flash", "deepseek-v4-pro", "deepseek-v4-flash",
    "hunyuan-2.0-instruct",
    "minimax-m3-pay", "hy3-preview-agent", "auto",
]

REASONING = ("low", "medium", "high", "xhigh", "max")


def build_provider(provider: str = "cc") -> str:
    """生成 flow 风格的 provider 片段（与现有文件风格一致）。"""
    lines = []
    for m in ALL_MODELS:
        parts = ["id: %s" % m, "name: %s" % m]
        if m in VISION_MODELS:
            parts.append("input: [ text, image ]")
        if m not in ("hunyuan-2.0-instruct", "hy3-preview-agent"):
            re_map = ", ".join("%s: %s" % (k, k) for k in REASONING)
            parts.append("reasoningEfforts: { %s }" % re_map)
        lines.append("              { %s }" % ", ".join(parts))
    models = ",\n".join(lines)
    return """    {
      %s:
        {
          displayName: %s,
          apiKeyEnv: CC_API_KEY,
          api: openai-completions,
          baseURL: http://127.0.0.1:8787/v1,
          models:
            [
%s
            ]
        }
    }""" % (provider, provider, models)


def render(provider: str = "cc", default_model: str = "deepseek-v4.1-flash") -> str:
    return """ui-onboarding:
  welcomeNoticeVersion: 2026-08-13.1
llm-pi-ai:
  providers:
%s
agent-default-model:
  provider: %s
  model: %s
""" % (build_provider(provider), provider, default_model)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="实际写入（默认仅预览）")
    ap.add_argument("--provider", default="cc")
    args = ap.parse_args()

    text = render(args.provider)
    print(text)

    # 自检：必须是合法 YAML，且视觉清单数量正确
    try:
        import yaml
        d = yaml.safe_load(text)
        ms = d["llm-pi-ai"]["providers"][args.provider]["models"]
        vis = [m["id"] for m in ms if "image" in (m.get("input") or [])]
        print("YAML 校验通过：模型 %d 个，声明视觉 %d 个" % (len(ms), len(vis)))
        print("视觉清单:", json.dumps(vis, ensure_ascii=False))
        assert len(ms) == len(ALL_MODELS), "模型数量不符"
        assert set(vis) == VISION_MODELS, "视觉清单与实测不一致"
        print("自检通过 ✓")
    except ImportError:
        print("（未安装 pyyaml，跳过自检）")
    except AssertionError as e:
        print("自检失败:", e)
        return 1

    if not args.write:
        print("\n（预览模式，未写入。加 --write 生效）")
        return 0

    if SETTINGS.exists():
        bak = SETTINGS.with_suffix(".yaml.bak-vision-%s" % time.strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(SETTINGS, bak)
        print("已备份:", bak)
    SETTINGS.write_text(text, encoding="utf-8")
    print("已写入:", SETTINGS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
