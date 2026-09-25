"""ClinePass TG Bot —— 核心逻辑层。

本模块不依赖 python-telegram-bot，只做三件事：
  1. JSON 配置存储（原子写入、损坏自愈、权限收紧）
  2. Cline API 客户端（重试、错误分类、宽容解析）
  3. 面板渲染与消息分片（HTML 转义、长度受限）

这样 bot.py 只负责 Telegram 交互，核心逻辑可以直接单元测试。
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

import requests

log = logging.getLogger("clinepass.core")

# 版本号（单一来源：bot 启动日志、/help、面板标题都取这里）
__version__ = "0.0.8"

#: 日志里必须抹掉的两个东西：Telegram Bot Token（藏在 httpx 的 URL 里、也藏在
#: PTB 异常消息里）和 ClinePass API Key
_SECRET_PATTERNS = (
    # 带 bot 前缀的 URL 形态，以及异常消息里裸着的 123456789:AAF... 形态
    (re.compile(r"(?:bot)?\d{5,}:[A-Za-z0-9_\-]{20,}"), "bot<TOKEN>"),
    (re.compile(r"sk_[A-Za-z0-9_\-]{8,}"), "sk_<KEY>"),
)


def redact(text: str) -> str:
    """把 Token / API Key 从任何准备写进日志的文本里抹掉。

    httpx 在 INFO 级别会打印完整 URL，而 Telegram 的 Token 就长在 URL 里
    （`https://api.telegram.org/bot<token>/getUpdates`），所以光靠"别打日志"
    不够，得在出口处兜底。
    """
    if not text:
        return text
    out = str(text)
    for pattern, replacement in _SECRET_PATTERNS:
        out = pattern.sub(replacement, out)
    return out


class RedactingFilter(logging.Filter):
    """日志出口的兜底脱敏：不管哪个库想打什么，都先过一遍 redact()。

    除了消息正文，还要处理 traceback —— PTB 抛 InvalidToken 时会把 Token
    原样写进异常消息里（`The token \\`123:AAF...\\` was rejected`）。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # 格式化失败就别动它，交给 Formatter 去报错
            message = None
        if message is not None:
            cleaned = redact(message)
            if cleaned != message:
                record.msg = cleaned
                record.args = ()
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        elif record.exc_info:
            try:
                text = logging.Formatter().formatException(record.exc_info)
            except Exception:
                return True
            cleaned = redact(text)
            if cleaned != text:
                record.exc_text = cleaned
                record.exc_info = None
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True

# ==================== 常量 ====================
DEFAULT_API_BASE = "https://api.cline.bot"
# ClinePass 官方额度接口：返回 5 小时 / 本周 / 本月三个窗口的已用百分比
DEFAULT_USAGE_PATH = "/api/v1/users/me/plan/usage-limits"
ACCOUNT_PATH = "/api/v1/users/me"
PLAN_PATH = "/api/v1/users/me/plan"

# 面板告警阈值（沿用 ClinePass 生态的约定：80% 预警，95% 视为耗尽）
WARN_PERCENT = 80.0
EXHAUSTED_PERCENT = 95.0

CONFIG_VERSION = 1
SECTION_SEP = "───────────────"
MESSAGE_LIMIT = 3800  # Telegram 上限 4096，留出安全余量

# 展示的额度窗口：(内部 key, 中文标题, API 中可能出现的字段名)
WINDOWS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "h5",
        "5 小时额度",
        (
            "five_hour",
            "five_hours",
            "5-hour",
            "5_hour",
            "5h",
            "h5",
            "last5hours",
            "last_5_hours",
            "last_5h",
        ),
    ),
    (
        "week",
        "本周额度",
        ("week", "weekend", "weekly", "7d", "seven_day", "last7days", "last_7_days"),
    ),
    (
        "month",
        "本月额度",
        ("month", "monthly", "30d", "thirty_day", "last30days", "last_30_days"),
    ),
)

PERCENT_KEYS = ("percentUsed", "percent", "percent_used", "used_percent", "usage_percent", "percentage", "used")
REMAINING_KEYS = ("remaining_str", "remaining", "remaining_time", "remaining_human", "left_str")
RESET_KEYS = ("resetsAt", "reset_time", "reset_str", "reset_at", "resets_at", "reset", "next_reset")

ALIAS_RE = re.compile(r"^[\w\u4e00-\u9fff][\w\u4e00-\u9fff .\-]{0,23}$")


# ==================== 配置 ====================
class ConfigError(RuntimeError):
    """配置存储不可用（权限、路径是目录等），必须让用户看见，不能静默吞掉。"""


#: 容器里以非 root 运行，挂载目录属主不对时给出可照抄的修复步骤
PERMISSION_HINT = (
    "容器内以非 root 运行（uid 10001，用户 app），挂载目录的属主必须交给它："
    "在宿主机执行 sudo chown -R 10001:10001 <你的数据目录>，"
    "或改用 docker-compose.yml 默认的命名卷（-v clinepass-data:/app/data）。"
)


class KeyLimitError(RuntimeError):
    """单个用户的 Key 数量超过上限。"""


def _env_str(env: Mapping[str, str], name: str, default: str = "") -> str:
    value = (env.get(name) or "").strip()
    return value or default


