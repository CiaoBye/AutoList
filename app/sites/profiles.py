"""站点档案：描述一个站点怎么搜、结果在哪、链接长什么样。

新增站点时优先只写档案；只有页面结构或接口与 NexusPHP 完全不同才需要新的解析器。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

# NexusPHP 默认规则。详情链接前面不能紧跟字母，避免把 userdetails.php?id= 当成种子详情。
NEXUS_DETAIL = r"(?<![a-z])details\.php\?[^#]*\bid=\d+"
NEXUS_DOWNLOAD = r"(?<![a-z])download\.php\?"


@dataclass(frozen=True)
class SiteProfile:
    key: str
    # nexusphp：通用 NexusPHP 页面；official_api：站点D官方 API。
    framework: str = "nexusphp"
    domains: tuple[str, ...] = ()
    search_path: str = "torrents.php"
    keyword_param: str = "search"
    extra_params: dict[str, str] = field(default_factory=dict)
    # IMDb 搜索方式：area（NexusPHP search_area=4）、keyword（把编号写进关键词）或 None（不支持，改用片名）。
    imdb_search: str | None = "area"
    # keyword 方式下关键词的写法，可用 {imdb}（tt0068646）与 {imdb_num}（0068646）。
    imdb_format: str = "{imdb}"
    list_selector: str = "table.torrents"
    detail_pattern: str = NEXUS_DETAIL
    download_pattern: str = NEXUS_DOWNLOAD
    # 表头优先按排序链接 sort=N 识别列（NexusPHP 通用），不符合约定的站点改按表头文字识别。
    header_by_sort: bool = True

    def matches(self, host: str) -> bool:
        return any(host == domain or host.endswith(f".{domain}") for domain in self.domains)

    @property
    def detail_regex(self) -> re.Pattern[str]:
        return re.compile(self.detail_pattern, re.I)

    @property
    def download_regex(self) -> re.Pattern[str]:
        return re.compile(self.download_pattern, re.I)


NEXUSPHP = SiteProfile(key="nexusphp")

PROFILES: tuple[SiteProfile, ...] = (
    # 站点A：搜索框是 search_field，IMDb 写成“imdb0068646”；种子链接为 /t/<id>/ 与 /dl/<id>/。
    SiteProfile(
        key="alt_layout",
        domains=("totheglory.im",),
        search_path="browse.php",
        keyword_param="search_field",
        extra_params={"c": "M"},
        imdb_search="keyword",
        imdb_format="imdb{imdb_num}",
        list_selector="#torrent_table",
        detail_pattern=r"(?:^|/)t/\d+/?$",
        download_pattern=r"(?:^|/)dl/\d+/",
        header_by_sort=False,
    ),
    # 站点D：网页有二次验证，填了 API Key 时改走官方搜索接口。
    SiteProfile(key="official_api_site", framework="official_api", domains=("hddolby.com",)),
)


def profile_for(base_url: str, *, has_api_key: bool = False) -> SiteProfile:
    """按站点地址选择档案；需要 API Key 的档案在没有 Key 时退回通用 NexusPHP。"""
    host = (urlparse(str(base_url or "")).hostname or "").lower()
    for profile in PROFILES:
        if profile.matches(host):
            if profile.framework.endswith("_api") and not has_api_key:
                return NEXUSPHP
            return profile
    return NEXUSPHP
