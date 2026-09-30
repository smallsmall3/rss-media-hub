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
    "hdrvivid", "hdrivid", "vivid", "hdr10plus",
    # 其他常见标记
    "repack", "proper", "internal", "complete", "limited", "remastered", "extended",
    "uncut", "unrated", "criterion", "imax", "ma", "multi", "dual", "chs", "cht", "gb", "big5",
    "hq", "hd", "sd", "uhq", "web", "fps", "ddp", "dd", "atmos", "dts", "truehd", "aac", "ac3",
    "简", "繁", "简繁", "中字", "国语", "粤语", "双语", "英语", "日语", "无字",
    "subs", "sub", "subtitle", "subtitles", "audio", "rip", "full", "batch", "合集",
    # 季集标记
    "season", "s", "ep", "episode", "e",
    # 片源平台
    "tx", "tx视频", "腾讯", "youku", "iqiyi", "爱奇艺", "芒果", "mgtv", "bilibili", "b站",
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

# 未闭合的括号：`[疾患 ` 或 `【简英|繁英|` 这种（RSS 源把标题截断了，括号没闭合）。
# 这类残片绝不会是片名，直接连括号一起删到结尾。
_UNCLOSED_BRACKET = re.compile(r"[\[【][^\[\]【】]*$")

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
    # 单独季号：S01 / S1（后面没有集号，如「The Girl in Blue S01 1080p」）
    m = re.search(r"(?i)\bS(\d{1,2})\b(?=\s|$|[.\-_（(])", text)
    if m:
        season = int(m.group(1))
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
    # 合集标记：全24集 / 全 24 集 / 24集全
    if re.match(r"^全?\s*\d{1,3}\s*[集话話]全?$", low) and re.search(r"\d", low):
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
    # 站点常见的地区短标记
    # ⚠️ 刻意**不**把两位国家码当技术词：`de` / `no` / `it` 这些
    # 是正常英文词（`De`、`No`），加进来会把片名吃掉 ——
    # 真实标题 `... Hang Zhou De Gu Shi` 里的 `De` 就这样丢过。
    # 帧率：60fps / 24fps
    if re.match(r"^\d{2,3}fps$", low):
        return True
    return False


def _drop_single_letter_fragments(keep: list[str]) -> tuple[list[str], bool]:
    """去掉由点号切碎产生的单字母碎片，但保住真正的单字母片名。

    判据：**只要前面出现过长度 ≥2 的词**，之后的单字母就算碎片。

      `S.W.A.T.`        → S W A T    前面没有长词，单字母簇全部保留
      `2001 A Space...` → 2001 在前，但 A 后面还有 Space，A 前面是"2001"
                          （数字，不算长词？）→ 这里按"数字不算长词"处理，保留 A
      `HDR H265` 切出的 `h` → 前面有 HDR/H265 这类长词 → 删掉

    这样既不会把 `S.W.A.T.` 吃成 `W A T`，也不会让技术串碎片留在片名里。
    """
    out: list[str] = []
    changed = False
    seen_long_word = False
    for token in keep:
        is_single_letter = len(token) == 1 and token.isascii() and token.isalpha()
        if is_single_letter and seen_long_word:
            changed = True
            continue
        # 长度 ≥2 的字母词才算"长词"；数字不算（否则 `2001 A Space` 的 A 会被误删）
        if len(token) >= 2 and any(ch.isalpha() for ch in token):
            seen_long_word = True
        out.append(token)
    return out, changed


def _is_empty_shell_token(token: str) -> bool:
    """判断"站点标识残片"。

    发布组名会被点号切碎：`... AAC-UBWEB` 切成 `AAC` 和 `UBWEB`，
    `UBWEB` 这种全大写、无元音的短串基本就是站点/组名前缀，
    留在片名里只会污染 TMDB 搜索（真实标题里踩到过）。

    判据保守：只吃"长度 3~8、全 ASCII 字母、全大写、且不含元音"的串，
    这样 `S.W.A.T.`、`NASA`、`BBC` 这类正常片名不会被误删。
    """
    if not (3 <= len(token) <= 7):
        return False
    if not token.isascii() or not token.isalpha():
        return False
    if not token.isupper():
        return False
    return not any(ch in "AEIOUaeiou" for ch in token)


