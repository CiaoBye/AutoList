"""站点访问失败的原因：每一种都给出用户能照着处理的中文说明。"""


class SiteError(RuntimeError):
    """站点返回了结果以外的页面或错误。"""


class CookieExpired(SiteError):
    def __init__(self) -> None:
        super().__init__("Cookie 已失效，站点返回登录页面")


class TwoFactorRequired(SiteError):
    def __init__(self) -> None:
        super().__init__("站点要求二次验证（2FA），请在浏览器完成验证后重新同步 Cookie；有官方 API 的站点可改填 API Key")


class SiteMaintenance(SiteError):
    def __init__(self) -> None:
        super().__init__("站点跳转到维护或公告页面，暂时无法搜索")


class CloudflareChallenge(SiteError):
    def __init__(self) -> None:
        super().__init__("站点开启了 Cloudflare 人机验证，暂时无法自动搜索")


class SearchCaptcha(SiteError):
    def __init__(self) -> None:
        super().__init__("站点要求搜索人机验证，请在浏览器打开该站的种子列表页完成验证后再搜索")


class ApiError(SiteError):
    """站点 API 返回的业务错误。"""
