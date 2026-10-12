"""共享的站点 / 搜索连接池（按超时与代理缓存 AsyncClient，复用 TCP 连接）。"""

from __future__ import annotations


import httpx



# 按 (timeout, proxy) 缓存模块级 AsyncClient，供搜索/检测方法复用连接池（审计 3-24）。
_search_clients: dict[tuple[float, str | None], httpx.AsyncClient] = {}


def _search_client(timeout: float, proxy: str | None = None) -> httpx.AsyncClient:
    key = (float(timeout), proxy)
    client = _search_clients.get(key)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(timeout=timeout, proxy=proxy, follow_redirects=False)
        _search_clients[key] = client
    return client


async def close_search_clients() -> None:
    """Close the shared site/search connection pools during shutdown and tests.

    The cache is intentionally process-local so search requests can reuse TCP
    connections.  Clearing only the dictionary leaves the underlying sockets
    open until garbage collection, which is especially visible as
    ``ResourceWarning`` on Python 3.14 and can retain proxy connections after
    a reload.  Remove the references first, then close each client defensively
    so one broken pool cannot prevent the remaining pools from being released.
    """
    clients = list(_search_clients.values())
    _search_clients.clear()
    for client in clients:
        try:
            await client.aclose()
        except Exception:
            continue
