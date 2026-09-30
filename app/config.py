"""配置加载：环境变量（密钥）+ YAML 文件（订阅与规则）。

设计原则：
1. 密钥只走环境变量（.env），永不写进 YAML，方便把 config 目录丢进 git。
2. YAML 读取优先用 PyYAML；万一环境里没有，退回内置的迷你解析器，
   保证"容器里少装一个包也能起来"。
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:  # pragma: no cover - 取决于运行环境
    import yaml as _yaml
except Exception:  # pragma: no cover
    _yaml = None


# --------------------------------------------------------------------------
# 迷你 YAML 解析器（仅支持本项目用到的子集：嵌套 map / list / 标量 / # 注释）
# --------------------------------------------------------------------------


class ConfigError(ValueError):
    """配置不合法时抛出，带上人能看懂的定位信息。

    继承 ValueError 是刻意的：网页 UI 把 ValueError 当作"用户填错了"返回 400，
    而配置内容不合法本质上也确实属于值错误（否则会被当成服务端 500）。
    """


# 订阅的两种工作模式
MODE_FEED = "feed"   # 订阅源全量：RSS 有什么推什么
MODE_SHOW = "show"   # 按剧追踪：只推某部剧，并比对媒体库算进度


def _mini_scalar(raw: str) -> Any:
    text = raw.strip()
    if text == "" or text in {"~", "null", "Null", "NULL"}:
        return None
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        inner = text[1:-1]
        if text[0] == '"':
            try:
                return ast.literal_eval(text)
            except Exception:
                return inner
        return inner.replace("''", "'")
    low = text.lower()
    if low in {"true", "yes", "on"}:
        return True
    if low in {"false", "no", "off"}:
        return False
    if re.fullmatch(r"[+-]?\d+", text):
        return int(text)
    if re.fullmatch(r"[+-]?(\d+\.\d*|\.\d+)([eE][+-]?\d+)?", text):
        return float(text)
    # 行内列表 [a, b, c]
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [_mini_scalar(part) for part in _split_inline(inner)]
    return text


def _split_inline(text: str) -> list[str]:
    parts: list[str] = []
    buf = ""
    quote = ""
    for ch in text:
        if quote:
            buf += ch
            if ch == quote:
                quote = ""
        elif ch in {"'", '"'}:
            quote = ch
            buf += ch
        elif ch == ",":
            parts.append(buf)
            buf = ""
        else:
            buf += ch
    if buf.strip():
        parts.append(buf)
    return [p.strip() for p in parts if p.strip()]


def _strip_comment(line: str) -> str:
    quote = ""
    for idx, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in {"'", '"'}:
            quote = ch
        elif ch == "#" and (idx == 0 or line[idx - 1] in " \t"):
            return line[:idx]
    return line


def _mini_yaml_load(text: str) -> Any:
    """只处理本项目需要的 YAML 子集；不支持的写法会明确报错而不是静默忽略。

    支持：嵌套 map、`- ` 列表、`- key: value` 字典列表、行内列表、# 注释、单双引号。
    """
    lines: list[tuple[int, str, int]] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        leading = raw[: len(raw) - len(raw.lstrip(" \t"))]
        if "\t" in leading:
            raise ConfigError(f"第 {lineno} 行：YAML 缩进请用空格，不要用 Tab")
        body = _strip_comment(raw).rstrip()
        if not body.strip():
            continue
        if body.strip() in {"---", "..."}:
            continue
        indent = len(body) - len(body.lstrip(" "))
        lines.append((indent, body.strip(), lineno))

    if not lines:
        return {}

    pos = 0
    total = len(lines)

    def parse_continuation(entry: dict[str, Any], item_indent: int, dash_indent: int, lineno: int) -> dict[str, Any]:
        """把字典列表项的后续键值对并进同一个 entry。

        YAML 里 `- id: a` 的键等效缩进是 dash 缩进 + 2，所以同级后续键
        （缩进等于 item_indent）也要算进来；比它更深的是嵌套结构。
        """
        nonlocal pos
        while pos < total:
            cur, text, _ = lines[pos]
            if cur > item_indent:
                break  # 交给下面的嵌套分支处理
            if cur <= dash_indent or ":" not in text:
                break
            key, _, val = text.partition(":")
            key = key.strip()
            pos += 1
            if val.strip():
                entry[key] = _mini_scalar(val)
            elif pos < total and lines[pos][0] > cur:
                entry[key] = parse_block(lines[pos][0])
            else:
                entry[key] = None
        if pos < total and lines[pos][0] > item_indent:
            child = parse_block(lines[pos][0])
            if isinstance(child, dict):
                entry.update(child)
            else:
                raise ConfigError(f"第 {lineno} 行：列表项的后续内容应为键值对")
        return entry

    def parse_block(indent: int) -> Any:
        nonlocal pos
        is_list = lines[pos][1] == "-" or lines[pos][1].startswith("- ")
        node: Any = [] if is_list else {}
        while pos < total:
            cur_indent, content, lineno = lines[pos]
            if cur_indent < indent:
                break
            if cur_indent > indent:
                raise ConfigError(f"第 {lineno} 行：缩进层级异常：{content!r}")

            # ---------------- 列表 ----------------
            if content == "-" or content.startswith("- "):
                if not is_list:
                    raise ConfigError(f"第 {lineno} 行：列表项与字典键混用")
                item = content[1:].strip()
                pos += 1
                if not item:
                    node.append(parse_block(lines[pos][0]) if pos < total and lines[pos][0] > cur_indent else None)
                    continue
                if ":" in item and not item.startswith(("'", '"')):
                    key, _, val = item.partition(":")
                    item_indent = cur_indent + 2  # "- key: value" 里 key 的等效缩进
                    entry: dict[str, Any] = {key.strip(): _mini_scalar(val) if val.strip() else None}
                    if not val.strip() and pos < total and lines[pos][0] > item_indent:
                        entry[key.strip()] = parse_block(lines[pos][0])
                    node.append(parse_continuation(entry, item_indent, cur_indent, lineno))
                    continue
                node.append(_mini_scalar(item))
                continue

            # ---------------- 字典 ----------------
            if is_list:
                raise ConfigError(f"第 {lineno} 行：列表里混进了字典键：{content!r}")
            if ":" not in content:
                raise ConfigError(f"第 {lineno} 行：缺少 ':'，无法解析：{content!r}")
            key, _, val = content.partition(":")
            key = key.strip()
            pos += 1
            if val.strip():
                node[key] = _mini_scalar(val)
            elif pos < total and lines[pos][0] > cur_indent:
                node[key] = parse_block(lines[pos][0])
            else:
                node[key] = None
        return node

    result = parse_block(lines[0][0])
    return {} if result is None else result


def load_yaml_text(text: str) -> Any:
    if _yaml is not None:
        data = _yaml.safe_load(text)
        return {} if data is None else data
    return _mini_yaml_load(text)


def load_yaml_file(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return {} if default is None else default
    try:
        return load_yaml_text(path.read_text(encoding="utf-8"))
    except ConfigError:
        raise
    except Exception as exc:  # pragma: no cover
        raise ConfigError(f"读取 {path} 失败：{exc}") from exc


def dump_yaml_file(path: Path, data: Any, header: str = "") -> None:
    """写回 YAML。

    有 PyYAML 就用它（格式漂亮）；没有就用内置序列化器（本项目自己写的配置
    一定能被自己读回来）。先写临时文件再 rename，保证不会写出半个文件；
    某些 NAS/SMB 挂载不允许覆盖式 rename 时回退为直接写。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if _yaml is not None:
        body = _yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
    else:
        body = _mini_yaml_dump(data)
    text = ""
    if header:
        text += "".join(f"# {line}\n" if line else "#\n" for line in header.splitlines())
    text += body

    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    except OSError:
        # SMB / 只允许原地覆盖的挂载：直接写目标文件
        try:
            path.write_text(text, encoding="utf-8")
        except OSError:
            raise
        with contextlib.suppress(OSError):
            tmp.unlink()


