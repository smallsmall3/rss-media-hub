"""从 PT 发布标题里提取「能拿去搜索的片名」和年份。

为什么需要它：feed 模式的条目只有一条标题（`超新星.2020.1080p.BluRay.x264-GROUP`），
没有 TMDB ID。想给每条推送配上海报，就必须先把它还原成「超新星」+「2020」
才能去 TMDB 搜。

难点在于 PT 标题用点号当分隔符，而片名本身也可能带点号
（`S.W.A.T.`、`3.10.to.Yuma`、`2001.A.Space.Odyssey`）。
所以不能简单地"从第一个点切开"，而要**从右往左**剥掉已知的规格段，
剩下的才是片名——这就是本模块的核心策略。

设计原则与 release.py 一致：**宁缺勿错**。
拿不准时不乱切，宁可搜不到海报，也不要搜出一张完全不相干的图。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# --------------------------------------------------------------------------
# 规格关键词：出现在标题里就说明"从这里开始不是片名了"
# --------------------------------------------------------------------------

# 分辨率 / 来源 / 编码 / 音轨 / 其他技术标记（与 release.py 的口径保持一致）
_TECH_WORDS = {
    # 分辨率
    "2160p", "1080p", "1080i", "720p", "576p", "480p", "4k", "8k", "uhd", "fhd", "hd",
    # 来源
    "bluray", "blu-ray", "bdrip", "brrip", "bdremux", "remux", "webrip", "web-dl", "webdl",
    "web", "hdtv", "dvdrip", "dvd", "hdrip", "tvrip", "uhdbd", "bd",
    # 编码
    "x264", "x265", "h264", "h265", "h.264", "h.265", "hevc", "avc", "av1", "xvid", "divx", "vp9",
    # 音轨
    "aac", "ac3", "eac3", "ddp", "dd", "dts", "dts-hd", "dtshd", "truehd", "atmos", "flac",
    "lpcm", "pcm", "mp3", "opus", "ddp5", "dd5",
    # HDR / 色彩
    "hdr", "hdr10", "hdr10+", "dv", "dolby", "vision", "sdr", "hlg", "10bit", "8bit",
    # 其他常见标记
    "repack", "proper", "internal", "complete", "limited", "remastered", "extended",
    "uncut", "unrated", "criterion", "imax", "ma", "multi", "dual", "chs", "cht", "gb", "big5",
    "简", "繁", "简繁", "中字", "国语", "粤语", "双语", "英语", "日语", "无字",
    "subs", "sub", "subtitle", "subtitles", "audio", "rip", "full", "batch", "合集",
    # 季集标记
    "season", "s", "ep", "episode", "e",
}

# 匹配"整段就是技术标记"的正则（用于从右往左剥）
_TECH_TOKEN = re.compile(r"^[a-z0-9][a-z0-9\-+.]*$")

# 剧集标记：S01E05 / S01 / E05 / EP05 / 第05集
_EPISODE_PATTERNS = [
    re.compile(r"(?i)\bS\d{1,2}E\d{1,4}\b"),
    re.compile(r"(?i)\bS\d{1,2}\b(?=\s|$|[.\-_])"),
    re.compile(r"(?i)\bE(?:P)?\d{1,4}\b"),
    re.compile(r"第\s*\d{1,4}\s*[集话話]"),
]

# 合集/多季标记：S01-S03 / 1-3季
_RANGE_PATTERN = re.compile(r"(?i)\bS\d{1,2}\s*[-~]\s*S?\d{1,2}\b")

# 年份：1900-2099，且必须作为独立段落出现（避免把 2001 这种片名年份切掉）
_YEAR_TOKEN = re.compile(r"^(19\d{2}|20\d{2})$")

# 画质/字幕组常见的方括号标签：[更多资源] [中字] [GROUP]
# 注意要容忍"括号里套括号"：`[动漫(Animations)]` 这种，
# 简单的 `[^\]】)）]*` 会在内层 `)` 处提前结束，匹配不到整个标签，
# 结果在片名前留下一个 `]`（真实反馈里踩到过）。
_BRACKET_TAG = re.compile(r"[\[【][^\[\]【】]*[\]】]|[（(][^（()）]*[）)]")

# 结尾的压制组：-GROUP（前面必须有连字符）
_TRAILING_GROUP = re.compile(r"-[A-Za-z0-9][A-Za-z0-9._-]{0,24}$")

# 流媒体平台名：出现在标题里但不是片名的一部分
# 注意 `tv` / `tv+`：`Apple TV+` 会被切成 Apple / TV+，
# 而 `.strip("-+")` 会把 `TV+` 变成 `tv`，所以两种写法都要登记。
_PLATFORM_WORDS = {
    "apple", "appletv", "appletv+", "itunes", "netflix", "nf", "amzn", "amazon", "prime",
    "disney", "disney+", "hulu", "hbo", "max", "hbomax", "paramount", "peacock",
    "atvp", "atv", "ip", "iqiyi", "youku", "bilibili", "viu", "tv", "tv+", "series",
}

# 片名与技术规格的分界标记（整段匹配才算）
_BOUNDARY_PATTERNS = [
    re.compile(r"(?i)^S\d{1,2}E\d{1,4}$"),       # S01E03
    re.compile(r"(?i)^S\d{1,2}$"),               # S01
    re.compile(r"(?i)^E(?:P)?\d{1,4}$"),         # E03 / EP03
    re.compile(r"^第\d{1,4}[集话話季]$"),          # 第3集
    re.compile(r"(?i)^S\d{1,2}[-~]S?\d{1,2}$"),  # S01-S03
    re.compile(r"^\d{1,2}季$"),                   # 3季
]

# 带点号的技术串：H.265 / H.264 / DDP5.1 / TrueHD.7.1 / DTS-HD.MA
# 必须在按点号切段**之前**整段删掉，否则会被拆成 H 和 265 这种碎片。
_DOTTED_TECH = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:"
    r"h\.?26[45]"
    r"|x\.?26[45]"
    r"|(?:dd|ddp|dts|aac|ac3|eac3|truehd|atmos|flac|lpcm|mp3)[.\-]?(?:hd|ma|plus|\d+(?:\.\d+)?)?"
    r"|(?:uhd|bd|web)[.\-]?(?:dl|rip|remux)"
    r")(?![A-Za-z0-9])"
)


@dataclass
class ParsedTitle:
    """解析结果。拿不准的字段留空，由调用方决定怎么办。"""

    title: str = ""          # 干净的片名（可直接拿去搜 TMDB）
    year: int | None = None  # 年份
    season: int | None = None
    episode: int | None = None
    raw: str = ""            # 原始标题
    confident: bool = False  # 是否有把握（标题看起来像"片名.年份.规格"）

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "year": self.year,
            "season": self.season,
            "episode": self.episode,
            "confident": self.confident,
        }

    def __str__(self) -> str:
        bits = [self.title or "(未识别)"]
        if self.year:
            bits.append(f"({self.year})")
        if self.season is not None and self.episode is not None:
            bits.append(f"S{self.season:02d}E{self.episode:02d}")
        return " ".join(bits)


def _strip_episode_marks(text: str) -> tuple[str, int | None, int | None]:
    """摘掉季集标记，返回 (剩余文本, 季, 集)。"""
    season = episode = None
    m = re.search(r"(?i)\bS(\d{1,2})E(\d{1,4})\b", text)
    if m:
        season, episode = int(m.group(1)), int(m.group(2))
        text = text[: m.start()] + " " + text[m.end() :]
        return text, season, episode
    m = re.search(r"第\s*(\d{1,4})\s*[集话話]", text)
    if m:
        episode = int(m.group(1))
        text = text[: m.start()] + " " + text[m.end() :]
        return text, season, episode
    m = re.search(r"(?i)\bE(?:P)?(\d{1,4})\b", text)
    if m:
        episode = int(m.group(1))
        text = text[: m.start()] + " " + text[m.end() :]
    return text, season, episode


def _is_tech_token(cleaned: str, low: str) -> bool:
    """这一段是不是纯规格标记（可以安全丢掉）。

    刻意**不认为**是规格的：
      * 单字母（`S.W.A.T.` 的 S/W/A/T）
      * 纯数字（`3.10.to.Yuma` 的 3 和 10）
      * 单个汉字
    因为把它们当规格剥掉，会把片名吃掉。
    """
    if low in _TECH_WORDS:
        return True
    # 分辨率：2160 / 1080 / 720 这种裸数字（有的站不写 p）
    if re.match(r"^(?:4320|2160|1440|1080|900|720|576|480)$", low):
        return True
    # 组合技术段：ddp5 / ddp5.1 / truehd7.1 / 10bit / 2ch / 24fps
    if re.match(r"^(?:dd|ddp|dts|aac|ac3|eac3|truehd|atmos|flac)\d", low):
        return True
    if re.match(r"^\d+(?:bit|ch|kbps|fps|mbps)$", low):
        return True
    # 点号被切开后的残留：`DDP.5.1` → DDP / 5 / 1，其中的数字片段
    if re.match(r"^\d(?:\.\d)?$", low):
        return True
    # 流媒体平台名（常出现在标题里，但不是片名）
    if low in _PLATFORM_WORDS:
        return True
    # 站点常见的地区/语言短标记
    if low in {"hk", "tw", "cn", "jp", "kr", "us", "uk", "fr", "de"}:
        return True
    return False


def _is_boundary_token(cleaned: str) -> bool:
    """这一段是不是「片名与技术规格的分界」。

    最可靠的分界是年份，其次是季集标记（`S01` / `S01E03` / `第3集`）。
    命中就说明"从这里往右全是规格"，可以停止剥离了。

    为什么需要季集当分界：剧集标题经常没有年份
    （`[TV Series]Brothers S01 2160p Apple TV+ WEB-DL ...`），
    没有分界的话会把 `S01 2160p Apple TV` 全当成片名。
    """
    if not cleaned:
        return False
    if _YEAR_TOKEN.match(cleaned):
        return True
    for pattern in _BOUNDARY_PATTERNS:
        if pattern.match(cleaned):
            return True
    return False


def parse_release_title(raw: str) -> ParsedTitle:
    """把一条发布标题解析成「片名 + 年份 + 季集」。

    策略：
      1. 去掉方括号标签（[中字] 之类）和结尾的 -压制组
      2. 摘掉季集标记、把 H.265 这类带点的技术串整段删掉
      3. 用「点/空格/下划线」切成段
      4. **从右往左**剥掉技术段落，剩下的拼成片名
      5. 途中遇到独立年份段就记下来，并从片名里去掉

    注意几个刻意**不剥离**的情况（否则会把片名吃掉）：
      * 单字母段：`S.W.A.T.` 里的 S / W / A / T
      * 纯数字段：`3.10.to.Yuma` 里的 3 和 10
      * 单个中文字：`流浪地球2` 若被切成 `流浪地球` + `2`，那个 2 要留
    """
    result = ParsedTitle(raw=raw)
    text = (raw or "").strip()
    if not text:
        return result

    # 1. 方括号标签 + 结尾压制组
    text = _BRACKET_TAG.sub(" ", text)
    # 季范围（S01-S03）整体丢掉
    text = _RANGE_PATTERN.sub(" ", text)
    text = _TRAILING_GROUP.sub("", text)

    # 2. 季集 + 带点的技术串（必须在切段之前处理，否则 H.265 会被拆成 H 和 265）
    text, season, episode = _strip_episode_marks(text)
    result.season, result.episode = season, episode
    text = _DOTTED_TECH.sub(" ", text)

    # 3. 切段：点号、下划线、连续空格都算分隔符
    parts = [p for p in re.split(r"[._\s]+", text) if p]

    # 4. 两阶段剥离。
    #    阶段一：从右往左剥规格，直到遇到"分界标记"（年份 / 季集）。
    #    阶段二：把剩下的（分界左边的）全部当作片名。
    #
    #    为什么季集也算分界：剧集标题经常没有年份，例如
    #      [TV Series]Brothers S01 2160p Apple TV+ WEB-DL DDP.5.1 Atmos HDR10+ H.265-CHD
    #    只认年份的话，会把「Brothers S01 2160p Apple TV」整串当片名，
    #    拿这个去搜 TMDB 必然搜不到（这是真实反馈里踩到的坑）。
    idx = len(parts) - 1
    year: int | None = None
    saw_tech = bool(season is not None or episode is not None)
    while idx >= 0:
        cleaned = parts[idx].strip("-+")
        if not cleaned:
            idx -= 1
            continue
        low = cleaned.lower()
        if _is_boundary_token(cleaned):
            # 分界标记本身不算片名
            if year is None and _YEAR_TOKEN.match(cleaned):
                year = int(cleaned)
            saw_tech = True
            idx -= 1
            break
        if _is_tech_token(cleaned, low):
            saw_tech = True
            idx -= 1
            continue
        break  # 遇到不像规格的段落：片名从这里开始

    # 阶段二：idx 及其左边全是片名
    keep = [p.strip("-+") for p in parts[: idx + 1] if p.strip("-+")]
    if not keep:
        return result

    # 阶段三：年份可能落在"片名区"里（例如 `... Shi 2026 S01E32 ...`，
    # 年份在季号左边）。从右往左找最后一个像年份的段当作发行年份，
    # 并从片名里去掉 —— 但保留最左边那个（`2001 A Space Odyssey` 的 2001）。
    if year is None:
        for pos in range(len(keep) - 1, 0, -1):
            if _YEAR_TOKEN.match(keep[pos]):
                year = int(keep.pop(pos))
                saw_tech = True
                break

    # 阶段四：年份被摘掉后，它的左边可能还留着季集标记。
    # 例如 `Another Show S03 2022 1080p` → 摘掉 2022 后剩 `Another Show S03`，
    # 末尾那个 S03 不是片名的一部分。只从末尾剥，避免误伤
    # `S.W.A.T.` 这类片名。
    while len(keep) > 1 and _is_boundary_token(keep[-1]):
        keep.pop()
        saw_tech = True

    title = " ".join(keep).strip(" -_.")

    # 5. 判断是否有把握：剥掉过技术段，且片名长度合理
    result.title = title
    result.year = year
    result.confident = bool(saw_tech and 1 <= len(title) <= 80)
    return result


def search_query(raw: str) -> str:
    """只要片名（搜索用）。解析失败时返回空串，不要拿整条标题去搜。"""
    return parse_release_title(raw).title
