"""通用 NexusPHP 页面搜索：按站点档案拼请求，按表头识别列，解析种子列表。"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urljoin

import lxml.html

from .errors import CloudflareChallenge, CookieExpired, SiteMaintenance, TwoFactorRequired
from .models import Torrent
from .profiles import SiteProfile

# 表头排序链接 sort=N 是 NexusPHP 的通用约定，改过主题的站点（如站点J）也保留了它。
SORT_COLUMNS = {"3": "comments", "4": "time", "5": "size", "6": "grabs", "7": "seeders", "8": "leechers"}
# 表头文字或图标说明；“做种/下载”合在一列时单独识别。按顺序匹配，先匹配更具体的写法。
HEADER_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("peers", ("做种/下载", "做種/下載")),
    ("size", ("size", "大小", "體積", "体积")),
    ("seeders", ("seeders", "做种", "做種", "种子数")),
    ("leechers", ("leechers", "下载数", "下載數")),
    ("grabs", ("snatched", "完成")),
    ("time", ("time", "存活", "添加", "发布时间")),
    ("comments", ("comments", "评论", "評論")),
)
# NexusPHP 优惠图标：下载折扣与上传倍数。
PROMOTIONS: dict[str, tuple[float, float]] = {
    "free2up": (0.0, 2.0),
    "free": (0.0, 1.0),
    "2up": (1.0, 2.0),
    "50pctdown2up": (0.5, 2.0),
    "50pctdown": (0.5, 1.0),
    "30pctdown": (0.3, 1.0),
}
PROMOTION_CLASS = re.compile(r"\bpro_(free2up|50pctdown2up|free|2up|50pctdown|30pctdown)\b", re.I)
IMDB_LINK = re.compile(r"imdb\.com/title/(tt\d{5,10})", re.I)
SIZE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(B|Ki?B|Mi?B|Gi?B|Ti?B)\b", re.I)
SIZE_UNITS = {"b": 1, "kb": 1024, "kib": 1024, "mb": 1024**2, "mib": 1024**2,
              "gb": 1024**3, "gib": 1024**3, "tb": 1024**4, "tib": 1024**4}
# 登录页与权限提示：沿用 MoviePilot 的判断词，另加密码表单特征。
LOGIN_MARKERS = ("未登录", "登录 / 注册", "必须在登录后才能访问", "你需要启用cookies才能登录")
# 详情页上“种子不存在或已被删除”的提示：NexusPHP 简繁英语言包与常见改版（站点A写作“没有此 ID 的种子”）。
# “你没有该权限”既可能是种子不存在也可能是权限不足，不算删除。
DELETED_MARKERS = re.compile(
    r"[没沒]有[该該此]\s*ID\s*的[种種]子|[种種]子不存在|[种種]子已被[删刪]除|No torrent with ID|Invalid ID|[无無]效的\s*ID",
    re.I,
)


def build_params(profile: SiteProfile, title: str, imdb_id: str | None) -> dict[str, Any]:
    """搜索参数：AND 模式、不计入站点热搜；IMDb 搜索按档案选择写法，不支持时改用片名。"""
    params: dict[str, Any] = dict(profile.extra_params)
    keyword = title
    if imdb_id and profile.imdb_search == "keyword":
        keyword = profile.imdb_format.format(imdb=imdb_id, imdb_num=imdb_id[2:])
    elif imdb_id and profile.imdb_search == "area":
        keyword = imdb_id
    params[profile.keyword_param] = keyword
    if profile.keyword_param == "search":
        # NexusPHP 的 search_area：0 = 标题，4 = IMDb 链接。
        params.update({"search_area": 4 if imdb_id and profile.imdb_search == "area" else 0, "search_mode": 0, "notnewword": 1})
    return params


def detect_interruption(final_path: str, html: str) -> None:
    """Cookie 能用不代表能搜：登录页、二次验证、维护公告、Cloudflare 都要明确报出来。"""
    path = (final_path or "").lower()
    head = html[:200_000]
    if path.endswith(("/login.php", "/takelogin.php")):
        raise CookieExpired()
    if re.search(r"2fa|twofactor|two_factor", path):
        raise TwoFactorRequired()
    if re.search(r"/(?:claim|maintain|maintenance|upgrade)\b", path):
        raise SiteMaintenance()
    if "<title>Just a moment...</title>" in head:
        raise CloudflareChallenge()
    title = re.search(r"<title>(.*?)</title>", head, re.I | re.S)
    if title and (title.group(1).strip() == "登录" or ":: 登录" in title.group(1)):
        raise CookieExpired()
    has_password = bool(re.search(r"<input[^>]+(?:name|type)=[\"'](?:password|passwd)[\"']", head, re.I))
    posts_login = bool(re.search(r"<form[^>]+action=[\"'][^\"']*(?:take)?login\.php", head, re.I))
    has_username = bool(re.search(r"<input[^>]+name=[\"'](?:username|user|uid)[\"']", head, re.I))
    if has_password and (posts_login or has_username):
        raise CookieExpired()
    if any(marker in head for marker in LOGIN_MARKERS) and "logout.php" not in head:
        raise CookieExpired()
    # 登录表单里嵌的 Turnstile 验证码也来自 challenges.cloudflare.com，所以放在登录页判断之后。
    if "challenges.cloudflare.com" in head[:5000] and "logout.php" not in head:
        raise CloudflareChallenge()


def torrent_deleted(status_code: int, html: str) -> bool:
    """详情页是否表明种子已不存在；只认明确的提示，拿不准时按仍然存在处理。"""
    if status_code in (404, 410):
        return True
    return bool(DELETED_MARKERS.search(html[:200_000]))


def _text(element: Any) -> str:
    return re.sub(r"\s+", " ", element.text_content() or "").strip()


def _own_rows(table: Any) -> list[Any]:
    """列表表格自己的行：跳过单元格里嵌套的小表格，兼容行被 <form> 或 <tbody> 包住的站点（如站点H）。"""
    return [row for row in table.iter("tr") if next(row.iterancestors("table"), None) is table]


def _header_columns(row: Any, profile: SiteProfile) -> dict[str, int]:
    columns: dict[str, int] = {}
    for index, cell in enumerate(row.xpath("./td|./th")):
        field = None
        if profile.header_by_sort:
            for link in cell.xpath(".//a/@href"):
                match = re.search(r"[?&]sort=(\d+)", link)
                if match and match.group(1) in SORT_COLUMNS:
                    field = SORT_COLUMNS[match.group(1)]
                    break
        if field is None:
            described = " ".join([_text(cell), *cell.xpath(".//@alt|.//@title|.//@class")]).lower()
            field = next((name for name, words in HEADER_WORDS if any(word.lower() in described for word in words)), None)
        if field and field not in columns:
            columns[field] = index
    return columns


def _number(text: str) -> int:
    match = re.search(r"\d[\d,]*", text or "")
    return int(match.group(0).replace(",", "")) if match else 0


def _size(text: str) -> int:
    match = SIZE.search(text or "")
    if not match:
        return 0
    return int(float(match.group(1).replace(",", ".")) * SIZE_UNITS[match.group(2).lower()])


def _publish_time(cell: Any, now: datetime) -> str | None:
    """时间列：优先读 title 里的完整时间，其次是日期文字或“2月 3天”这类存活时间。"""
    for value in [*cell.xpath(".//@title"), _text(cell)]:
        match = re.search(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})", value)
        if match:
            return f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    text = _text(cell)
    if "昨天" in text:
        return (now - timedelta(days=1)).strftime("%Y-%m-%d")
    days = 0
    for unit, factor in (("年", 365), ("月", 30), ("周", 7), ("天", 1)):
        match = re.search(rf"(\d+)\s*{unit}", text)
        if match:
            days += int(match.group(1)) * factor
    return (now - timedelta(days=days)).strftime("%Y-%m-%d") if days or re.search(r"\d+\s*(?:时|小时|分)", text) else None


def _promotion(row: Any) -> tuple[float, float, list[str]]:
    classes = " ".join(row.xpath(".//@class"))
    match = PROMOTION_CLASS.search(classes)
    if match:
        down, up = PROMOTIONS[match.group(1).lower()]
    elif re.search(r"(?:^|[\s_/.-])(freeleech|free)(?:[\s_/.-]|$)", " ".join(row.xpath(".//img/@src|.//img/@alt|.//@class")), re.I):
        down, up = 0.0, 1.0
    else:
        down, up = 1.0, 1.0
    labels = []
    if down == 0:
        labels.append("FREE")
    elif down < 1:
        labels.append(f"{int(down * 100)}%")
    if up > 1:
        labels.append(f"{up:g}X")
    return down, up, labels


def parse_page(html: str, profile: SiteProfile, base_url: str, *, now: datetime | None = None) -> list[Torrent]:
    """解析一页搜索结果（纯函数，测试直接喂保存下来的页面）。"""
    now = now or datetime.now()
    document = lxml.html.fromstring(html)
    detail_regex, download_regex = profile.detail_regex, profile.download_regex
    tables = document.cssselect(profile.list_selector)
    if not tables:
        # 没有档案里的列表表格（未登记的站点或改过主题）：退回到种子行最多的表格。
        scored = [
            (sum(1 for row in _own_rows(table) if any(detail_regex.search(href) for href in row.xpath(".//a/@href"))), table)
            for table in document.iter("table")
        ]
        tables = [table for count, table in sorted(scored, key=lambda pair: -pair[0]) if count][:1]
    if not tables:
        return []
    rows = _own_rows(tables[0])
    header = next((row for row in rows if row.xpath("./th") or row.xpath("./td[contains(@class,'colhead')]")), None)
    if header is None and rows and not any(detail_regex.search(href) for href in rows[0].xpath(".//a/@href")):
        header = rows[0]
    columns = _header_columns(header, profile) if header is not None else {}
    results: dict[str, Torrent] = {}
    for row in rows:
        if row is header:
            continue
        links = row.xpath(".//a[@href]")
        detail = next((link for link in links if detail_regex.search(link.get("href", ""))), None)
        if detail is None:
            continue
        download = next((link.get("href") for link in links if download_regex.search(link.get("href", ""))), None)
        method = "get"
        if not download:
            action = next((form.get("action") for form in row.xpath(".//form[@action]") if download_regex.search(form.get("action", ""))), None)
            download, method = action, "post"
        if not download:
            continue
        title = (detail.get("title") or _text(detail)).strip()
        if not title:
            continue
        cells = row.xpath("./td")
        value = lambda field: _text(cells[columns[field]]) if field in columns and columns[field] < len(cells) else ""
        seeders, leechers = _number(value("seeders")), _number(value("leechers"))
        grabs = _number(value("grabs"))
        if "seeders" not in columns and "peers" not in columns:
            # 没有表头时按 NexusPHP 的列顺序推断：大小之后依次是做种、下载、完成。
            size_index = next((index for index, cell in enumerate(cells) if SIZE.search(_text(cell))), None)
            counts = [_number(_text(cell)) for cell in cells[size_index + 1:] if re.fullmatch(r"[\d,]+", _text(cell))] if size_index is not None else []
            seeders, leechers, grabs = (counts + [0, 0, 0])[:3]
        peers = re.fullmatch(r"\s*([\d,]+)\s*/\s*([\d,]+)\s*", value("peers") or "")
        if peers:
            seeders, leechers = _number(peers.group(1)), _number(peers.group(2))
        size_text = value("size") or next((_text(cell) for cell in cells if SIZE.search(_text(cell))), "")
        detail_cell = next(detail.iterancestors("td"), None)
        description = ""
        if detail_cell is not None:
            description = _text(detail_cell).replace(_text(detail), "", 1).strip()[:200]
        down, up, labels = _promotion(row)
        imdb = next((match.group(1) for href in row.xpath(".//a/@href") if (match := IMDB_LINK.search(href))), None)
        if imdb:
            # 部分站点省略前导零（tt68646），IMDb 编号至少 7 位。
            imdb = "tt" + imdb[2:].zfill(7)
        time_cell = cells[columns["time"]] if "time" in columns and columns["time"] < len(cells) else None
        torrent = Torrent(
            title=title,
            description=description,
            detail_url=urljoin(base_url, detail.get("href")),
            download_url=urljoin(base_url, download),
            size=_size(size_text),
            seeders=seeders,
            leechers=leechers,
            grabs=grabs,
            publish_time=_publish_time(time_cell, now) if time_cell is not None else None,
            download_factor=down,
            upload_factor=up,
            imdb_id=imdb,
            download_method=method,
            labels=labels,
        )
        # 同一种子在嵌套表格里可能出现两次，按下载地址去重，保留信息更完整的一条。
        current = results.get(torrent.download_url)
        if current is None or torrent.size > current.size:
            results[torrent.download_url] = torrent
    return list(results.values())