_SAFE_BARE = re.compile(r"^[A-Za-z\u4e00-\u9fff0-9_./\-+*@()\[\]{}:,% ]+$")
_RESERVED = {"true", "false", "yes", "no", "on", "off", "null", "~", ""}


def _mini_scalar_dump(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    needs_quote = (
        text.lower() in _RESERVED
        or not _SAFE_BARE.match(text)
        or text != text.strip()
        or ": " in text
        or text.startswith(("-", "?", "#", "&", "*", "!", "|", ">", "%", "@", "`"))
        or text.endswith(":")
    )
    if needs_quote:
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


def _mini_yaml_dump(data: Any, indent: int = 0) -> str:
    pad = " " * indent
    out: list[str] = []
    if isinstance(data, dict):
        for key, val in data.items():
            skey = _mini_scalar_dump(key)
            if isinstance(val, (dict, list)) and val:
                out.append(f"{pad}{skey}:")
                out.append(_mini_yaml_dump(val, indent + 2).rstrip("\n"))
            elif isinstance(val, (dict, list)):
                out.append(f"{pad}{skey}: {'{}' if isinstance(val, dict) else '[]'}")
            else:
                out.append(f"{pad}{skey}: {_mini_scalar_dump(val)}")
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item:
                first = True
                for key, val in item.items():
                    skey = _mini_scalar_dump(key)
                    prefix = f"{pad}- " if first else f"{pad}  "
                    first = False
                    if isinstance(val, (dict, list)) and val:
                        out.append(f"{prefix}{skey}:")
                        out.append(_mini_yaml_dump(val, indent + 4).rstrip("\n"))
                    elif isinstance(val, (dict, list)):
                        out.append(f"{prefix}{skey}: {'{}' if isinstance(val, dict) else '[]'}")
                    else:
                        out.append(f"{prefix}{skey}: {_mini_scalar_dump(val)}")
            else:
                out.append(f"{pad}- {_mini_scalar_dump(item)}")
    else:
        out.append(f"{pad}{_mini_scalar_dump(data)}")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------
# 设置对象
# --------------------------------------------------------------------------


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or "").strip() or default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on", "y"}


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass
class TelegramSettings:
    bot_token: str = ""
    chat_id: str = ""
    thread_id: str = ""
    api_base: str = "https://api.telegram.org"
    proxy: str = ""  # HTTP/SOCKS 正向代理，例如 http://192.168.31.142:10809
    disable_notification: bool = False
    send_poster: bool = True
    # feed（订阅源全量）模式是否给每条内容配海报。
    # 需要 TMDB 才能按片名搜海报；一条推送只发第一张，避免刷屏。
    feed_poster: bool = True

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)