def _env_int(env: Mapping[str, str], name: str, default: int, low: int = 0, high: int = 10**9) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(float(raw))
    except ValueError:
        log.warning("环境变量 %s=%r 不是数字，使用默认值 %s", name, raw, default)
        return default
    return max(low, min(high, value))


def _env_float(env: Mapping[str, str], name: str, default: float, low: float = 0.0, high: float = 10**9) -> float:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        log.warning("环境变量 %s=%r 不是数字，使用默认值 %s", name, raw, default)
        return default
    return max(low, min(high, value))


def _env_bool(env: Mapping[str, str], name: str, default: bool = False) -> bool:
    raw = (env.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


def _env_ids(env: Mapping[str, str], name: str) -> frozenset[int]:
    raw = (env.get(name) or "").strip()
    if not raw:
        return frozenset()
    ids: set[int] = set()
    for chunk in re.split(r"[,\s;]+", raw):
        if not chunk:
            continue
        try:
            ids.add(int(chunk))
        except ValueError:
            log.warning("环境变量 %s 中的 %r 不是合法的用户 ID，已忽略", name, chunk)
    return frozenset(ids)


@dataclass(frozen=True)
class Settings:
    """全部运行期配置，来源为环境变量；非法值回退默认值而不是崩溃。"""

    config_file: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    api_base: str = DEFAULT_API_BASE
    usage_path: str = DEFAULT_USAGE_PATH
    request_timeout: float = 12.0
    http_retries: int = 2
    retry_backoff: float = 1.5
    max_parallel: int = 4
    max_keys_per_user: int = 10
    status_cooldown: float = 5.0
    message_limit: int = MESSAGE_LIMIT
    demo_mode: bool = False
    allowed_user_ids: frozenset[int] = frozenset()

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Settings":
        env = os.environ if env is None else env
        default_config = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
        return cls(
            config_file=_env_str(env, "CONFIG_FILE", default_config),
            api_base=_env_str(env, "CLINEPASS_API_BASE", DEFAULT_API_BASE).rstrip("/"),
            usage_path=_env_str(env, "CLINEPASS_USAGE_PATH", DEFAULT_USAGE_PATH),
            request_timeout=_env_float(env, "REQUEST_TIMEOUT", 12.0, 1.0, 120.0),
            http_retries=_env_int(env, "HTTP_RETRIES", 2, 0, 5),
            retry_backoff=_env_float(env, "RETRY_BACKOFF", 1.5, 0.0, 30.0),
            max_parallel=_env_int(env, "MAX_PARALLEL", 4, 1, 16),
            max_keys_per_user=_env_int(env, "MAX_KEYS_PER_USER", 10, 1, 100),
            status_cooldown=_env_float(env, "STATUS_COOLDOWN", 5.0, 0.0, 600.0),
            message_limit=_env_int(env, "MESSAGE_LIMIT", MESSAGE_LIMIT, 500, 4096),
            demo_mode=_env_bool(env, "DEMO_MODE", False),
            allowed_user_ids=_env_ids(env, "ALLOWED_USER_IDS"),
        )

    def is_allowed(self, user_id: int) -> bool:
        return not self.allowed_user_ids or user_id in self.allowed_user_ids


# ==================== JSON 配置存储 ====================
class ConfigStore:
    """把每个用户的 Key 存进一个 JSON 文件。

    - 首次使用自动创建文件（这样 docker 挂载目录而不是挂载文件，不会再踩坑）
    - 原子写入：先写临时文件再 os.replace，进程被杀不会留下半截 JSON
    - 文件损坏：备份为 *.corrupt-<时间戳> 后重建，而不是无声清空
    - 路径是目录 / 无权限：抛 ConfigError，让 Bot 明确报错而不是假装保存成功
    """

    def __init__(self, path: str, max_keys_per_user: int = 10):
        self.path = path
        self.max_keys_per_user = max_keys_per_user

    # ---- 基础读写 ----
    @staticmethod
    def default_data() -> dict:
        return {"version": CONFIG_VERSION, "user_keys": {}}

    def _check_path(self) -> None:
        if os.path.isdir(self.path):
            raise ConfigError(
                f"配置路径 {self.path} 是一个目录而不是文件。"
                "通常是 docker 把宿主机上不存在的文件挂载成了目录，"
                "请改为挂载目录（如 ./data:/app/data）并设置 CONFIG_FILE=/app/data/config.json。"
            )
        parent = os.path.dirname(os.path.abspath(self.path))
        if parent and not os.path.isdir(parent):
            try:
                os.makedirs(parent, exist_ok=True)
            except PermissionError as exc:
                raise ConfigError(f"无法创建配置目录 {parent}：权限不足。{PERMISSION_HINT}\n（原始错误：{exc}）") from exc
            except OSError as exc:
                raise ConfigError(f"无法创建配置目录 {parent}：{exc}") from exc

    def load(self) -> dict:
        """读取配置；文件不存在时创建默认配置。"""
        self._check_path()
        if not os.path.exists(self.path):
            data = self.default_data()
            self.save(data)
            return data
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except json.JSONDecodeError as exc:
            backup = f"{self.path}.corrupt-{int(time.time())}"
            try:
                os.replace(self.path, backup)
                log.error("配置文件损坏（%s），已备份为 %s 并重建", exc, backup)
            except OSError:
                log.error("配置文件损坏（%s）且备份失败，将重建", exc)
            data = self.default_data()
            self.save(data)
            return data
        except OSError as exc:
            raise ConfigError(f"无法读取配置 {self.path}：{exc}") from exc

        if not isinstance(data, dict):
            raise ConfigError(f"配置文件 {self.path} 结构异常（顶层不是对象）")
        data.setdefault("version", CONFIG_VERSION)
        keys = data.get("user_keys")
        if not isinstance(keys, dict):
            data["user_keys"] = {}
        elif self._clean_stored_keys(data):
            self.save(data)
        return data

    @staticmethod
    def _clean_stored_keys(data: dict) -> bool:
        """清洗老版本存下来的 Key。

        0.0.5 起才会在绑定时去掉不可见字符，之前存进去的 Key 可能夹着 U+200B 之类，
        肉眼完全正常、打开 config.json 也看不出，但服务端只会回 401。
        加载时统一过一遍，有改动就落盘。
        """
        changed = False
        for user_id, user_keys in list(data.get("user_keys", {}).items()):
            if not isinstance(user_keys, dict):
                continue
            for alias, key in list(user_keys.items()):
                if not isinstance(key, str):
                    continue
                cleaned, fixed = normalize_api_key(key)
                if fixed and cleaned:
                    log.warning(
                        "存储的 Key 含不可见字符，已自动清理：user=%s 别名=%r（长度 %d → %d）",
                        user_id,
                        alias,
                        len(key),
                        len(cleaned),
                    )
                    user_keys[alias] = cleaned
                    changed = True
        return changed

    def save(self, data: dict) -> None:
        """原子写入 + 收紧权限（Key 是敏感信息）。

        mkstemp 也必须包在 try 里：目录不可写时它抛的是 PermissionError，
        漏出去会让进程直接崩在启动阶段（见 v0.0.1 的线上报错）。
        """
        self._check_path()
        parent = os.path.dirname(os.path.abspath(self.path)) or "."
        tmp_path = ""
        try:
            fd, tmp_path = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=parent)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, self.path)
        except PermissionError as exc:
            self._discard(tmp_path)
            raise ConfigError(f"无法写入配置 {self.path}：权限不足。{PERMISSION_HINT}\n（原始错误：{exc}）") from exc
        except OSError as exc:
            self._discard(tmp_path)
            raise ConfigError(f"无法写入配置 {self.path}：{exc}") from exc

    @staticmethod
    def _discard(tmp_path: str) -> None:
        """清理写了一半的临时文件；失败也无所谓。"""
        if not tmp_path:
            return
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    def self_check(self) -> tuple[bool, str]:
        """冒烟探针：确认配置目录真的能写。

        用来把「能读但写不进去」这种最阴的故障提前暴露出来——它平时不报错，
        只在你 /addkey 时才炸，看起来就像「命令没生效」。不留下任何文件。
        """
        try:
            self._check_path()
        except ConfigError as exc:
            return False, str(exc)
        parent = os.path.dirname(os.path.abspath(self.path)) or "."
        probe = ""
        try:
            fd, probe = tempfile.mkstemp(prefix=".config-probe-", suffix=".tmp", dir=parent)
            os.close(fd)
        except OSError as exc:
            self._discard(probe)
            return False, f"目录 {parent} 不可写：{exc}"
        self._discard(probe)
        if os.path.exists(self.path):
            if not os.access(self.path, os.W_OK):
                return False, f"{self.path} 不可写"
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    json.load(fh)
            except (OSError, ValueError) as exc:
                return False, f"{self.path} 读不出来：{exc}"
        return True, f"{parent} 可写"

    # ---- 业务操作 ----
    def keys(self, user_id: int) -> dict[str, str]:
        data = self.load()
        user_keys = data["user_keys"].get(str(user_id)) or {}
        return {str(k): str(v) for k, v in user_keys.items()}

    def add(self, user_id: int, alias: str, api_key: str) -> int:
        """保存/覆盖一个别名，返回该用户当前的 Key 个数。"""
        data = self.load()
        user_keys = data["user_keys"].setdefault(str(user_id), {})
        if alias not in user_keys and len(user_keys) >= self.max_keys_per_user:
            raise KeyLimitError(
                f"每个用户最多保存 {self.max_keys_per_user} 个 Key，请先 /delkey 删除不用的。"
            )
        user_keys[alias] = api_key
        self.save(data)
        return len(user_keys)

    def delete(self, user_id: int, alias: str) -> bool:
        data = self.load()
        user_keys = data["user_keys"].get(str(user_id)) or {}
        if alias in user_keys:
            del user_keys[alias]
            if not user_keys:
                data["user_keys"].pop(str(user_id), None)
            self.save(data)
            return True
        return False

    def clear(self, user_id: int) -> int:
        data = self.load()
        removed = len(data["user_keys"].get(str(user_id)) or {})
        if removed:
            data["user_keys"].pop(str(user_id), None)
            self.save(data)
        return removed


