"""从发布标题里提取"看点"：分辨率、HDR、编码、来源、音轨、字幕、压制组。

为什么要单独做这件事：PT 站推送的价值就在于"这一版是什么规格"，
原生标题又长又乱（`[中字]某剧.S01E05.2160p.WEB-DL.HDR.HEVC.DDP5.1-XXX`），
直接丢给用户等于没整理。这里把它拆成可一眼扫过的标签。

设计原则：**只认明确的写法，认不出就不写**——宁缺勿错，
不然用户按标签过滤时会发现"标了 2160p 其实是 1080p"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 顺序有意义：从高到低，取第一个命中的
RESOLUTION_PATTERNS: list[tuple[str, str]] = [
    ("4320p", r"(?<![0-9])4320p(?![0-9])"),
    ("2160p", r"(?<![0-9])2160p(?![0-9])"),
    ("2160p", r"(?<![0-9])4k(?![0-9a-z])"),
    ("1440p", r"(?<![0-9])1440p(?![0-9])"),
    ("1080p", r"(?<![0-9])1080p(?![0-9])"),
    ("1080i", r"(?<![0-9])1080i(?![0-9])"),
    ("720p", r"(?<![0-9])720p(?![0-9])"),
    ("576p", r"(?<![0-9])576p(?![0-9])"),
    ("480p", r"(?<![0-9])480p(?![0-9])"),
]

HDR_PATTERNS: list[tuple[str, str]] = [
    ("杜比视界", r"dolby[\s._-]?vision|(?<![a-z])dv(?![a-z])"),
    ("HDR10+", r"hdr10\+|hdr10plus"),
    ("HDR10", r"hdr10(?![+a-z])"),
    ("HDR", r"(?<![a-z])hdr(?![0-9a-z])"),
]

VIDEO_CODEC_PATTERNS: list[tuple[str, str]] = [
    ("HEVC", r"(?<![a-z])(?:hevc|x265|h\.?265)(?![0-9a-z])"),
    ("AVC", r"(?<![a-z])(?:x264|h\.?264|avc)(?![0-9a-z])"),
    ("AV1", r"(?<![a-z])av1(?![0-9a-z])"),
    ("MPEG2", r"mpeg[\s._-]?2"),
    ("VP9", r"(?<![a-z])vp9(?![0-9a-z])"),
]

AUDIO_PATTERNS: list[tuple[str, str]] = [
    ("TrueHD", r"truehd"),
    ("Atmos", r"atmos"),
    ("DTS-X", r"dts[\s._-]?x"),
    ("DTS-HD", r"dts[\s._-]?hd"),
    ("DTS", r"(?<![a-z])dts(?![0-9a-z-])"),
    ("DDP", r"ddp[\s._-]?[0-9]?|eac3|e-?ac-?3"),
    ("AC3", r"(?<![a-z])ac3(?![0-9a-z])"),
    ("FLAC", r"(?<![a-z])flac(?![0-9a-z])"),
    ("AAC", r"(?<![a-z])aac(?![0-9a-z])"),
]

SOURCE_PATTERNS: list[tuple[str, str]] = [
    ("原盘", r"blu[\s._-]?ray[\s._-]?(?:disk|disc)|(?<![a-z])bdmv(?![a-z])|remux"),
    ("BluRay", r"blu[\s._-]?ray|(?<![a-z])bdrip(?![a-z])|(?<![a-z])bd(?![a-z])"),
    ("WEB-DL", r"web[\s._-]?dl"),
    ("WEBRip", r"web[\s._-]?rip"),
    ("WEB", r"(?<![a-z])web(?![0-9a-z])"),
    ("HDTV", r"hdtv"),
    ("DVDRip", r"dvd[\s._-]?rip|dvdrip"),
    ("HDDVD", r"hddvd"),
    ("TVRip", r"tv[\s._-]?rip"),
]

LANG_PATTERNS: list[tuple[str, str]] = [
    ("国语", r"国语|国配|普通话"),
    ("粤语", r"粤语|粵語|广东话"),
    ("中字", r"中字|简中|繁中|简繁|中文字幕|chs|cht|chi|zho"),
    ("双语", r"双语|雙語|简英|简日"),
    ("日语", r"日语|日語|jpn|japanese"),
    ("英语", r"(?<![a-z])eng(?![a-z])|english"),
]

# 压制组：标题结尾 "-GROUP"（PT 站最常见）
RE_GROUP_TAIL = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._]{0,20})$")
# 常见但其实是规格的词，不能当压制组
_NOT_A_GROUP = {
    "2160p", "1080p", "1080i", "720p", "576p", "480p", "4k", "hdr", "hdr10", "hdr10+",
    "web", "webdl", "bluray", "bdrip", "brrip", "hdtv", "hdrip", "dvdrip", "webrip",
    "hevc", "x265", "x264", "avc", "av1", "aac", "ac3", "eac3", "dts", "dtshd", "ddp",
    "truehd", "atmos", "flac", "mp3", "repack", "proper", "v2", "internal", "complete",
    "10bit", "8bit", "hq", "ma", "dd", "ddp5", "5", "1", "7", "2", "0",
}
# 这些词里含连字符但不是"组名分隔符"，先归一化掉
_RE_NORMALIZE_HYPHEN_TERMS = [
    (re.compile(r"web[\s._-]?dl", re.IGNORECASE), "WEBDL"),
    (re.compile(r"web[\s._-]?rip", re.IGNORECASE), "WEBRIP"),
    (re.compile(r"blu[\s._-]?ray", re.IGNORECASE), "BLURAY"),
    (re.compile(r"dvd[\s._-]?rip", re.IGNORECASE), "DVDRIP"),
    (re.compile(r"dts[\s._-]?hd", re.IGNORECASE), "DTSHD"),
    (re.compile(r"dts[\s._-]?x", re.IGNORECASE), "DTSX"),
    (re.compile(r"h\.?26([45])", re.IGNORECASE), r"H26\1"),
    (re.compile(r"hdr10\+", re.IGNORECASE), "HDR10PLUS"),
]


def extract_group(title: str) -> str:
    """提取压制组。

    从右边往左试每个 "-" 切出来的尾巴，跳过其实是规格的那些
    （`Show S01E01-1080p` 的 1080p、`Show-HEVC` 的 HEVC 都不是组名）。

    先归一化 `WEB-DL` / `DTS-HD` 这类"自带连字符的规格"，
    否则 `WEB-DL-FGT` 会被切成 `DL-FGT`。
    """
    text = (title or "").strip()
    if not text:
        return ""
    for pattern, replacement in _RE_NORMALIZE_HYPHEN_TERMS:
        text = pattern.sub(replacement, text)

    parts = re.split(r"[-–]", text)
    if len(parts) < 2:
        return ""
    # 从最右边开始找第一个像组名的片段
    for chunk in reversed(parts[1:]):
        candidate = chunk.strip().strip("._-")
        if not candidate:
            continue
        if candidate.lower() in _NOT_A_GROUP:
            continue
        if candidate.isdigit():
            continue
        if not RE_GROUP_TAIL.match(candidate):
            continue
        return candidate
    return ""


@dataclass
class ReleaseTags:
    """一条发布的可读规格。字段为空字符串表示"没识别出来"。"""

    resolution: str = ""
    hdr: str = ""
    video: str = ""
    audio: str = ""
    source: str = ""
    langs: list[str] = field(default_factory=list)
    group: str = ""

    @property
    def is_empty(self) -> bool:
        return not any([self.resolution, self.hdr, self.video, self.audio, self.source, self.langs, self.group])

    def badges(self, *, limit: int = 5) -> list[str]:
        """给推送/界面用的一行标签，按重要性排序。"""
        out: list[str] = []
        if self.resolution:
            out.append(self.resolution)
        if self.hdr:
            out.append(self.hdr)
        if self.video:
            out.append(self.video)
        if self.source:
            out.append(self.source)
        if self.audio:
            out.append(self.audio)
        for lang in self.langs:
            out.append(lang)
        if self.group:
            out.append(self.group)
        return out[:limit]

    def badge_line(self, sep: str = " · ") -> str:
        return sep.join(self.badges())

    def to_dict(self) -> dict:
        return {
            "resolution": self.resolution,
            "hdr": self.hdr,
            "video": self.video,
            "audio": self.audio,
            "source": self.source,
            "langs": list(self.langs),
            "group": self.group,
        }

    def quality_rank(self) -> int:
        """用于"同一条目取最好的版本"排序：分辨率优先，其次 HDR、来源。"""
        order = {"4320p": 8, "2160p": 7, "1440p": 6, "1080p": 5, "1080i": 4, "720p": 3, "576p": 2, "480p": 1}
        rank = order.get(self.resolution, 0) * 100
        if self.hdr:
            rank += 30 if self.hdr == "杜比视界" else (20 if "HDR10" in self.hdr else 10)
        if self.source in {"原盘", "BluRay"}:
            rank += 5
        elif self.source in {"WEB-DL", "WEBRip", "WEB"}:
            rank += 3
        return rank


def _first_match(title: str, patterns: list[tuple[str, str]]) -> str:
    for label, pattern in patterns:
        if re.search(pattern, title, re.IGNORECASE):
            return label
    return ""


def _all_matches(title: str, patterns: list[tuple[str, str]], limit: int = 3) -> list[str]:
    out: list[str] = []
    for label, pattern in patterns:
        if re.search(pattern, title, re.IGNORECASE):
            out.append(label)
            if len(out) >= limit:
                break
    return out


def describe_release(title: str) -> ReleaseTags:
    """从发布标题解析规格。识别不出就留空，不猜。"""
    if not title:
        return ReleaseTags()
    text = title.strip()
    return ReleaseTags(
        resolution=_first_match(text, RESOLUTION_PATTERNS),
        hdr=_first_match(text, HDR_PATTERNS),
        video=_first_match(text, VIDEO_CODEC_PATTERNS),
        audio=_first_match(text, AUDIO_PATTERNS),
        source=_first_match(text, SOURCE_PATTERNS),
        langs=_all_matches(text, LANG_PATTERNS),
        group=extract_group(text),
    )


# --------------------------------------------------------------------------
# 内容类型判断（决定推送用什么图标）
# --------------------------------------------------------------------------

RE_MOVIE_YEAR = re.compile(r"(?:^|[.\s\[(])((?:19|20)\d{2})(?:[.\s\])]|$)")
RE_MUSIC = re.compile(r"(?<![a-z])(flac|mp3|dsd|ape|wav|24bit|hi-?res)(?![a-z])", re.IGNORECASE)
RE_PACK = re.compile(r"全\s*\d+\s*[集话話]|合集|全集|complete|batch|season[\s._-]?\d+[\s._-]?complete", re.IGNORECASE)
RE_SOFTWARE = re.compile(r"(?<![a-z])(?:iso|dmg|exe|msi|apk|破解|绿色版)(?![a-z])", re.IGNORECASE)
RE_BOOK = re.compile(r"(?<![a-z])(?:epub|mobi|pdf|azw3|有声书|audiobook)(?![a-z])", re.IGNORECASE)


def classify_item(title: str, *, has_episode: bool = False) -> tuple[str, str]:
    """返回 (类型, emoji)。用于推送时的视觉分组。"""
    text = title or ""
    if RE_MUSIC.search(text) and not has_episode:
        return "音乐", "🎵"
    if RE_SOFTWARE.search(text):
        return "软件", "💿"
    if RE_BOOK.search(text):
        return "图书", "📚"
    if RE_PACK.search(text):
        return "合集", "📦"
    if has_episode:
        return "剧集", "📺"
    if RE_MOVIE_YEAR.search(text):
        return "电影", "🎬"
    return "资源", "📄"
