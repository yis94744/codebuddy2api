# 模型能力表

本文档记录 CodeBuddy2API 网关对接的各模型**实测**能力，供配置客户端（如 DeepSeek Harness 等）时参考。

> 所有数据均为**实测结果**（2026-09 于本机环境），非厂商文档抄录。上游能力可能随时调整，如发现不符请重新实测。

---

## 一、模型清单

网关默认暴露以下模型（`/v1/models`）：

| 模型 ID | 视觉 | 推理档位 | 备注 |
|---|:---:|:---:|---|
| `glm-5.3` | ✗ | ✓ | 旗舰；**不支持图片输入** |
| `glm-5.3-flash` | ✓ | ✓ | 快且支持视觉，推荐日常使用 |
| `glm-5.2` | ✓ | ✓ | 稳定，支持视觉 |
| `glm-5.1` | ✗ | ✓ | 不支持图片输入 |
| `glm-5v-turbo` | ✓ | ✓ | 视觉专用模型（v = vision） |
| `kimi-k2.7` | ✓ | ✓ | 支持视觉 |
| `kimi-k2.6` | ✓ | ✓ | 支持视觉 |
| `kimi-k2.5` | ✓ | ✓ | 支持视觉 |
| `deepseek-v4.1-flash` | ✓ | ✓ | 新接入；支持视觉 + 推理档位 |
| `deepseek-v4-pro` | ✗ | ✓ | 速度较慢（约 46 tok/s） |
| `deepseek-v4-flash` | ✓ | ✓ | 支持视觉 |
| `hunyuan-2.0-instruct` | ✗ | ✗ | 明确提示"请切换至多模态模型" |
| `minimax-m3-pay` | ✗ | ✓ | 不支持图片输入 |
| `hy3-preview-agent` | ✗ | ✗ | 预览版，行为不稳定 |
| `auto` | ✓ | ✓ | 自动路由，实测支持视觉 |

**统计**：15 个模型中，**8 个支持视觉**（glm-5.3-flash、glm-5.2、glm-5v-turbo、kimi-k2.7/2.6/2.5、deepseek-v4.1-flash、deepseek-v4-flash、auto）。

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

```yaml
llm-pi-ai:
  providers:
    codebuddy:
      displayName: 积分
      apiKeyEnv: CODEBUDDY_API_KEY
      api: openai-completions
      baseURL: http://127.0.0.1:8000/v1
      compat:
        supportsDeveloperRole: false
      models:
        # 支持视觉 + 推理档位的模型
        - id: glm-5.3-flash
          input: [text, image]
          reasoningEfforts:
            low: low
            medium: medium
            high: high
            xhigh: xhigh
            max: max
        # 纯文本模型：不声明 image，客户端会在发送前拦截图片请求
        - id: glm-5.3
          reasoningEfforts:
            low: low
            medium: medium
            high: high
            xhigh: xhigh
            max: max
```

**关键说明**：

1. **`input` 声明决定客户端是否允许发送图片**。客户端（如 DSH）在发送前检查此字段，未声明 `image` 时会直接拒绝，请求不会到达网关。
2. **只给真正支持视觉的模型声明 `image`**。若给不支持视觉的模型声明，客户端会放行图片，但上游会忽略或报错，反而造成困扰。
3. `reasoningEfforts` 建立客户端档位到线上参数的映射，未配置则客户端不显示推理档位选择器。

---

## 五、其他客户端

### OpenAI 兼容（任意 SDK）

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="<你的 api_key>")

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
