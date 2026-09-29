"""PT 站点接入：按站点档案搜索与检测。

参考 MoviePilot 的做法：大多数站点只需一份声明式档案（搜索路径、参数、IMDb 搜索方式、
列表与链接规则），由通用的 NexusPHP 解析器处理；有官方 API 的站点（如站点D）单独实现。
对外暴露 ``search``、``check`` 与提交前确认种子仍存在的 ``verify``，返回与寻片流程兼容的种子字典。
"""

from .engine import check, search, verify
from .errors import SiteError
from .profiles import SiteProfile, profile_for

__all__ = ["SiteError", "SiteProfile", "check", "profile_for", "search", "verify"]
