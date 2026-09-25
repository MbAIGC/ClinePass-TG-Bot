"""ClinePass TG Bot —— Telegram 交互层。

用法：
    export TELEGRAM_BOT_TOKEN=123456:ABC...
    python bot.py

核心逻辑（配置存储、API 客户端、渲染）见 core.py。
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import sys

from telegram import BotCommand, Update
from telegram.constants import ChatType, ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes

from core import (
    ConfigError,
    ConfigStore,
    Cooldown,
    ClinePassClient,
    KeyLimitError,
    Settings,
    __version__,
    esc,
    mask_key,
    render_panel,
    split_alias_and_key,
    split_message,
)

log = logging.getLogger("clinepass.bot")

HELP_TEXT = (
    f"🤖 <b>ClinePass TG Bot</b> · v{__version__}\n\n"
    "🔹 /status — 查看所有已绑定 Key 的额度面板\n"
    "🔹 /addkey &lt;别名&gt; &lt;API_KEY&gt; — 添加或更新指定别名的 Key\n"
    "🔹 /delkey &lt;别名&gt; — 删除指定的 Key\n"
    "🔹 /keys — 列出已绑定的 Key 别名（只显示掩码）\n"
    "🔹 /clear confirm — 清空你绑定的全部 Key\n"
    "🔹 /id — 查看你的 Telegram 用户 ID\n"
    "🔹 /help — 显示本帮助\n\n"
    "🔒 涉及 Key 的指令仅在私聊生效；包含 Key 的消息会被自动撤回。"
)


def setup_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )


class BotContext:
    """把设置、存储、客户端、节流器打包，避免到处用全局变量。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = ConfigStore(settings.config_file, settings.max_keys_per_user)
        self.client = ClinePassClient(settings)
        self.cooldown = Cooldown(settings.status_cooldown)
        self.locks: dict[int, asyncio.Lock] = {}

    def lock_for(self, user_id: int) -> asyncio.Lock:
        return self.locks.setdefault(user_id, asyncio.Lock())


def ctx_of(context: ContextTypes.DEFAULT_TYPE) -> BotContext:
    return context.application.bot_data["ctx"]


def _safe_args(args: object) -> str:
    """日志里描述命令参数，但绝不把 API Key 写进去。"""
    tokens = [str(a) for a in (args or [])]  # type: ignore[union-attr]
    shown = [
        tok if len(tok) <= 16 and not tok.lower().startswith(("sk_", "sk-")) else f"<{len(tok)}字符>"
        for tok in tokens
    ]
    return f"共 {len(tokens)} 个 {shown}"


# ==================== 通用守卫 ====================
async def _guard(update: Update, context: ContextTypes.DEFAULT_TYPE, private: bool = True) -> bool:
    """统一鉴权：白名单 + 私聊限制。返回 True 表示可以继续处理。"""
    ctx = ctx_of(context)
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return False

    if not ctx.settings.is_allowed(user.id):
        await message.reply_text(
            f"⛔️ 你不在白名单中，无法使用本 Bot。\n你的用户 ID：<code>{user.id}</code>",
            parse_mode=ParseMode.HTML,
        )
        return False

    chat = update.effective_chat
    if private and (chat is None or chat.type != ChatType.PRIVATE):
        await message.reply_text("🔒 该指令涉及你的 API Key，请在私聊中使用。")
        return False
    return True


async def _load_keys(ctx: BotContext, user_id: int, message) -> dict[str, str] | None:
    """读取用户 Key；存储不可用时明确报错（而不是假装成功）。"""
    try:
        return ctx.store.keys(user_id)
    except ConfigError as exc:
        log.error("读取配置失败：%s", exc)
        await message.reply_text(
            "❌ 配置存储不可用，操作已取消。\n"
            f"原因：<code>{str(exc)[:300]}</code>\n"
            "请检查 CONFIG_FILE 路径与挂载权限。",
            parse_mode=ParseMode.HTML,
        )
        return None