def _is_strippable(token: str) -> bool:
    """剥离阶段判断：技术词、分界标记、站点残片，都算"该剥掉"。

    把 `_is_empty_shell_token` 也算进来很重要：`-UBWEB` 这种发布组残片
    既不是技术词也不是分界标记，如果只认前两者，从右往左的剥离会
    在它身上立刻停住，后面真正的技术段（`2160p` 等）就全留在片名里了
    —— 这正是"搜不到海报"的根因之一。
    """
    return (
        _is_tech_token(token, token.lower())
        or _is_boundary_token(token)
        or _is_empty_shell_token(token)
    )


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

    # 0. 先提取「括号里的年份」：`（2026）` / `(2026)` / `[2026]`。
    #    必须在删括号**之前**做，否则年份被 _BRACKET_TAG 连括号一起删掉，
    #    年份信息就丢了（The Girl in Blue S01 1080p TX（2026）全24集 实测踩到）。
    #    全角括号也算：很多中文标题用 （2026）。
    _year_paren = re.search(r"[（(\[]\s*(19\d{2}|20\d{2})\s*[）)\]]", text)
    if _year_paren:
        result.year = int(_year_paren.group(1))

    # 1. 方括号标签。
    #    顺序很关键：**必须先删方括号，再处理结尾压制组**。
    #    因为有些站把别名方括号直接粘在组名后面：
    #      `... AAC-UBWEB[李熊猫/李熊猫与恶魔]`
    #    如果先跑 `_TRAILING_GROUP`，`-UBWEB[` 不匹配 `-GROUP$`，
    #    这个组名就留在了标题里，接着被当成"内容词"，
    #    导致从右往左的剥离提前停住、`2160p` 之类残留进片名（实测踩到）。
    #
    #    先处理**未闭合**的括号：RSS 源有时把标题截断，末尾留下
    #    `[疾患 【简英|繁英|简|繁|…` 这种只有左括号没有右括号的残片，
    #    它绝不是片名，连括号一起删到结尾，否则会污染片名（实测踩到）。
    #    循环删：一个标题里可能叠了好几个未闭合的括号（[疾患 【简英|…）。
    for _ in range(5):
        new_text = _UNCLOSED_BRACKET.sub("", text)
        if new_text == text:
            break
        text = new_text
    text = _BRACKET_TAG.sub(" ", text)
    # 季范围（S01-S03）整体丢掉
    text = _RANGE_PATTERN.sub(" ", text)
    # 先 rstrip：方括号替换会留下尾随空格，而 `_TRAILING_GROUP` 的 `$`
    # 匹配不上"末尾带空格"的串，结果 `-UBWEB` 留了下来（实测踩到）
    text = text.rstrip()
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
        if _is_strippable(cleaned):
            saw_tech = True
            idx -= 1
            continue
        break  # 遇到不像规格的段落：片名从这里开始

    # 阶段二：idx 及其左边算是"片名区"
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
    while len(keep) > 1 and _is_boundary_token(keep[-1]):
        keep.pop()
        saw_tech = True

    # 阶段五（关键）：片名区尾部仍可能混着技术段。
    # 真实情况：`... Gu Shi 2026 S01E32 2160p WEB-DL HDRVivid H265 AAC-UBWEB`
    # —— 阶段一从右往左剥，剥到 `HDRVivid`（当时还不在词表里）就停住了，
    # 结果 `2160p` 留在了片名里，拿去搜 TMDB 必然搜不到、也就没有海报。
    #
    # 但**不能简单地把所有技术段都删掉** —— 那会误伤片名里的单字母与数字：
    # `S.W.A.T.` 的 S、`3.10.to.Yuma` 的 3、`2001 A Space Odyssey` 的 2001
    # （都实测错过）。
    #
    # 判据：技术段在片名区里总是**连续出现在末尾**，所以只删
    # "最后一个非技术段之后"的部分，前面的词一律保护。
    if len(keep) > 1:
        last_content = 0
        for pos, token in enumerate(keep):
            if not (_is_tech_token(token, token.lower())
                    or _is_boundary_token(token)
                    or _is_empty_shell_token(token)):
                last_content = pos
        if last_content < len(keep) - 1:
            saw_tech = True
            keep = keep[: last_content + 1]

    # 开头残留的多字符技术串（例如顺序被切乱时 `BluRay` 落到最左）。
    # 刻意**不删**单字母与纯数字：它们常是片名开头
    # （`S.W.A.T.` 的 S、`3.10.to.Yuma` 的 3、`2001` 的 2001）。
    while len(keep) > 1:
        head = keep[0]
        if len(head) < 2 or head.isdigit():
            break
        if not _is_tech_token(head, head.lower()):
            break
        keep.pop(0)
        saw_tech = True

    while keep and _is_tech_token(keep[-1], keep[-1].lower()):
        keep.pop()

    if not keep:
        return result

    title = " ".join(keep).strip(" -_.")

    # 5. 判断是否有把握：剥掉过技术段，且片名长度合理
    result.title = title
    # 括号里提前提取的年份优先保留（局部 year 没找到时用 result.year）
    result.year = year or result.year
    result.confident = bool(saw_tech and 1 <= len(title) <= 80)
    return result


