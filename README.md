# workbuddy2api

把 **WorkBuddy / CodeBuddy（腾讯代码助手）** 的桌面端登录态，转成你本机可直接使用的 **OpenAI / Anthropic 兼容 API**。

适用场景：

- 用 **Codex CLI** 走 `/v1/responses`
- 用 **Claude Code / CC Switch** 走 `/v1/messages`
- 用 **Cherry Studio / ZCode / LobeChat / NextChat / Open WebUI** 走 `/v1/chat/completions`

[English](#english) · [中文](#中文)

---

## 中文

### 这是什么

`workbuddy2api` 是一个本地协议转换器。它会读取你已经登录好的 WorkBuddy / CodeBuddy 桌面端凭据，转发到腾讯后端 `copilot.tencent.com`，然后在本地暴露这些接口：

- `POST /v1/chat/completions`
- `POST /v1/responses`
- `POST /v1/messages`
- `GET /v1/models`
- `GET /health`
- 内置 **Web 管理面板**（浏览器访问 `http://127.0.0.1:8787/`）

### Web 管理面板（内置 UI，默认开启）

本项目已内置一个 Web 管理面板，浏览器打开服务地址即可使用，无需额外部署：

| 页面 | 功能 |
|---|---|
| 仪表盘 | 服务运行状态、当前账号、Token 过期倒计时、今日请求统计、耗时趋势图 |
| 账号管理 | **多账号池**：自动扫描本机所有登录态、一键切换账号（无需重启）、有效性检测、手动导入、fixed/auto/failover 路由策略、标记积分耗尽/恢复账号 |
| 实时日志 | 请求/响应/审核拦截实时推送（WebSocket）、级别过滤、关键字搜索 |
| 设置 | 脱敏开关、API Key、日志路径、模型列表热更新 |
| 接入向导 | 一键生成 Codex CLI / CC Switch / 通用客户端配置 |

**新增参数**：

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--ui` | 开 | 启用管理面板（浏览器访问 `/`） |
| `--no-ui` | 关 | 关闭管理面板（纯 API 模式，同原版行为） |
| `--prefer NAME` | 无 | 优先使用指定名称/昵称的账号（默认选 mtime 最新的账号，而非字典序第一个） |
| `--strategy` | `failover` | 账号路由策略：`failover` 积分耗尽自动顶上 / `fixed` 固定 / `auto` 轮询 |

**管理 API**：`/api/accounts`（账号池）、`/api/config`（配置）、`/api/logs`（日志）、`/ws/logs`（实时日志 WebSocket）、`/api/status`（服务状态）。

> 多账号提示：本机 auth 目录下若有多个 `*.info`，原版会取字典序第一个（可能是个失效账号导致 401）。使用账号池可在面板里一键切换到有效账号。

它不负责登录，不模拟桌面端，也不替你执行工具。它只做三件事：

1. 读取本机登录态并注入鉴权头
2. 在 OpenAI / Anthropic 协议和腾讯后端协议之间转换
3. 对 `Codex CLI` 这类长上下文 agent 请求做后端友好的压缩投影

> 命名说明：项目对外名称现在叫 `workbuddy2api`。代码里仍保留部分历史命名，比如 `codebuddy2openai`、`CODEBUDDY2OPENAI_*`，目的是兼容旧配置和环境变量。
> 另外，GitHub 仓库路径当前也可能仍沿用 `codebuddy2openai`，这是仓库路径与项目展示名尚未完全统一，不影响使用。

### 你能用它做什么

- 把 WorkBuddy 订阅复用到 OpenAI 兼容客户端
- 让 Codex CLI 直接接腾讯后端，而不是只接 OpenAI 官方
- 让 Claude Code 通过 CC Switch 复用 WorkBuddy 支持的模型
- 保留原生 `tools` / `tool_calls` / 流式 SSE / 多轮工具调用

### 当前支持

| 客户端 / 协议 | 接口 | 当前状态 |
|------|------|------|
| OpenAI Chat Completions | `/v1/chat/completions` | 已支持 |
| OpenAI Responses | `/v1/responses` | 已支持，适配 Codex CLI |
| Anthropic Messages | `/v1/messages` | 已支持，适配 Claude Code / CC Switch |
| OpenAI Models | `/v1/models` | 已支持 |
| Health Check | `/health` | 已支持 |

---

## 模型能力表

哪些模型支持**图片输入**、哪些支持**推理档位**（reasoning_effort）、各自实测速度如何 —— 见 [docs/model-capabilities.md](docs/model-capabilities.md)。

配置客户端（DeepSeek Harness / Cherry Studio / 任意 SDK）时请参照该表：**只给真正支持视觉的模型声明 image 输入**，否则客户端放行图片但上游无法处理。

---

## 3 分钟上手

### 1. 前置条件

你需要先满足这 3 个条件：

1. 本机已经安装并登录 **WorkBuddy / CodeBuddy** 桌面端
2. 本机有 **Python 3.8+**
3. 已安装依赖 `fastapi`、`uvicorn`、`httpx`

默认会在这些位置寻找登录态：

- macOS: `~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/*.info`
- Windows: `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\*.info`
- Linux: `~/.local/share/CodeBuddyExtension/Data/Public/auth/*.info`

### 2. 安装依赖

推荐用 `uv`：

```bash
git clone https://github.com/ShouZhuo0413/codebuddy2openai.git workbuddy2api
cd workbuddy2api

uv venv
uv pip install -r requirements.txt
```

也可以用虚拟环境：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

> 注意：无论是启动服务，还是执行 `python3 converter.py --help`，都必须先装依赖。

### 3. 启动

最常用的启动方式：

```bash
uv run converter.py --desensitize --log converter.log
```

或：

```bash
python3 converter.py --desensitize --log converter.log
```

看到监听 `http://127.0.0.1:8787` 就说明已经起来了。

### 4. 快速自检

```bash
curl http://127.0.0.1:8787/health
curl http://127.0.0.1:8787/v1/models
```

如果这两条能通，说明本地服务、登录态、基本路由都没问题。

---

## 客户端接入

### Codex CLI

这是当前最推荐的接法。Codex CLI 走的是 `/v1/responses`，而不是 `/v1/chat/completions`。

推荐启动命令：

```bash
uv run converter.py --desensitize --log converter.log
```

把下面配置合并到 `~/.codex/config.toml`：

```toml
[model_providers.workbuddy]
name = "WorkBuddy (via local converter)"
base_url = "http://127.0.0.1:8787/v1"
wire_api = "responses"
env_key = "CODEBUDDY2OPENAI_KEY"

[profiles.workbuddy]
model = "glm-5.2"
model_provider = "workbuddy"
```

设置一个占位环境变量：

```bash
export CODEBUDDY2OPENAI_KEY=any-value
```

启动：

```bash
codex --profile workbuddy "你的任务描述"
```

补充说明：

- 推荐保留 `--desensitize`
- 当前 `/v1/responses` 默认已经会做投影压缩
- 如果你想尽量保留原始 system prompt，可试 `--desensitize --no-compact`
- `--desensitize --no-compact` 下若仍命中审核，当前实现会自动退回紧凑模式重试一次

### Claude Code / CC Switch

Claude Code 不走 OpenAI 协议，而是走 Anthropic Messages。

推荐启动命令：

```bash
uv run converter.py --desensitize --log converter.log
```

在 CC Switch 里配置：

```json
{
  "DeepSeek-V4-Pro": {
    "base_url": "http://127.0.0.1:8787/v1/messages",
    "api_key": "",
    "model": "deepseek-v4-pro"
  }
}
```

注意：

- 模型名必须填写腾讯后端支持的真实模型名
- 不做 Anthropic 模型名到腾讯模型名的自动映射
- Claude Code 场景强烈建议开启 `--desensitize`

### 其他 OpenAI 兼容客户端

适用于：

- Cherry Studio
- ZCode
- LobeChat
- NextChat
- Open WebUI
- 自己写的 OpenAI SDK 客户端

配置方式：

- Base URL: `http://127.0.0.1:8787/v1`
- API Key: 留空，或填你启动时设置的 `--api-key`
- 模型名: `glm-5.2` / `deepseek-v4-pro` / `kimi-k2.7` / `auto` 等

---

## Web 管理面板（多账号）

服务默认会把浏览器访问 `http://127.0.0.1:8787/` 打开为 Web 管理面板（`--no-ui` 可关闭），支持：

- 查看所有已登录账号（昵称 / token 有效性 / 目录 / **健康状态**）
- 一键切换当前账号（`fixed` 策略下立即生效）
- 路由策略切换：
  - `failover`（默认）：**当前账号积分/额度耗尽、被限流或鉴权失败时，自动标记该账号并切换到下一个可用账号顶上重试**，冷却到期自动恢复参与路由
  - `fixed`（固定一个账号）或 `auto`（多账号轮询，提升并发）
- 手动「标记积分耗尽」（立即顶上其他账号）与「恢复账号」（解除冷却/耗尽标记）
- 启用 / 禁用账号、重命名、手动导入 `.info`
- 热更新配置（如 `desensitize`），无需重启
- 实时请求日志与统计

账号池会自动扫描本机所有 `*.info` 登录态。多账号场景建议用 `--prefer 名称` 指定主账号：

```bash
python3 converter.py --desensitize --prefer "烟逝"
```

> 提示：桌面端每次新登录都会生成新的 `*.info` 目录，账号池会自动发现并纳入管理，也可在面板里禁用旧账号。

---

## 常用命令

### 基本启动

```bash
python3 converter.py
python3 converter.py --desensitize
python3 converter.py --desensitize --log converter.log
python3 converter.py --api-key mysecret
python3 converter.py --port 9000
```

### 命令行参数

| 参数 | 默认值 | 说明 |
|------|------|------|
| `--host` | `127.0.0.1` | 监听地址 |
| `--port` | `8787` | 监听端口 |
| `--api-key` | 无 | 给本地客户端加一层鉴权 |
| `--log` | 无 | 记录请求与响应日志 |
| `--desensitize` | 关 | 压缩运行时提示、去掉 tool description、零宽脱敏高风险关键词 |
| `--no-compact` | 关 | 配合 `--desensitize` 使用，保留更完整的原始 system prompt |
| `--skip-check` | 否 | 跳过启动预检 |
| `--prefer` | 无 | 多账号模式下优先使用指定名称/昵称的账号（默认用登录时间最新的账号） |
| `--strategy` | `failover` | 账号路由策略：`failover` 积分耗尽自动顶上 / `fixed` 固定 / `auto` 轮询 |
| `--ui` | 开 | 启用 Web 管理面板，浏览器访问 `/` 即可管理账号、热改配置、看日志 |
| `--no-ui` | 关 | 关闭 Web 管理面板（纯 API 模式） |

### curl 示例

```bash
curl http://127.0.0.1:8787/v1/models

curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-5.2","messages":[{"role":"user","content":"你好"}]}'

curl -N http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-5.2","stream":true,"messages":[{"role":"user","content":"数1到5"}]}'
```

---

## 打包 exe 与安装包

### 1. 打包成单文件 exe

```bat
build.bat
```

产出 `dist\CodeBuddy2API.exe`（onefile、无控制台窗口，并内嵌 WorkBuddy 客户端图标）。

脚本会自动挑选一个装了 PyInstaller 的解释器（优先项目 `.venv`，其次系统 Python）；也可以用 `set PYTHON=<解释器路径>` 显式指定。

打包完成后脚本会**自动跑一次完整性自检**（`dist\CodeBuddy2API.exe --selfcheck`），结果打印到屏幕并写入 `dist\selfcheck.log`。也可以随时手动跑：

```bat
dist\CodeBuddy2API.exe --selfcheck
```

自检会逐项确认模块与随包数据是否齐全。**看到任何 `[FAIL]` 就说明打包缺件，别急着发出去。**

> **别动 `build.bat` 里那串 `--collect-*` / `--add-data`。** PyInstaller 的静态分析看不见「动态导入」和「数据文件」，源码跑得好好的、打包后就炸。已经踩过四个：
>
> | 缺的东西 | 症状 | 补法 |
> |---|---|---|
> | `uvicorn.protocols.http.auto` | 服务起不来 | `--collect-submodules uvicorn` |
> | `anyio._backends._asyncio` | 每个请求都 500 | `--collect-submodules anyio` |
> | `certifi` 的 `cacert.pem` | 所有 httpx 请求 `[Errno 2]`，签到直接失败 | `--collect-data certifi` |
> | `assets/` 目录 | exe 图标正常，但窗口/任务栏退回 tkinter 羽毛图标 | `--add-data "assets;assets"` |
>
> 另外运行时还有一道兜底：`ssl_bootstrap.py` 会在 certifi 证书缺失时自动换用其它可用 CA，避免同类问题再次导致全链路失败。

脚本会保留上一版 `dist\config.json`（打包过程会清空 `dist`，脚本会先备份再放回），所以改过的端口 / key 不会在重打包后被重置。

### 2. 生成安装包

需要先装 [Inno Setup 6](https://jrsoftware.org/isdl.php)，然后：

```bat
ISCC.exe setup.iss
```

产出 `release\CodeBuddy2API-Setup-<版本>.exe`。

安装包会：

- 把安装程序自身、**桌面与开始菜单快捷方式**、卸载项图标统一换成 `assets\icon.ico`（取自 WorkBuddy 客户端）
- 默认勾选"创建桌面快捷方式"
- 不携带 `config.json`，首次启动在 exe 同目录自动生成默认配置

> 换图标只需替换 `assets\icon.ico`，无需改脚本。

---

## 日志与排障

### 推荐启动方式

```bash
uv run converter.py --desensitize --log converter.log
```

### 日志里能看到什么

每次请求都会带一个唯一 ID，常见日志包括：

- `REQUEST BODY`
- `RESPONSES → CHAT BODY`
- `RESPONSES PROJECTION`
- `RESPONSE BODY`
- `RESPONSE RAW SSE`
- `⚠️内容审核拦截`

其中 `RESPONSES PROJECTION` 会告诉你：

- 投影前后消息数
- 投影前后字符数
- tool schema 压缩量
- 是否丢掉了 harness 消息
- 是否保留了 anchor user

### 最常见问题

#### 找不到登录文件

说明桌面端没登录，或者登录目录不在默认路径。先确认桌面端已经真正完成登录。

#### 401

分两种：

- 本地 401：你启用了 `--api-key`，但客户端没带同一个 key
- 后端 401：腾讯 token 失效，尝试重新打开桌面端登录

#### 响应慢

先换快一点的模型，比如 `deepseek-v4-flash`。

#### 被“敏感内容”拦截

这是腾讯后端的内容审核，不一定是用户问题本身敏感，很多时候是 agent runtime 文本触发的，比如：

- `DoS`
- `exploit`
- `credential`
- `sandbox`
- `escalation`
- 竞争品牌词
- tool description 中的安全术语

建议排查顺序：

1. 开 `--log`
2. 看同一请求 ID 下的 `REQUEST BODY` 或 `RESPONSES → CHAT BODY`
3. 如果是 Codex CLI，再看 `RESPONSES PROJECTION`
4. 开 `--desensitize`
5. 如果还不稳，再尝试 `--desensitize --no-compact`

---

## Docker 部署

如果你更习惯用 Docker，可以直接用。

前提是把宿主机登录态目录挂进去，因为容器里拿不到桌面端 auth 文件。

### docker compose

先改 `docker-compose.yml` 里的 auth 挂载路径，再执行：

```bash
docker compose up -d --build
```

### docker run

```bash
docker build -t workbuddy2api .

docker run -d --name workbuddy2api -p 8787:8787 \
  -v ~/Library/Application Support/CodeBuddyExtension/Data/Public/auth:/data/auth:ro \
  -e CODEBUDDY_AUTH_DIR=/data/auth \
  workbuddy2api
```

### 相关环境变量

| 变量 | 说明 |
|------|------|
| `CODEBUDDY_AUTH_DIR` | 指定登录态目录 |
| `CODEBUDDY2OPENAI_KEY` | 本地 API Key |
| `CODEBUDDY2OPENAI_LOG` | 日志路径 |

---

## 模型列表

当前内置默认模型列表（与 `converter.py` 的 `DEFAULT_MODELS` 一致，共 15 个）：

`glm-5.3`、`glm-5.3-flash`、`glm-5.2`、`glm-5.1`、`glm-5v-turbo`、`kimi-k2.7`、`kimi-k2.6`、`kimi-k2.5`、`deepseek-v4.1-flash`、`deepseek-v4-pro`、`deepseek-v4-flash`、`hunyuan-2.0-instruct`、`minimax-m3-pay`、`hy3-preview-agent`、`auto`

其中 **9 个支持图片输入**，完整能力对照见 [docs/model-capabilities.md](docs/model-capabilities.md)。

具体能不能用，取决于你的 WorkBuddy / CodeBuddy 订阅。

---

## 项目结构

```text
workbuddy2api/
├── converter.py
├── responses_adapter.py
├── responses_projection.py
├── anthropic_adapter.py
├── desensitize.py
├── account_pool.py
├── billing.py
├── cn_importer.py
├── ui_admin.py
├── app.py
├── ssl_bootstrap.py
├── selfcheck.py
├── build.bat
├── setup.iss
├── tools/
│   └── verify_build.py
├── docs/
│   └── model-capabilities.md
├── assets/
│   ├── icon.ico
│   └── icon.png
├── ui/
│   ├── index.html
│   ├── favicon.svg
│   └── vendor/
├── codex-codebuddy.example.toml
├── test_responses_adapter.py
├── test_anthropic_adapter.py
├── test_account_pool.py
├── test_ui_admin.py
├── README.md
└── LICENSE
```

各文件作用：

- `converter.py`: 主入口，FastAPI 服务
- `responses_adapter.py`: OpenAI Responses ↔ Chat 适配
- `responses_projection.py`: Codex / agent 请求投影压缩
- `anthropic_adapter.py`: Anthropic Messages ↔ Chat 适配
- `desensitize.py`: 运行时文本压缩与零宽脱敏
- `account_pool.py`: 多账号池，扫描/切换/路由策略/启用禁用
- `billing.py`: 上游积分查询与签到
- `cn_importer.py`: 批量导入国内账号
- `ui_admin.py`: Web 管理面板的后端路由与内存日志总线
- `app.py`: Tkinter 桌面启动器（GUI）
- `ssl_bootstrap.py`: CA 证书定位兜底（打包漏收证书时的第二道防线）
- `selfcheck.py`: 打包完整性自检，见「打包 exe 与安装包」
- `build.bat`: PyInstaller 打包脚本
- `setup.iss`: Inno Setup 安装包脚本
- `tools/verify_build.py`: 校验 exe 是否内嵌图标 / 可正常启动
- `assets/`: 应用图标（打包与快捷方式用）
- `ui/`: 管理面板前端页面

---

## 致谢

本项目基于 [HanHan666666/codebuddy2openai](https://github.com/HanHan666666/codebuddy2openai) 的思路演进而来，感谢原作者的开源贡献。

## 免责声明

本项目仅用于个人学习与研究。与腾讯、WorkBuddy、CodeBuddy、OpenAI、Anthropic 无官方关联。请仅在你合法拥有订阅的前提下使用，并自行承担风险。

## 开源协议

[MIT](./LICENSE)

---

<a name="english"></a>
## English

`workbuddy2api` exposes your already logged-in **WorkBuddy / CodeBuddy** desktop session as local **OpenAI- and Anthropic-compatible APIs**.

Supported endpoints:

- `POST /v1/chat/completions`
- `POST /v1/responses`
- `POST /v1/messages`
- `GET /v1/models`
- `GET /health`

Recommended use cases:

- **Codex CLI** via `/v1/responses`
- **Claude Code / CC Switch** via `/v1/messages`
- **Cherry Studio / ZCode / LobeChat / Open WebUI** via `/v1/chat/completions`

### Quick Start

```bash
git clone https://github.com/ShouZhuo0413/codebuddy2openai.git workbuddy2api
cd workbuddy2api

uv venv
uv pip install -r requirements.txt
uv run converter.py --desensitize --log converter.log
```

Then verify:

```bash
curl http://127.0.0.1:8787/health
curl http://127.0.0.1:8787/v1/models
```

### Codex CLI

Use `/v1/responses` and keep `--desensitize` enabled.

```toml
[model_providers.workbuddy]
name = "WorkBuddy (via local converter)"
base_url = "http://127.0.0.1:8787/v1"
wire_api = "responses"
env_key = "CODEBUDDY2OPENAI_KEY"

[profiles.workbuddy]
model = "glm-5.2"
model_provider = "workbuddy"
```

Run:

```bash
export CODEBUDDY2OPENAI_KEY=any-value
codex --profile workbuddy "your task"
```

### Claude Code / CC Switch

Use `/v1/messages`:

```json
{
  "DeepSeek-V4-Pro": {
    "base_url": "http://127.0.0.1:8787/v1/messages",
    "api_key": "",
    "model": "deepseek-v4-pro"
  }
}
```

### Notes

- `--desensitize` is recommended for both Codex CLI and Claude Code
- `/v1/responses` already applies backend-facing projection by default
- `--desensitize --no-compact` preserves more of the original system prompt
- if that still gets review-blocked, `/v1/responses` will retry once in compact mode

### CLI Options

```bash
python3 converter.py [--host HOST] [--port PORT] [--api-key KEY] [--log PATH] [--desensitize] [--skip-check]
```

### Disclaimer

For personal learning and research only. Not affiliated with Tencent, WorkBuddy, CodeBuddy, OpenAI, or Anthropic.

---

<sub>
Keywords: codebuddy to openai · codebuddy2openai · workbuddy api proxy · workbuddy openai adapter · codex cli workbuddy · claude code workbuddy · tencent code assistant openai compatible api
</sub>