# ==================== 指令处理 ====================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context, private=False):
        return
    user = update.effective_user
    assert user is not None
    await update.effective_message.reply_text(  # type: ignore[union-attr]
        f"👋 你好，<b>{user.first_name or '朋友'}</b>！\n\n"
        "我可以帮你把 Cline / ClinePass 账号的额度做成面板。\n"
        "先私聊发送 <code>/addkey 主账号 sk_xxxx</code> 绑定一个 Key，"
        "再用 <code>/status</code> 查看额度。\n\n"
        "输入 <code>/help</code> 查看全部指令。\n\n"
        f"<code>ClinePass TG Bot v{__version__}</code>",
        parse_mode=ParseMode.HTML,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context, private=False):
        return
    await update.effective_message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)  # type: ignore[union-attr]


async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """不只是给 ID，还顺手把「我到底在哪个容器、写哪个文件」讲清楚。

    排查「/addkey 说保存了但 /status 看不到」时，最需要的就是这几行：
    容器名（有没有两个实例在抢同一个 Token）和配置文件路径。
    """
    if not await _guard(update, context, private=False):
        return
    ctx = ctx_of(context)
    user, chat = update.effective_user, update.effective_chat
    lines = [f"🆔 用户 ID：<code>{user.id}</code>" if user else "🆔 用户 ID：未知"]
    if chat:
        lines.append(f"💬 会话 ID：<code>{chat.id}</code>（{chat.type}）")
    lines.append(f"🤖 版本：<code>v{__version__}</code>　🏠 容器：<code>{esc(socket.gethostname())}</code>")
    lines.append(f"🗂 配置文件：<code>{esc(ctx.settings.config_file)}</code>")

    ok, detail = ctx.store.self_check()
    lines.append(f"💾 存储：{'✅ 可读写' if ok else '❌ 不可写'}（{esc(detail[:200])}）")

    if user:
        try:
            keys = ctx.store.keys(user.id)
        except ConfigError as exc:
            lines.append(f"🔑 已绑定：读取失败 <code>{esc(str(exc)[:160])}</code>")
        else:
            if keys:
                names = "、".join(esc(a) for a in keys)
                lines.append(f"🔑 已绑定：{len(keys)} 个（{names}）")
            else:
                lines.append("🔑 已绑定：0 个")
    lines.append("")
    lines.append("把用户 ID 填进 <code>ALLOWED_USER_IDS</code> 即可启用白名单。")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)  # type: ignore[union-attr]


async def keys_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    ctx = ctx_of(context)
    user_keys = await _load_keys(ctx, update.effective_user.id, update.effective_message)  # type: ignore[union-attr]
    if user_keys is None:
        return
    if not user_keys:
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            "⚠️ 你还没有绑定任何 Key。\n用法：<code>/addkey 主账号 sk_xxxx</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    lines = ["📋 <b>已绑定的 Key</b>", ""]
    lines.extend(f"• <b>{esc(alias)}</b>：<code>{esc(mask_key(key))}</code>" for alias, key in user_keys.items())
    lines.append("")
    lines.append(f"共 {len(user_keys)} 个")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)  # type: ignore[union-attr]