def sanitize_alias(raw: str) -> Optional[str]:
    """校验并规范化别名；不合法返回 None。

    规则：1–24 个字符，首字符为中文/字母/数字/下划线，其余还可含空格、点、连字符。
    因此 `主账号`、`Cline-01`、`cline_01`、`Cline.01` 都合法；`#`、`/`、`:`、emoji 不合法。
    """
    alias = (raw or "").strip()
    if not alias or len(alias) > 24:
        return None
    if not ALIAS_RE.match(alias):
        return None
    return alias


#: 复制粘贴最常见的"看不见的字符"：零宽空格/连接符、词连接符、BOM
#: （普通空白如 \u00a0、\u3000 已经被 str.strip() 处理掉了）
_INVISIBLE_CHARS = "\u200b\u200c\u200d\u2060\ufeff"


def normalize_api_key(raw: str) -> tuple[str, bool]:
    """清洗 API Key，返回 (清理后的 key, 是否真的清理过)。

    Cline 的 Key 形如 `sk_` + 一串字符，**长度不设上限**（`sk_` 加 59 位很正常）。
    真正会让人抓狂的是从网页复制时夹带的零宽字符：肉眼一模一样，
    但请求头里多了一个 U+200B，服务端只会回 401。
    """
    text = (raw or "").strip()
    cleaned = text
    for ch in _INVISIBLE_CHARS:
        cleaned = cleaned.replace(ch, "")
    cleaned = "".join(c for c in cleaned if c.isprintable())
    cleaned = cleaned.strip()
    return cleaned, cleaned != text