def search_query(raw: str) -> str:
    """只要片名（搜索用）。解析失败时返回空串，不要拿整条标题去搜。"""
    return parse_release_title(raw).title


# --------------------------------------------------------------------------
# 中文别名提取
# --------------------------------------------------------------------------
#
# 国产动漫/剧集常常用**拼音**当标题：
#   [动漫(Animations)]Su Dong Po Yu Hang Zhou De Gu Shi 2026 S01E32 ...
# 而 TMDB 上只有中文条目（`苏东坡与杭州的故事`），拿拼音去搜是 0 条 → 没海报。
#
# 好消息是这类标题的方括号里往往就带着中文名：
#   ...[吞噬星空 | 第243集 | 导演: xxx]      ← `吞噬星空`
#   ...[李熊猫/李熊猫与恶魔]                  ← `李熊猫`
# 所以把中文名抓出来当**备选搜索词**：先用拼音搜，搜不到再用中文搜。

# 别名里的噪音：集数、季度、导演/演员表、站点后缀
_ALIAS_NOISE = re.compile(
    r"(?:第\s*\d+\s*[集话話季]|\d+\s*[集话話季]|EP?\d+|S\d{1,2}E?\d{0,3}|"
    r"第[一二三四五六七八九十]+季|[一二三四五六七八九十]+季|"
    r"导演|主演|编剧|演员|类型|地区|语言|片长|上映|简介|剧情|"
    r"澳剧|美剧|英剧|日剧|韩剧|国产剧|港剧|台剧|泰剧|新剧|"
    r"简繁|中字|内封|外挂|国语|粤语|日语|双语|原盘|"
    r"WEB-?DL|BluRay|HDR|H\.?26[45]|HEVC|AVC|AAC|DDP|DTS|Atmos|"
    r"\d{3,4}p|\d+bit|\d+Fps|UBWEB|CHDWEB|X264|X265|"
    r"Animations?|Animation|Series|Movie|Documentary)",
    re.IGNORECASE,
)

# 演职员表：`导演: 沈乐平` / `主演：xxx` —— 关键词后面的人名要整段删掉。
# 只删"导演"这个词是不够的，名字会留下来被当成别名（实测踩到）。
_ALIAS_CREDITS = re.compile(
    r"(?:导演|主演|编剧|演员|配音|原作|监制|制片)\s*[:：]?\s*[^|｜/\[\]【】]*",
)

# 语言/地区前缀：`澳剧：最后目击` → `最后目击`
_ALIAS_PREFIX = re.compile(
    r"^(?:澳剧|美剧|英剧|日剧|韩剧|国产剧|港剧|台剧|泰剧|新剧|"
    r"欧美剧|日番|国漫|动漫|动画)\s*[:：]?\s*",
)

