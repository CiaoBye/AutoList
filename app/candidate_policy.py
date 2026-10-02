"""Movie candidate parsing and deterministic AutoList policy evaluation."""

from __future__ import annotations

import re
from typing import Any

from .util import to_int


# AutoList's built-in canonical names and aliases. The order matters: preferred
# groups are evaluated before broader site-family expressions.

def _has_uneven_alternation(rule: str) -> bool:
    """交替式指数回溯检测（审计 2-3 残余）：``(a|aa)+b`` 族无法被嵌套量词
    规则识别。组后带量词且组内交替分支长度不一致时，失败路径呈指数级
    回溯；等长分支（如 ``(?:AB|CD)+``）保持线性，仍允许。
    """
    stack: list[int] = []
    for index, char in enumerate(rule):
        if char == "(":
            stack.append(index)
        elif char == ")" and stack:
            start = stack.pop()
            inner = rule[start + 1:index]
            after = rule[index + 1:index + 2]
            if after in ("*", "+", "{"):
                if inner.startswith("?:"):
                    inner = inner[2:]
                branches = [len(branch) for branch in inner.split("|")]
                if len(set(branches)) > 1:
                    return True
    return False

NESTED_QUANTIFIER = re.compile(r"\([^()]*[+*][^()]*\)\s*[+*{]")


def _has_nested_quantifier(rule: str) -> bool:
    """Detect nested quantifiers (e.g. ``(?:A+)+``) that enable catastrophic backtracking.

    A quantified group whose own body contains another quantifier is the dangerous
    shape; a single quantifier over a plain alternation (``(?:AB|CD)+``) stays
    linear and remains allowed.
    """
    if re.search(r"[+*][+*{]", rule):
        return True
    stack: list[int] = []
    for index, char in enumerate(rule):
        if char == "(":
            stack.append(index)
        elif char == ")" and stack:
            start = stack.pop()
            inner = rule[start + 1:index]
            after = rule[index + 1:index + 2]
            if re.search(r"[+*{]", inner) and after in ("*", "+", "{"):
                return True
    return False

