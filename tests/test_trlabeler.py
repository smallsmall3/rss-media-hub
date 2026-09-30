"""Transmission 站点标签打标模块测试。

不依赖真实 Transmission：RPC 用假客户端，核心逻辑（域名匹配、映射加载、
标签收集、只增不删）全部离线验证。
"""

from __future__ import annotations

import contextlib
import itertools
import os
import shutil
import unittest
from pathlib import Path
from typing import Any

from app.trlabeler import (
    LabelMappings,
    TransmissionClient,
    collect_plan,
    load_mappings,
    match_label,
    scan_and_label,
    tracker_host,
)

_counter = itertools.count()


@contextlib.contextmanager
def temp_dir():
    base = Path(os.environ.get("RMH_TEST_TMP") or Path(__file__).resolve().parent / ".tmp")
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"trlabel-{os.getpid()}-{next(_counter)}"
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class FakeTr:
    """假 Transmission RPC：记录调用、返回固定数据。"""

    def __init__(self, torrents: list[dict[str, Any]]) -> None:
        self.torrents = torrents
        self.session = {"version": "4.0.5", "rpc-version": 17}
        self.sets: list[tuple[list[int], list[str]]] = []
        self.calls: list[str] = []

    async def aclose(self) -> None:
        pass

    async def call(self, method: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append(method)
        args = arguments or {}
        if method == "session-get":
            return self.session
        if method == "torrent-get":
            fields = args.get("fields", [])
            if fields and set(fields) == {"id", "labels"}:  # 复核阶段
                return {"torrents": [{"id": t["id"], "labels": t.get("labels") or []} for t in self.torrents]}
            return {"torrents": self.torrents}
        if method == "torrent-set":
            self.sets.append((list(args.get("ids", [])), list(args.get("labels", []))))
            # 模拟写入：更新内部状态，供复核
            for tid in args.get("ids", []):
                for t in self.torrents:
                    if t["id"] == tid:
                        t["labels"] = list(args.get("labels", []))
            return {}
        return {}


def make_torrent(tid: int, trackers: list[str], labels: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": tid,
        "name": f"torrent-{tid}",
        "labels": labels or [],
        "trackers": [{"announce": u} for u in trackers],
    }


class HostAndMatchTest(unittest.TestCase):
    def test_tracker_host(self):
        self.assertEqual(tracker_host("http://t.ubits.club/announce?passkey=x"), "t.ubits.club")
        self.assertEqual(tracker_host("https://audiences.me/announce.php"), "audiences.me")
        self.assertIsNone(tracker_host(""))
        self.assertIsNone(tracker_host("not-a-url"))

    def test_match_exact_and_subdomain(self):
        pairs = [("ubits.club", "站点/ubits"), ("audiences.me", "站点/audiences")]
        self.assertEqual(match_label("ubits.club", pairs), "站点/ubits")
        self.assertEqual(match_label("t.ubits.club", pairs), "站点/ubits")  # 子域命中
        self.assertEqual(match_label("audiences.me", pairs), "站点/audiences")
        self.assertIsNone(match_label("example.com", pairs))


class LoadMappingsTest(unittest.TestCase):
    def test_load(self):
        with temp_dir() as root:
            p = root / "mappings.txt"
            p.write_text(
                "# comment\n"
                "ubits.club=站点/ubits\n"
                "m-team.cc=站点/m-team\n"
                "\n"
                "badline\n"
                "=emptydomain\n"
                "emptylabel=\n",
                encoding="utf-8",
            )
            pairs = load_mappings(p)
            self.assertEqual(pairs, [("ubits.club", "站点/ubits"), ("m-team.cc", "站点/m-team")])

    def test_missing_file(self):
        with temp_dir() as root:
            self.assertEqual(load_mappings(root / "nope.txt"), [])


class CollectPlanTest(unittest.TestCase):
    def test_adds_labels_without_overwriting(self):
        mappings = LabelMappings([("ubits.club", "站点/ubits"), ("audiences.me", "站点/audiences")])
        torrents = [
            make_torrent(1, ["http://t.ubits.club/announce"], labels=["PT"]),
            make_torrent(2, ["http://audiences.me/announce"], labels=[]),
        ]
        plan = collect_plan(torrents, mappings, auto_pt=True)
        # torrent 1：已有 PT，追加 站点/ubits
        self.assertEqual(plan.plan[1], ["PT", "站点/ubits"])
        # torrent 2：无 PT，auto_pt 补 PT 再追加 站点/audiences
        self.assertEqual(plan.plan[2], ["PT", "站点/audiences"])
        self.assertEqual(plan.changes, 2)

    def test_unmapped_skipped(self):
        mappings = LabelMappings([("ubits.club", "站点/ubits")])
        torrents = [
            make_torrent(1, ["http://unknown.example/announce"], labels=[]),
            make_torrent(2, ["http://t.ubits.club/announce"], labels=[]),
        ]
        plan = collect_plan(torrents, mappings, auto_pt=True)
        self.assertEqual(plan.plan, {2: ["PT", "站点/ubits"]})
        self.assertEqual(plan.skipped_unmapped, 1)
        self.assertIn("unknown.example", plan.unmapped)

    def test_no_pt(self):
        mappings = LabelMappings([("ubits.club", "站点/ubits")])
        torrents = [make_torrent(1, ["http://t.ubits.club/announce"], labels=[])]
        plan = collect_plan(torrents, mappings, auto_pt=False)
        self.assertEqual(plan.plan[1], ["站点/ubits"])

    def test_no_change_when_labels_match(self):
        mappings = LabelMappings([("ubits.club", "站点/ubits")])
        torrents = [make_torrent(1, ["http://t.ubits.club/announce"], labels=["PT", "站点/ubits"])]
        plan = collect_plan(torrents, mappings, auto_pt=True)
        self.assertEqual(plan.plan, {}, "标签已是最新，无需改动")


class ScanAndLabelTest(unittest.IsolatedAsyncioTestCase):
    async def test_apply_writes_and_verifies(self):
        mappings = LabelMappings([("ubits.club", "站点/ubits")])
        torrents = [make_torrent(1, ["http://t.ubits.club/announce"], labels=[])]
        fake = FakeTr(torrents)
        client = TransmissionClient("http://tr.example/rpc")
        client._client = None  # 让 call 走假客户端；这里直接用 FakeTr 替换
        # 直接用 FakeTr 充当客户端
        plan = await scan_and_label(fake, mappings, apply=True)  # type: ignore[arg-type]
        self.assertEqual(plan.changes, 1)
        self.assertEqual(fake.sets, [([1], ["PT", "站点/ubits"])])
        # 复核后 torrent 1 的 labels 已更新
        self.assertEqual(torrents[0]["labels"], ["PT", "站点/ubits"])

    async def test_dry_run_no_write(self):
        mappings = LabelMappings([("ubits.club", "站点/ubits")])
        torrents = [make_torrent(1, ["http://t.ubits.club/announce"], labels=[])]
        fake = FakeTr(torrents)
        plan = await scan_and_label(fake, mappings, apply=False)  # type: ignore[arg-type]
        self.assertEqual(plan.changes, 1)
        self.assertEqual(fake.sets, [], "dry-run 不应调用 torrent-set")
        self.assertEqual(torrents[0]["labels"], [], "dry-run 不应改数据")


class TransmissionClientTest(unittest.IsolatedAsyncioTestCase):
    async def test_not_configured_raises(self):
        client = TransmissionClient("")
        with self.assertRaises(Exception):
            await client.call("session-get")


if __name__ == "__main__":
    unittest.main()
