# 🤖 ClinePass TG Bot

> 当前版本 **0.0.7**（代码里的 `core.__version__` 是唯一来源，标签发布见「持续集成与发布」）

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

**别名随便起**，不必是中文，`Cline-01`、`cline_01`、`Cline.01`、`Codex 备用` 都可以：

| 规则 | 说明 |
| --- | --- |
| 长度 | 1–24 个字符 |
| 首字符 | 中文、字母、数字或下划线（不能以空格、`.`、`-` 开头） |
| 其余字符 | 中文、字母、数字、下划线、空格、`.`、`-` |
| 不支持 | `#`、`/`、`:`、`@`、括号、emoji 等 |

> 别名里可以带空格：Bot 把**最后一个参数当 Key**，前面的都算别名，
> 所以 `/addkey Codex 备用 sk_xxx` 存下来的别名就是 `Codex 备用`。
>
> **Key 没有长度上限**：`sk_` + 59 位、上百位都正常，只要求 ≥ 8 个字符且不含空格。
> 从网页复制时容易夹带零宽字符（U+200B 之类）——肉眼一样但服务端只会回 401，
> Bot 会自动清理，并在回复里加一句「已自动去掉不可见的字符」。
>
> **命令写歪了也能用**：全角斜杠 `／addkey`、中文输入法打出的全角空格、
> 复制带进来的零宽字符、被 ```或引号包住的命令，Bot 都会先修好再执行（0.0.6+）；
> 实在救不回来时，回复里会明确指出是哪个字符在捣乱。

## 指令一览

| 指令 | 说明 |
| --- | --- |
| `/start` | 欢迎语与上手引导 |
| `/help` | 指令帮助 |
| `/status`（别名 `/quota`） | 查询所有已绑定 Key 的额度面板 |
| `/addkey <别名> <API_KEY>` | 添加或更新 Key，如 `/addkey Cline-01 sk_xxx`（仅私聊） |
| `/delkey <别名>` | 删除指定 Key（仅私聊） |
| `/keys` | 列出已绑定的别名与掩码（仅私聊） |
| `/clear confirm` | 清空自己的全部 Key（仅私聊） |
| `/id` | 诊断信息：用户 ID、会话、版本、容器名、配置文件、存储是否可写、自己绑了几个 Key |

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
├── .github/workflows/ci.yml  # CI：单测矩阵 → 构建镜像+冒烟 → 打标签时推 GHCR
├── bot.py                # Telegram 交互层：指令、鉴权、限流、错误兜底
├── core.py               # 核心逻辑：JSON 存储、API 客户端、面板渲染（无 PTB 依赖，可单测）
├── tests/test_core.py    # 97 个单元测试，纯标准库
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

覆盖范围：进度条边界、别名规则与 `/addkey` 参数拆分（多词别名、最后一段才是 Key）、存储自检探针、消息分片（含超长单行硬切）、冷却器、配置读写/上限/权限/损坏恢复/目录异常/写入失败转 ConfigError、官方额度接口解析（`five_hour`/`weekly`/`monthly`、未知类型、非法值、空列表）、纳秒时间戳解析、重置倒计时、80%/95% 告警、版本号、客户端的成功、401、404、503 重试、网络异常、非 JSON 响应、DEMO 模式、HTML 转义与长面板分片。

## 持续集成与发布

`.github/workflows/ci.yml` 在 push / PR / 手动触发时跑三个 job：

| job | 内容 |
| --- | --- |
| `test` | Python 3.10 / 3.11 / 3.12 矩阵：装依赖 → `compileall` 编译检查 → 97 个单元测试 |
| `docker` | buildx 构建镜像（带 gha 缓存）→ 镜像内自检：能导入、非 root(10001)、命名卷可写配置、缺 Token 时退出码为 1 |
| `publish` | 仅在 `main` 分支或 `v*` 标签上触发，推送到 GHCR（`ghcr.io/mbaigc/clinepass-tg-bot`） |

发布一个新版本：

```bash
# 1) 改 core.py 里的 __version__（例如 0.0.5），提交并推送
vim core.py && git commit -am "release: 0.0.5" && git push

# 2) 打标签并推送，CI 会自动构建并推送镜像
git tag v0.0.5 && git push origin v0.0.5
```

镜像标签规则：`v0.0.5` → `0.0.5`、`0.0`；`main` 分支 → `main` 与 `latest`。镜像公开后可以直接部署：

```bash
docker run -d --name clinepass_tg_bot --restart unless-stopped \
  -e TELEGRAM_BOT_TOKEN=xxx \
  -v clinepass-data:/app/data \
  ghcr.io/mbaigc/clinepass-tg-bot:0.0.5