@dataclass
class TmdbSettings:
    api_key: str = ""
    api_base: str = "https://api.themoviedb.org/3"
    language: str = "zh-CN"
    image_base: str = "https://image.tmdb.org/t/p/w500"
    proxy: str = ""  # HTTP/SOCKS 正向代理

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)


@dataclass
class LibrarySettings:
    """Emby / Jellyfin 连接配置。两者 API 同源，少量路径差异内部兼容。"""

    url: str = ""
    api_key: str = ""
    user_id: str = ""
    kind: str = "auto"  # auto | emby | jellyfin
    count_aired_only: bool = True
    include_specials: bool = False
    cache_ttl: int = 300
    verify_tls: bool = False
    timeout: float = 20.0
    proxy: str = ""  # 一般留空（局域网直连）；填了会强制走代理

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.api_key)


@dataclass
class Subscription:
    """一条订阅。

    mode 决定这条订阅怎么工作：
      * "feed"：**订阅源全量**。RSS 里出现什么就推什么，不比对媒体库，
                也不需要 tmdb_id。当"RSS 播报器"用。
      * "show"：**按剧追踪**（原来的模式）。只推这部剧的条目，
                每轮比对媒体库算入库进度，全部入库后推送完成并自动退订。
    """

    id: str
    name: str
    rss: str = ""
    mode: str = MODE_FEED           # feed | show
    tmdb_id: int | None = None
    year: int | None = None
    season: int | None = None      # 限定只关注某一季；空=全部季
    enabled: bool = True
    notify_new: bool = True        # 发现新种是否推送
    remove_when_done: bool = True  # 追完是否删除这条订阅（仅 show 模式有效）
    seed: bool | None = None       # 首轮是否静默登记历史条目；空=按 mode 取默认
    name_filter: str = ""          # 正则：命中的条目标题才推送（大小写不敏感）
    exclude_filter: str = ""       # 正则：命中的条目标题直接忽略
    quality: list[str] = field(default_factory=list)  # 标题需包含任一关键字
    tmdb_required: bool = False    # feed 模式下：只有能匹配到 TMDB 的条目才推送
    tags: list[str] = field(default_factory=list)
    note: str = ""
    # 运行时字段（不写回 YAML）
    source: str = "file"

    def __post_init__(self) -> None:
        if not self.id:
            self.id = slugify(self.name)
        normalized = (self.mode or MODE_FEED).strip().lower()
        if normalized in {"all", "rss", "raw", "forward", "full"}:
            normalized = MODE_FEED  # 兼容几种口语化写法
        if normalized not in {MODE_FEED, MODE_SHOW}:
            raise ConfigError(
                f"订阅 {self.id} 的 mode 只能是 'feed' 或 'show'，收到：{self.mode!r}"
            )
        self.mode = normalized
        if self.seed is None:
            # feed 模式默认把历史条目也补推（用户要的就是"全量"）；
            # show 模式默认静默登记，避免一上线被几百条历史刷屏。
            self.seed = self.mode == MODE_SHOW

    @property
    def is_feed(self) -> bool:
        return self.mode == MODE_FEED

    @property
    def is_show(self) -> bool:
        return self.mode == MODE_SHOW

    @property
    def mode_label(self) -> str:
        return "订阅源全量" if self.is_feed else "按剧追踪"

    @property
    def rss_urls(self) -> list[str]:
        return [u.strip() for u in re.split(r"[\s,]+", self.rss or "") if u.strip()]

    def validate(self) -> None:
        """只拦真正会导致功能不可用的组合，不做过度限制。

        刻意允许的用法：
          * 先建订阅、之后再补 rss（网页表单也是逐个字段填的）
          * show 模式只给剧名，靠名称去 TMDB 搜索匹配
        """
        if not self.rss and not self.name:
            raise ConfigError(f"订阅 {self.id} 既没有 rss 也没有 name")
        if self.is_feed and self.tmdb_id and not self.rss:
            raise ConfigError(f"订阅 {self.id} 是 feed 模式但没填 rss 地址")

    def allows_title(self, title: str) -> tuple[bool, str]:
        """按订阅规则判断某个 RSS 标题是否该推送。返回 (是否通过, 原因)。"""
        if not title:
            return False, "标题为空"
        if self.name_filter:
            if not re.search(self.name_filter, title, re.IGNORECASE):
                return False, f"不匹配 name_filter={self.name_filter}"
        if self.exclude_filter and re.search(self.exclude_filter, title, re.IGNORECASE):
            return False, f"命中 exclude_filter={self.exclude_filter}"
        if self.quality:
            low = title.lower()
            if not any(q.lower() in low for q in self.quality):
                return False, f"缺少画质关键字 {self.quality}"
        return True, ""


