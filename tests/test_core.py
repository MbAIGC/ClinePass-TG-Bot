"""core.py 的单元测试：python3 -m unittest discover -s tests -v

只依赖标准库（core 需要 requests，运行环境已自带）。
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core  # noqa: E402
from core import (  # noqa: E402
    ApiError,
    ClinePassClient,
    ConfigError,
    ConfigStore,
    Cooldown,
    KeyLimitError,
    Settings,
    describe_reset,
    humanize_delta,
    mask_key,
    parse_limits_list,
    parse_timestamp,
    parse_usage,
    progress_bar,
    render_panel,
    render_snapshot,
    sanitize_alias,
    split_message,
    Snapshot,
    Window,
)

# 真实接口（2026-09 实测）的原样返回，用于回归测试
REAL_LIMITS_PAYLOAD = {
    "data": {
        "limits": [
            {"type": "five_hour", "percentUsed": 2, "resetsAt": "2026-09-25T14:32:27.073666206Z"},
            {"type": "weekly", "percentUsed": 57, "resetsAt": "2026-09-30T12:08:27.075836336Z"},
            {"type": "monthly", "percentUsed": 28, "resetsAt": "2026-10-23T12:08:27.07803017Z"},
        ]
    },
    "success": True,
}


# ==================== 假 HTTP ====================
class FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    def __init__(self, routes):
        """routes: {path: FakeResponse | list[FakeResponse] | Exception}"""
        self.routes = routes
        self.headers = {}
        self.calls: list[str] = []

    def get(self, url, headers=None, timeout=None):
        path = url.split("cline.bot", 1)[-1]
        self.calls.append(path)
        if path not in self.routes:
            return FakeResponse(404, {"error": "Not Found", "success": False})
        route = self.routes[path]
        if isinstance(route, list):
            route = route.pop(0) if len(route) > 1 else route[0]
        if isinstance(route, Exception):
            raise route
        return route


def settings_for(tmp: str, **kwargs) -> Settings:
    base = dict(
        config_file=os.path.join(tmp, "config.json"),
        api_base="https://api.cline.bot",
        http_retries=0,
        retry_backoff=0.0,
        status_cooldown=0.0,
        max_keys_per_user=3,
    )
    base.update(kwargs)
    return Settings(**base)


# ==================== 渲染工具 ====================
class TestHelpers(unittest.TestCase):
    def test_progress_bar_edges(self):
        self.assertEqual(progress_bar(0), "░" * 10)
        self.assertEqual(progress_bar(100), "█" * 10)
        self.assertEqual(progress_bar(-5), "░" * 10)
        self.assertEqual(progress_bar(1000), "█" * 10)
        self.assertEqual(len(progress_bar(63)), 10)
        self.assertEqual(progress_bar(50, 4), "██░░")

    def test_mask_key(self):
        self.assertEqual(mask_key("sk_1234567890"), "sk_1…7890")
        self.assertEqual(mask_key("short"), "****")
        self.assertEqual(mask_key(""), "****")

    def test_sanitize_alias(self):
        self.assertEqual(sanitize_alias(" 主账号 "), "主账号")
        self.assertEqual(sanitize_alias("work-1.v2"), "work-1.v2")
        self.assertIsNone(sanitize_alias(""))
        self.assertIsNone(sanitize_alias("a" * 25))
        self.assertIsNone(sanitize_alias("bad;rm -rf"))

    def test_split_message(self):
        self.assertEqual(split_message("hi", 100), ["hi"])
        text = "\n".join(f"line-{i}" for i in range(200))
        chunks = split_message(text, 100)
        self.assertTrue(all(len(c) <= 100 for c in chunks))
        self.assertEqual("".join(chunks).replace("\n", ""), text.replace("\n", ""))

    def test_split_message_hard_cut(self):
        chunks = split_message("x" * 250, 100)
        self.assertEqual([len(c) for c in chunks], [100, 100, 50])

    def test_cooldown(self):
        cd = Cooldown(5)
        self.assertEqual(cd.hit(1, now=100.0), 0.0)
        self.assertEqual(cd.hit(1, now=102.0), 3.0)
        self.assertEqual(cd.hit(1, now=106.0), 0.0)
        self.assertEqual(cd.hit(2, now=106.0), 0.0)  # 不同用户互不影响
        self.assertEqual(Cooldown(0).hit(1), 0.0)

    def test_parse_timestamp(self):
        # 官方返回纳秒精度 + Z，Python 3.11 的 fromisoformat 吃不下，必须规范化
        self.assertEqual(
            parse_timestamp("2026-09-25T14:32:27.073666206Z"),
            datetime(2026, 9, 25, 14, 32, 27, 73666, tzinfo=timezone.utc),
        )
        self.assertEqual(
            parse_timestamp("2026-09-25T14:32:27Z"),
            datetime(2026, 9, 25, 14, 32, 27, tzinfo=timezone.utc),
        )
        self.assertEqual(parse_timestamp("2026-09-25T14:32:27+08:00").utcoffset(), timedelta(hours=8))
        self.assertEqual(parse_timestamp("2026-09-25 14:32:27").tzinfo, timezone.utc)
        self.assertIsNone(parse_timestamp("not a time"))
        self.assertIsNone(parse_timestamp(""))
        self.assertIsNone(parse_timestamp(None))

    def test_humanize_delta(self):
        self.assertEqual(humanize_delta(-1), "已到重置时间")
        self.assertEqual(humanize_delta(30), "不到 1 分钟")
        self.assertEqual(humanize_delta(600), "10 分钟")
        self.assertEqual(humanize_delta(3600 * 2 + 60 * 5), "2 小时 5 分")
        self.assertEqual(humanize_delta(3600 * 24 * 3), "3 天")

    def test_describe_reset(self):
        now = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
        window = Window("w", 10, reset_dt=datetime(2026, 9, 25, 14, 0, tzinfo=timezone.utc))
        text = describe_reset(window, now)
        self.assertIn("还有 2 小时", text)
        self.assertRegex(text, r"^\d{2}-\d{2} \d{2}:\d{2}（还有 2 小时）$")
        self.assertEqual(describe_reset(Window("w", 10, reset="周一 08:00"), now), "周一 08:00")
        self.assertIsNone(describe_reset(Window("w", 10), now))


# ==================== 配置存储 ====================
class TestConfigStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "config.json")
        self.store = ConfigStore(self.path, max_keys_per_user=3)

    def test_creates_file_on_first_load(self):
        data = self.store.load()
        self.assertEqual(data["user_keys"], {})
        self.assertTrue(os.path.isfile(self.path))

    def test_add_delete_persist(self):
        self.store.add(1, "主账号", "sk_aaaaaaaaaaaa")
        self.store.add(1, "备用", "sk_bbbbbbbbbbbb")
        self.assertEqual(self.store.keys(1), {"主账号": "sk_aaaaaaaaaaaa", "备用": "sk_bbbbbbbbbbbb"})
        self.assertTrue(self.store.delete(1, "备用"))
        self.assertFalse(self.store.delete(1, "不存在"))
        # 重新读盘确认真的落盘
        self.assertEqual(ConfigStore(self.path).keys(1), {"主账号": "sk_aaaaaaaaaaaa"})
        self.assertEqual(self.store.clear(1), 1)
        self.assertEqual(self.store.keys(1), {})

    def test_key_limit(self):
        for i in range(3):
            self.store.add(1, f"k{i}", "sk_123456789")
        with self.assertRaises(KeyLimitError):
            self.store.add(1, "overflow", "sk_123456789")
        # 覆盖已有别名不受限制
        self.store.add(1, "k0", "sk_updatedvalue")

    def test_file_permissions(self):
        self.store.add(1, "a", "sk_123456789")
        mode = os.stat(self.path).st_mode & 0o777
        self.assertEqual(mode, 0o600, f"配置文件权限应为 600，实际 {oct(mode)}")

    def test_corrupt_file_is_backed_up_not_wiped(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{ this is not json")
        data = self.store.load()
        self.assertEqual(data["user_keys"], {})
        backups = [f for f in os.listdir(self.tmp.name) if ".corrupt-" in f]
        self.assertEqual(len(backups), 1)

    def test_directory_path_raises_config_error(self):
        os.makedirs(os.path.join(self.tmp.name, "as-dir.json"))
        store = ConfigStore(os.path.join(self.tmp.name, "as-dir.json"))
        with self.assertRaises(ConfigError) as cm:
            store.load()
        self.assertIn("目录", str(cm.exception))

    def test_non_object_json_rejected(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump([1, 2, 3], fh)
        with self.assertRaises(ConfigError):
            self.store.load()

    def test_atomic_write_leaves_no_temp_files(self):
        self.store.add(1, "a", "sk_123456789")
        leftovers = [f for f in os.listdir(self.tmp.name) if f.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_permission_error_becomes_config_error(self):
        """线上 v0.0.1 的坑：目录不可写时 mkstemp 抛 PermissionError，直接崩进程。"""
        from unittest import mock

        with mock.patch.object(core.tempfile, "mkstemp", side_effect=PermissionError(13, "Permission denied")):
            with self.assertRaises(ConfigError) as cm:
                self.store.add(1, "a", "sk_123456789")
        message = str(cm.exception)
        self.assertIn("权限不足", message)
        self.assertIn("chown", message)

    def test_write_oserror_becomes_config_error(self):
        from unittest import mock

        with mock.patch.object(core.tempfile, "mkstemp", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(ConfigError):
                self.store.add(1, "a", "sk_123456789")

    def test_replace_failure_cleans_up_temp_file(self):
        from unittest import mock

        with mock.patch.object(core.os, "replace", side_effect=OSError(1, "Operation not permitted")):
            with self.assertRaises(ConfigError):
                self.store.add(1, "a", "sk_123456789")
        leftovers = [f for f in os.listdir(self.tmp.name) if f.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_load_never_raises_bare_oserror(self):
        """ConfigError 之外的 OSError 不应该漏给调用方（否则会崩在启动阶段）。"""
        from unittest import mock

        with mock.patch.object(core.tempfile, "mkstemp", side_effect=PermissionError(13, "denied")):
            try:
                self.store.load()
            except ConfigError:
                pass
            except OSError as exc:  # pragma: no cover - 失败即为回归
                self.fail(f"应当转成 ConfigError，实际漏出 {exc!r}")


# ==================== 额度解析 ====================
class TestParseUsage(unittest.TestCase):
    def test_envelope_shape(self):
        windows = parse_usage(
            {
                "success": True,
                "data": {
                    "h5": {"percent": 63, "remaining_str": "1h 52m", "reset_time": "18:32"},
                    "week": {"percent": 48.4, "reset_str": "周一 08:00"},
                    "month": {"percent": 31},
                },
            }
        )
        self.assertIsNotNone(windows)
        assert windows is not None
        self.assertEqual([w.label for w in windows], ["5 小时额度", "本周额度", "本月额度"])
        self.assertEqual(windows[0].remaining, "1h 52m")
        self.assertEqual(windows[1].percent, 48.4)

    def test_toplevel_and_aliases(self):
        windows = parse_usage({"5h": 10, "weekly": {"used_percent": 20}, "monthly": {"percent": 0}})
        self.assertIsNotNone(windows)
        assert windows is not None
        self.assertEqual(len(windows), 3)
        self.assertEqual(windows[2].percent, 0.0)

    def test_nested_usage_key(self):
        windows = parse_usage({"data": {"usage": {"h5": {"percent": 5}}}})
        assert windows is not None
        self.assertEqual(windows[0].percent, 5.0)

    def test_unparseable_returns_none(self):
        self.assertIsNone(parse_usage({"data": {"email": "a@b.c"}}))
        self.assertIsNone(parse_usage({"h5": {"foo": "bar"}}))
        self.assertIsNone(parse_usage("nope"))
        self.assertIsNone(parse_usage({}))

    def test_percent_clamped_and_nan_rejected(self):
        windows = parse_usage({"h5": {"percent": 250}, "week": {"percent": "abc"}, "month": {"percent": -3}})
        assert windows is not None
        self.assertEqual(windows[0].percent, 100.0)
        self.assertEqual([w.label for w in windows], ["5 小时额度", "本月额度"])


# ==================== 官方额度接口 ====================
class TestOfficialLimits(unittest.TestCase):
    def test_real_payload(self):
        windows = parse_usage(REAL_LIMITS_PAYLOAD)
        self.assertIsNotNone(windows)
        assert windows is not None
        self.assertEqual([w.label for w in windows], ["5 小时额度", "本周额度", "本月额度"])
        self.assertEqual([w.percent for w in windows], [2.0, 57.0, 28.0])
        self.assertEqual([w.remaining_percent for w in windows], [98.0, 43.0, 72.0])
        self.assertIsNotNone(windows[0].reset_dt)

    def test_limits_without_envelope(self):
        windows = parse_limits_list(
            {"limits": [{"type": "weekly", "percentUsed": 10, "resetsAt": "2026-09-30T12:08:27Z"}]}
        )
        assert windows is not None
        self.assertEqual(windows[0].label, "本周额度")
        self.assertEqual(windows[0].percent, 10.0)

    def test_type_aliases(self):
        # 社区实现里出现过 '5-hour' 与线上 'five_hour' 不一致的问题，这里两种都要认
        for raw in ("five_hour", "5-hour", "5_hour", "5h", "FIVE_HOUR"):
            windows = parse_usage({"data": {"limits": [{"type": raw, "percentUsed": 7}]}})
            assert windows is not None, raw
            self.assertEqual(windows[0].label, "5 小时额度", raw)

    def test_unknown_type_is_kept(self):
        windows = parse_usage(
            {"data": {"limits": [{"type": "daily", "percentUsed": 12}, {"type": "weekly", "percentUsed": 30}]}}
        )
        assert windows is not None
        self.assertEqual([w.label for w in windows], ["本周额度", "daily"])

    def test_malformed_entries_skipped(self):
        self.assertIsNone(parse_usage({"data": {"limits": []}}))
        self.assertIsNone(parse_usage({"data": {"limits": ["nope", {"type": ""}]}}))
        self.assertIsNone(parse_limits_list({"data": {}}))

    def test_warning_thresholds(self):
        self.assertEqual(Window("w", 79.9).warning, "")
        self.assertEqual(Window("w", 80).warning, "⚠️")
        self.assertEqual(Window("w", 95).warning, "⛔️")
        self.assertEqual(Window("w", None).warning, "")

    def test_rendered_panel_shows_quota_and_countdown(self):
        snapshot = Snapshot(
            alias="主账号", key_mask="sk_7…aaaa", windows=parse_usage(REAL_LIMITS_PAYLOAD)
        )
        text = render_snapshot(snapshot, now=datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc))
        self.assertIn("剩余 98%", text)
        self.assertIn("剩余 43%", text)
        self.assertIn("重置：", text)
        self.assertIn("还有", text)
        self.assertNotIn("⚠️", text)  # 2% / 57% / 28% 都不触发告警
        self.assertNotIn("⛔️", text)

    def test_badges_appear_when_hot(self):
        self.assertIn("⚠️", render_snapshot(Snapshot("a", "k", windows=[Window("本周额度", 80.0)])))
        self.assertIn("⛔️", render_snapshot(Snapshot("a", "k", windows=[Window("本周额度", 96.0)])))


# ==================== API 客户端 ====================
class TestClient(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = settings_for(self.tmp.name)

    def client(self, routes):
        return ClinePassClient(self.settings, session=FakeSession(routes))

    def test_happy_path_with_real_envelope(self):
        session = FakeSession(
            {
                "/api/v1/users/me": FakeResponse(200, {"success": True, "data": {"email": "a@b.c", "displayName": "Tester"}}),
                "/api/v1/users/me/plan": FakeResponse(
                    200,
                    {
                        "success": True,
                        "data": {
                            "plan": {"displayName": "Cline Pass (Monthly)", "interval": "Monthly", "isActive": True},
                            "currentPeriodStart": "2026-09-23T12:03:54Z",
                            "currentPeriodEnd": "2026-10-23T12:03:54Z",
                        },
                    },
                ),
                "/api/v1/users/me/plan/usage-limits": FakeResponse(200, REAL_LIMITS_PAYLOAD),
            }
        )
        snapshot = ClinePassClient(self.settings, session=session).fetch_snapshot_sync("主账号", "sk_test123456")
        self.assertEqual(snapshot.account["email"], "a@b.c")
        self.assertEqual(snapshot.plan["displayName"], "Cline Pass (Monthly)")
        self.assertEqual([w.percent for w in snapshot.windows], [2.0, 57.0, 28.0])
        self.assertEqual(snapshot.warnings, [])

    def test_invalid_key_short_circuits(self):
        session = FakeSession({"/api/v1/users/me": FakeResponse(401, {"error": "Unauthorized"})})
        snapshot = ClinePassClient(self.settings, session=session).fetch_snapshot_sync("x", "sk_bad")
        self.assertFalse(snapshot.has_usage)
        self.assertTrue(any("401" in w for w in snapshot.warnings))
        # 401 后不再请求其它接口
        self.assertEqual(session.calls, ["/api/v1/users/me"])

    def test_missing_usage_endpoint_is_reported_not_faked(self):
        session = FakeSession(
            {
                "/api/v1/users/me": FakeResponse(200, {"data": {"email": "a@b.c"}}),
                "/api/v1/users/me/plan": FakeResponse(200, {"data": {"plan": {"name": "Cline Pass"}}}),
            }
        )
        snapshot = ClinePassClient(self.settings, session=session).fetch_snapshot_sync("x", "sk_test123456")
        self.assertIn("/api/v1/users/me/plan/usage-limits", session.calls)  # 默认打官方额度接口
        self.assertFalse(snapshot.has_usage)
        self.assertTrue(any("404" in w for w in snapshot.warnings))
        text = render_snapshot(snapshot)
        self.assertIn("暂无可显示的额度数据", text)
        self.assertNotIn("63%", text)

    def test_retry_on_server_error_then_success(self):
        session = FakeSession(
            {
                "/api/v1/users/me": FakeResponse(200, {"data": {"email": "a@b.c"}}),
                "/api/v1/users/me/plan": FakeResponse(200, {"data": {}}),
                "/api/v1/users/me/plan/usage-limits": [
                    FakeResponse(503, {"error": "boom"}),
                    FakeResponse(200, REAL_LIMITS_PAYLOAD),
                ],
            }
        )
        settings = settings_for(self.tmp.name, http_retries=1)
        original_sleep = core.time.sleep
        core.time.sleep = lambda *_: None
        try:
            snapshot = ClinePassClient(settings, session=session).fetch_snapshot_sync("x", "sk_test123456")
        finally:
            core.time.sleep = original_sleep
        self.assertEqual(snapshot.windows[0].percent, 2.0)

    def test_network_error_classified(self):
        import requests

        session = FakeSession({"/api/v1/users/me": requests.ConnectionError("no route")})
        snapshot = ClinePassClient(self.settings, session=session).fetch_snapshot_sync("x", "sk_test123456")
        self.assertTrue(any("网络" in w for w in snapshot.warnings))

    def test_non_json_body(self):
        session = FakeSession({"/api/v1/users/me": FakeResponse(200, None, text="<html>oops</html>")})
        snapshot = ClinePassClient(self.settings, session=session).fetch_snapshot_sync("x", "sk_test123456")
        self.assertTrue(any("账号信息获取失败" in w for w in snapshot.warnings))

    def test_demo_mode_marks_sample_data(self):
        settings = settings_for(self.tmp.name, demo_mode=True)
        session = FakeSession({"/api/v1/users/me": FakeResponse(200, {"data": {"email": "a@b.c"}}), "/api/v1/users/me/plan": FakeResponse(200, {"data": {}})})
        snapshot = ClinePassClient(settings, session=session).fetch_snapshot_sync("x", "sk_test123456")
        self.assertTrue(any("DEMO_MODE" in w for w in snapshot.warnings))
        self.assertEqual(len(snapshot.windows), 3)


# ==================== 渲染与安全 ====================
class TestRender(unittest.TestCase):
    def test_html_escaping(self):
        snapshot = Snapshot(
            alias="<script>alert(1)</script>",
            key_mask=mask_key("sk_1234567890"),
            account={"email": "<b>x</b>@y.z"},
            windows=[Window("5 小时额度", 63.0, "1h 52m", "18:32")],
        )
        text = render_panel([snapshot], now=datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc))
        self.assertNotIn("<script>", text)
        self.assertIn("&lt;script&gt;", text)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;@y.z", text)
        self.assertRegex(text, r"更新时间</b> \d{2}:\d{2}:\d{2}")

    def test_alias_with_markdown_chars_is_rendered_literally(self):
        snapshot = Snapshot(alias="a_b*c", key_mask="sk_1…7890", windows=[Window("本周额度", 50.0)])
        text = render_snapshot(snapshot)
        self.assertIn("a_b*c", text)
        self.assertNotIn("**", text)

    def test_missing_fields_do_not_crash(self):
        text = render_panel([Snapshot(alias="x", key_mask="****")], now=datetime(2026, 9, 25, tzinfo=timezone.utc))
        self.assertIn("账号/别名：x", text)

    def test_split_panel_keeps_all_aliases(self):
        snapshots = [Snapshot(alias=f"acc{i}", key_mask="sk_1…7890", windows=[Window("本周额度", 50.0)]) for i in range(60)]
        chunks = split_message(render_panel(snapshots, now=datetime(2026, 9, 25, tzinfo=timezone.utc)), 800)
        joined = "\n".join(chunks)
        for i in range(60):
            self.assertIn(f"acc{i}", joined)
        self.assertTrue(all(len(c) <= 800 for c in chunks))


# ==================== 存储自检 ====================
class TestSelfCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "config.json")
        self.store = core.ConfigStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_writable_dir_is_ok(self):
        ok, detail = self.store.self_check()
        self.assertTrue(ok, detail)
        self.assertIn("可写", detail)

    def test_probe_leaves_no_files(self):
        self.store.self_check()
        self.assertEqual(os.listdir(self.tmp.name), [])

    def test_unwritable_dir_reports_false(self):
        from unittest import mock

        with mock.patch.object(core.tempfile, "mkstemp", side_effect=PermissionError(13, "denied")):
            ok, detail = self.store.self_check()
        self.assertFalse(ok)
        self.assertIn("不可写", detail)

    def test_directory_path_reports_false(self):
        ok, _ = core.ConfigStore(self.tmp.name).self_check()
        self.assertFalse(ok)

    def test_add_returns_key_count(self):
        self.assertEqual(self.store.add(1, "a", "sk_123456789"), 1)
        self.assertEqual(self.store.add(1, "b", "sk_123456789"), 2)
        self.assertEqual(self.store.add(1, "a", "sk_987654321"), 2)


# ==================== 命令文本修复 ====================
class TestCommandNormalize(unittest.TestCase):
    def test_plain_command(self):
        self.assertEqual(core.parse_command_tokens("/addkey Cline01 sk_x"), ("addkey", ["Cline01", "sk_x"]))

    def test_ideographic_space_from_chinese_ime(self):
        """中文输入法的全角空格：Telegram 不认，实体会把别名也吞进命令名。"""
        self.assertEqual(
            core.parse_command_tokens("/addkey\u3000Cline01 sk_x"), ("addkey", ["Cline01", "sk_x"])
        )

    def test_zero_width_inside_command_name_is_deleted(self):
        self.assertEqual(
            core.parse_command_tokens("/add\u200bkey Cline-03 sk_x"),
            ("addkey", ["Cline-03", "sk_x"]),
        )

    def test_zero_width_used_as_separator_is_a_space(self):
        """零宽字符也可能被当成命令与参数之间的分隔符。"""
        self.assertEqual(
            core.parse_command_tokens("/addkey\u200bCline01 sk_x", "space"),
            ("addkey", ["Cline01", "sk_x"]),
        )

    def test_candidates_cover_both_meanings(self):
        self.assertIn(("addkey", ["Cline01", "sk_x"]), core.parse_command_candidates("/addkey\u200bCline01 sk_x"))
        self.assertIn(("addkey", ["Cline-03", "sk_x"]), core.parse_command_candidates("/add\u200bkey Cline-03 sk_x"))
        self.assertEqual(len(core.parse_command_candidates("/keys")), 1)

    def test_full_width_slash(self):
        self.assertEqual(core.parse_command_tokens("／status"), ("status", []))
        self.assertEqual(core.parse_command_tokens("／addkey Cline01 sk_x"), ("addkey", ["Cline01", "sk_x"]))

    def test_code_block_wrapper(self):
        self.assertEqual(
            core.parse_command_tokens("`/addkey Cline01 sk_x`"), ("addkey", ["Cline01", "sk_x"])
        )
        self.assertEqual(core.parse_command_tokens("``/status``"), ("status", []))

    def test_bot_mention_is_stripped(self):
        self.assertEqual(core.parse_command_tokens("/addkey@MyBot a sk_x"), ("addkey", ["a", "sk_x"]))

    def test_command_name_is_lowercased(self):
        self.assertEqual(core.parse_command_tokens("/ADDKEY a sk_x")[0], "addkey")

    def test_junk_input(self):
        for text in ("", "   ", "在吗", "``"):
            name, args = core.parse_command_tokens(text)
            self.assertEqual(args, [], text)
        self.assertEqual(core.parse_command_tokens("在吗")[0], "在吗")

    def test_suspicious_chars_are_named(self):
        self.assertEqual(
            core.suspicious_chars("/addkey\u3000Cline01 sk_x"),
            ["全角空格 U+3000"],
        )
        self.assertEqual(core.suspicious_chars("／addkey"), ["全角斜杠 U+FF0F"])
        self.assertEqual(core.suspicious_chars("/addkey Cline01 sk_x"), [])

    def test_suspicious_chars_are_deduplicated(self):
        self.assertEqual(core.suspicious_chars("/a\u200b\u200bb"), ["零宽空格 U+200B"])

    def test_normalize_does_not_touch_the_key(self):
        text = core.normalize_command_text("／addkey\u3000Cline-01 sk_05b0abcdef123456")
        self.assertIn("sk_05b0abcdef123456", text)


# ==================== API Key 清洗 ====================
class TestNormalizeApiKey(unittest.TestCase):
    def test_long_key_is_fine(self):
        """`sk_` + 59 位很正常，长度不设上限。"""
        key = "sk_" + "a" * 59
        self.assertEqual(core.normalize_api_key(key), (key, False))

    def test_even_longer_key_is_fine(self):
        key = "sk_" + "9" * 200
        cleaned, changed = core.normalize_api_key(key)
        self.assertEqual(cleaned, key)
        self.assertFalse(changed)

    def test_zero_width_chars_are_removed(self):
        cleaned, changed = core.normalize_api_key("sk_abc\u200bdef\ufeffghi")
        self.assertEqual(cleaned, "sk_abcdefghi")
        self.assertTrue(changed)

    def test_surrounding_whitespace_is_trimmed_without_flag(self):
        """首尾空白本来就会被 strip，不算"清理掉了不可见字符"。"""
        cleaned, changed = core.normalize_api_key("  sk_abcdefghijk  ")
        self.assertEqual(cleaned, "sk_abcdefghijk")
        self.assertFalse(changed)

    def test_empty_input(self):
        self.assertEqual(core.normalize_api_key(""), ("", False))


# ==================== 日志脱敏 ====================
class TestRedaction(unittest.TestCase):
    def test_telegram_token_in_url_is_redacted(self):
        text = "HTTP Request: POST https://api.telegram.org/bot8123456789:AAHsecretsecretsecret123/getUpdates"
        out = core.redact(text)
        self.assertNotIn("AAHsecretsecretsecret123", out)
        self.assertIn("bot<TOKEN>", out)

    def test_api_key_is_redacted(self):
        out = core.redact("已保存 sk_05b0abcdef123456 成功")
        self.assertNotIn("05b0abcdef123456", out)
        self.assertIn("sk_<KEY>", out)

    def test_plain_text_is_untouched(self):
        self.assertEqual(core.redact("普通日志 123"), "普通日志 123")
        self.assertEqual(core.redact(""), "")

    def test_bare_token_in_exception_message_is_redacted(self):
        """PTB 的 InvalidToken 会把 Token 原样写进异常消息里。"""
        out = core.redact("The token `8999999999:AAFaketokenfortesting1234567890` was rejected")
        self.assertNotIn("AAFaketokenfortesting1234567890", out)
        self.assertIn("bot<TOKEN>", out)

    def test_filter_redacts_traceback_too(self):
        try:
            raise RuntimeError("The token `8999999999:AAFaketokenfortesting1234567890` was rejected")
        except RuntimeError:
            exc_info = sys.exc_info()
        record = logging.LogRecord("t", logging.ERROR, __file__, 1, "boom", (), exc_info)
        core.RedactingFilter().filter(record)
        text = logging.Formatter().format(record)
        self.assertNotIn("AAFaketokenfortesting1234567890", text)
        self.assertIn("bot<TOKEN>", text)

    def test_filter_rewrites_the_record(self):
        record = logging.LogRecord(
            "httpx",
            logging.INFO,
            __file__,
            1,
            "url=%s",
            ("https://api.telegram.org/bot123456789:AAHsecretsecretsecret123/x",),
            None,
        )
        self.assertTrue(core.RedactingFilter().filter(record))
        message = record.getMessage()
        self.assertIn("bot<TOKEN>", message)
        self.assertNotIn("AAHsecretsecretsecret123", message)


# ==================== 版本 ====================
class TestVersion(unittest.TestCase):
    def test_version_is_semver(self):
        self.assertRegex(core.__version__, r"^\d+\.\d+\.\d+$")

    def test_panel_shows_version(self):
        text = render_panel([Snapshot("a", "k")], now=datetime(2026, 9, 25, tzinfo=timezone.utc))
        self.assertIn(f"v{core.__version__}", text)


# ==================== /addkey 参数拆分 ====================
class TestAliasSplit(unittest.TestCase):
    def test_alias_allows_hyphen_dot_underscore(self):
        for alias in ("主账号", "Cline-01", "cline_01", "Cline.01", "01", "a" * 24, "Cline 01"):
            self.assertEqual(core.sanitize_alias(alias), alias, alias)

    def test_alias_rejects_bad_input(self):
        for alias in ("", "   ", "a" * 25, "-bad", "Cline#01", "Cline/01", "Cline:01", "主账号（备用）", "🚀"):
            self.assertIsNone(core.sanitize_alias(alias), alias)

    def test_last_token_is_the_key(self):
        self.assertEqual(
            core.split_alias_and_key(["Cline-01", "sk_1234567890"]),
            ("Cline-01", "sk_1234567890"),
        )

    def test_multiword_alias_is_joined_back(self):
        """Telegram 按空白切参数，多词别名要能拼回来。"""
        self.assertEqual(
            core.split_alias_and_key(["Cline", "01", "sk_1234567890"]),
            ("Cline 01", "sk_1234567890"),
        )

    def test_needs_at_least_two_tokens(self):
        for args in ([], ["onlyalias"], ["", "   "]):
            alias, key = core.split_alias_and_key(args)
            self.assertEqual(key, "", args)
            self.assertIsNone(alias, args)

    def test_whitespace_is_trimmed(self):
        self.assertEqual(
            core.split_alias_and_key([" 主账号 ", " sk_1234567890 "]),
            ("主账号", "sk_1234567890"),
        )

    def test_oversized_joined_alias_is_rejected_key_kept(self):
        alias, key = core.split_alias_and_key(["a" * 20, "b" * 10, "sk_1234567890"])
        self.assertIsNone(alias)
        self.assertEqual(key, "sk_1234567890")


# ==================== Settings ====================
class TestSettings(unittest.TestCase):
    def test_defaults_and_bad_values(self):
        s = Settings.from_env({})
        self.assertEqual(s.api_base, "https://api.cline.bot")
        self.assertEqual(s.max_keys_per_user, 10)
        self.assertFalse(s.demo_mode)
        self.assertTrue(s.is_allowed(123))  # 无白名单 = 全放开

        s = Settings.from_env(
            {
                "MAX_KEYS_PER_USER": "not-a-number",
                "REQUEST_TIMEOUT": "abc",
                "DEMO_MODE": "true",
                "ALLOWED_USER_IDS": "1, 2; x 3",
                "CLINEPASS_API_BASE": "https://example.com/",
            }
        )
        self.assertEqual(s.max_keys_per_user, 10)
        self.assertEqual(s.request_timeout, 12.0)
        self.assertTrue(s.demo_mode)
        self.assertEqual(s.allowed_user_ids, frozenset({1, 2, 3}))
        self.assertEqual(s.api_base, "https://example.com")
        self.assertTrue(s.is_allowed(2))
        self.assertFalse(s.is_allowed(99))

    def test_clamping(self):
        s = Settings.from_env({"HTTP_RETRIES": "99", "MAX_PARALLEL": "0", "MESSAGE_LIMIT": "99999"})
        self.assertEqual(s.http_retries, 5)
        self.assertEqual(s.max_parallel, 1)
        self.assertEqual(s.message_limit, 4096)


if __name__ == "__main__":
    unittest.main()