```

> 上面用的是**命名卷**，Docker 会把镜像里 `/app/data` 的属主（uid 10001）带过来，开箱可写。
> 如果你改成挂载宿主机目录（`-v ./data:/app/data`），必须先把目录交给 uid 10001，否则会报权限错误——见「故障排查」。

## 日志与脱敏

| 内容 | 说明 |
| --- | --- |
| 记录什么 | 每次 `/addkey`、`/delkey`、`/clear` 的别名与结果；每条收到的更新（**只记命令名**，不记正文） |
| 不记什么 | API Key 全文、Telegram Token 全文；超过 16 字符的参数一律显示为 `<N字符>` |
| 兜底脱敏 | 日志出口的过滤器会把 `bot<TOKEN>`、`sk_<KEY>` 替换掉，异常 traceback 和 `sys.excepthook` 也走同一条路 |
| 噪音 | `httpx`（每个 getUpdates 一行）默认降到 WARNING，`LOG_LEVEL=DEBUG` 才打开 |

排查「发了指令没有任何反馈」时，第一件事是看日志里有没有这一行：

```
收到更新：update_id=123 消息=Message 指令=/addkey chat=42(private) user=42
```

- **没有这行** → 更新压根没到 Bot：多半是斜杠打成了全角 `／`（Telegram 不当命令）、在群里且 Bot 开了隐私模式、或者有另一个实例/webhook 抢走了 updates。
- **有这行但没有回复** → 往下看同一条 update_id 附近的 WARNING/ERROR，0.0.5 起每条失败路径都会留下原因。

## 故障排查

| 现象 | 原因与处理 |
| --- | --- |
| 启动即退出，日志 `未配置 TELEGRAM_BOT_TOKEN` | 没读到 Token；本地检查 `export`，容器检查 `.env` |
| `PermissionError: … /app/data/.config-*.tmp`（0.0.1 会直接崩） | 挂载目录属主不是 uid 10001，见下方「挂载与权限」 |
| `❌ 配置存储不可用 … 权限不足`（0.0.2 起的不崩版本） | 同上，日志里的提示就是修复命令 |
| `❌ 配置存储不可用 … 是一个目录` | 宿主机上把不存在的 `config.json` **文件**挂进了容器；改成挂载目录 |
| `/addkey` 后 `/status` 仍说没有 Key | 先发 `/id`（0.0.4+）看存储是否可写；多半是「文件可读、目录不可写」，见下方排查 |
| 发指令完全没反应 | 先看日志有没有 `收到更新：…`；没有就是更新没到 Bot（群组隐私模式、另一个实例抢 updates） |
| 明明发的是 `/addkey` 却回「没识别出这个指令」 | 命令里混了全角空格 / 零宽字符，或被包进了代码块或引号。0.0.6 起会自动纠正后照常执行（日志里有一行「兜底识别出指令」），救不回来时会明确指出是哪个字符 |
| 日志里出现 `bot<TOKEN>` / `sk_<KEY>` | 这是脱敏后的样子，属正常；真 Token 不会进日志 |
| `📊 额度接口不可用：接口不存在（404）` | 默认路径已对准官方接口；若你改过 `CLINEPASS_USAGE_PATH`，检查拼写 |
| `🔒 API Key 无效或已过期（401）` | Cline 拒绝了这把 Key。面板会附上 Cline 的原话和 Key 长度（0.0.7+）：**长度明显短于 67 位就是没复制全**，重新复制后 `/addkey`；长度正常则是 Key 已失效/被撤销，去 Cline 重新生成 |
| 想自己确认 Key 到底行不行 | `curl -sS -i https://api.cline.bot/api/v1/users/me/plan/usage-limits -H "Authorization: Bearer sk_你的Key"`，有效时是 `HTTP/2 200` + `{"data":{"limits":[…]}}`，无效时是 401 + `Unauthorized: …re-authenticate your Cline account` |
| 面板只发出了一部分 | 已按 `MESSAGE_LIMIT` 自动分片，属正常 |
| `⏳ 操作太快了` | `STATUS_COOLDOWN` 限流，稍等即可 |

### 挂载与权限

容器**以非 root（uid 10001，用户 `app`）运行**，所以挂载进去的目录必须由它拥有。两种情况：

**1）挂载宿主机目录（`-v ./data:/app/data`）**

```bash
mkdir -p ./data
sudo chown -R 10001:10001 ./data      # 关键一步
docker compose up -d
```

**2）命名卷被更早的 root 版容器写过**

如果你之前用过旧版（以 root 运行）并复用了同一个卷，卷里的文件属主是 root，同样写不进去。用镜像自带的 `chown` 修一下即可：

```bash
docker compose down
docker run --rm --user root -v clinepass-data:/app/data \
  ghcr.io/mbaigc/clinepass-tg-bot:0.0.5 chown -R 10001:10001 /app/data
docker compose up -d
```

