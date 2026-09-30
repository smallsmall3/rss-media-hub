"""版本与构建信息测试。

这功能是为了解决一个真实困惑：镜像 tag 是 v1.0.2，界面却显示 v1.0.0
（因为版本号常量没跟着改），让人以为升级没生效。
所以界面必须能显示"版本号 + 构建提交"，且要在缺构建信息时优雅降级。
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from app import __version__
from app.version import BuildInfo


class BuildInfoTest(unittest.TestCase):
    def _clean(self):
        return mock.patch.dict(os.environ, {}, clear=False)

    def test_version_matches_package(self):
        info = BuildInfo.current()
        self.assertEqual(info.version, __version__)

    def test_absent_build_vars_degrade_gracefully(self):
        """源码直接跑（没有构建信息）时，不能报错，也不能显示奇怪的占位符。"""
        with mock.patch.dict(os.environ, {}, clear=False):
            for key in ("RMH_BUILD_COMMIT", "RMH_BUILD_TIME", "RMH_BUILD_TAG"):
                os.environ.pop(key, None)
            info = BuildInfo.current()
            self.assertEqual(info.commit, "")
            self.assertEqual(info.display(), f"v{__version__}")
            self.assertNotIn("None", info.display())
            self.assertNotIn("'", info.display())

    def test_reads_build_vars(self):
        with mock.patch.dict(
            os.environ,
            {
                "RMH_BUILD_COMMIT": "ae1c682a2b2e",
                "RMH_BUILD_TIME": "2026-09-30T02:20:00Z",
                "RMH_BUILD_TAG": "v1.0.3",
            },
        ):
            info = BuildInfo.current()
            self.assertEqual(info.commit, "ae1c682a2b2e")   # 不再截断（<12 位保持原样）
            self.assertEqual(info.tag, "v1.0.3")
            self.assertIn("v" + __version__, info.display())
            self.assertIn("ae1c682", info.display())

    def test_long_commit_truncated(self):
        with mock.patch.dict(os.environ, {"RMH_BUILD_COMMIT": "a" * 40}):
            self.assertEqual(len(BuildInfo.current().commit), 12)

    def test_display_with_time(self):
        with mock.patch.dict(os.environ, {"RMH_BUILD_TIME": "2026-09-30T02:20:00Z"}):
            info = BuildInfo.current()
            short = info.display(full=True)
            self.assertIn("09-30", short, "带时间时应该显示 MM-DD")
            # 默认显示不带时间，界面那一行要短
            self.assertNotIn("09-30", info.display())

    def test_bad_time_does_not_crash(self):
        with mock.patch.dict(os.environ, {"RMH_BUILD_TIME": "不是时间"}):
            info = BuildInfo.current()
            self.assertIsInstance(info.short_time, str)

    def test_timezone_conversion(self):
        """构建时间是 UTC，显示要转成本地时区。"""
        with mock.patch.dict(os.environ, {"RMH_BUILD_TIME": "2026-09-30T02:20:00Z"}):
            info = BuildInfo.current()
            self.assertTrue(info.short_time)
            # 不管本地时区是什么，格式都该是 MM-DD HH:MM
            self.assertRegex(info.short_time, r"^\d{2}-\d{2} \d{2}:\d{2}$")

    def test_to_dict_shape(self):
        with mock.patch.dict(
            os.environ,
            {"RMH_BUILD_COMMIT": "abc12345", "RMH_BUILD_TIME": "2026-09-30T02:20:00Z"},
        ):
            d = BuildInfo.current().to_dict()
            for key in ("version", "commit", "built_at", "built_at_short", "tag", "display", "display_full"):
                self.assertIn(key, d)
            self.assertEqual(d["commit"], "abc12345")
            self.assertTrue(d["display"].startswith("v"))


if __name__ == "__main__":
    unittest.main()
