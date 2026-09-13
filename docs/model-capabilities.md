# 模型能力表

本文档记录 CodeBuddy2API 网关对接的各模型**实测**能力，供配置客户端（如 DeepSeek Harness 等）时参考。

> 所有数据均为**实测结果**（2026-09 于本机环境），非厂商文档抄录。上游能力可能随时调整，如发现不符请重新实测。

---

## 〇、视觉能力的实测方法（重要）

视觉能力**不能靠模型名或厂商文档判断**，必须实测。本仓库的探针演进过程说明了原因：

| 版本 | 方法 | 暴露的问题 |
|---|---|---|
| v1 | 1x1 纯色图，问颜色 | 模型在瞎猜颜色；上游对不支持的图片**静默返回 200**，导致 15/15 全判"支持"——完全失真 |
| v2 | 方块+对角线图 | 正则匹配到 "geometric shapes" 就判 YES，把明确回答"整张图空白"的模型误判为支持 |
| v3 | 否认优先判定 | 单次结论不稳定：同一模型时而答对、时而说看不到 |
| v4 | 3 次多数票 | 把"上游抖动导致的空回答"误判成"不支持视觉"（kimi-k2.7 3 次全空，加测 8 次后却成功描述了特征） |
| v5 | 加纯文本对照组 | 对照组失效（所有模型纯文本都正常），且仍有模型靠先验"编造"命中特征 |
| **v6** | **双图交叉验证** | 用两张**反直觉**图（右下角圆 / 上方水平线），要求分别答对各自形状。猜中一张可靠运气，两张都答对则几乎不可能是幻觉 |

**结论：判定"支持视觉"必须要求模型在两张不同的、非典型图片上都答对具体形状与方位**，否则会把"否认看到图"的模型误判为支持。

**上游抖动是常态**：同一模型同一问题，成功与失败可能交替出现（实测 `kimi-k2.7` 6 次中仅 1 次成功，`hunyuan-2.0-instruct` 6 次中 5 次明确否认）。因此**单次调用结果不可作为依据**。

---

## 一、模型清单

网关默认暴露以下模型（`/v1/models`）：

| 模型 ID | 视觉 | 推理档位 | 备注 |
|---|:---:|:---:|---|
| `glm-5.3` | ✓ | ✓ | 旗舰 |
| `glm-5.3-flash` | ✓ | ✓ | 快且支持视觉，推荐日常使用 |
| `glm-5.2` | ✓ | ✓ | 稳定，支持视觉 |
| `glm-5.1` | ✓ | ✓ | 支持视觉 |
| `glm-5v-turbo` | ✓ | ✓ | 视觉专用模型（v = vision） |
| `kimi-k2.7` | ✓ | ✓ | 支持视觉，但成功率偏低（实测 6 次中 1 次成功） |
| `kimi-k2.6` | ✓ | ✓ | 支持视觉 |
| `kimi-k2.5` | ✓ | ✓ | 支持视觉 |
| `deepseek-v4.1-flash` | ✓ | ✓ | 支持视觉 + 推理档位，首选 |
| `deepseek-v4-pro` | ✓ | ✓ | 支持视觉；速度较慢（约 46 tok/s） |
| `deepseek-v4-flash` | ✓ | ✓ | 支持视觉 |
| `hunyuan-2.0-instruct` | ✗ | ✗ | 明确提示"请切换至多模态模型" |
| `minimax-m3-pay` | ✓ | ✓ | 支持视觉 |
| `hy3-preview-agent` | ✓ | ✗ | 预览版；v6 双图验证通过，但整体行为不稳定 |
| `auto` | ✓ | ✓ | 自动路由，实测支持视觉 |

> **注**：上表的视觉列以 v6 双图交叉验证为准。`hunyuan-2.0-instruct` 多次明确回答"无法查看图片"，判定为不支持；`hy3-preview-agent` 在 v6 中也通过（两图均 shape=4/4），故一并声明 `image`。


---

## 二、推理档位（reasoning_effort）

支持推理的模型接受以下档位，值会被透传到上游：

| 档位 | 说明 |
|---|---|
| `low` | 轻度思考 |
| `medium` | 中度思考 |
| `high` | 高度思考 |
| `xhigh` | 极高（部分模型等同 max） |
| `max` | 最大思考量 |

实测（`deepseek-v4.1-flash`，思考 token 数）：

| 档位 | 思考 token |
|---|---|
| 不传 | 0 |
| `low` | 132 |
| `high` | 282 |
| `max` | 满格（被 max_tokens 截断） |