（卷名以 `docker volume ls` 看到的为准，compose 项目通常会加 `项目名_` 前缀。）

**3）确认结果**

```bash
docker compose exec clinepass-bot ls -ln /app/data     # 应为 10001 10001
docker compose exec clinepass-bot cat /app/data/config.json
docker compose cp clinepass-bot:/app/data/config.json ./config.json.bak   # 备份出来
```

### 排查：`/addkey` 好像没生效、`/status` 又说没有 Key

这种情况几乎都是**「配置文件能读、目录不能写」**：读出来当然是空的，而写入被内核拒绝了。
0.0.4 起直接发 `/id` 就能看到答案（版本、容器名、配置文件、存储是否可写、已绑定几个）：

```
💾 存储：❌ 不可写（目录 /app/data 不可写：[Errno 13] Permission denied）
```

升级前也可以手工四连：

```bash
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}'    # ① 有没有两个容器在抢同一个 Token
docker compose exec clinepass-bot ls -ln /app/data          # ② 属主不是 10001 → 权限问题
docker compose exec clinepass-bot cat /app/data/config.json # ③ 里面没有你的用户 ID → 写没落地
docker compose logs --tail 100 | grep -iE "addkey|permission|traceback|conflict"
```

- ② 的属主不对 → 按上面「挂载与权限」`chown` 一下即可。
- ① 出现两个同名 bot 容器 → 它们在轮流抢 Telegram 的 updates，`/addkey` 与 `/status` 可能落在不同实例，自然写到不同卷。
- 日志里能看到每次 `/addkey` 收到的参数个数、别名与保存结果（API Key 永远不会写进日志）。

## 与旧版本的差异

- 删除了"接口失败就用环境变量里的固定数字"的兜底逻辑——那是假数据；`CLINEPASS_5H_USAGE` 等变量随之失效，需要示例数据请用 `DEMO_MODE=1`
- 额度接口从 `GET /api/v1/user/usage`（实测 404）改为官方 `GET /api/v1/users/me/plan/usage-limits`，并新增剩余百分比、重置倒计时、80%/95% 告警
- `CLINEPASS_API_URL` → 拆成 `CLINEPASS_API_BASE` + `CLINEPASS_USAGE_PATH`
- `config.json` 由 `bot.py` 同级改为可配置，容器内落进数据卷
- 新增 `core.py`、单元测试、`.env.example`、`.gitignore`、`.dockerignore`
- Markdown → HTML 渲染，别名里带 `_`、`*`、`<` 等字符不再导致发送失败

## 版本历史

| 版本 | 说明 |
| --- | --- |
| **0.0.7** | 401 诊断升级：面板带上 Cline 的原始报错（`Unauthorized: …re-authenticate your Cline account`）和 Key 长度（`sk_0…2df9 · 22 字符`）；短于 50 位时直接点出「很可能没复制完整」（实测有效 Key 为 67 位），`/addkey` 绑定当场就提醒；`/keys` 也显示每把 Key 的长度 |
| **0.0.6** | 命令兜底救援：Telegram 只把「标准命令」交给我们，全角斜杠 `／addkey`、全角空格、零宽字符、被代码块包住的命令都会漏掉。现在这些文本会被自动修好并交给对应处理器执行；救不回来时回复里点名「全角空格 U+3000」这类元凶 |
| **0.0.5** | 日志改造：所有输出走兜底脱敏（Token → `bot<TOKEN>`、Key → `sk_<KEY>`，连 traceback 与 `sys.excepthook` 都覆盖），httpx 噪音降到 WARNING；新增 group -1 的「收到更新」日志（只记命令名，不记正文）；全角斜杠/打错指令名会明确回一句而不是沉默；API Key 自动清理零宽字符 |
| **0.0.4** | 加诊断：`/id` 显示版本/容器名/配置文件/存储可写性/已绑定数量；启动时做写入探针；`/addkey`、`/delkey`、`/clear` 每次都有 INFO 日志（Key 绝不入日志）；未捕获异常会回一条带异常名的提示 |
| **0.0.3** | `/addkey` 约定「最后一个参数是 Key，其余拼成别名」，带空格的别名（`Codex 备用`）不再是坑；别名规则与报错文案写清楚（`Cline-01` 一直合法） |
| **0.0.2** | 修复：挂载目录不可写时 `tempfile.mkstemp` 抛出的 `PermissionError` 会漏出，导致进程崩在启动阶段；现在统一转成带修复指引的 `ConfigError`，Bot 降级运行并在日志/聊天里说明原因 |
| 0.0.1 | 首个版本：官方额度接口、多 Key 面板、Docker 镜像与 GHCR 发布 |

## 许可

仓库未附带 License 文件；如需开源发布，建议补充（如 MIT）。