BUILTIN_RELEASE_GROUP_RULES: tuple[tuple[str, str], ...] = (
    ("ADE", r"ADE"),
    ("FRDS", r"FRDS"),
    # HDSPad / CHDPAD 是面向 iPad 的小体积压制，不算正规组发布，不归入 HDS / CHD。
    ("HDS", r"HDS(?:ky|TV|WEB|)"),
    ("HDS", r"AQLJ"),
    ("CHD", r"CHD(?:Bits|HKTV|TV|WEB|)"),
    ("CHD", r"StBOX|OneHD|Lee|xiaopie"),
    ("CMCT", r"CMCT(?:V|)"),
    ("0FF", r"FF(?:(?:A|WE)B|CD|E(?:DU|B)|TV)"),
    ("AUDIENCES", r"Audies|AD(?:Audio|E(?:book|)|Music|Web)"),
    ("BEITAI", r"BeiTai"),
    ("BTSCHOOL", r"Bts(?:CHOOL|HD|PAD|TV)|Zone"),
    ("CARPT", r"CarPT"),
    ("TLF", r"(?:(?:iNT|(?:HALFC|Mini(?:S|H|FH)D))-|)TLF"),
    ("GAINBOUND", r"(?:DG|GBWE)B"),
    ("HARES", r"Hares(?:(?:M|T)V|Web|)"),
    ("HDAREA", r"HDA(?:pad|rea|TV)|EPiC"),
    ("HDCHINA", r"HDC(?:hina|TV|)|k9611|tudou|iHD"),
    ("HDDOLBY", r"D(?:ream|BTV)|(?:HD|QHstudI)o"),
    ("HDFANS", r"beAst(?:TV|)"),
    ("HDHOME", r"HDH(?:ome|Pad|TV|WEB|)"),
    ("HDPT", r"HDPT(?:Web|)"),
    ("HDZONE", r"HDZ(?:one|)"),
    ("HHWEB", r"HHWEB"),
    ("HTPT", r"HTPT"),
    ("YUMI", r"Yumi"),
    ("CXCY", r"cXcY"),
    ("LEMONHD", r"L(?:eague(?:(?:C|H)D|(?:M|T)V|NF|WEB)|HD)|i18n|CiNT"),
    ("MTEAM", r"MTeam(?:TV|)|MPAD|MWeb"),
    ("OURBITS", r"Our(?:Bits|TV)|FLTTH|Ao|PbK|MGs|iLove(?:HD|TV)"),
    ("PANDA", r"Panda|AilMWeb"),
    ("PIGGO", r"PiGo(?:NF|(?:H|WE)B)"),
    ("PTER", r"PTer(?:DIY|Game|(?:M|T)V|WEB|)"),
    ("PTHOME", r"PTH(?:Audio|eBook|music|ome|tv|WEB|)"),
    ("PTSBAO", r"PTsbao|OPS|F(?:Fans(?:AIeNcE|BD|D(?:VD|IY)|TV|WEB)|HDMv)|SGXT"),
    ("PUTAO", r"PuTao"),
    ("SHARKPT", r"Shark(?:WEB|DIY|TV|MV|)"),
    ("TJUPT", r"TJUPT"),
    ("TTG", r"TTG|WiKi|NGB|DoA|(?:ARi|ExRE)N"),
    ("BMDru", r"BMDru"),
    ("BEYONDHD", r"BeyondHD"),
    ("BTN", r"BTN"),
    ("CFANDORA", r"Cfandora"),
    ("CTRLHD", r"CtrlHD"),
    ("CMRG", r"CMRG"),
    ("DON", r"DON"),
    ("EVO", r"EVO"),
    ("FLUX", r"FLUX"),
    ("HONEY", r"HONE(?:yG|)"),
    ("NTB", r"NTb"),
    ("NTG", r"NTG"),
    ("NOGROUP", r"NoGroup"),
    ("PANDAMOON", r"PandaMoon"),
    ("SMURF", r"SMURF"),
    ("TEPES", r"TEPES"),
    ("FURY", r"Fury"),
    ("SPM", r"SPM"),
    ("TAENGOO", r"Taengoo"),
    ("TROLLHD", r"TrollHD"),
    ("ANI", r"ANi"),
    ("HYSUB", r"HYSUB"),
    ("KTXP", r"KTXP"),
    ("LOLIHOUSE", r"LoliHouse"),
    ("MCE", r"MCE"),
    ("NEKOMOE", r"Nekomoe kissaten"),
    ("SWEETSUB", r"SweetSub"),
    ("MINGY", r"MingY"),
    ("RAWS", r"(?:Lilith|NC)-Raws"),
    ("FROG", r"FROG(?:E|Web|)"),
    ("UBITS", r"UB(?:its|WEB|TV)"),
    ("喵萌奶茶屋", r"喵萌奶茶屋"),
    ("织梦字幕组", r"织梦字幕组"),
    ("银色子弹字幕组", r"银色子弹字幕组"),
    ("绿茶字幕组", r"绿茶字幕组"),
    ("枫叶字幕组", r"枫叶字幕组"),
    ("猎户手抄部", r"猎户手抄部"),
    ("漫猫字幕社", r"漫猫字幕社"),
    ("霜庭云花SUB", r"霜庭云花Sub"),
    ("北宇治字幕组", r"北宇治字幕组"),
    ("氢气烤肉架", r"氢气烤肉架"),
    ("云歌字幕组", r"云歌字幕组"),
    ("萌樱字幕组", r"萌樱字幕组"),
    ("极影字幕社", r"极影字幕社"),
    ("悠哈璃羽字幕社", r"悠哈璃羽字幕社"),
    ("拨雪寻春", r"❀拨雪寻春❀"),
    ("沸羊羊", r"沸羊羊(?:制作|字幕组)"),
    ("樱都字幕组", r"(?:桜|樱)都字幕组"),
)

# 知名压制组的惯例编码：组名出现但标题编码与惯例不符时，判定为冒用组名的发布。
# Fury 是 x265 压制组、SPM 是 x264 压制组；HDSky 等站点上常见组名被其他发布借用。
KNOWN_GROUP_CODECS: dict[str, str] = {
    "FURY": "x265",
    "SPM": "x264",
}


DEFAULT_HARD_EXCLUSIONS: tuple[dict[str, Any], ...] = (
    {"id": "diy", "label": "DIY", "enabled": True},
    {"id": "remux", "label": "REMUX", "enabled": True},
    {"id": "webdl", "label": "WEB-DL / WEBDL", "enabled": True},
    {"id": "webrip", "label": "WEBRip", "enabled": True},
    {"id": "bdmv", "label": "BDMV", "enabled": True},
    {"id": "iso", "label": "ISO", "enabled": True},
    {"id": "complete_bluray", "label": "COMPLETE BLU-RAY", "enabled": True},
    {"id": "full_bluray", "label": "FULL BLU-RAY", "enabled": True},
    {"id": "raw_disc", "label": "原盘结构 / AVC、MPEG-2 原盘", "enabled": True},
)