@dataclass
class Settings:
    config_dir: Path
    state_dir: Path
    telegram: TelegramSettings
    tmdb: TmdbSettings
    library: LibrarySettings
    poll_interval: int = 900
    reconcile_interval: int = 1800
    seed_silent: bool = False
    health_port: int = 8080
    log_level: str = "INFO"
    max_items_per_poll: int = 40
    scan_cache_ttl: int = 12 * 3600   # 全库扫描时 TMDB 结果缓存多久（秒）
    scan_concurrency: int = 5         # 全库扫描并发数（太大容易被 TMDB 限流）
    scan_max_series: int = 0          # 全库扫描最多检查多少部剧，0=不限
    ui_token: str = ""                # 网页 UI 访问口令，留空=不校验（仅建议内网使用）
    ui_enabled: bool = True           # 是否提供网页 UI
    subscriptions: list[Subscription] = field(default_factory=list)

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.yaml"

    @property
    def overrides_file(self) -> Path:
        """网页 UI 保存的配置（和环境变量、config.yaml 分离）。"""
        return overrides_path(self.config_dir)

    @property
    def subs_file(self) -> Path:
        return self.config_dir / "subscriptions.yaml"

    @property
    def db_file(self) -> Path:
        return self.state_dir / "rss-media-hub.db"