async def addkey_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    ctx = ctx_of(context)
    message = update.effective_message
    user_id = update.effective_user.id  # type: ignore[union-attr]
    chat_id = update.effective_chat.id

    async def say(text: str) -> None:
        await context.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)

    # 先撤回这条含明文 Key 的消息：无论参数是否合法，都不把 Key 留在聊天记录里。
    # 原消息删除后仍可正常 send_message，所以后续统一用它回复。
    deleted = False
    try:
        await message.delete()  # type: ignore[union-attr]
        deleted = True
    except TelegramError as exc:
        log.warning("撤回含 Key 的消息失败：%s", exc)

    args = context.args or []
    log.info("收到 /addkey：user=%s，%s", user_id, _safe_args(args))
    alias, api_key = split_alias_and_key(args)
    if not api_key:
        log.warning("addkey 参数不足：user=%s，%s", user_id, _safe_args(args))
        await say(
            "⚠️ 参数不完整。\n"
            "格式：<code>/addkey &lt;别名&gt; &lt;API_KEY&gt;</code>\n"
            "示例：<code>/addkey 主账号 sk_1234567890</code>\n"
            "　　　<code>/addkey Cline-01 sk_1234567890</code>"
        )
        return

    if alias is None:
        log.warning("addkey 别名不合法：user=%s，%s", user_id, _safe_args(args))
        await say(
            "⚠️ 别名不合法：1–24 个字符，以中英文、数字或下划线开头，"
            "之后可含空格、点、连字符"
            "（<code>#</code>、<code>/</code>、<code>:</code>、emoji 不行）。\n"
            "示例：<code>/addkey 主账号 sk_1234567890</code>、<code>/addkey Cline-01 sk_1234567890</code>"
        )
        return
    if len(api_key) < 8 or any(ch.isspace() for ch in api_key):
        log.warning("addkey 的 Key 看起来不合法：user=%s，长度=%d", user_id, len(api_key))
        await say("⚠️ API Key 看起来不合法（长度需 ≥ 8 且不含空白字符）。")
        return

    try:
        async with ctx.lock_for(user_id):
            total = await asyncio.to_thread(ctx.store.add, user_id, alias, api_key)
    except KeyLimitError as exc:
        log.warning("addkey 超出上限：user=%s，%s", user_id, exc)
        await say(f"⚠️ {esc(str(exc))}")
        return
    except ConfigError as exc:
        log.error("写入配置失败：user=%s，%s", user_id, exc)
        await say("❌ 保存失败，Key <b>没有</b>被记录。\n" f"原因：<code>{esc(str(exc)[:300])}</code>")
        return

    log.info("已保存 Key：user=%s，别名=%r，该用户现有 %s 个", user_id, alias, total)
    note = "（含 Key 的消息已撤回）" if deleted else "（⚠️ 未能撤回原消息，建议自行删除）"
    await say(
        f"✅ 已保存 Key\n📌 别名：<code>{esc(alias)}</code>\n"
        f"🔐 Key：<code>{esc(mask_key(api_key))}</code>\n{note}"
    )


async def delkey_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    ctx = ctx_of(context)
    if not context.args:
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            "⚠️ 请指定别名。\n格式：<code>/delkey &lt;别名&gt;</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    alias = context.args[0].strip()
    try:
        async with ctx.lock_for(update.effective_user.id):  # type: ignore[union-attr]
            removed = await asyncio.to_thread(ctx.store.delete, update.effective_user.id, alias)  # type: ignore[union-attr]
    except ConfigError as exc:
        log.error("删除失败：%s", exc)
        await update.effective_message.reply_text(f"❌ 删除失败：<code>{esc(str(exc)[:200])}</code>", parse_mode=ParseMode.HTML)  # type: ignore[union-attr]
        return
    log.info("删除别名：user=%s，别名=%r，结果=%s", update.effective_user.id, alias, removed)  # type: ignore[union-attr]
    text = f"🗑️ 已删除别名 <b>{esc(alias)}</b>。" if removed else f"❌ 未找到别名 <b>{esc(alias)}</b>。"
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)  # type: ignore[union-attr]