# 分类标签不算别名
_ALIAS_STOPWORDS = {
    "动漫", "动画", "动画片", "综艺", "纪录片", "电视剧", "电影", "合集", "体育",
    "animations", "animation", "tvseries", "tv series", "tv", "series",
    "movie", "movies", "documentary", "music", "sports", "anime",
}

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


def _clean_alias(text: str) -> str:
    """把一段别名文本清洗成可搜索的名字。"""
    out = text or ""
    # 先整段删掉演职员表（关键词连人名一起）
    out = _ALIAS_CREDITS.sub(" ", out)
    # 再去掉语言/地区前缀
    out = _ALIAS_PREFIX.sub("", out)
    # 最后删剩余噪音词
    out = _ALIAS_NOISE.sub(" ", out)
    # 去掉各种括号与分隔符残留
    out = re.sub(r"[\[\]【】()（）<>《》|｜/\\,，;；:：\-—_·*]+", " ", out)
    out = re.sub(r"\s{2,}", " ", out).strip()
    return out


def aliases(raw: str, *, include_body: bool = True) -> list[str]:
    """从发布标题里提取可搜索的中文别名（按可信度排序）。

    来源有两处：
      1. 方括号/【】里的中文（`[吞噬星空 | 第243集...]` → `吞噬星空`）
      2. 标题正文里的中文段（`某某剧.S01E01.1080p` → `某某剧`）

    全部清洗过、去过重、去掉了分类标签；拿不准的一律不给，
    因为搜错会推一张不相干的图。
    """
    text = raw or ""
    if not text:
        return []

    # 先清掉未闭合的括号残片（`[疾患 【简英|…` 这种），否则「疾患」会从
    # 正文里漏进别名，污染搜索词（实测踩到）。
    for _ in range(5):
        cleaned_text = _UNCLOSED_BRACKET.sub("", text)
        if cleaned_text == text:
            break
        text = cleaned_text

    found: list[str] = []
    seen: set[str] = set()

    def add(candidate: str) -> None:
        cleaned = _clean_alias(candidate)
        if not cleaned or not _CJK.search(cleaned):
            return
        # 去掉空格和标点后再比对停用词，这样 `动漫(Animations)` 也能命中"动漫"
        bare = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", cleaned.lower())
        for stop in _ALIAS_STOPWORDS:
            if bare == re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", stop.lower()):
                return
        # 去掉分类前缀后如果只剩停用词/空，也不要
        stripped = re.sub(r"^(动漫|动画|综艺|纪录片|电视剧|电影|合集)+\s*", "", cleaned).strip()
        if stripped and len(_CJK.findall(stripped)) >= 2:
            cleaned = stripped
        # 太短（少于 2 个汉字）或太长都不靠谱
        cjk_count = len(_CJK.findall(cleaned))
        if cjk_count < 2 or len(cleaned) > 40:
            return
        if cleaned in seen:
            return
        seen.add(cleaned)
        found.append(cleaned)

    # 1. 方括号里的内容（可能有多个，逐个试）
    for bracket in re.findall(r"[\[【]([^\[\]【】]+)[\]】]", text):
        for piece in re.split(r"[|｜/]", bracket):
            add(piece)

    # 2. 标题正文里的中文段（去掉方括号部分，避免重复）
    if include_body:
        body = re.sub(r"[\[【][^\[\]【】]*[\]】]", " ", text)
        for piece in re.split(r"[._\s|｜/]+", body):
            add(piece)

    # 长的（信息更完整）排前面
    found.sort(key=len, reverse=True)
    return found


def search_terms(raw: str) -> list[str]:
    """按优先级给出所有可搜索的名字：先拼音/原文，再中文别名。

    PosterResolver 会依次尝试，任何一个搜到就停 —— 这样
    `Su Dong Po Yu ...`（拼音，TMDB 没有）能退到中文名去搜。
    """
    terms: list[str] = []
    parsed = parse_release_title(raw)
    if parsed.title and parsed.confident:
        terms.append(parsed.title)
    for alias in aliases(raw):
        if alias not in terms:
            terms.append(alias)
    return terms
