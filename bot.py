import os
import json
import datetime
import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ==================== 基础配置 ====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
CLINEPASS_API_URL = os.getenv("CLINEPASS_API_URL", "https://api.cline.bot/api/v1/user/usage")

# 配置文件路径
CONFIG_FILE = "config.json"

# 默认备用数值（当 API 无法响应时显示）
ENV_5H_USAGE = float(os.getenv("CLINEPASS_5H_USAGE", "63.0"))
ENV_WEEK_USAGE = float(os.getenv("CLINEPASS_WEEK_USAGE", "48.0"))
ENV_MONTH_USAGE = float(os.getenv("CLINEPASS_MONTH_USAGE", "31.0"))


# ==================== JSON 配置文件管理 ====================
def load_config() -> dict:
    """读取 config.json，支持多 Key 字典结构"""
    default_config = {
        "user_keys": {}  # 格式: { "user_id": { "key_alias1": "key1", "key_alias2": "key2" } }
    }

    if not os.path.exists(CONFIG_FILE):
        save_config(default_config)
        return default_config

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if "user_keys" not in data:
                data["user_keys"] = {}
            return data
    except Exception as e:
        print(f"[ERROR] 读取 config.json 失败: {e}")
        return default_config


def save_config(config_data: dict):
    """保存配置数据到 config.json"""
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[ERROR] 写入 config.json 失败: {e}")


def add_user_key(user_id: int, alias: str, api_key: str):
    """为指定用户增加或修改一个 API Key"""
    config = load_config()
    uid = str(user_id)
    if uid not in config["user_keys"]:
        config["user_keys"][uid] = {}
    
    config["user_keys"][uid][alias] = api_key
    save_config(config)


def delete_user_key(user_id: int, alias: str) -> bool:
    """删除指定的 API Key"""
    config = load_config()
    uid = str(user_id)
    if uid in config["user_keys"] and alias in config["user_keys"][uid]:
        del config["user_keys"][uid][alias]
        save_config(config)
        return True
    return False


def get_user_keys(user_id: int) -> dict:
    """获取指定用户绑定的所有 Key 字典: {alias: key}"""
    config = load_config()
    return config["user_keys"].get(str(user_id), {})


# ==================== 工具与 API 请求函数 ====================
def make_progress_bar(percent: float, length: int = 10) -> str:
    """生成进度条 [████████░░░░]"""
    percent = max(0.0, min(100.0, percent))
    filled_length = int(round(length * percent / 100))
    return '█' * filled_length + '░' * (length - filled_length)