def slugify(text: str, fallback: str = "") -> str:
    """把剧名变成可用作 id / 文件名的短标识。

    中文等非 ASCII 字符会被转成稳定的短哈希（同一剧名永远得到同一个 id），
    避免用中文当 id 在 YAML / 日志 / URL 里出问题。
    """
    raw = (text or "").strip().lower()
    ascii_part = re.sub(r"[^0-9a-z]+", "-", re.sub(r"[\u4e00-\u9fff]+", "-", re.sub(r"[\s/\\|:：]+", "-", raw)))
    ascii_part = ascii_part.strip("-._")
    if ascii_part:
        result = ascii_part[:48].strip("-._")
        if result:
            return result
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8] if raw else ""
    if digest:
        return f"sub-{digest}"
    return re.sub(r"[^0-9a-z\-_.]+", "", fallback.lower()).strip("-._") or "sub"


# --------------------------------------------------------------------------
# 订阅解析
# --------------------------------------------------------------------------


def _as_bool(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def _as_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [p.strip() for p in re.split(r"[,，\s]+", str(value)) if p.strip()]


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def parse_subscription(raw: Any, index: int = 0) -> Subscription:
    if isinstance(raw, str):  # 允许 subscriptions.yaml 里直接写 RSS 地址
        raw = {"rss": raw}
    if not isinstance(raw, dict):
        raise ConfigError(f"subscriptions.yaml 第 {index + 1} 条不是字典：{raw!r}")

    name = _as_str(raw.get("name")) or _as_str(raw.get("title"))
    rss = _as_str(raw.get("rss") or raw.get("rss_url") or raw.get("feed") or raw.get("url"))
    if not name and not rss:
        raise ConfigError(f"subscriptions.yaml 第 {index + 1} 条既没有 name 也没有 rss")

    tmdb_id = _as_int(raw.get("tmdb_id") or raw.get("tmdb"))
    # mode 可以从 mode 字段读，也可以从简写 all/show 推断
    raw_mode = _as_str(raw.get("mode")) or _as_str(raw.get("type"))
    mode = raw_mode or (MODE_SHOW if tmdb_id is not None else MODE_FEED)
    seed_raw = raw.get("seed")
    seed = None if seed_raw is None else _as_bool(seed_raw, True)
    sub = Subscription(
        id=_as_str(raw.get("id")) or (slugify(name or rss or f"sub-{index + 1}")),
        name=name or f"sub-{index + 1}",
        rss=rss,
        mode=mode,
        tmdb_id=tmdb_id,
        year=_as_int(raw.get("year")),
        season=_as_int(raw.get("season")),
        enabled=_as_bool(raw.get("enabled"), True),
        notify_new=_as_bool(raw.get("notify_new"), True),
        remove_when_done=_as_bool(raw.get("remove_when_done"), True),
        seed=seed,
        name_filter=_as_str(raw.get("name_filter") or raw.get("include")),
        exclude_filter=_as_str(raw.get("exclude_filter") or raw.get("exclude")),
        quality=_as_list(raw.get("quality") or raw.get("resolutions")),
        tmdb_required=_as_bool(raw.get("tmdb_required"), False),
        tags=_as_list(raw.get("tags")),
        note=_as_str(raw.get("note")),
    )
    sub.validate()
    return sub


def load_subscriptions(path: Path) -> list[Subscription]:
    data = load_yaml_file(path, default={})
    if not data:
        return []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("subscriptions") or []
    else:
        raise ConfigError(f"{path} 结构不对，应为 subscriptions 列表")
    if not isinstance(items, list):
        raise ConfigError(f"{path} 的 subscriptions 必须是列表")

    subs: list[Subscription] = []
    seen: set[str] = set()
    for idx, raw in enumerate(items):
        sub = parse_subscription(raw, idx)
        base = sub.id
        n = 2
        while sub.id in seen:
            sub.id = f"{base}-{n}"
            n += 1
        seen.add(sub.id)
        subs.append(sub)
    return subs


def save_subscriptions(path: Path, subs: list[Subscription]) -> None:
    payload = []
    for sub in subs:
        item: dict[str, Any] = {"id": sub.id, "name": sub.name}
        if sub.tmdb_id:
            item["tmdb_id"] = sub.tmdb_id
        if sub.year:
            item["year"] = sub.year
        if sub.season:
            item["season"] = sub.season
        if sub.rss:
            item["rss"] = sub.rss
        if sub.mode != MODE_FEED:
            item["mode"] = sub.mode   # feed 是默认值，不写出来让文件更干净
        if sub.seed is not None and sub.seed != (sub.mode == MODE_SHOW):
            item["seed"] = sub.seed
        if not sub.enabled:
            item["enabled"] = False
        if not sub.notify_new:
            item["notify_new"] = False
        if not sub.remove_when_done:
            item["remove_when_done"] = False
        if sub.name_filter:
            item["name_filter"] = sub.name_filter
        if sub.exclude_filter:
            item["exclude_filter"] = sub.exclude_filter
        if sub.quality:
            item["quality"] = sub.quality
        if sub.tmdb_required:
            item["tmdb_required"] = True
        if sub.tags:
            item["tags"] = sub.tags
        if sub.note:
            item["note"] = sub.note
        payload.append(item)
    dump_yaml_file(
        path,
        {"subscriptions": payload},
        header=(
            "rss-media-hub 订阅表\n"
            "由程序自动维护（追完自动删除时会重写本文件）。\n"
            "手工编辑也完全没问题：改完 docker compose restart 即可生效。\n"
            "\n"
            "字段说明：\n"
            "  id                唯一标识，留空自动从 name 生成\n"
            "  name              名称（show 模式填剧名；feed 模式随便填个便于识别的名字）\n"
            "  mode              feed = 订阅源全量（RSS 有什么推什么，不比对媒体库）\n"
            "                    show = 按剧追踪（只推这部剧，算入库进度，追完自动退订）\n"
            "                    留空时：填了 tmdb_id 就按 show，否则按 feed\n"
            "  tmdb_id           show 模式强烈建议填写，避免同名剧匹配错误\n"
            "  year              首播年份，辅助匹配\n"
            "  season            只关注某一季；留空=全部季\n"
            "  rss               PT 站 RSS 地址（可多个，空格或逗号分隔）\n"
            "  name_filter       正则，命中的条目才推送\n"
            "  exclude_filter    正则，命中的条目忽略\n"
            "  quality           ['1080p','2160p'] 之类的关键字白名单\n"
            "  tmdb_required     feed 模式下：只有能匹配到 TMDB 的条目才推送\n"
            "  seed              首轮是否静默登记历史条目（feed 默认 false，show 默认 true）\n"
            "  notify_new        发现新种是否推送（默认 true）\n"
            "  remove_when_done  全部入库后是否删除本订阅（默认 true）\n"
        ),
    )


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------


def overrides_path(config_dir: Path) -> Path:
    """网页 UI 保存的配置放这里，和手写的 config.yaml 分开，互不覆盖。"""
    return Path(config_dir) / "settings.yaml"


def _merge_dict(base: dict, extra: dict) -> dict:
    """浅合并一层嵌套：extra 里有的键覆盖 base，没提到的保留。"""
    out = dict(base or {})
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            merged = dict(out[key])
            merged.update(value)
            out[key] = merged
        else:
            out[key] = value
    return out


def load_settings(config_dir: Path | None = None, state_dir: Path | None = None) -> Settings:
    cfg_dir = Path(config_dir or _env("RMH_CONFIG_DIR", "./config")).expanduser()
    st_dir = Path(state_dir or _env("RMH_STATE_DIR", "./state")).expanduser()
    cfg_dir.mkdir(parents=True, exist_ok=True)
    st_dir.mkdir(parents=True, exist_ok=True)

    # 优先级：环境变量 > settings.yaml(网页 UI 写的) > config.yaml(手写的)
    file_cfg = load_yaml_file(cfg_dir / "config.yaml", default={}) or {}
    overrides = load_yaml_file(overrides_path(cfg_dir), default={}) or {}
    if overrides:
        file_cfg = _merge_dict(file_cfg, overrides)
    tg_file = file_cfg.get("telegram") or {}
    tmdb_file = file_cfg.get("tmdb") or {}
    lib_file = file_cfg.get("library") or file_cfg.get("emby") or {}
    run_file = file_cfg.get("runtime") or {}
    ui_file = file_cfg.get("ui") or {}

    # 正向代理：全局 RMH_PROXY 兜底，单个服务可用 RMH_TG_PROXY / RMH_TMDB_PROXY 覆盖。
    # 注意 Emby 在局域网时不要让它走代理（局域网不需要，且代理可能连不通内网）。
    global_proxy = _env("RMH_PROXY")

    telegram = TelegramSettings(
        bot_token=_env("RMH_TG_BOT_TOKEN") or _as_str(tg_file.get("bot_token")),
        chat_id=_env("RMH_TG_CHAT_ID") or _as_str(tg_file.get("chat_id")),
        thread_id=_env("RMH_TG_THREAD_ID") or _as_str(tg_file.get("thread_id")),
        api_base=(_env("RMH_TG_API_BASE") or _as_str(tg_file.get("api_base")) or "https://api.telegram.org").rstrip("/"),
        proxy=_env("RMH_TG_PROXY") or _as_str(tg_file.get("proxy")) or global_proxy,
        disable_notification=_env_bool("RMH_TG_SILENT", _as_bool(tg_file.get("disable_notification"), False)),
        send_poster=_env_bool("RMH_TG_SEND_POSTER", _as_bool(tg_file.get("send_poster"), True)),
        feed_poster=_env_bool("RMH_TG_FEED_POSTER", _as_bool(tg_file.get("feed_poster"), True)),
    )

    tmdb = TmdbSettings(
        api_key=_env("RMH_TMDB_API_KEY") or _as_str(tmdb_file.get("api_key")),
        api_base=(_env("RMH_TMDB_API_BASE") or _as_str(tmdb_file.get("api_base")) or "https://api.themoviedb.org/3").rstrip("/"),
        language=_env("RMH_TMDB_LANGUAGE", _as_str(tmdb_file.get("language"), "zh-CN")),
        image_base=(_env("RMH_TMDB_IMAGE_BASE") or _as_str(tmdb_file.get("image_base")) or "https://image.tmdb.org/t/p/w500").rstrip("/"),
        proxy=_env("RMH_TMDB_PROXY") or _as_str(tmdb_file.get("proxy")) or global_proxy,
    )

    library = LibrarySettings(
        url=(_env("RMH_EMBY_URL") or _as_str(lib_file.get("url"))).rstrip("/"),
        api_key=_env("RMH_EMBY_API_KEY") or _as_str(lib_file.get("api_key")),
        user_id=_env("RMH_EMBY_USER_ID") or _as_str(lib_file.get("user_id")),
        kind=(_env("RMH_EMBY_KIND") or _as_str(lib_file.get("kind"), "auto")).lower(),
        count_aired_only=_env_bool("RMH_EMBY_COUNT_AIRED_ONLY", _as_bool(lib_file.get("count_aired_only"), True)),
        include_specials=_env_bool("RMH_EMBY_INCLUDE_SPECIALS", _as_bool(lib_file.get("include_specials"), False)),
        cache_ttl=_env_int("RMH_EMBY_CACHE_TTL", _as_int(lib_file.get("cache_ttl")) or 300),
        verify_tls=_env_bool("RMH_EMBY_VERIFY_TLS", _as_bool(lib_file.get("verify_tls"), False)),
        timeout=float(_env_int("RMH_HTTP_TIMEOUT", 20)),
        # Emby 默认直连；只有显式配了 RMH_EMBY_PROXY 才走代理
        proxy=_env("RMH_EMBY_PROXY") or _as_str(lib_file.get("proxy")),
    )

    subs = load_subscriptions(cfg_dir / "subscriptions.yaml")

    return Settings(
        config_dir=cfg_dir,
        state_dir=st_dir,
        telegram=telegram,
        tmdb=tmdb,
        library=library,
        poll_interval=_env_int("RMH_POLL_INTERVAL", _as_int(run_file.get("poll_interval")) or 900),
        reconcile_interval=_env_int("RMH_RECONCILE_INTERVAL", _as_int(run_file.get("reconcile_interval")) or 1800),
        seed_silent=_env_bool("RMH_SEED_SILENT", _as_bool(run_file.get("seed_silent"), False)),
        health_port=_env_int("RMH_HEALTH_PORT", _as_int(run_file.get("health_port")) or 8080),
        log_level=_env("RMH_LOG_LEVEL", _as_str(run_file.get("log_level"), "INFO")).upper(),
        max_items_per_poll=_env_int("RMH_MAX_ITEMS_PER_POLL", _as_int(run_file.get("max_items_per_poll")) or 40),
        scan_cache_ttl=_env_int(
            "RMH_SCAN_CACHE_TTL", _as_int(run_file.get("scan_cache_ttl")) or 12 * 3600
        ),
        scan_concurrency=_env_int("RMH_SCAN_CONCURRENCY", _as_int(run_file.get("scan_concurrency")) or 5),
        scan_max_series=_env_int("RMH_SCAN_MAX_SERIES", _as_int(run_file.get("scan_max_series")) or 0),
        ui_token=_env("RMH_UI_TOKEN") or _as_str(ui_file.get("token")),
        ui_enabled=_env_bool("RMH_UI_ENABLED", _as_bool(ui_file.get("enabled"), True)),
        subscriptions=subs,
    )


# --------------------------------------------------------------------------
# 网页 UI 写配置
# --------------------------------------------------------------------------

# 允许网页 UI 修改的字段白名单：组 -> {字段名: 类型}
# 白名单之外的一律拒绝，避免 UI 写坏配置文件。
UI_EDITABLE: dict[str, dict[str, str]] = {
    "telegram": {
        "bot_token": "str",
        "chat_id": "str",
        "thread_id": "str",
        "api_base": "str",
        "proxy": "str",
        "send_poster": "bool",
        "feed_poster": "bool",
        "disable_notification": "bool",
    },
    "tmdb": {
        "api_key": "str",
        "api_base": "str",
        "language": "str",
        "proxy": "str",
    },
    "library": {
        "url": "str",
        "api_key": "str",
        "user_id": "str",
        "kind": "str",
        "count_aired_only": "bool",
        "include_specials": "bool",
        "verify_tls": "bool",
        "cache_ttl": "int",
        "proxy": "str",
    },
    "runtime": {
        "poll_interval": "int",
        "reconcile_interval": "int",
        "seed_silent": "bool",
        "log_level": "str",
        "scan_cache_ttl": "int",
        "scan_concurrency": "int",
        "scan_max_series": "int",
    },
    "ui": {
        "token": "str",
        "enabled": "bool",
    },
}


def apply_overrides(
    config_dir: Path,
    *,
    changes: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """把网页 UI 提交的改动合并进 settings.yaml。

    changes 形如 {"telegram": {"bot_token": "123:abc"}}。
    空字符串表示"清除此项"。返回合并后的完整配置。
    只接受 UI_EDITABLE 里登记的字段，其他一律忽略并记录。
    """
    path = overrides_path(config_dir)
    current = load_yaml_file(path, default={}) or {}
    if not isinstance(current, dict):
        current = {}

    ignored: list[str] = []
    for group, fields in (changes or {}).items():
        if group not in UI_EDITABLE or not isinstance(fields, dict):
            ignored.extend([f"{group}.{k}" for k in (fields or {})])
            continue
        allowed = UI_EDITABLE[group]
        bucket = current.setdefault(group, {})
        if not isinstance(bucket, dict):
            bucket = {}
            current[group] = bucket
        for key, value in fields.items():
            if key not in allowed:
                ignored.append(f"{group}.{key}")
                continue
            kind = allowed[key]
            if value is None:
                bucket.pop(key, None)
                continue
            if kind == "bool":
                bucket[key] = _as_bool(value, False)
            elif kind == "int":
                parsed = _as_int(value)
                if parsed is None:
                    ignored.append(f"{group}.{key}")
                    continue
                bucket[key] = parsed
            else:
                text = _as_str(value)
                if text == "":
                    bucket.pop(key, None)  # 空字符串 = 恢复默认
                else:
                    bucket[key] = text

    # 清理空分组，让文件好看一点
    for group in list(current):
        if isinstance(current[group], dict) and not current[group]:
            current.pop(group)

    dump_yaml_file(
        path,
        current,
        header=(
            "由网页 UI 保存的配置（优先级高于 config.yaml，低于环境变量）\n"
            "想恢复默认就删掉对应行，或直接删掉本文件。\n"
            "注意：环境变量里已经设置的值会覆盖这里的设置。\n"
        ),
    )
    return {"settings": current, "ignored": ignored}