#: 各种"像空格但不是空格"的字符：换成普通空格（全角空格最常见，来自中文输入法）
_SPACE_LIKE_CHARS = (
    "\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008"
    "\u2009\u200a\u202f\u205f\u3000"
)

#: 常被人拿来包裹命令的符号（代码块、各种引号）
_COMMAND_WRAPPERS = "`'\"“”‘’"


def normalize_command_text(text: str, invisible: str = "delete") -> str:
    """把「看起来是命令、实际却不是」的文本修回可解析的样子。

    真实踩过的坑：
    - 中文输入法打出的全角空格（U+3000）：Telegram 不认，命令实体还会把别名一起吞进去
    - 全角斜杠 `／addkey`：Telegram 压根不当命令
    - 从网页/消息复制时带进来的零宽字符（U+200B、BOM）
    - 被包成代码块或引号：`` `/addkey ...` ``

    `invisible="delete"` 把零宽字符直接删掉（`/add\u200bkey` → `/addkey`），
    `invisible="space"` 把它当分隔符（`/addkey\u200bCline01` → `/addkey Cline01`）——
    两种都有可能，所以 :func:`parse_command_candidates` 会都算一遍。
    """
    out = (text or "").strip()
    while out and out[:1] in _COMMAND_WRAPPERS:
        out = out[1:].lstrip()
    while out and out[-1:] in _COMMAND_WRAPPERS:
        out = out[:-1].rstrip()
    for ch in _SPACE_LIKE_CHARS:
        out = out.replace(ch, " ")
    for ch in _INVISIBLE_CHARS:
        out = out.replace(ch, " " if invisible == "space" else "")
    if out[:1] in ("／", "＼"):
        out = "/" + out[1:]
    return out.strip()


def parse_command_tokens(text: str, invisible: str = "delete") -> tuple[str, list[str]]:
    """从（可能被修过的）文本里取出 (命令名, 参数)；命令名不含斜杠与 @bot。"""
    tokens = normalize_command_text(text, invisible).split()
    if not tokens:
        return "", []
    head = tokens[0]
    if head[:1] in ("/", "／", "＼"):
        head = head[1:]
    return head.split("@", 1)[0].lower(), tokens[1:]


#: 可疑字符的中文说明，用于给用户一个"看得见"的解释
_SUSPICIOUS_NAMES = {
    "\u3000": "全角空格 U+3000",
    "\u00a0": "不换行空格 U+00A0",
    "\u200b": "零宽空格 U+200B",
    "\u200c": "零宽不连字 U+200C",
    "\u200d": "零宽连字 U+200D",
    "\u2060": "词连接符 U+2060",
    "\ufeff": "BOM U+FEFF",
    "／": "全角斜杠 U+FF0F",
    "＼": "全角反斜杠 U+FF3C",
}


def suspicious_chars(text: str) -> list[str]:
    """列出文本里那些"肉眼看不见但会让命令失效"的字符（去重，按出现顺序）。"""
    found: list[str] = []
    for ch in text or "":
        label = _SUSPICIOUS_NAMES.get(ch)
        if label is None and ord(ch) > 127 and not ch.isprintable():
            label = f"U+{ord(ch):04X}"
        if label and label not in found:
            found.append(label)
    return found


def parse_command_candidates(text: str) -> list[tuple[str, list[str]]]:
    """把两种零宽字符处理方式都算一遍，按可信度排序，供兜底救援逐个尝试。"""
    first = parse_command_tokens(text, "delete")
    second = parse_command_tokens(text, "space")
    return [first] if first == second else [first, second]


def split_alias_and_key(args: Sequence[str]) -> tuple[Optional[str], str]:
    """把 `/addkey` 的参数拆成 (别名, API Key)。

    Telegram 按空白切分命令参数，带空格的别名会被拆成多个 token
    （`/addkey Cline 01 sk_xxx` → `["Cline", "01", "sk_xxx"]`）。
    这里约定**最后一个 token 是 Key**，其余拼回别名，所以带空格的别名也能用。
    """
    tokens = [t for t in (args or []) if isinstance(t, str) and t.strip()]
    if len(tokens) < 2:
        return None, ""
    return sanitize_alias(" ".join(tokens[:-1])), tokens[-1].strip()