async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    ctx = ctx_of(context)
    if [a.lower() for a in (context.args or [])] != ["confirm"]:
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            "⚠️ 这会删除你绑定的<b>全部</b> Key，确认请输入：<code>/clear confirm</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    try:
        async with ctx.lock_for(update.effective_user.id):  # type: ignore[union-attr]
            removed = await asyncio.to_thread(ctx.store.clear, update.effective_user.id)  # type: ignore[union-attr]
    except ConfigError as exc:
        log.error("清空失败：%s", exc)
        await update.effective_message.reply_text(f"❌ 操作失败：<code>{esc(str(exc)[:200])}</code>", parse_mode=ParseMode.HTML)  # type: ignore[union-attr]
        return
    log.info("清空 Key：user=%s，删除 %s 个", update.effective_user.id, removed)  # type: ignore[union-attr]
    await update.effective_message.reply_text(f"🧹 已清空 {removed} 个 Key。", parse_mode=ParseMode.HTML)  # type: ignore[union-attr]


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    ctx = ctx_of(context)
    message = update.effective_message
    user_id = update.effective_user.id  # type: ignore[union-attr]

    wait = ctx.cooldown.hit(user_id)
    if wait > 0:
        await message.reply_text(f"⏳ 操作太快了，请 {wait:.0f} 秒后再试。")  # type: ignore[union-attr]
        return

    user_keys = await _load_keys(ctx, user_id, message)
    if user_keys is None:
        return
    if not user_keys:
        await message.reply_text(  # type: ignore[union-attr]
            "⚠️ 你还没有绑定任何 Key。\n用法：<code>/addkey 主账号 sk_xxxx</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    notice = await message.reply_text(f"⏳ 正在查询 {len(user_keys)} 个账号…")  # type: ignore[union-attr]
    try:
        snapshots = await ctx.client.fetch_all(list(user_keys.items()))
        chunks = split_message(render_panel(snapshots), ctx.settings.message_limit)
    finally:
        # 无论成功失败都收起"正在查询"，避免残留一条假进度
        try:
            await notice.delete()
        except TelegramError:
            pass

    for chunk in chunks:
        await context.bot.send_message(
            update.effective_chat.id,
            chunk,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("处理更新时发生未捕获异常", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        name = type(context.error).__name__ if context.error else "Unknown"
        try:
            await update.effective_message.reply_text(
                f"😵 处理时出错了（<code>{esc(name)}</code>）。\n"
                "如果这是 /addkey 报的，多半是配置存储写不进去——先用 /id 看一眼存储状态，"
                "再把 <code>docker logs</code> 里的 traceback 发给管理员。",
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass


async def post_init(application: Application) -> None:
    await application.bot.set_my_commands(
        [
            BotCommand("status", "查看额度面板"),
            BotCommand("addkey", "添加或更新 Key"),
            BotCommand("delkey", "删除 Key"),
            BotCommand("keys", "列出已绑定的别名"),
            BotCommand("clear", "清空全部 Key"),
            BotCommand("id", "查看我的用户 ID"),
            BotCommand("help", "帮助"),
        ]
    )


def build_application(settings: Settings, token: str) -> Application:
    ctx = BotContext(settings)
    application = Application.builder().token(token).post_init(post_init).build()
    application.bot_data["ctx"] = ctx

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("id", id_command))
    application.add_handler(CommandHandler(["status", "quota"], status_command))
    application.add_handler(CommandHandler("addkey", addkey_command))
    application.add_handler(CommandHandler("delkey", delkey_command))
    application.add_handler(CommandHandler("keys", keys_command))
    application.add_handler(CommandHandler("clear", clear_command))
    application.add_error_handler(on_error)
    return application


def main() -> int:
    setup_logging()
    settings = Settings.from_env()

    store = ConfigStore(settings.config_file, settings.max_keys_per_user)
    try:
        store.load()
    except ConfigError as exc:
        log.critical("配置存储不可用：%s", exc)
    except OSError as exc:  # 兜底：宁可降级运行，也不要崩成 restart 循环
        log.critical("配置存储初始化失败：%s", exc)
    else:
        ok, detail = store.self_check()
        if ok:
            log.info("配置存储就绪：%s（%s）", settings.config_file, detail)
        else:
            log.critical("配置存储不可写，/addkey 一定不会生效：%s", detail)

    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token or token == "YOUR_TELEGRAM_BOT_TOKEN":
        log.critical("未配置 TELEGRAM_BOT_TOKEN，请参考 .env.example 设置后重启。")
        return 1

    suffix = f"…{token[-4:]}" if len(token) > 8 else ""
    log.info(
        "ClinePass TG Bot v%s 启动中：API=%s 额度路径=%s 并行=%s 白名单=%s 演示模式=%s Token=***%s",
        __version__,
        settings.api_base,
        settings.usage_path,
        settings.max_parallel,
        len(settings.allowed_user_ids) or "关闭",
        settings.demo_mode,
        suffix,
    )

    application = build_application(settings, token)
    application.run_polling(drop_pending_updates=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