**注意**：`hunyuan-2.0-instruct` 与 `hy3-preview-agent` 不响应推理档位。

---

## 三、性能实测

测试条件：单轮约 500 字中文生成，流式输出，本机 → 网关 → 上游。

| 模型 | 首字延迟 | 生成吞吐 |
|---|---|---|
| `deepseek-v4.1-flash` | 2.4 s | **131–143 tok/s** |
| `deepseek-v4-flash` | 2.6 s | 138 tok/s |
| `deepseek-v4-pro` | 3.0 s | 46 tok/s |
| `glm-5.3-flash` | 2.8 s | 86 tok/s |

> 首字延迟含网关转发与上游处理。网关自 v1.0.0 起使用共享连接池，实测首字延迟由约 2.0s 降至约 0.8–1.1s。

---

## 四、客户端配置示例

### DeepSeek Harness（`~/.dsh/settings.yaml`）

下面是最小可用骨架。**完整可用的参考配置**见本机 `~/.dsh/settings.yaml`（已把 14 个视觉模型全部声明了 `image`）。

> ⚠️ **别手改 `~/.dsh/settings.yaml`。** 该文件是 flow 风格（大括号）、且 **DSH 会重写它**——实测 2026-09-12 14:06 写入的视觉声明，在 14:12 DSH 重写后被整段抹掉。请用 `python tools/apply_vision_config.py --write` 生成（自带备份 + YAML 自检 + 视觉清单断言）。写入后若发现字段又没了，说明 DSH 侧会覆盖，需要在 DSH 界面内配置而非改文件。

```yaml
llm-pi-ai:
  providers:
    cc:
      displayName: cc
      apiKeyEnv: CC_API_KEY
      api: openai-completions
      baseURL: http://127.0.0.1:8787/v1
      models:
        # 支持视觉 + 推理档位的模型：input 必须声明 image
        - id: glm-5.3-flash
          name: glm-5.3-flash
          input: [ text, image ]
          reasoningEfforts:
            low: low
            medium: medium
            high: high
            xhigh: xhigh
            max: max
        # 纯文本模型：不声明 image，客户端会在发送前拦截图片请求
        - id: glm-5.3
          name: glm-5.3
          reasoningEfforts:
            low: low
            medium: medium
            high: high
            xhigh: xhigh
            max: max
agent-default-model:
  provider: cc
  model: deepseek-v4.1-flash
  reasoningEffort: max
```

**需要声明 `input: [ text, image ]` 的完整清单**（15 个模型中的 14 个，与 `tools/apply_vision_config.py` 的 `VISION_MODELS` 常量保持一致）：

`glm-5.3`、`glm-5.3-flash`、`glm-5.2`、`glm-5.1`、`glm-5v-turbo`、`kimi-k2.7`、`kimi-k2.6`、`kimi-k2.5`、`deepseek-v4.1-flash`、`deepseek-v4-pro`、`deepseek-v4-flash`、`minimax-m3-pay`、`hy3-preview-agent`、`auto`

只有 1 个（`hunyuan-2.0-instruct`）**不要**声明 `image`。

**关键说明**：

1. **`input` 声明决定客户端是否允许发送图片**。客户端（如 DSH）在发送前检查此字段，未声明 `image` 时会直接拒绝，请求不会到达网关。
2. **只给真正支持视觉的模型声明 `image`**。若给不支持视觉的模型声明，客户端会放行图片，但上游会忽略或报错，反而造成困扰。
3. `reasoningEfforts` 建立客户端档位到线上参数的映射，未配置则客户端不显示推理档位选择器。

---

## 五、其他客户端

### OpenAI 兼容（任意 SDK）

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8787/v1", api_key="<你的 api_key>")

resp = client.chat.completions.create(
    model="deepseek-v4.1-flash",
    messages=[{"role": "user", "content": "你好"}],
    extra_body={"reasoning_effort": "high"},   # 可选
)
```

### Anthropic 兼容

网关同时提供 `/v1/messages` 端点，可直接对接 Anthropic SDK。

---

## 六、图片输入说明

- 支持 OpenAI 标准的 `image_url` 内容块
- **推荐使用 base64 data URL**（`data:image/png;base64,...`），实测稳定
- 远程图片 URL 可能被上游拒绝（返回 `Please start a new conversation, replace the image`），建议客户端读取本地图片后转为 base64 再发送
