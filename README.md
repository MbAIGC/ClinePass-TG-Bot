# 🤖 ClinePass TG Bot

把 Cline / ClinePass 账号的用量做成 Telegram 面板：一个用户可绑定多个 API Key，`/status` 一次看全部账号。

```
🤖 ClinePass Status Panel

───────────────

🔑 账号/别名：主账号  sk_7f2a…9c41
👤 w***@gmail.com · Wang Jays
💳 Cline Pass (Monthly)（Monthly · ✅ 生效）
📆 计费周期：2026-09-23 → 2026-10-23
📊 5 小时额度（已用）
░░░░░░░░░░ 2% · 剩余 98%
重置：09-25 22:32（还有 4 小时 12 分）
📊 本周额度（已用）
██████░░░░ 57% · 剩余 43%
重置：09-30 20:08（还有 5 天 1 小时）
📊 本月额度（已用）
███░░░░░░░ 28% · 剩余 72%
重置：10-23 20:08（还有 28 天 1 小时）

───────────────

🔄 更新时间 18:20:11
```

## 功能

- **多 Key 管理**：`/addkey`、`/delkey`、`/keys`、`/clear`，每个用户的数据互相隔离
- **安全的 Key 处理**：只显示掩码（`sk_7f2a…9c41`），含 Key 的消息自动撤回，配置文件权限 `600`
- **真实额度**：调用官方 `plan/usage-limits`，展示 5 小时 / 本周 / 本月进度条、剩余百分比、重置时间与倒计时，`≥80%` 标 ⚠️、`≥95%` 标 ⛔️
- **多账号并行查询**：`asyncio` + 线程池，不会阻塞 Bot 的其他用户
- **不编造数据**：接口失败就显示失败原因，绝不拿"默认数字"冒充真实额度
- **健壮存储**：JSON 原子写入、文件损坏自动备份重建、路径异常时明确报错
- **可运维**：白名单、限流冷却、消息自动分片、错误统一兜底、结构化日志
- **开箱即用**：`docker compose up -d`，无需手动创建配置文件

## 快速开始

### 方式一：Docker Compose（推荐）

```bash
git clone https://github.com/MbAIGC/ClinePass-TG-Bot.git
cd ClinePass-TG-Bot
cp .env.example .env
vim .env                     # 至少填 TELEGRAM_BOT_TOKEN

docker compose up -d --build
docker compose logs -f
```

### 方式二：本地运行（Python ≥ 3.10）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export TELEGRAM_BOT_TOKEN=123456:ABC...
python bot.py
```

### 跑起来之后

1. 私聊 Bot 发送 `/id`，拿到你的用户 ID（可选：填进 `.env` 的 `ALLOWED_USER_IDS` 开启白名单）
2. `/addkey 主账号 sk_你的key`
3. `/status` 查看面板

## 指令一览

| 指令 | 说明 |
| --- | --- |
| `/start` | 欢迎语与上手引导 |
| `/help` | 指令帮助 |
| `/status`（别名 `/quota`） | 查询所有已绑定 Key 的额度面板 |
| `/addkey <别名> <API_KEY>` | 添加或更新 Key（仅私聊） |
| `/delkey <别名>` | 删除指定 Key（仅私聊） |
| `/keys` | 列出已绑定的别名与掩码（仅私聊） |
| `/clear confirm` | 清空自己的全部 Key（仅私聊） |
| `/id` | 查看自己的 Telegram 用户 ID |

> 涉及 Key 的指令只在**私聊**生效，避免在群里泄露账号信息；`/addkey` 发出的那条消息会被 Bot 立即撤回。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | 无（必填） | BotFather 给的 Token，缺失时启动即退出（退出码 1） |
| `CLINEPASS_API_BASE` | `https://api.cline.bot` | API 根地址 |
| `CLINEPASS_USAGE_PATH` | `/api/v1/users/me/plan/usage-limits` | 额度接口路径，见下文 |
| `REQUEST_TIMEOUT` | `12` | 单次请求超时（秒） |
| `HTTP_RETRIES` | `2` | 429 / 5xx / 网络错误的重试次数 |
| `RETRY_BACKOFF` | `1.5` | 重试退避基数（秒），指数增长 |
| `MAX_PARALLEL` | `4` | 同时查询的账号数 |
| `MAX_KEYS_PER_USER` | `10` | 单用户 Key 上限 |
| `STATUS_COOLDOWN` | `5` | `/status` 冷却（秒） |
| `MESSAGE_LIMIT` | `3800` | 单条消息最大长度，超长自动分片 |
| `ALLOWED_USER_IDS` | 空 | 白名单，逗号分隔；留空 = 所有人可用 |
| `DEMO_MODE` | `0` | `1` = 用示例数字渲染（截图/开发用，面板会标注） |
| `LOG_LEVEL` | `INFO` | 日志级别 |
| `CONFIG_FILE` | 脚本同目录 `config.json` | 配置文件路径（容器里为 `/app/data/config.json`） |

