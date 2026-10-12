"""AI 辅助识别（兼容 OpenAI 接口）。"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from ..config import settings
from ..outbound import safe_request


class AIRecognitionClient:
    async def suggest(self, title: str, year: int | None) -> dict[str, Any] | None:
        if not settings.ai_base_url or not settings.ai_api_key or not settings.ai_model:
            return None
        prompt = (
            "识别下面的电影名称，只返回JSON对象，字段为 title、original_title、year。"
            "不要解释，不确定时保持输入。\n"
            f"输入标题：{title}\n输入年份：{year or ''}"
        )
        headers = {"Authorization": f"Bearer {settings.ai_api_key}", "Content-Type": "application/json"}
        payload = {
            "model": settings.ai_model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": prompt}],
        }
        async with httpx.AsyncClient(base_url=settings.ai_base_url, headers=headers, timeout=settings.mp_timeout_seconds) as client:
            response = await safe_request(
                client, "POST", "chat/completions", headers=headers, json=payload, label="AI 地址", allow_private=True,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
        match = re.search(r"\{.*\}", content, re.S)
        return json.loads(match.group(0)) if match else None
