"""版本与构建信息。

为什么单独搞一个模块：界面上要显示"当前运行的到底是哪次构建"。
只显示 `__version__` 是不够的 —— 那个值在源码里，改了代码忘了改它，
界面就一直显示旧版本号，你会以为镜像没更新成功（真实踩过这个坑：
镜像 tag 是 v1.0.2，界面却显示 v1.0.0）。

所以除了版本号，再带上构建时注入的 git 提交与时间：
    v1.0.3 · ae1c682 · 09-30 10:20

构建信息由 CI 通过 --build-arg 注入，本地源码直接跑时为空（显示纯版本号）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone

from . import __version__


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


@dataclass
class BuildInfo:
    version: str = __version__
    commit: str = ""       # git 短哈希
    built_at: str = ""     # ISO 时间（构建时注入）
    tag: str = ""          # 镜像 tag，例如 v1.0.3

    @classmethod
    def current(cls) -> "BuildInfo":
        return cls(
            version=__version__,
            commit=_env("RMH_BUILD_COMMIT")[:12],
            built_at=_env("RMH_BUILD_TIME"),
            tag=_env("RMH_BUILD_TAG"),
        )

    @property
    def short_time(self) -> str:
        """把 ISO 时间压成 MM-DD HH:MM（本地时区），方便塞进界面。"""
        if not self.built_at:
            return ""
        try:
            text = self.built_at.replace("Z", "+00:00")
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone().strftime("%m-%d %H:%M")
        except ValueError:
            return self.built_at[:16]

    def display(self, *, full: bool = False) -> str:
        """界面上显示的一行。

        full=True 时带上构建时间（给「设置」或悬浮提示用）；
        默认只给"版本 · 提交"，够判断新旧了。
        """
        parts = [f"v{self.version}"]
        if self.commit:
            parts.append(self.commit)
        if full and self.short_time:
            parts.append(self.short_time)
        return " · ".join(parts)

    def to_dict(self) -> dict[str, str]:
        return {
            "version": self.version,
            "commit": self.commit,
            "built_at": self.built_at,
            "built_at_short": self.short_time,
            "tag": self.tag,
            "display": self.display(),
            "display_full": self.display(full=True),
        }