DEFAULT_POLICY: dict[str, Any] = {
    "profiles": [
        {"id": "primary_x265", "label": "首选 x265", "enabled": True,
         "codecs": ["x265"], "groups": ["ADE", "FRDS", "HDS", "CHD"], "tier": 1},
        {"id": "fallback_x264", "label": "保底 x264", "enabled": True,
         "codecs": ["x264"], "groups": ["CMCT"], "tier": 2},
    ],
    "resolution_order": ["2160p", "1080p"],
    "hard_exclusions": list(DEFAULT_HARD_EXCLUSIONS),
    "custom_release_groups": [],
    "candidate_limit": 6,
}


def normalized_policy(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    default_profiles_value = DEFAULT_POLICY.get("profiles")
    default_profiles = default_profiles_value if isinstance(default_profiles_value, list) else []
    profiles_value = source.get("profiles")
    profiles = profiles_value if isinstance(profiles_value, list) else default_profiles
    cleaned_profiles: list[dict[str, Any]] = []
    for index, profile in enumerate(profiles[:8]):
        if not isinstance(profile, dict):
            continue
        codecs = list(dict.fromkeys(str(item).strip().lower() for item in profile.get("codecs", []) if str(item).strip()))
        groups = list(dict.fromkeys(str(item).strip().upper() for item in profile.get("groups", []) if str(item).strip()))
        if not codecs or not groups:
            continue
        cleaned_profiles.append({
            "id": str(profile.get("id") or f"profile_{index + 1}")[:40],
            "label": str(profile.get("label") or f"策略 {index + 1}")[:40],
            "enabled": bool(profile.get("enabled", True)),
            "codecs": codecs, "groups": groups,
            "tier": max(1, min(to_int(profile.get("tier"), index + 1), 9)),
        })
    if not cleaned_profiles:
        cleaned_profiles = [dict(item) for item in DEFAULT_POLICY["profiles"]]

    default_exclusions = {item["id"]: item for item in DEFAULT_HARD_EXCLUSIONS}
    configured = {
        str(item.get("id")): item for item in source.get("hard_exclusions", [])
        if isinstance(item, dict) and item.get("id") in default_exclusions
    }
    exclusions = [
        {**default, "enabled": bool(configured.get(rule_id, {}).get("enabled", default["enabled"]))}
        for rule_id, default in default_exclusions.items()
    ]
    resolutions = [value for value in source.get("resolution_order", []) if value in {"2160p", "1080p", "720p"}]
    resolutions = list(dict.fromkeys(resolutions)) or list(DEFAULT_POLICY["resolution_order"])
    custom = merge_custom_rules(source.get("custom_release_groups", []))
    return {
        "profiles": cleaned_profiles,
        "resolution_order": resolutions,
        "hard_exclusions": exclusions,
        "custom_release_groups": custom,
        "candidate_limit": max(1, min(to_int(source.get("candidate_limit"), 6), 20)),
    }


def merge_custom_rules(values: Any) -> list[str]:
    if isinstance(values, str):
        values = values.splitlines()
    if not isinstance(values, list):
        return []
    builtins = {pattern.casefold() for _, pattern in BUILTIN_RELEASE_GROUP_RULES}
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        rule = str(value or "").strip()
        key = rule.casefold()
        if not rule or key in seen or key in builtins:
            continue
        if len(rule) > 160:
            raise ValueError("单条自定义制作组规则不能超过 160 个字符")
        try:
            re.compile(rule, re.I)
        except re.error as exc:
            raise ValueError(f"无效的制作组规则：{rule}") from exc
        if NESTED_QUANTIFIER.search(rule) or _has_nested_quantifier(rule):
            raise ValueError("规则包含嵌套量词（如 (?:A+)+），可能造成匹配性能问题")
        if _has_uneven_alternation(rule):
            raise ValueError("规则包含不等长交替分支的量词（如 (a|aa)+），可能造成匹配性能问题")
        seen.add(key)
        result.append(rule)
    return result[:300]


def release_group_catalog(policy: dict[str, Any]) -> dict[str, Any]:
    custom = merge_custom_rules(policy.get("custom_release_groups", []))
    names = list(dict.fromkeys(name for name, _ in BUILTIN_RELEASE_GROUP_RULES))
    return {
        "builtin_count": len(BUILTIN_RELEASE_GROUP_RULES),
        "custom_count": len(custom),
        "merged_count": len(BUILTIN_RELEASE_GROUP_RULES) + len(custom),
        "builtin_names": names,
        "custom_rules": custom,
    }


def match_release_group(title: str, policy: dict[str, Any]) -> str | None:
    # 先剥掉 @站点名 后缀（如 Fury@HDSky / SPM@HDSky），站点名不能参与组名识别，
    # 否则 HDSky 会先命中 HDS 规则，把其他组的发布误判为 HDS 正规组。
    wrapped = re.sub(r"@[A-Za-z0-9._-]+", " ", f" {title} ")
    left = r"(?<=[\-@\[￡【&._\s])"
    right = r"(?=$|[@.\s\]\[】&/_-])"
    for name, pattern in BUILTIN_RELEASE_GROUP_RULES:
        if re.search(f"{left}(?:{pattern}){right}", wrapped, re.I):
            return name.upper()
    for pattern in merge_custom_rules(policy.get("custom_release_groups", [])):
        match = re.search(f"{left}(?:{pattern}){right}", wrapped, re.I)
        if match:
            return str(match.group(0)).upper()
    return None


# 只认 2160p / 1080i 这类写法；“4K REMASTERED”之类的说明不算分辨率。
MULTI_RESOLUTION = re.compile(r"(?<![0-9])(2160|1440|1080|720|576|480)[PI](?![0-9A-Z])", re.I)


def parse_resolution(upper: str) -> str:
    if "2160P" in upper or re.search(r"(?:^|\W)4K(?:$|\W)", upper):
        return "2160p"
    if "1080P" in upper or "1080I" in upper:
        return "1080p"
    if "720P" in upper:
        return "720p"
    return "其他"


def parse_codec(upper: str) -> str:
    if any(marker in upper for marker in ("X265", "H.265", "H265", "HEVC")):
        return "x265"
    if any(marker in upper for marker in ("X264", "H.264", "H264")):
        return "x264"
    # Bare AVC is kept as x264 metadata, but raw-disc detection can still reject it.
    if re.search(r"(?:^|[ ._-])AVC(?:$|[ ._-])", upper):
        return "x264"
    return "其他"


def parse_source(upper: str) -> str:
    if "REMUX" in upper:
        return "REMUX"
    if re.search(r"WEB[ ._-]?DL", upper):
        return "WEB-DL"
    if re.search(r"WEB[ ._-]?RIP", upper):
        return "WEBRIP"
    if "BLURAY" in upper or "BLU-RAY" in upper or "BDRIP" in upper:
        return "BLURAY"
    return "其他"


def hard_exclusion_reason(title: str, policy: dict[str, Any]) -> str | None:
    upper = title.upper()
    enabled = {item["id"] for item in policy.get("hard_exclusions", []) if item.get("enabled")}
    checks = (
        ("diy", "包含 DIY", r"DIY"),
        ("remux", "包含 REMUX", r"(?:^|[ ._-])REMUX(?:$|[ ._-])"),
        ("webdl", "WEB-DL / WEBDL 不在电影策略范围", r"WEB[ ._-]?DL"),
        ("webrip", "WEBRip 不在电影策略范围", r"WEB[ ._-]?RIP"),
        ("bdmv", "检测到 BDMV 原盘结构", r"(?:^|[ ._-])BDMV(?:$|[ ._-])"),
        ("iso", "检测到 ISO 原盘镜像", r"(?:^|[ ._-])ISO(?:$|[ ._-])"),
        ("complete_bluray", "检测到 COMPLETE BLU-RAY", r"COMPLETE[ ._-]+BLU[ ._-]?RAY"),
        ("full_bluray", "检测到 FULL BLU-RAY", r"FULL[ ._-]+BLU[ ._-]?RAY"),
    )
    for rule_id, reason, pattern in checks:
        if rule_id in enabled and re.search(pattern, upper, re.I):
            return reason
    if "raw_disc" in enabled:
        is_bluray = "BLURAY" in upper or "BLU-RAY" in upper or "UHD BD" in upper
        has_raw_codec = bool(re.search(r"(?:^|[ ._-])(?:AVC|MPEG[ ._-]?2)(?:$|[ ._-])", upper))
        has_encode_marker = any(marker in upper for marker in ("X264", "X265", "H264", "H265", "H.264", "H.265", "HEVC"))
        # BDrip/重编码标记放行：只有无编码标记的完整原盘结构才判为原盘。
        is_bdrip = "BDRIP" in upper or bool(re.search(r"(?:^|[ ._-])RE(?:-|_)?ENCODE(?:$|[ ._-])", upper))
        if is_bluray and has_raw_codec and not has_encode_marker and not is_bdrip:
            return "检测到 AVC / MPEG-2 完整蓝光原盘结构"
    return None


def analyze(title: str, index: int, policy_value: Any, torrent: dict[str, Any] | None = None) -> dict[str, Any]:
    policy = normalized_policy(policy_value)
    torrent = torrent or {}
    upper = title.upper()
    resolution = parse_resolution(upper)
    codec = parse_codec(upper)
    source = parse_source(upper)
    group = match_release_group(title, policy)
    exclusion = hard_exclusion_reason(title, policy)
    # 同时打包多个分辨率（如 2160p&1080p）体积翻倍且多半是原盘合集，不作为单一资源入馆。
    if not exclusion and len(set(MULTI_RESOLUTION.findall(title))) >= 2:
        exclusion = "同时包含多个分辨率版本"
    seeders = to_int(torrent.get("seeders") or torrent.get("seeder"))
    # 0 人做种的资源无法实际下载，直接排除（仅在实际搜索/试算携带种子数据时生效）。
    if torrent and not exclusion and seeders == 0:
        exclusion = "0 人做种，无法下载"
    # 组名与知名压制组的惯例编码不符时判定为冒用组名（Fury=x265、SPM=x264）。
    if not exclusion and group and KNOWN_GROUP_CODECS.get(group) and codec != KNOWN_GROUP_CODECS[group]:
        exclusion = f"{group} 组为 {KNOWN_GROUP_CODECS[group]} 编码，疑似冒用组名"
    profile = next((item for item in policy["profiles"] if item["enabled"] and codec in item["codecs"] and group in item["groups"]), None)
    eligible = exclusion is None and profile is not None
    if not exclusion and not group:
        exclusion = "制作组未识别"
    elif not exclusion and not profile:
        exclusion = f"{codec} 与 {group or '未知制作组'} 不在允许组合中"

    volume = torrent.get("volume_factor", 1)
    try:
        volume = float(volume)
    except (TypeError, ValueError):
        volume = 1.0
    site_priority = max(1, min(to_int(torrent.get("_site_priority"), 100), 999))
    resolution_rank = policy["resolution_order"].index(resolution) if resolution in policy["resolution_order"] else 9
    profile_tier = to_int(profile["tier"], 9) if profile else 9
    # Deterministic priority: profile -> site -> resolution -> seeders -> promotion -> source order.
    # Free/discount is a continuous weight: 0.0 (free) ranks first, 1.0 (full price) last.
    ranking = (
        (0 if eligible else 1) * 10**12
        + profile_tier * 10**10
        + site_priority * 10**6
        + resolution_rank * 10**4
        + max(0, 9999 - min(seeders, 9999))
        + max(0, min(100, to_int(volume * 100)))
        + index
    )
    score = 0 if not eligible else max(1, 100 - (profile_tier - 1) * 25 - resolution_rank * 5)
    recommendation = "excluded" if not eligible else ("preferred" if profile_tier == 1 else "fallback")
    breakdown = [
        {"label": profile["label"] if profile else "未命中允许组合", "score": score},
        {"label": f"站点优先级 {site_priority}", "score": 0},
        {"label": resolution, "score": 0},
        {"label": f"{seeders} 做种", "score": 0},
    ]
    profile_label = profile["label"] if profile else "未命中允许组合"
    reason = exclusion or f"{profile_label} · 站点优先级 {site_priority} · {resolution}"
    return {
        "ranking": ranking, "score": score, "breakdown": breakdown,
        "tier": profile_tier, "group": group, "resolution": resolution,
        "codec": codec, "source": source, "recommendation": recommendation,
        "reason": reason, "manual": not eligible, "manual_reasons": [exclusion] if exclusion else [],
        "eligible": eligible, "exclusion_reason": exclusion,
        "profile_id": profile["id"] if profile else None,
        "profile_label": profile["label"] if profile else None,
    }