非法数值会被记录警告并回退默认值，不会让进程崩在启动阶段。

## 配置文件

单文件 JSON，按 Telegram 用户 ID 分组，首次运行自动创建：

```json
{
  "version": 1,
  "user_keys": {
    "123456789": {
      "主账号": "sk_xxxxxxxx",
      "备用账号": "sk_yyyyyyyy"
    }
  }
}
```

写入策略：

- 先写同目录临时文件 + `fsync`，再 `os.replace` 原子替换 → 进程被 kill 也不会留下半截 JSON
- 文件权限收紧到 `600`
- JSON 损坏 → 备份为 `config.json.corrupt-<时间戳>` 后重建，不会无声清空
- 路径是目录（docker 把不存在的文件挂载成目录的经典坑）→ 抛错并在聊天里明确提示，而不是假装"保存成功"

> ⚠️ 该文件保存的是**明文** API Key，已在 `.gitignore` 中排除，切勿提交或公开分享。

## 额度接口说明

ClinePass 的额度来自官方接口：

```
GET https://api.cline.bot/api/v1/users/me/plan/usage-limits
Authorization: Bearer sk_xxx
```

实测（真实 Cline Pass Key）返回：

```json
{
  "success": true,
  "data": {
    "limits": [
      { "type": "five_hour", "percentUsed": 2,  "resetsAt": "2026-09-25T14:32:27.073666206Z" },
      { "type": "weekly",    "percentUsed": 57, "resetsAt": "2026-09-30T12:08:27.075836336Z" },
      { "type": "monthly",   "percentUsed": 28, "resetsAt": "2026-10-23T12:08:27.07803017Z" }
    ]
  }
}
```

要点：

- `type` 就是 `five_hour` / `weekly` / `monthly`（**不是** `5-hour`）；`percentUsed` 是**已用**百分比；`resetsAt` 是纳秒精度的 UTC 时间
- 面板据此渲染进度条、剩余百分比、重置时间和倒计时；`percentUsed ≥ 80%` 标 ⚠️，`≥ 95%` 标 ⛔️
- 重置时间按容器时区显示（默认 `TZ=Asia/Shanghai`），因为接口给的是 UTC
- 官方将来新增窗口类型会自动多显示一行，不必改代码

面板另外会调用下面两个接口（失败不影响额度显示）：

| 接口 | 用途 |
| --- | --- |
| `GET /api/v1/users/me` | 账号邮箱 / 显示名 |
| `GET /api/v1/users/me/plan` | 套餐名、`interval`、`isActive`、计费周期 |

原始版本用的默认地址 `GET /api/v1/user/usage` 实测是 **404**，这正是它当初只能显示写死数字的原因；现在默认已改为上面的官方路径。想换数据源（自建反代等）改 `CLINEPASS_USAGE_PATH` 即可，解析器同时兼容 `limits` 列表和下面这种字典写法：

```json
{
  "success": true,
  "data": {
    "h5":    { "percent": 63, "remaining_str": "1h 52m", "reset_time": "18:32" },
    "week":  { "percent": 48 },
    "month": { "percent": 31 }
  }
}
```

- 信封层可有可无：顶层、`data`、`data.usage`、`usage` 都会被尝试
- 窗口名支持 `five_hour / 5-hour / 5h`、`weekly / week`、`monthly / month` 等别名
- 百分比字段支持 `percentUsed / percent / percent_used / used_percent / usage_percent`，数值夹到 0–100
- 解析不到额度时显示"接口已响应，但未包含可识别的额度字段"，绝不编造数字