def fetch_data_from_api(api_key: str) -> dict:
    """携带指定的 API Key 请求接口"""
    headers = {"User-Agent": "ClinePass-TG-Bot/1.0"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    response = requests.get(CLINEPASS_API_URL, headers=headers, timeout=10)
    response.raise_for_status()
    data = response.json()

    return {
        "h5_percent": float(data.get("h5", {}).get("percent", ENV_5H_USAGE)),
        "h5_remaining": data.get("h5", {}).get("remaining_str", "1h 52m"),
        "h5_reset": data.get("h5", {}).get("reset_time", "18:32"),
        "week_percent": float(data.get("week", {}).get("percent", ENV_WEEK_USAGE)),
        "week_reset": data.get("week", {}).get("reset_str", "周一 08:00"),
        "month_percent": float(data.get("month", {}).get("percent", ENV_MONTH_USAGE)),
        "month_reset": data.get("month", {}).get("reset_str", "10月1日")
    }


def render_single_status(alias: str, api_key: str) -> str:
    """渲染单个 API Key 的面板文本"""
    now = datetime.datetime.now()
    usage = None

    if CLINEPASS_API_URL and api_key:
        try:
            usage = fetch_data_from_api(api_key)
        except Exception as e:
            print(f"[Warning] Key [{alias}] 请求失败 ({e})")

    if not usage:
        if now.month == 12:
            next_month = datetime.date(now.year + 1, 1, 1)
        else:
            next_month = datetime.date(now.year, now.month + 1, 1)

        usage = {
            "h5_percent": ENV_5H_USAGE,
            "h5_remaining": "1h 52m",
            "h5_reset": (now + datetime.timedelta(hours=1, minutes=52)).strftime("%H:%M"),
            "week_percent": ENV_WEEK_USAGE,
            "week_reset": "周一 08:00",
            "month_percent": ENV_MONTH_USAGE,
            "month_reset": f"{next_month.month}月1日"
        }

    h5_bar = make_progress_bar(usage["h5_percent"])
    week_bar = make_progress_bar(usage["week_percent"])
    month_bar = make_progress_bar(usage["month_percent"])

    return (
        f"🔑 **账号/别名：{alias}**\n"
        f"📊 **5小时额度**\n"
        f"`{h5_bar}` {int(usage['h5_percent'])}%\n"
        f"剩余：{usage['h5_remaining']}  重置：{usage['h5_reset']}\n\n"
        f"📅 **本周额度**\n"
        f"`{week_bar}` {int(usage['week_percent'])}%\n"
        f"重置：{usage['week_reset']}\n\n"
        f"📆 **本月额度**\n"
        f"`{month_bar}` {int(usage['month_percent'])}%\n"
        f"重置：{usage['month_reset']}"
    )


# ==================== Telegram 命令处理函数 ====================
async def addkey_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /addkey <别名> <API_KEY> 指令"""
    user_id = update.effective_user.id
    
    if len(context.args) < 2:
        await update.message.reply_text(
            "⚠️ 请输入完整参数！\n"
            "格式：`/addkey <别名/账号标识> <API_KEY>`\n"
            "示例：`/addkey 主账号 sk-12345678`",
            parse_mode="Markdown"
        )
        return

    alias = context.args[0].strip()
    api_key = context.args[1].strip()

    add_user_key(user_id, alias, api_key)

    # 尝试撤回包含敏感 Key 的消息
    try:
        await update.message.delete()
    except Exception:
        pass

    masked_key = api_key[:4] + "...." + api_key[-4:] if len(api_key) > 8 else "****"
    await update.message.reply_text(
        f"✅ 已成功保存/更新 Key！\n"
        f"📌 别名：`{alias}`\n"
        f"🔐 Key：`{masked_key}`\n"
        f"*(包含 Key 的消息已自动撤回)*",
        parse_mode="Markdown"
    )


async def delkey_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /delkey <别名> 指令"""
    user_id = update.effective_user.id
    
    if not context.args:
        await update.message.reply_text("⚠️ 请指定要删除的 Key 别名！\n格式：`/delkey <别名>`", parse_mode="Markdown")
        return

    alias = context.args[0].strip()
    if delete_user_key(user_id, alias):
        await update.message.reply_text(f"🗑️ 已成功删除别名为 `{alias}` 的 Key。", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ 未找到别名为 `{alias}` 的 Key。", parse_mode="Markdown")


async def keys_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查看当前绑定的所有 Key 列表"""
    user_id = update.effective_user.id
    user_keys = get_user_keys(user_id)

    if not user_keys:
        await update.message.reply_text("⚠️ 你尚未绑定任何 Key。请使用 `/addkey <别名> <API_KEY>` 添加。", parse_mode="Markdown")
        return

    text = "📋 **你已绑定的 Key 列表：**\n\n"
    for alias, key in user_keys.items():
        masked_key = key[:4] + "...." + key[-4:] if len(key) > 8 else "****"
        text += f"• **{alias}**: `{masked_key}`\n"

    await update.message.reply_text(text, parse_mode="Markdown")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """汇总并展示所有 Key 的额度面板"""
    user_id = update.effective_user.id
    user_keys = get_user_keys(user_id)

    if not user_keys:
        await update.message.reply_text(
            "⚠️ 当前未绑定任何 API Key！\n"
            "请先使用 `/addkey <别名> <API_KEY>` 绑定你的密钥。",
            parse_mode="Markdown"
        )
        return

    now_str = datetime.datetime.now().strftime("%H:%M:%S")
    sections = [f"🤖 **ClinePass Status Panel**"]

    # 循环遍历用户绑定的每一个 Key 并生成对应的渲染面板
    for alias, api_key in user_keys.items():
        sections.append(render_single_status(alias, api_key))

    sections.append(f"🔄 **更新时间** {now_str}")
    
    # 组合多段内容输出，用分割线分隔各个 Key 的状态
    full_message = "\n\n───────────────\n\n".join(sections)
    await update.message.reply_text(full_message, parse_mode="Markdown")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "🤖 **ClinePass TG Bot 指令列表**\n\n"
        "🔹 `/status` - 查看所有已绑定 Key 的额度面板\n"
        "🔹 `/addkey <别名> <API_KEY>` - 添加或更新指定别名的 Key\n"
        "🔹 `/delkey <别名>` - 删除指定的 Key\n"
        "🔹 `/keys` - 列出你已绑定的所有 Key 别名\n"
        "🔹 `/help` - 显示帮助菜单"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")


def main():
    if TELEGRAM_BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN" or not TELEGRAM_BOT_TOKEN:
        print("[ERROR] 请在环境变量中提供有效的 TELEGRAM_BOT_TOKEN！")
        return

    load_config()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", status_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("addkey", addkey_command))
    app.add_handler(CommandHandler("delkey", delkey_command))
    app.add_handler(CommandHandler("keys", keys_command))
    app.add_handler(CommandHandler("help", help_command))

    print("ClinePass TG Bot (多 Key 版本) 启动成功...")
    app.run_polling()


if __name__ == '__main__':
    main()
