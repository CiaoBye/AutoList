"""Transmission：下载状态读取与（同步时）删除被取代的停滞任务。"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from ..config import settings
from ..outbound import safe_request


_global_transmission_session_id: str = ""


class TransmissionClient:
    def __init__(self) -> None:
        self.base_url = settings.tr_base_url.rstrip("/")
        if self.base_url and not self.base_url.endswith("/transmission/rpc"):
            self.base_url += "/transmission/rpc"
        self.auth = (
            httpx.BasicAuth(settings.tr_username, settings.tr_password or "")
            if settings.tr_username or settings.tr_password
            else None
        )
        self.session_id = _global_transmission_session_id

    async def _rpc(self, method: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        global _global_transmission_session_id
        if not self.base_url:
            raise RuntimeError("未配置 Transmission 地址")
        session_id = _global_transmission_session_id or self.session_id
        headers = {"X-Transmission-Session-Id": session_id} if session_id else {}
        async with httpx.AsyncClient(timeout=settings.mp_timeout_seconds, follow_redirects=False) as client:
            response = await safe_request(
                client, "POST", self.base_url, headers=headers, json={"method": method, "arguments": arguments or {}},
                auth=self.auth, label="Transmission 地址", allow_private=True,
            )
            if response.status_code == 409:
                _global_transmission_session_id = response.headers.get("X-Transmission-Session-Id", "")
                self.session_id = _global_transmission_session_id
                response = await safe_request(
                    client, "POST", self.base_url, headers={"X-Transmission-Session-Id": _global_transmission_session_id},
                    json={"method": method, "arguments": arguments or {}}, auth=self.auth, label="Transmission 地址",
                    allow_private=True,
                )
            response.raise_for_status()
            data = response.json()
        if data.get("result") != "success":
            raise RuntimeError(data.get("result") or "Transmission RPC 失败")
        return data.get("arguments", {})

    async def check(self) -> dict[str, Any]:
        if not self.base_url:
            return {"ok": False, "configured": False, "message": "未配置 Transmission 地址"}
        data = await self._rpc("session-get")
        return {"ok": True, "configured": True, "version": data.get("version"), "rpc_version": data.get("rpc-version")}

    async def remove_torrents(self, hashes: list[str], delete_data: bool) -> None:
        """按 hash 删除种子任务；``delete_data`` 为真时一并删除已下载的文件。"""
        if hashes:
            await self._rpc("torrent-remove", {"ids": hashes, "delete-local-data": delete_data})

    async def script_hooks(self) -> dict[str, str]:
        """“添加”“完成”两个脚本钩子当前启用的脚本（未启用的为空字符串）。"""
        data = await self._rpc("session-get", {"fields": [
            "script-torrent-added-enabled", "script-torrent-added-filename",
            "script-torrent-done-enabled", "script-torrent-done-filename",
        ]})
        return {
            event: str(data.get(f"script-torrent-{event}-filename") or "") if data.get(f"script-torrent-{event}-enabled") else ""
            for event in ("added", "done")
        }

    async def current_downloads(self) -> list[dict[str, Any]]:
        if not self.base_url:
            return []
        fields = [
            "id", "name", "hashString", "status", "percentDone", "totalSize", "labels", "downloadDir",
            # 下载进度展示：速度、剩余时间、连接的做种者、错误与停滞。
            "rateDownload", "eta", "error", "errorString", "peersSendingToUs", "peersConnected", "isStalled",
            # 停滞多久：最后一次有数据传输的时间（从未传输为 0）与加入时间。
            "activityDate", "addedDate",
        ]
        data = await self._rpc("torrent-get", {"fields": fields})
        return data.get("torrents", [])

    async def overview(self) -> dict[str, Any]:
        """下载页用：全部种子的详细状态、当前总速度与下载目录剩余空间（只读）。"""
        fields = [
            "id", "name", "hashString", "status", "percentDone", "totalSize", "sizeWhenDone", "leftUntilDone",
            "labels", "rateDownload", "rateUpload", "eta", "error", "errorString", "peersSendingToUs",
            "peersGettingFromUs", "isStalled", "uploadRatio", "addedDate", "doneDate", "queuePosition", "activityDate",
        ]
        async def free_space() -> Any:
            try:
                directory = (await self._rpc("session-get", {"fields": ["download-dir"]})).get("download-dir")
                return (await self._rpc("free-space", {"path": directory})).get("size-bytes") if directory else None
            except Exception:
                return None

        # 四次 RPC 互不依赖（除了剩余空间要先知道下载目录），并行读取。
        listing, stats, free_bytes = await asyncio.gather(
            self._rpc("torrent-get", {"fields": fields}), self._rpc("session-stats"), free_space(),
        )
        torrents = listing.get("torrents", [])
        return {
            "torrents": torrents, "download_bps": stats.get("downloadSpeed") or 0,
            "upload_bps": stats.get("uploadSpeed") or 0, "free_bytes": free_bytes,
        }

    def web_url(self) -> str | None:
        """Transmission 自带的网页界面地址，供下载页跳转（不含账号密码）。"""
        if not self.base_url:
            return None
        return self.base_url.removesuffix("/rpc") + "/web/"
