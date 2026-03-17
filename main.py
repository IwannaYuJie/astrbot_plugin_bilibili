from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
import math
from pathlib import Path
from typing import Any

import httpx
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path


@dataclass
class BiliCommentPreview:
    comment_id: str
    aid: str
    bvid: str
    video_title: str
    user_name: str
    user_mid: str
    message: str
    ctime: int
    mentioned: bool

    @property
    def time_text(self) -> str:
        try:
            return datetime.fromtimestamp(self.ctime).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return str(self.ctime)


class BilibiliApiClient:
    """B站 API 的最小客户端。

    当前版本实现：
    - 读取登录态
    - 读取视频列表
    - 读取视频评论（只读）
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

    async def get_video_comments(self, aid: str, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        params = {
            "type": 1,
            "oid": aid,
            "pn": page,
            "ps": page_size,
            "sort": 2,
        }
        return await self._request("GET", "https://api.bilibili.com/x/v2/reply", params=params)


@register("astrbot_plugin_bilibili", "IwannaYuJie", "基于 AstrBot 的 B 站评论区自动回复插件（基础骨架版）", "0.2.0")
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
                        "version": 2,
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

    def _configured_uid(self) -> str:
        return str(self.config.get("bilibili_uid", "") or "").strip()

    def _scan_video_limit(self) -> int:
        return int(self.config.get("scan_video_limit", 10) or 10)

    def _scan_comment_page_size(self) -> int:
        return int(self.config.get("scan_comment_page_size", 20) or 20)

    def _scan_comment_page_limit(self) -> int:
        return int(self.config.get("scan_comment_page_limit", 2) or 2)

    @staticmethod
    def _is_mention(message: str, uname: str) -> bool:
        text = (message or "").strip()
        target = (uname or "").strip()
        if not text or not target:
            return False
        return f"@{target}" in text or f"＠{target}" in text

    def _build_comment_preview(
        self,
        *,
        reply: dict[str, Any],
        aid: str,
        bvid: str,
        title: str,
        self_mid: str,
        self_uname: str,
    ) -> BiliCommentPreview | None:
        member = reply.get("member", {}) or {}
        content = reply.get("content", {}) or {}
        user_mid = str(member.get("mid", "") or "")
        user_name = str(member.get("uname", "") or "")
        message = str(content.get("message", "") or "").strip()
        if not message:
            return None
        if self_mid and user_mid == self_mid:
            return None
        return BiliCommentPreview(
            comment_id=str(reply.get("rpid", "") or ""),
            aid=aid,
            bvid=bvid,
            video_title=title,
            user_name=user_name,
            user_mid=user_mid,
            message=message,
            ctime=int(reply.get("ctime", 0) or 0),
            mentioned=self._is_mention(message, self_uname),
        )

    async def _scan_recent_mentions(self) -> tuple[dict[str, Any], list[BiliCommentPreview]]:
        uid = self._configured_uid()
        client = self._build_client()
        if not client.is_configured():
            raise ValueError("未配置 bilibili_cookie")
        if not uid:
            raise ValueError("未配置 bilibili_uid")

        nav = await client.get_login_info()
        nav_data = nav.get("data", {}) if isinstance(nav, dict) else {}
        self_mid = str(nav_data.get("mid", "") or "")
        self_uname = str(nav_data.get("uname", "") or "").strip()

        target_video_limit = self._scan_video_limit()
        page_size = min(target_video_limit, 20) if target_video_limit > 0 else 10
        video_pages = max(1, math.ceil(target_video_limit / page_size))

        vlist: list[dict[str, Any]] = []
        for page in range(1, video_pages + 1):
            videos = await client.get_video_list(uid=uid, page=page, page_size=page_size)
            page_vlist = (
                videos.get("data", {})
                .get("list", {})
                .get("vlist", [])
                if isinstance(videos, dict)
                else []
            )
            if not page_vlist:
                break
            for video in page_vlist:
                if isinstance(video, dict):
                    vlist.append(video)
                    if len(vlist) >= target_video_limit:
                        break
            if len(vlist) >= target_video_limit:
                break

        previews: list[BiliCommentPreview] = []
        video_debug: list[dict[str, Any]] = []
        for video in vlist:
            aid = str(video.get("aid", "") or "")
            bvid = str(video.get("bvid", "") or "")
            title = str(video.get("title", "") or "")
            if not aid:
                continue

            per_video_count = 0
            for page in range(1, self._scan_comment_page_limit() + 1):
                comments = await client.get_video_comments(
                    aid=aid,
                    page=page,
                    page_size=self._scan_comment_page_size(),
                )
                replies = comments.get("data", {}).get("replies", []) if isinstance(comments, dict) else []
                if not replies:
                    break
                for reply in replies or []:
                    if not isinstance(reply, dict):
                        continue
                    preview = self._build_comment_preview(
                        reply=reply,
                        aid=aid,
                        bvid=bvid,
                        title=title,
                        self_mid=self_mid,
                        self_uname=self_uname,
                    )
                    if preview:
                        previews.append(preview)
                        per_video_count += 1

                    for sub_reply in (reply.get("replies", []) or []):
                        if not isinstance(sub_reply, dict):
                            continue
                        sub_preview = self._build_comment_preview(
                            reply=sub_reply,
                            aid=aid,
                            bvid=bvid,
                            title=title,
                            self_mid=self_mid,
                            self_uname=self_uname,
                        )
                        if sub_preview:
                            previews.append(sub_preview)
                            per_video_count += 1

                if len(replies) < self._scan_comment_page_size():
                    break

            video_debug.append(
                {
                    "title": title,
                    "bvid": bvid,
                    "aid": aid,
                    "comment_count": per_video_count,
                }
            )

        meta = {
            "self_mid": self_mid,
            "self_uname": self_uname,
            "video_count": len(vlist),
            "comment_count": len(previews),
            "mention_count": len([item for item in previews if item.mentioned]),
            "video_debug": video_debug,
        }
        return meta, previews

    def _base_status_text(self) -> str:
        uid = self._configured_uid()
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
            f"- scan_video_limit: {self._scan_video_limit()}\n"
            f"- scan_comment_page_size: {self._scan_comment_page_size()}\n"
            f"- scan_comment_page_limit: {self._scan_comment_page_limit()}\n"
            f"- plugin_data_dir: {self.plugin_data_dir}"
        )

    @filter.command("bili_status")
    async def bili_status(self, event: AstrMessageEvent):
        """查看插件当前基础状态。"""
        yield event.plain_result(self._base_status_text())

    @filter.command("bili_probe")
    async def bili_probe(self, event: AstrMessageEvent):
        """使用 B 站只读接口探测 Cookie / UID 是否可用。"""
        uid = self._configured_uid()
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

    @filter.command("bili_scan")
    async def bili_scan(self, event: AstrMessageEvent):
        """读取最近评论并标记是否命中 @我，仅做只读预览。"""
        try:
            meta, previews = await self._scan_recent_mentions()
        except httpx.HTTPStatusError as e:
            logger.exception("B站扫描 HTTP 错误")
            yield event.plain_result(f"B站扫描失败：HTTP {e.response.status_code}")
            return
        except Exception as e:  # noqa: BLE001
            logger.exception("B站扫描异常")
            yield event.plain_result(f"B站扫描失败：{e}")
            return

        matched = [item for item in previews if item.mentioned]
        lines = [
            "B站评论扫描结果",
            f"- self_uname: {meta.get('self_uname') or '未知'}",
            f"- scanned_videos: {meta.get('video_count', 0)}",
            f"- scanned_comments: {meta.get('comment_count', 0)}",
            f"- matched_mentions: {len(matched)}",
        ]
        if not previews:
            lines.append("- 当前扫描范围内没有读到评论。")
            yield event.plain_result("\n".join(lines))
            return

        lines.append("\n最近评论预览（最多 8 条）：")
        for item in previews[:8]:
            flag = "[命中@]" if item.mentioned else "[未命中]"
            lines.append(
                f"{flag} {item.user_name} | {item.video_title[:20]} | {item.time_text}\n"
                f"{item.message[:120]}"
            )

        yield event.plain_result("\n".join(lines))

    @filter.command("bili_scan_mentions")
    async def bili_scan_mentions(self, event: AstrMessageEvent):
        """仅展示命中 @我的评论。"""
        try:
            meta, previews = await self._scan_recent_mentions()
        except httpx.HTTPStatusError as e:
            logger.exception("B站扫描 HTTP 错误")
            yield event.plain_result(f"B站扫描失败：HTTP {e.response.status_code}")
            return
        except Exception as e:  # noqa: BLE001
            logger.exception("B站扫描异常")
            yield event.plain_result(f"B站扫描失败：{e}")
            return

        matched = [item for item in previews if item.mentioned]
        lines = [
            "B站 @我 命中结果",
            f"- self_uname: {meta.get('self_uname') or '未知'}",
            f"- scanned_videos: {meta.get('video_count', 0)}",
            f"- matched_mentions: {len(matched)}",
        ]
        if not matched:
            lines.append("- 当前扫描范围内没有发现 @你的评论。")
            yield event.plain_result("\n".join(lines))
            return

        lines.append("")
        for item in matched[:10]:
            lines.append(
                f"- {item.user_name} | {item.video_title[:24]} | {item.time_text}\n"
                f"  comment_id={item.comment_id} bvid={item.bvid}\n"
                f"  {item.message[:160]}"
            )

        yield event.plain_result("\n".join(lines))

    @filter.command("bili_scan_debug")
    async def bili_scan_debug(self, event: AstrMessageEvent):
        """输出更详细的扫描调试信息。"""
        try:
            meta, previews = await self._scan_recent_mentions()
        except httpx.HTTPStatusError as e:
            logger.exception("B站扫描 HTTP 错误")
            yield event.plain_result(f"B站扫描失败：HTTP {e.response.status_code}")
            return
        except Exception as e:  # noqa: BLE001
            logger.exception("B站扫描异常")
            yield event.plain_result(f"B站扫描失败：{e}")
            return

        lines = [
            "B站扫描 Debug",
            f"- self_uname: {meta.get('self_uname') or '未知'}",
            f"- self_mid: {meta.get('self_mid') or '未知'}",
            f"- scanned_videos: {meta.get('video_count', 0)}",
            f"- scanned_comments: {meta.get('comment_count', 0)}",
            f"- matched_mentions: {meta.get('mention_count', 0)}",
            "",
            "视频扫描明细：",
        ]
        for item in meta.get("video_debug", [])[:10]:
            lines.append(
                f"- {str(item.get('title', ''))[:30]} | bvid={item.get('bvid')} | comments={item.get('comment_count', 0)}"
            )

        if previews:
            lines.append("")
            lines.append("评论样本（最多 10 条）：")
            for item in previews[:10]:
                flag = "[命中@]" if item.mentioned else "[未命中]"
                lines.append(f"{flag} {item.user_name}: {item.message[:100]}")
        else:
            lines.append("")
            lines.append("没有读到任何评论样本。")

        yield event.plain_result("\n".join(lines))

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