> 同类实现可对照 [`yhshzh/dsh-cline-pass`](https://github.com/yhshzh/dsh-cline-pass)（把 `five_hour` 映射为 fiveHour，与线上一致）和 [`GooDAnDReaDY/dsh-clinebot`](https://github.com/GooDAnDReaDY/dsh-clinebot)。后者用的是 `parseWindow('5-hour')`，与线上返回的 `five_hour` 并不匹配，读它的代码时留意这一点。

想先看面板长什么样，可以设 `DEMO_MODE=1`：会渲染示例数字并明确标注 🧪。

## 安全建议

- `.env`、`config.json` 已加入 `.gitignore` 与 `.dockerignore`，**不要**为了"方便"把它们提交上去
- 用 `/id` 拿到自己的用户 ID 后填 `ALLOWED_USER_IDS`，Bot 就不会被陌生人调用
- 容器默认以非 root（uid 10001）运行、根文件系统只读、`no-new-privileges`
- 公开仓库部署前建议先 `git log -p -- config.json` 确认历史里没有误提交过 Key；一旦提交过，立刻去 Cline 后台吊销并换新

## 目录结构

```
.
├── bot.py                # Telegram 交互层：指令、鉴权、限流、错误兜底
├── core.py               # 核心逻辑：JSON 存储、API 客户端、面板渲染（无 PTB 依赖，可单测）
├── tests/test_core.py    # 43 个单元测试，纯标准库
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── .gitignore
├── .dockerignore
└── README.md
```

## 开发与测试

```bash
python3 -m unittest discover -s tests -t . -v
```

覆盖范围：进度条边界、别名校验、消息分片（含超长单行硬切）、冷却器、配置读写/上限/权限/损坏恢复/目录异常、官方额度接口解析（`five_hour`/`weekly`/`monthly`、未知类型、非法值、空列表）、纳秒时间戳解析、重置倒计时、80%/95% 告警、客户端的成功、401、404、503 重试、网络异常、非 JSON 响应、DEMO 模式、HTML 转义与长面板分片。

## 故障排查

| 现象 | 原因与处理 |
| --- | --- |
| 启动即退出，日志 `未配置 TELEGRAM_BOT_TOKEN` | 没读到 Token；本地检查 `export`，容器检查 `.env` |
| `❌ 配置存储不可用 … 是一个目录` | 宿主机上把不存在的 `config.json` 文件挂进了容器；改用挂载目录（见下） |
| `❌ 保存失败，Key 没有被记录` | 挂载目录属主不对，容器内 uid 10001 无写权限 |
| `📊 额度接口不可用：接口不存在（404）` | 默认路径已对准官方接口；若你改过 `CLINEPASS_USAGE_PATH`，检查拼写 |
| `📊 额度接口不可用：API Key 无效（401）` | 该 Key 已失效，重新 `/addkey` |
| `🔒 API Key 无效或已过期（401）` | 该 Key 无效/过期，重新 `/addkey` |
| 面板只发出了一部分 | 已按 `MESSAGE_LIMIT` 自动分片，属正常 |
| `⏳ 操作太快了` | `STATUS_COOLDOWN` 限流，稍等即可 |

**用宿主机目录保存配置**（方便直接查看 `config.json`）：

```bash
mkdir -p ./data && sudo chown 10001:10001 ./data
# 然后编辑 docker-compose.yml，把命名卷换成： - ./data:/app/data
```

**用命名卷时怎么看配置**：

```bash
docker compose exec clinepass-bot cat /app/data/config.json
docker compose cp clinepass-bot:/app/data/config.json ./config.json.bak
```

## 与旧版本的差异

- 删除了"接口失败就用环境变量里的固定数字"的兜底逻辑——那是假数据；`CLINEPASS_5H_USAGE` 等变量随之失效，需要示例数据请用 `DEMO_MODE=1`
- 额度接口从 `GET /api/v1/user/usage`（实测 404）改为官方 `GET /api/v1/users/me/plan/usage-limits`，并新增剩余百分比、重置倒计时、80%/95% 告警
- `CLINEPASS_API_URL` → 拆成 `CLINEPASS_API_BASE` + `CLINEPASS_USAGE_PATH`
- `config.json` 由 `bot.py` 同级改为可配置，容器内落进数据卷
- 新增 `core.py`、单元测试、`.env.example`、`.gitignore`、`.dockerignore`
- Markdown → HTML 渲染，别名里带 `_`、`*`、`<` 等字符不再导致发送失败

## 许可

仓库未附带 License 文件；如需开源发布，建议补充（如 MIT）。