def mask_key(api_key: str, show_length: bool = False) -> str:
    """只展示首尾各 4 位；`show_length` 时附上总长度（排查 401 时最有用）。"""
    key = (api_key or "").strip()
    if len(key) <= 8:
        masked = "****"
    else:
        masked = f"{key[:4]}…{key[-4:]}"
    if show_length and key:
        masked += f" · {len(key)} 字符"
    return masked


#: 实测有效的 Cline API Key 是 67 个字符（`sk_` + 64）；短得离谱基本都是没复制全
TYPICAL_KEY_LENGTH = 67
_MIN_PLAUSIBLE_KEY_LENGTH = 50
_MAX_PLAUSIBLE_KEY_LENGTH = 80


def key_shape_note(api_key: str) -> str:
    """Key 形态明显不对时给一句提醒（Cline 的 401 不区分"截断"与"已失效"）。"""
    key = (api_key or "").strip()
    length = len(key)
    if length == 0:
        return "⚠️ 当前 Key 是空的，请重新 /addkey 绑定。"
    # Key 一定是纯 ASCII：混进中文标点/全角字符多半是多复制了东西
    odd = sorted({ch for ch in key if ord(ch) > 127})
    if odd:
        shown = "、".join(f"{ch} (U+{ord(ch):04X})" for ch in odd[:5])
        return f"⚠️ Key 里混进了非 ASCII 字符：{shown}，多半是多复制了标点或中文，请重新复制。"
    if length < _MIN_PLAUSIBLE_KEY_LENGTH:
        return (
            f"⚠️ 当前 Key 只有 {length} 个字符（Cline 的 Key 通常是 {TYPICAL_KEY_LENGTH} 个），"
            "很可能复制时漏了尾巴，请重新复制完整 Key 再 /addkey。"
        )
    if length > _MAX_PLAUSIBLE_KEY_LENGTH:
        return (
            f"⚠️ 当前 Key 有 {length} 个字符（Cline 的 Key 通常是 {TYPICAL_KEY_LENGTH} 个），"
            "可能多复制了内容，请只保留 sk_ 开头的那一串。"
        )
    return ""


# ==================== API 客户端 ====================
class ApiError(RuntimeError):
    """带分类的接口错误，方便给用户看人话。"""

    MESSAGES = {
        "unauthorized": "API Key 无效或已过期（401），请重新 /addkey 绑定",
        "forbidden": "该 Key 无权访问此接口（403）",
        "not_found": "接口不存在（404），请检查 CLINEPASS_USAGE_PATH 配置",
        "rate_limited": "请求过于频繁（429），请稍后再试",
        "server": "Cline 服务端错误（5xx），请稍后再试",
        "timeout": "请求超时，请检查网络或稍后再试",
        "network": "网络异常，无法连接 Cline API",
        "bad_response": "接口返回了非 JSON 内容",
    }

    def __init__(self, kind: str, detail: str = "", status: Optional[int] = None):
        self.kind = kind
        self.detail = detail
        self.status = status
        super().__init__(f"{kind}: {detail}" if detail else kind)

    @property
    def friendly(self) -> str:
        base = self.MESSAGES.get(self.kind, "未知接口错误")
        # 401/403 时把 Cline 自己的原话带上 —— 它比我们猜的原因准得多
        if self.kind in {"unauthorized", "forbidden"} and self.detail:
            detail = " ".join(str(self.detail).split())[:160]
            return f"{base}\nCline 返回：{detail}"
        return base


def _error_from_response(resp: Any) -> ApiError:
    status = getattr(resp, "status_code", None)
    detail = ""
    try:
        payload = resp.json()
        if isinstance(payload, dict):
            detail = str(payload.get("error") or payload.get("message") or "")
    except Exception:  # 非 JSON 响应体，忽略
        detail = ""
    if status == 401:
        kind = "unauthorized"
    elif status == 403:
        kind = "forbidden"
    elif status == 404:
        kind = "not_found"
    elif status == 429:
        kind = "rate_limited"
    elif isinstance(status, int) and status >= 500:
        kind = "server"
    else:
        kind = "bad_response"
    return ApiError(kind, detail, status)


@dataclass
class Window:
    """一个额度窗口的展示数据。"""

    label: str
    percent: Optional[float] = None
    remaining: Optional[str] = None
    reset: Optional[str] = None
    reset_dt: Optional[datetime] = None

    @property
    def usable(self) -> bool:
        return self.percent is not None or bool(self.remaining) or bool(self.reset) or self.reset_dt is not None

    @property
    def remaining_percent(self) -> Optional[float]:
        if self.percent is None:
            return None
        return max(0.0, min(100.0, 100.0 - self.percent))

    @property
    def warning(self) -> str:
        """80% 预警、95% 视为耗尽（与 ClinePass 生态的约定一致）。"""
        if self.percent is None:
            return ""
        if self.percent >= EXHAUSTED_PERCENT:
            return "⛔️"
        if self.percent >= WARN_PERCENT:
            return "⚠️"
        return ""


