from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path


class BilibiliApiClient:
    """B站 API 的最小只读客户端。

    当前版本只实现：
    - 读取登录态
    - 读取视频列表

    后续版本再补充评论拉取、发送回复、Cookie 刷新等能力。
    """

    def __init__(self, cookie: str, timeout: int = 20):
        self.cookie = cookie.strip()
        self.timeout = timeout

    @staticmethod
    def _parse_cookie(cookie_str: str) -> dict[str, str]:
        cookie_dict: dict[str, str] = {}
        for part in cookie_str.split(";"):
            item = part.strip()
            if not item or "=" not in item:
                continue
            key, value = item.split("=", 1)
            cookie_dict[key.strip()] = value.strip()
        return cookie_dict

    @property
    def csrf_token(self) -> str:
        return self._parse_cookie(self.cookie).get("bili_jct", "")

    def is_configured(self) -> bool:
        return bool(self.cookie)

    async def _request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.bilibili.com/",
        }
        cookies = self._parse_cookie(self.cookie)

        async with httpx.AsyncClient(timeout=self.timeout, headers=headers, cookies=cookies) as client:
            response = await client.request(method, url, **kwargs)
            response.raise_for_status()
            return response.json()

    async def get_login_info(self) -> dict[str, Any]:
        return await self._request("GET", "https://api.bilibili.com/x/web-interface/nav")

    async def get_video_list(self, uid: str, page: int = 1, page_size: int = 5) -> dict[str, Any]:
        params = {
            "mid": uid,
            "pn": page,
            "ps": page_size,
            "order": "pubdate",
        }
        return await self._request("GET", "https://api.bilibili.com/x/space/arc/search", params=params)


@register("astrbot_plugin_bilibili", "IwannaYuJie", "基于 AstrBot 的 B 站评论区自动回复插件（基础骨架版）", "0.1.0")
class BilibiliReplyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self.plugin_data_dir = Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_bilibili"
        self.state_file = self.plugin_data_dir / "state.json"
        self.plugin_data_dir.mkdir(parents=True, exist_ok=True)

    async def initialize(self):
        await self._ensure_state_file()
        logger.info("astrbot_plugin_bilibili initialized")

    async def terminate(self):
        logger.info("astrbot_plugin_bilibili terminated")

    async def _ensure_state_file(self):
        if not self.state_file.exists():
            self.state_file.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "notes": "基础版状态文件，后续用于保存运行状态、游标、缓存信息。",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

    def _build_client(self) -> BilibiliApiClient:
        timeout = int(self.config.get("http_timeout_seconds", 20) or 20)
        cookie = str(self.config.get("bilibili_cookie", "") or "")
        return BilibiliApiClient(cookie=cookie, timeout=timeout)

    def _provider_id_from_config(self) -> str:
        return str(self.config.get("provider_id", "") or "").strip()

    def _base_status_text(self) -> str:
        uid = str(self.config.get("bilibili_uid", "") or "").strip()
        cookie = str(self.config.get("bilibili_cookie", "") or "").strip()
        provider_id = self._provider_id_from_config()
        auto_poll = bool(self.config.get("auto_poll", False))
        dry_run = bool(self.config.get("dry_run", True))
        only_at = bool(self.config.get("reply_only_when_mentioned", True))
        return (
            "B站回复插件状态\n"
            f"- enabled: {bool(self.config.get('enabled', True))}\n"
            f"- auto_poll: {auto_poll}\n"
            f"- dry_run: {dry_run}\n"
            f"- only_reply_when_mentioned: {only_at}\n"
            f"- bilibili_uid_configured: {bool(uid)}\n"
            f"- bilibili_cookie_configured: {bool(cookie)}\n"
            f"- provider_id_configured: {bool(provider_id)}\n"
            f"- plugin_data_dir: {self.plugin_data_dir}"
        )

    @filter.command("bili_status")
    async def bili_status(self, event: AstrMessageEvent):
        """查看插件当前基础状态。"""
        yield event.plain_result(self._base_status_text())

    @filter.command("bili_probe")
    async def bili_probe(self, event: AstrMessageEvent):
        """使用 B 站只读接口探测 Cookie / UID 是否可用。"""
        uid = str(self.config.get("bilibili_uid", "") or "").strip()
        client = self._build_client()

        if not client.is_configured():
            yield event.plain_result("未配置 bilibili_cookie，无法探测。")
            return

        if not uid:
            yield event.plain_result("未配置 bilibili_uid，无法探测视频列表。")
            return

        try:
            nav = await client.get_login_info()
            nav_code = nav.get("code")
            nav_data = nav.get("data", {}) if isinstance(nav, dict) else {}
            uname = nav_data.get("uname", "未知")
            mid = nav_data.get("mid", "未知")
            is_login = nav_data.get("isLogin", False)

            videos = await client.get_video_list(uid=uid, page=1, page_size=5)
            videos_code = videos.get("code")
            vlist = (
                videos.get("data", {})
                .get("list", {})
                .get("vlist", [])
                if isinstance(videos, dict)
                else []
            )
            sample_titles = [item.get("title", "") for item in vlist[:3] if isinstance(item, dict)]

            lines = [
                "B站探针结果",
                f"- nav.code: {nav_code}",
                f"- is_login: {is_login}",
                f"- uname: {uname}",
                f"- mid: {mid}",
                f"- csrf_present: {bool(client.csrf_token)}",
                f"- video_api.code: {videos_code}",
                f"- sample_video_count: {len(vlist)}",
            ]
            if sample_titles:
                lines.append("- sample_titles:")
                lines.extend([f"  - {title}" for title in sample_titles])

            yield event.plain_result("\n".join(lines))
        except httpx.HTTPStatusError as e:
            logger.exception("B站探针 HTTP 错误")
            yield event.plain_result(f"B站探针失败：HTTP {e.response.status_code}")
        except Exception as e:  # noqa: BLE001
            logger.exception("B站探针异常")
            yield event.plain_result(f"B站探针失败：{e}")

    @filter.command("bili_dry_run")
    async def bili_dry_run(self, event: AstrMessageEvent):
        """调用 AstrBot 已配置的 LLM 做一次回复演练。"""
        provider_id = self._provider_id_from_config()
        if not provider_id:
            yield event.plain_result("未配置 provider_id，无法执行 dry run。")
            return

        raw = event.message_str.strip()
        prompt_text = raw.replace("/bili_dry_run", "", 1).strip()
        if not prompt_text:
            yield event.plain_result("请在命令后带上测试评论文本，例如：/bili_dry_run 你好呀")
            return

        system_prompt = str(self.config.get("persona_prompt", "") or "").strip()
        max_chars = int(self.config.get("max_reply_chars", 80) or 80)
        reply_prefix = str(self.config.get("reply_prefix", "") or "")

        try:
            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=(
                    "请把下面这条B站评论回复成自然、简短、像真人会说的话。"
                    f"要求：不超过{max_chars}字。\n\n"
                    f"评论：{prompt_text}"
                ),
                system_prompt=system_prompt,
            )
            text = (llm_resp.completion_text or "").strip()
            if len(text) > max_chars:
                text = text[:max_chars]
            yield event.plain_result(f"Dry Run 回复：\n{reply_prefix}{text}")
        except Exception as e:  # noqa: BLE001
            logger.exception("LLM dry run 失败")
            yield event.plain_result(f"LLM dry run 失败：{e}")