@dataclass
class Snapshot:
    """一个别名的完整状态。"""

    alias: str
    key_mask: str
    account: Optional[dict] = None
    plan: Optional[dict] = None
    plan_period: dict = field(default_factory=dict)
    windows: list[Window] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def has_usage(self) -> bool:
        return any(w.usable for w in self.windows)


def _first_str(source: Mapping[str, Any], names: Iterable[str]) -> Optional[str]:
    for name in names:
        value = source.get(name)
        if value is None or isinstance(value, (dict, list, bool)):
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _to_percent(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return max(0.0, min(100.0, number))


_TIMESTAMP_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:?\d{2})?$"
)


def parse_timestamp(value: Any) -> Optional[datetime]:
    """解析 resetsAt 时间戳。

    官方返回的是纳秒精度 UTC 时间，例如 2026-09-25T14:32:27.073666206Z；
    datetime.fromisoformat 无法直接吃下 9 位小数，这里先规范化到微秒。
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str):
        return None
    match = _TIMESTAMP_RE.match(value.strip())
    if match is None:
        return None
    date, clock, fraction, zone = match.groups()
    micro = ((fraction or "")[:6]).ljust(6, "0")
    if zone in (None, "Z"):
        offset = "+00:00"
    elif ":" in zone:
        offset = zone
    else:
        offset = f"{zone[:3]}:{zone[3:]}"
    try:
        return datetime.fromisoformat(f"{date}T{clock}.{micro}{offset}")
    except ValueError:
        return None


def humanize_delta(seconds: float) -> str:
    """把剩余秒数说成人话。"""
    if seconds <= 0:
        return "已到重置时间"
    minutes = int(seconds // 60)
    if minutes < 1:
        return "不到 1 分钟"
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    if days:
        return f"{days} 天 {hours} 小时" if hours else f"{days} 天"
    if hours:
        return f"{hours} 小时 {mins} 分" if mins else f"{hours} 小时"
    return f"{mins} 分钟"


def describe_reset(window: Window, now: Optional[datetime] = None) -> Optional[str]:
    """重置时间：有精确时间戳就显示本地时间 + 倒计时，否则退回接口给的字符串。"""
    if window.reset_dt is not None:
        reference = now or datetime.now(timezone.utc)
        seconds = (window.reset_dt - reference).total_seconds()
        human = humanize_delta(seconds)
        prefix = "" if human.startswith("已到") else "还有 "
        return f"{window.reset_dt.astimezone().strftime('%m-%d %H:%M')}（{prefix}{human}）"
    return window.reset


def _window_key_for_type(raw_type: str) -> Optional[tuple[str, str]]:
    """把接口的 type（five_hour / weekly / monthly）映射到展示窗口。"""
    needle = raw_type.strip().lower().replace("-", "_").replace(" ", "_")
    if not needle:
        return None
    for key, label, aliases in WINDOWS:
        if needle in {alias.lower().replace("-", "_") for alias in aliases}:
            return key, label
    return None


def parse_limits_list(payload: Any) -> Optional[list[Window]]:
    """解析官方额度接口：`{success, data:{limits:[{type, percentUsed, resetsAt}]}}`。

    type 为 five_hour / weekly / monthly。未知 type 也会保留成一行，
    这样官方将来新增窗口时面板会直接多一行，而不必改代码。
    """
    if not isinstance(payload, Mapping):
        return None
    roots: list[Mapping[str, Any]] = [payload]
    nested = payload.get("data")
    if isinstance(nested, Mapping):
        roots.append(nested)

    limits: Optional[list[Any]] = None
    for root in roots:
        candidate = root.get("limits")
        if isinstance(candidate, list):
            limits = candidate
            break
    if limits is None:
        return None

    known: dict[str, Window] = {}
    extras: list[Window] = []
    for item in limits:
        if not isinstance(item, Mapping):
            continue
        raw_type = str(item.get("type") or "").strip()
        matched = _window_key_for_type(raw_type)
        window = Window(
            label=matched[1] if matched else raw_type,
            percent=_to_percent(item.get("percentUsed")),
            reset_dt=parse_timestamp(item.get("resetsAt")),
        )
        if not window.usable:
            continue
        if matched:
            known[matched[0]] = window
        elif raw_type:
            extras.append(window)

    ordered = [known[key] for key, _, _ in WINDOWS if key in known]
    return (ordered + extras) or None


def _parse_window(raw: Any, label: str) -> Optional[Window]:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return Window(label=label, percent=_to_percent(raw))
    if isinstance(raw, str):
        return Window(label=label, remaining=raw.strip() or None)
    if not isinstance(raw, dict):
        return None
    percent = next((p for p in (_to_percent(raw.get(k)) for k in PERCENT_KEYS) if p is not None), None)
    window = Window(
        label=label,
        percent=percent,
        remaining=_first_str(raw, REMAINING_KEYS),
        reset=_first_str(raw, RESET_KEYS),
        reset_dt=parse_timestamp(raw.get("resetsAt") or raw.get("resets_at")),
    )
    return window if window.usable else None


def parse_usage(payload: Any) -> Optional[list[Window]]:
    """宽容解析额度数据。

    真实接口是 {"success": true, "data": {"limits": [...]}}，但社区里还流传着
    data.h5/week/month 这类写法，两种都支持：先按官方 limits 列表解析，再退回字典形状。
    解析不到就返回 None，由上层显示“未提供”，绝不编造数字。
    """
    if not isinstance(payload, dict):
        return None

    limits = parse_limits_list(payload)
    if limits:
        return limits

    roots: list[Mapping[str, Any]] = [payload]
    for container in (payload, payload.get("data") if isinstance(payload.get("data"), dict) else None):
        if isinstance(container, dict):
            for sub in ("usage", "data", "limits", "quota", "windows"):
                nested = container.get(sub)
                if isinstance(nested, dict):
                    roots.append(nested)

    for root in roots:
        windows: list[Window] = []
        for _, label, aliases in WINDOWS:
            raw = next((root[a] for a in aliases if a in root), None)
            if raw is None:
                continue
            window = _parse_window(raw, label)
            if window:
                windows.append(window)
        if windows:
            ordered = {w.label: w for w in windows}
            return [ordered[label] for _, label, _ in WINDOWS if label in ordered]
    return None


class ClinePassClient:
    """Cline API 客户端；同步实现 + 异步包装，避免阻塞 Telegram 事件循环。

    并发查询运行在线程池里，因此不能共用一个 requests.Session（它不是线程安全的），
    这里为每个线程各建一个 Session；测试可以注入假 session。
    """

    USER_AGENT = "ClinePass-TG-Bot/2.0"

    def __init__(self, settings: Settings, session: Optional[Any] = None):
        self.settings = settings
        self._injected = session
        self._local = threading.local()

    def _session(self) -> Any:
        if self._injected is not None:
            return self._injected
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update({"User-Agent": self.USER_AGENT})
            self._local.session = session
        return session

    def _url(self, path: str) -> str:
        return f"{self.settings.api_base}{path}"

    def get_json(self, path: str, api_key: str) -> dict:
        """带重试的 GET；4xx 直接失败，429/5xx/网络错误重试。"""
        attempts = self.settings.http_retries + 1
        last_error: Optional[ApiError] = None
        for attempt in range(attempts):
            try:
                resp = self._session().get(
                    self._url(path),
                    headers={"Authorization": f"Bearer {api_key}"},
                    timeout=self.settings.request_timeout,
                )
            except requests.Timeout:
                last_error = ApiError("timeout", f"GET {path}")
            except requests.RequestException as exc:
                last_error = ApiError("network", str(exc))
            else:
                status = getattr(resp, "status_code", 0)
                if status == 200:
                    try:
                        payload = resp.json()
                    except ValueError as exc:
                        raise ApiError("bad_response", f"GET {path}: {exc}") from exc
                    if not isinstance(payload, dict):
                        raise ApiError("bad_response", f"GET {path}: 顶层不是对象")
                    return payload
                error = _error_from_response(resp)
                if error.kind in {"rate_limited", "server"}:
                    last_error = error
                else:
                    raise error

            if attempt < attempts - 1:
                delay = self.settings.retry_backoff * (2**attempt)
                if delay:
                    time.sleep(delay)

        raise last_error or ApiError("network", f"GET {path}")

    # ---- 解析真实接口 ----
    @staticmethod
    def _unwrap(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        data = payload.get("data")
        return data if isinstance(data, dict) else payload

    def fetch_snapshot_sync(self, alias: str, api_key: str) -> Snapshot:
        snapshot = Snapshot(alias=alias, key_mask=mask_key(api_key, show_length=True))

        try:
            snapshot.account = dict(self._unwrap(self.get_json(ACCOUNT_PATH, api_key)))
        except ApiError as exc:
            if exc.kind in {"unauthorized", "forbidden"}:
                snapshot.warnings.append(f"🔒 {exc.friendly}")
                # 401 是最常见的求助场景：顺手把"Key 是不是没复制全"也说了
                note = key_shape_note(api_key)
                if note:
                    snapshot.warnings.append(note)
                return snapshot
            snapshot.warnings.append(f"👤 账号信息获取失败：{exc.friendly}")

        try:
            plan_data = self._unwrap(self.get_json(PLAN_PATH, api_key))
            plan = plan_data.get("plan") if isinstance(plan_data.get("plan"), dict) else plan_data
            snapshot.plan = dict(plan) if isinstance(plan, dict) else None
            snapshot.plan_period = {
                "start": plan_data.get("currentPeriodStart"),
                "end": plan_data.get("currentPeriodEnd"),
            }
        except ApiError as exc:
            snapshot.warnings.append(f"💳 套餐信息获取失败：{exc.friendly}")

        if self.settings.demo_mode:
            snapshot.windows = [
                Window("5 小时额度", 63.0, "1h 52m", "18:32"),
                Window("本周额度", 48.0, None, "周一 08:00"),
                Window("本月额度", 31.0, None, "10月1日"),
            ]
            snapshot.warnings.append("🧪 DEMO_MODE 已开启，额度为示例数据")
            return snapshot

        try:
            payload = self.get_json(self.settings.usage_path, api_key)
        except ApiError as exc:
            snapshot.warnings.append(f"📊 额度接口不可用：{exc.friendly}")
        else:
            windows = parse_usage(payload)
            if windows:
                snapshot.windows = windows
            else:
                snapshot.warnings.append("📊 额度接口已响应，但未包含可识别的额度字段")

        return snapshot

    async def fetch_snapshot(self, alias: str, api_key: str) -> Snapshot:
        return await asyncio.to_thread(self.fetch_snapshot_sync, alias, api_key)

    async def fetch_all(self, items: Sequence[tuple[str, str]]) -> list[Snapshot]:
        semaphore = asyncio.Semaphore(self.settings.max_parallel)

        async def one(alias: str, api_key: str) -> Snapshot:
            async with semaphore:
                return await self.fetch_snapshot(alias, api_key)

        return list(await asyncio.gather(*(one(a, k) for a, k in items)))


# ==================== 渲染 ====================
def progress_bar(percent: float, length: int = 10) -> str:
    """生成进度条；percent 会被夹到 0~100。"""
    value = max(0.0, min(100.0, float(percent)))
    filled = int(round(length * value / 100))
    return "█" * filled + "░" * (length - filled)


def esc(value: Any) -> str:
    """HTML 转义（bot.py 复用，避免用户输入破坏消息实体）。"""
    return html.escape(str(value), quote=False)


_esc = esc


def _account_lines(account: Optional[Mapping[str, Any]]) -> list[str]:
    if not account:
        return []
    name = account.get("displayName") or account.get("name")
    email = account.get("email")
    parts = [p for p in (email, name) if p]
    if not parts:
        return []
    return [f"👤 {' · '.join(_esc(p) for p in parts)}"]


def _plan_lines(plan: Optional[Mapping[str, Any]], period: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    if plan:
        title = plan.get("displayName") or plan.get("name")
        interval = plan.get("interval")
        active = plan.get("isActive")
        if title:
            tail = []
            if interval:
                tail.append(_esc(interval))
            if active is not None:
                tail.append("✅ 生效" if active else "⛔️ 已失效")
            suffix = f"（{' · '.join(tail)}）" if tail else ""
            lines.append(f"💳 {_esc(title)}{suffix}")
    start, end = period.get("start") if period else None, period.get("end") if period else None
    if start and end:
        lines.append(f"📆 计费周期：{_esc(str(start)[:10])} → {_esc(str(end)[:10])}")
    return lines


def render_snapshot(snapshot: Snapshot, now: Optional[datetime] = None) -> str:
    """把单个别名渲染成一段 HTML 消息。"""
    lines = [f"🔑 <b>账号/别名：{_esc(snapshot.alias)}</b>  <code>{_esc(snapshot.key_mask)}</code>"]
    lines.extend(_account_lines(snapshot.account))
    lines.extend(_plan_lines(snapshot.plan, snapshot.plan_period))

    if snapshot.windows:
        for window in snapshot.windows:
            badge = f" {window.warning}" if window.warning else ""
            block = [f"📊 <b>{_esc(window.label)}</b>（已用）{badge}".rstrip()]
            if window.percent is not None:
                tail = f"{round(window.percent)}%"
                if window.remaining_percent is not None:
                    tail += f" · 剩余 {round(window.remaining_percent)}%"
                block.append(f"<code>{progress_bar(window.percent)}</code> {tail}")
            details = []
            if window.remaining:
                details.append(f"剩余：{_esc(window.remaining)}")
            reset = describe_reset(window, now)
            if reset:
                details.append(f"重置：{_esc(reset)}")
            if details:
                block.append("  ".join(details))
            lines.append("\n".join(block))
    else:
        lines.append("📊 <b>额度</b>：暂无可显示的额度数据")

    for warning in snapshot.warnings:
        lines.append(_esc(warning))

    return "\n".join(lines)


def render_panel(snapshots: Sequence[Snapshot], now: Optional[datetime] = None) -> str:
    """渲染整块面板（不含分片）。"""
    moment = now or datetime.now(timezone.utc)
    stamp = moment.astimezone().strftime("%H:%M:%S")
    sections = [f"🤖 <b>ClinePass Status Panel</b> · v{__version__}"]
    sections.extend(render_snapshot(s, moment) for s in snapshots)
    sections.append(f"🔄 <b>更新时间</b> {_esc(stamp)}")
    return f"\n\n{SECTION_SEP}\n\n".join(sections)


def split_message(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """按行切分长消息，尽量在空行处断开，保证每片不超过 limit。"""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.split("\n"):
        piece = len(line) + 1
        if size + piece > limit and current:
            chunks.append("\n".join(current).rstrip())
            current, size = [], 0
        if piece > limit:  # 单行就超长，硬切
            if current:
                chunks.append("\n".join(current).rstrip())
                current, size = [], 0
            for i in range(0, len(line), limit):
                chunks.append(line[i : i + limit])
            continue
        current.append(line)
        size += piece
    if current:
        chunks.append("\n".join(current).rstrip())
    return [c for c in chunks if c.strip()]


class Cooldown:
    """按 key（用户 ID）的简单节流器，防止刷接口。"""

    def __init__(self, seconds: float):
        self.seconds = max(0.0, float(seconds))
        self._last: dict[Any, float] = {}

    def hit(self, key: Any, now: Optional[float] = None) -> float:
        """返回还需等待的秒数；未命中则记录本次调用并返回 0。"""
        if self.seconds <= 0:
            return 0.0
        moment = time.monotonic() if now is None else now
        previous = self._last.get(key)
        if previous is not None and moment - previous < self.seconds:
            return self.seconds - (moment - previous)
        self._last[key] = moment
        return 0.0
