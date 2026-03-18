from __future__ import annotations

import asyncio
import json
import math
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from hashlib import md5
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlparse

import httpx
from Crypto.Cipher import PKCS1_OAEP
from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path




@dataclass
class BiliMessageTrigger:
    msg_id: str
    msg_kind: str
    user_name: str
    user_mid: str
    oid: str
    root_id: str
    parent_id: str
    source_content: str
    title: str
    ctime: int
    business: str

    @property
    def time_text(self) -> str:
        try:
            return datetime.fromtimestamp(self.ctime).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return str(self.ctime)


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
    _MIXIN_KEY_ENC_TAB = [
        46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
        27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
        37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
        22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
    ]

    _COOKIE_REFRESH_PUBKEY = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDLgd2OAkcGVtoE3ThUREbio0Eg
Uc/prcajMKXvkCKFCWhJYJcLkcM2DKKcSeFpD/j6Boy538YXnR6VhcuUJOhH2x71
nzPjfdTcqMz7djHum0qSZA0AyCBDABUqCrfNgCiJ00Ra7GmRj+YCK1NJEuewlb40
JNrRuoEUXpabUzGB8QIDAQAB
-----END PUBLIC KEY-----"""

    def __init__(self, cookie: str, timeout: int = 20, refresh_token: str = ""):
        self.cookie = cookie.strip()
        self.timeout = timeout
        self.refresh_token = refresh_token.strip()
        self._nav_cache: dict[str, Any] | None = None
        self._wbi_keys_cache: tuple[str, str] | None = None

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

    def has_refresh_token(self) -> bool:
        return bool(self.refresh_token)

    def update_cookie_from_dict(self, cookie_dict: dict[str, str]):
        current = self._parse_cookie(self.cookie)
        current.update(cookie_dict)
        self.cookie = "; ".join(f"{k}={v}" for k, v in current.items() if v is not None)
        self._nav_cache = None
        self._wbi_keys_cache = None

    @classmethod
    def _generate_correspond_path(cls, timestamp_ms: int) -> str:
        key = RSA.import_key(cls._COOKIE_REFRESH_PUBKEY)
        cipher = PKCS1_OAEP.new(key, hashAlgo=SHA256)
        encrypted = cipher.encrypt(f"refresh_{timestamp_ms}".encode("utf-8"))
        return encrypted.hex()

    def is_configured(self) -> bool:
        return bool(self.cookie)

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.bilibili.com/",
            "Origin": "https://www.bilibili.com",
        }

    async def _request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        cookies = self._parse_cookie(self.cookie)
        async with httpx.AsyncClient(timeout=self.timeout, headers=self._headers(), cookies=cookies) as client:
            response = await client.request(method, url, **kwargs)
            response.raise_for_status()
            return response.json()

    async def get_login_info(self) -> dict[str, Any]:
        if self._nav_cache is None:
            self._nav_cache = await self._request("GET", "https://api.bilibili.com/x/web-interface/nav")
        return self._nav_cache

    async def get_cookie_refresh_info(self) -> dict[str, Any]:
        params = {"csrf": self.csrf_token} if self.csrf_token else {}
        return await self._request("GET", "https://passport.bilibili.com/x/passport-login/web/cookie/info", params=params)

    async def get_refresh_csrf(self, timestamp_ms: int | None = None) -> str:
        ts = timestamp_ms or int(time.time() * 1000)
        correspond_path = self._generate_correspond_path(ts)
        url = f"https://www.bilibili.com/correspond/1/{quote(correspond_path, safe='')}"
        cookies = self._parse_cookie(self.cookie)
        async with httpx.AsyncClient(timeout=self.timeout, headers=self._headers(), cookies=cookies) as client:
            response = await client.get(url)
            response.raise_for_status()
            html = response.text
        match = re.search(r'<div id="1-name">([^<]+)</div>', html)
        if not match:
            raise ValueError("未能从 correspond 页面提取 refresh_csrf")
        return match.group(1).strip()

    async def refresh_cookie(self) -> dict[str, Any]:
        if not self.refresh_token:
            raise ValueError("未配置 refresh_token")
        refresh_info = await self.get_cookie_refresh_info()
        data_info = (refresh_info.get("data") or {}) if isinstance(refresh_info, dict) else {}
        timestamp_ms = int(data_info.get("timestamp", 0) or int(time.time() * 1000))
        refresh_csrf = await self.get_refresh_csrf(timestamp_ms)
        old_refresh_token = self.refresh_token
        cookies = self._parse_cookie(self.cookie)
        payload = {
            "csrf": self.csrf_token,
            "refresh_csrf": refresh_csrf,
            "source": "main_web",
            "refresh_token": self.refresh_token,
        }
        async with httpx.AsyncClient(timeout=self.timeout, headers=self._headers(), cookies=cookies) as client:
            response = await client.post(
                "https://passport.bilibili.com/x/passport-login/web/cookie/refresh",
                data=payload,
            )
            response.raise_for_status()
            result = response.json()
            response_cookies = {k: v for k, v in response.cookies.items()}
        if result.get("code") != 0:
            return {"ok": False, "stage": "refresh", "result": result}
        if response_cookies:
            self.update_cookie_from_dict(response_cookies)
        new_refresh_token = str((result.get("data", {}) or {}).get("refresh_token", "") or "")
        if new_refresh_token:
            self.refresh_token = new_refresh_token
        confirm_payload = {
            "csrf": self.csrf_token,
            "refresh_token": old_refresh_token,
        }
        confirm_result = await self._request(
            "POST",
            "https://passport.bilibili.com/x/passport-login/web/confirm/refresh",
            data=confirm_payload,
        )
        return {
            "ok": result.get("code") == 0 and confirm_result.get("code") == 0,
            "stage": "done",
            "refresh_info": refresh_info,
            "refresh_result": result,
            "confirm_result": confirm_result,
            "new_refresh_token": self.refresh_token,
            "new_cookie": self.cookie,
        }

    @staticmethod
    def _extract_wbi_key(url: str) -> str:
        path = urlparse(url).path
        return path.rsplit("/", 1)[-1].split(".", 1)[0]

    @classmethod
    def _get_mixin_key(cls, orig: str) -> str:
        mixed = "".join(orig[i] for i in cls._MIXIN_KEY_ENC_TAB)
        return mixed[:32]

    async def _get_wbi_keys(self) -> tuple[str, str]:
        if self._wbi_keys_cache is not None:
            return self._wbi_keys_cache
        nav = await self.get_login_info()
        nav_data = (nav.get("data") or {}) if isinstance(nav, dict) else {}
        wbi_img = nav_data.get("wbi_img", {}) if isinstance(nav_data, dict) else {}
        img_url = str(wbi_img.get("img_url", "") or "")
        sub_url = str(wbi_img.get("sub_url", "") or "")
        if not img_url or not sub_url:
            raise ValueError("无法从 nav 接口中获取 WBI keys")
        img_key = self._extract_wbi_key(img_url)
        sub_key = self._extract_wbi_key(sub_url)
        self._wbi_keys_cache = (img_key, sub_key)
        return self._wbi_keys_cache

    async def _sign_wbi_params(self, params: dict[str, Any]) -> dict[str, Any]:
        img_key, sub_key = await self._get_wbi_keys()
        mixin_key = self._get_mixin_key(img_key + sub_key)
        signed = {k: v for k, v in params.items() if v is not None}
        signed["wts"] = int(time.time())
        signed = dict(sorted(signed.items()))
        clean_signed: dict[str, Any] = {}
        for key, value in signed.items():
            text = re.sub(r"[!'()*]", "", str(value))
            clean_signed[key] = text
        query = urlencode(clean_signed)
        clean_signed["w_rid"] = md5((query + mixin_key).encode("utf-8")).hexdigest()
        return clean_signed

    async def get_video_list(self, uid: str, page: int = 1, page_size: int = 5) -> dict[str, Any]:
        params = {
            "mid": uid,
            "ps": page_size,
            "pn": page,
            "tid": 0,
            "keyword": "",
            "order": "pubdate",
            "platform": "web",
            "web_location": 1550101,
            "order_avoided": "true",
        }
        signed = await self._sign_wbi_params(params)
        return await self._request("GET", "https://api.bilibili.com/x/space/wbi/arc/search", params=signed)

    async def get_video_comments(self, aid: str, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        params = {"type": 1, "oid": aid, "pn": page, "ps": page_size, "sort": 2}
        return await self._request("GET", "https://api.bilibili.com/x/v2/reply", params=params)

    async def get_msg_feed_unread(self) -> dict[str, Any]:
        params = {"platform": "web", "build": 0, "mobi_app": "web", "web_location": 333.40164}
        return await self._request("GET", "https://api.bilibili.com/x/msgfeed/unread", params=params)

    async def get_msg_feed_at(self) -> dict[str, Any]:
        params = {"platform": "web", "build": 0, "mobi_app": "web", "web_location": 333.40164}
        return await self._request("GET", "https://api.bilibili.com/x/msgfeed/at", params=params)

    async def get_msg_feed_reply(self) -> dict[str, Any]:
        params = {"platform": "web", "build": 0, "mobi_app": "web", "web_location": 333.40164}
        return await self._request("GET", "https://api.bilibili.com/x/msgfeed/reply", params=params)

    async def reply_to_comment(self, oid: str, reply_type: int, root_id: str, parent_id: str, message: str) -> dict[str, Any]:
        if not self.csrf_token:
            raise ValueError("Cookie 中缺少 bili_jct，无法发送回复")
        data = {
            "type": reply_type,
            "oid": oid,
            "root": root_id,
            "parent": parent_id,
            "message": message,
            "csrf": self.csrf_token,
        }
        return await self._request("POST", "https://api.bilibili.com/x/v2/reply/add", data=data)


@register("astrbot_plugin_bili_autoreply", "IwannaYuJie", "基于 AstrBot 的 B 站评论区自动回复插件", "0.6.2")
class BilibiliReplyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self.plugin_data_dir = Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_bili_autoreply"
        self.legacy_plugin_data_dir = Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_bilibili"
        self.state_file = self.plugin_data_dir / "state.json"
        self.processed_file = self.plugin_data_dir / "processed_comments.json"
        self.processed_msg_file = self.plugin_data_dir / "processed_messages.json"
        self.message_baseline_file = self.plugin_data_dir / "message_baseline.json"
        self.history_file = self.plugin_data_dir / "reply_history.jsonl"
        self.plugin_data_dir.mkdir(parents=True, exist_ok=True)
        self.processed_comments: set[str] = set()
        self.processed_messages: set[str] = set()
        self.message_baseline: dict[str, Any] = {}
        self._auto_task: asyncio.Task | None = None
        self._cycle_lock = asyncio.Lock()

    async def initialize(self):
        self._migrate_legacy_data_if_needed()
        await self._ensure_state_file()
        self._load_processed_comments()
        if self._enabled() and self._auto_poll_enabled():
            self._start_auto_task()
        logger.info("astrbot_plugin_bili_autoreply initialized")

    async def terminate(self):
        await self._stop_auto_task()
        self._save_processed_comments()
        logger.info("astrbot_plugin_bili_autoreply terminated")

    def _migrate_legacy_data_if_needed(self):
        try:
            if self.plugin_data_dir.exists() and any(self.plugin_data_dir.iterdir()):
                return
            if not self.legacy_plugin_data_dir.exists():
                return
            for item in self.legacy_plugin_data_dir.iterdir():
                target = self.plugin_data_dir / item.name
                if target.exists():
                    continue
                if item.is_file():
                    target.write_bytes(item.read_bytes())
        except Exception as e:  # noqa: BLE001
            logger.warning(f"迁移旧插件数据目录失败: {e}")

    async def _ensure_state_file(self):
        if not self.state_file.exists():
            self.state_file.write_text(
                json.dumps({"version": 4, "notes": "运行状态文件"}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        if not self.processed_file.exists():
            self.processed_file.write_text("[]", encoding="utf-8")
        if not self.processed_msg_file.exists():
            self.processed_msg_file.write_text("[]", encoding="utf-8")
        if not self.message_baseline_file.exists():
            self.message_baseline_file.write_text("{}", encoding="utf-8")
        if not self.history_file.exists():
            self.history_file.write_text("", encoding="utf-8")

    def _load_processed_comments(self):
        try:
            if self.processed_file.exists():
                data = json.loads(self.processed_file.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    self.processed_comments = {str(x) for x in data}
        except Exception as e:  # noqa: BLE001
            logger.warning(f"加载 processed_comments 失败: {e}")
            self.processed_comments = set()
        try:
            if self.processed_msg_file.exists():
                data = json.loads(self.processed_msg_file.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    self.processed_messages = {str(x) for x in data}
        except Exception as e:  # noqa: BLE001
            logger.warning(f"加载 processed_messages 失败: {e}")
            self.processed_messages = set()
        try:
            if self.message_baseline_file.exists():
                data = json.loads(self.message_baseline_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self.message_baseline = data
        except Exception as e:  # noqa: BLE001
            logger.warning(f"加载 message_baseline 失败: {e}")
            self.message_baseline = {}

    def _save_processed_comments(self):
        try:
            self.processed_file.write_text(
                json.dumps(sorted(self.processed_comments), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"保存 processed_comments 失败: {e}")
        try:
            self.processed_msg_file.write_text(
                json.dumps(sorted(self.processed_messages), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"保存 processed_messages 失败: {e}")
        try:
            self.message_baseline_file.write_text(
                json.dumps(self.message_baseline, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"保存 message_baseline 失败: {e}")

    def _append_history(self, item: dict[str, Any]):
        try:
            with self.history_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"写入回复历史失败: {e}")

    def _update_runtime_auth(self, *, cookie: str | None = None, refresh_token: str | None = None):
        changed = False
        if cookie is not None and cookie.strip():
            self.config["bilibili_cookie"] = cookie.strip()
            changed = True
        if refresh_token is not None and refresh_token.strip():
            self.config["bilibili_refresh_token"] = refresh_token.strip()
            changed = True
        if changed and hasattr(self.config, "save_config"):
            try:
                self.config.save_config()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"保存插件配置失败: {e}")

    def _enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    def _auto_poll_enabled(self) -> bool:
        return bool(self.config.get("auto_poll", False))

    def _build_client(self) -> BilibiliApiClient:
        timeout = int(self.config.get("http_timeout_seconds", 20) or 20)
        cookie = str(self.config.get("bilibili_cookie", "") or "")
        refresh_token = str(self.config.get("bilibili_refresh_token", "") or "")
        return BilibiliApiClient(cookie=cookie, timeout=timeout, refresh_token=refresh_token)

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

    def _poll_interval_seconds(self) -> int:
        return int(self.config.get("poll_interval_seconds", 120) or 120)

    def _max_comments_per_cycle(self) -> int:
        return int(self.config.get("max_comments_per_cycle", 5) or 5)

    def _reply_delay_seconds(self) -> float:
        return float(self.config.get("reply_delay_seconds", 2) or 2)

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
        nav_data = (nav.get("data") or {}) if isinstance(nav, dict) else {}
        self_mid = str(nav_data.get("mid", "") or "")
        self_uname = str(nav_data.get("uname", "") or "").strip()

        target_video_limit = self._scan_video_limit()
        page_size = min(target_video_limit, 20) if target_video_limit > 0 else 10
        video_pages = max(1, math.ceil(target_video_limit / page_size))

        vlist: list[dict[str, Any]] = []
        for page in range(1, video_pages + 1):
            videos = await client.get_video_list(uid=uid, page=page, page_size=page_size)
            page_vlist = videos.get("data", {}).get("list", {}).get("vlist", []) if isinstance(videos, dict) else []
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
                comments = await client.get_video_comments(aid=aid, page=page, page_size=self._scan_comment_page_size())
                replies = comments.get("data", {}).get("replies", []) if isinstance(comments, dict) else []
                if not replies:
                    break
                for reply in replies or []:
                    if not isinstance(reply, dict):
                        continue
                    preview = self._build_comment_preview(reply=reply, aid=aid, bvid=bvid, title=title, self_mid=self_mid, self_uname=self_uname)
                    if preview:
                        previews.append(preview)
                        per_video_count += 1
                    for sub_reply in (reply.get("replies", []) or []):
                        if not isinstance(sub_reply, dict):
                            continue
                        sub_preview = self._build_comment_preview(reply=sub_reply, aid=aid, bvid=bvid, title=title, self_mid=self_mid, self_uname=self_uname)
                        if sub_preview:
                            previews.append(sub_preview)
                            per_video_count += 1
                if len(replies) < self._scan_comment_page_size():
                    break
            video_debug.append({"title": title, "bvid": bvid, "aid": aid, "comment_count": per_video_count})

        meta = {
            "self_mid": self_mid,
            "self_uname": self_uname,
            "video_count": len(vlist),
            "comment_count": len(previews),
            "mention_count": len([item for item in previews if item.mentioned]),
            "video_debug": video_debug,
        }
        return meta, previews

    async def _fetch_message_debug_payload(self) -> dict[str, Any]:
        client = self._build_client()
        unread = await client.get_msg_feed_unread()
        at_data = await client.get_msg_feed_at()
        reply_data = await client.get_msg_feed_reply()
        return {
            "unread": unread,
            "at": at_data,
            "reply": reply_data,
        }

    async def _scan_message_triggers(self) -> tuple[dict[str, Any], list[BiliMessageTrigger]]:
        client = self._build_client()
        if not client.is_configured():
            raise ValueError("未配置 bilibili_cookie")

        payload = await self._fetch_message_debug_payload()
        unread = payload["unread"]
        at_data = payload["at"]
        reply_data = payload["reply"]

        triggers: list[BiliMessageTrigger] = []

        def _append_items(raw: dict[str, Any], kind: str):
            data = raw.get("data", {}) if isinstance(raw, dict) else {}
            items = data.get("items", []) if isinstance(data, dict) else []
            for item in items or []:
                if not isinstance(item, dict):
                    continue
                user = item.get("user", {}) or {}
                content = item.get("item", {}) or {}
                msg_id = str(item.get("id", "") or "")
                oid = str(content.get("subject_id", "") or content.get("business_id", "") or "")
                root_id = str(content.get("root_id", "") or content.get("source_id", "") or "")
                parent_id = str(content.get("source_id", "") or content.get("target_id", "") or root_id)
                source_content = str(content.get("source_content", "") or content.get("message", "") or content.get("target_reply_content", "") or "").strip()
                title = str(content.get("title", "") or content.get("detail_title", "") or "")
                ctime = int(item.get("at_time", 0) or item.get("reply_time", 0) or 0)
                business = str(content.get("business", "") or "")
                if not msg_id or not oid or not root_id:
                    continue
                triggers.append(BiliMessageTrigger(
                    msg_id=msg_id,
                    msg_kind=kind,
                    user_name=str(user.get("nickname", "") or ""),
                    user_mid=str(user.get("mid", "") or ""),
                    oid=oid,
                    root_id=root_id,
                    parent_id=parent_id,
                    source_content=source_content,
                    title=title,
                    ctime=ctime,
                    business=business,
                ))

        _append_items(at_data, "at")
        _append_items(reply_data, "reply")
        triggers.sort(key=lambda x: x.ctime, reverse=True)
        unread_data = unread.get("data", {}) if isinstance(unread, dict) else {}
        meta = {
            "unread_code": unread.get("code") if isinstance(unread, dict) else None,
            "unread_message": unread.get("message") if isinstance(unread, dict) else None,
            "unread_at": unread_data.get("at", 0) if isinstance(unread_data, dict) else 0,
            "unread_reply": unread_data.get("reply", 0) if isinstance(unread_data, dict) else 0,
            "at_code": at_data.get("code") if isinstance(at_data, dict) else None,
            "at_message": at_data.get("message") if isinstance(at_data, dict) else None,
            "reply_code": reply_data.get("code") if isinstance(reply_data, dict) else None,
            "reply_message": reply_data.get("message") if isinstance(reply_data, dict) else None,
            "at_count": len((at_data.get("data", {}) or {}).get("items", []) if isinstance(at_data, dict) else []),
            "reply_count": len((reply_data.get("data", {}) or {}).get("items", []) if isinstance(reply_data, dict) else []),
            "at_cursor": (at_data.get("data", {}) or {}).get("cursor") if isinstance(at_data, dict) else None,
            "reply_cursor": (reply_data.get("data", {}) or {}).get("cursor") if isinstance(reply_data, dict) else None,
            "trigger_count": len(triggers),
        }
        return meta, triggers

    def _infer_reply_type(self, trigger: BiliMessageTrigger) -> int:
        business = (trigger.business or "").lower()
        title = (trigger.title or "").lower()
        if business in {"dynamic", "dyn", "reply", "opus"}:
            return 11
        if "动态" in trigger.title or "置顶" in trigger.title:
            return 11
        if business in {"archive", "video", "av"}:
            return 1
        return 1

    async def _generate_reply_for_trigger(self, trigger: BiliMessageTrigger) -> str:
        provider_id = self._provider_id_from_config()
        if not provider_id:
            raise ValueError("未配置 provider_id")
        system_prompt = str(self.config.get("persona_prompt", "") or "").strip()
        max_chars = int(self.config.get("max_reply_chars", 80) or 80)
        reply_prefix = str(self.config.get("reply_prefix", "") or "")
        llm_resp = await self.context.llm_generate(
            chat_provider_id=provider_id,
            prompt=(
                "下面是一条来自B站消息中心的新互动，请基于具体评论内容生成一条自然、简短、像真人会说的话的回复。"
                f"要求：不超过{max_chars}字，不要机械客服腔，不要自称AI。\n\n"
                f"消息类型：{trigger.msg_kind}\n"
                f"视频标题：{trigger.title}\n"
                f"对方用户：{trigger.user_name}\n"
                f"评论内容：{trigger.source_content}"
            ),
            system_prompt=system_prompt,
        )
        text = (llm_resp.completion_text or "").strip()
        if len(text) > max_chars:
            text = text[:max_chars]
        return f"{reply_prefix}{text}".strip()

    async def _generate_reply_text(self, comment: BiliCommentPreview) -> str:
        provider_id = self._provider_id_from_config()
        if not provider_id:
            raise ValueError("未配置 provider_id")
        system_prompt = str(self.config.get("persona_prompt", "") or "").strip()
        max_chars = int(self.config.get("max_reply_chars", 80) or 80)
        reply_prefix = str(self.config.get("reply_prefix", "") or "")
        llm_resp = await self.context.llm_generate(
            chat_provider_id=provider_id,
            prompt=(
                "请为下面这条B站评论生成一条自然、简短、像真人会说的话的回复。"
                f"要求：不超过{max_chars}字，不要机械客服腔，不要自称AI。\n\n"
                f"视频标题：{comment.video_title}\n"
                f"评论用户：{comment.user_name}\n"
                f"评论内容：{comment.message}"
            ),
            system_prompt=system_prompt,
        )
        text = (llm_resp.completion_text or "").strip()
        if len(text) > max_chars:
            text = text[:max_chars]
        return f"{reply_prefix}{text}".strip()

    def _dedupe_triggers(self, triggers: list[BiliMessageTrigger]) -> list[BiliMessageTrigger]:
        deduped: list[BiliMessageTrigger] = []
        seen: set[tuple[str, str, str]] = set()
        for item in sorted(triggers, key=lambda x: (x.ctime, x.msg_id), reverse=True):
            key = (item.oid, item.root_id, item.parent_id)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return deduped

    def _is_after_baseline(self, trigger: BiliMessageTrigger) -> bool:
        if not self.message_baseline:
            return True
        baseline_time = int(self.message_baseline.get("time", 0) or 0)
        baseline_id = str(self.message_baseline.get("msg_id", "") or "")
        if trigger.ctime > baseline_time:
            return True
        if trigger.ctime == baseline_time and baseline_id and trigger.msg_id > baseline_id:
            return True
        return False

    def _ensure_message_baseline(self, triggers: list[BiliMessageTrigger]) -> bool:
        if self.message_baseline:
            return False
        if not triggers:
            self.message_baseline = {"time": int(time.time()), "msg_id": ""}
        else:
            newest = sorted(triggers, key=lambda x: (x.ctime, x.msg_id), reverse=True)[0]
            self.message_baseline = {"time": newest.ctime, "msg_id": newest.msg_id}
        self._save_processed_comments()
        return True

    async def _process_one_cycle(self) -> dict[str, Any]:
        async with self._cycle_lock:
            meta, triggers = await self._scan_message_triggers()
            triggers = self._dedupe_triggers(triggers)
            baseline_initialized = self._ensure_message_baseline(triggers)
            candidates = [
                item for item in triggers
                if item.msg_id not in self.processed_messages and self._is_after_baseline(item)
            ]
            max_count = self._max_comments_per_cycle()
            dry_run = bool(self.config.get("dry_run", True))
            processed_now: list[dict[str, Any]] = []
            client = self._build_client()

            if baseline_initialized:
                return {
                    "meta": meta,
                    "candidates": len(candidates),
                    "processed": [],
                    "dry_run": dry_run,
                    "baseline_initialized": True,
                }

            for trigger in candidates[:max_count]:
                reply_text = await self._generate_reply_for_trigger(trigger)
                history = {
                    "time": datetime.now().isoformat(),
                    "dry_run": dry_run,
                    "trigger": asdict(trigger),
                    "reply_text": reply_text,
                }
                if dry_run:
                    history["status"] = "dry_run"
                    self._append_history(history)
                    processed_now.append(history)
                    continue

                reply_type = self._infer_reply_type(trigger)
                history["reply_type"] = reply_type
                result = await client.reply_to_comment(
                    oid=trigger.oid,
                    reply_type=reply_type,
                    root_id=trigger.root_id,
                    parent_id=trigger.parent_id or trigger.root_id,
                    message=reply_text,
                )
                history["api_result"] = result
                if result.get("code") == 0:
                    history["status"] = "replied"
                    self.processed_messages.add(trigger.msg_id)
                    if trigger.parent_id:
                        self.processed_comments.add(trigger.parent_id)
                    self._save_processed_comments()
                else:
                    history["status"] = "failed"
                self._append_history(history)
                processed_now.append(history)
                await asyncio.sleep(self._reply_delay_seconds())

            return {
                "meta": meta,
                "candidates": len(candidates),
                "processed": processed_now,
                "dry_run": dry_run,
                "baseline_initialized": False,
            }

    def _start_auto_task(self):
        if self._auto_task and not self._auto_task.done():
            return
        self._auto_task = asyncio.create_task(self._auto_poll_loop(), name="bili-auto-reply-loop")

    async def _stop_auto_task(self):
        if self._auto_task and not self._auto_task.done():
            self._auto_task.cancel()
            try:
                await self._auto_task
            except asyncio.CancelledError:
                pass
        self._auto_task = None

    async def _auto_poll_loop(self):
        while True:
            try:
                if self._enabled() and self._auto_poll_enabled():
                    result = await self._process_one_cycle()
                    logger.info(
                        "bili auto cycle done: candidates=%s processed=%s dry_run=%s",
                        result["candidates"],
                        len(result["processed"]),
                        result["dry_run"],
                    )
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.exception(f"bili auto poll loop error: {e}")
            await asyncio.sleep(self._poll_interval_seconds())

    def _base_status_text(self) -> str:
        uid = self._configured_uid()
        cookie = str(self.config.get("bilibili_cookie", "") or "").strip()
        provider_id = self._provider_id_from_config()
        auto_poll = self._auto_poll_enabled()
        dry_run = bool(self.config.get("dry_run", True))
        only_at = bool(self.config.get("reply_only_when_mentioned", True))
        return (
            "B站回复插件状态\n"
            f"- enabled: {self._enabled()}\n"
            f"- auto_poll: {auto_poll}\n"
            f"- auto_task_running: {bool(self._auto_task and not self._auto_task.done())}\n"
            f"- dry_run: {dry_run}\n"
            f"- only_reply_when_mentioned: {only_at}\n"
            f"- bilibili_uid_configured: {bool(uid)}\n"
            f"- bilibili_cookie_configured: {bool(cookie)}\n"
            f"- provider_id_configured: {bool(provider_id)}\n"
            f"- refresh_token_configured: {bool(str(self.config.get('bilibili_refresh_token', '') or '').strip())}\n"
            f"- processed_comments: {len(self.processed_comments)}\n"
            f"- processed_messages: {len(self.processed_messages)}\n"
            f"- message_baseline: {self.message_baseline}\n"
            f"- scan_video_limit: {self._scan_video_limit()}\n"
            f"- scan_comment_page_size: {self._scan_comment_page_size()}\n"
            f"- scan_comment_page_limit: {self._scan_comment_page_limit()}\n"
            f"- plugin_data_dir: {self.plugin_data_dir}"
        )

    @filter.command("bili_status")
    async def bili_status(self, event: AstrMessageEvent):
        """查看插件当前基础状态。"""
        yield event.plain_result(self._base_status_text())

    @filter.command("bili_cookie_status")
    async def bili_cookie_status(self, event: AstrMessageEvent):
        """查看 Cookie 是否需要刷新。"""
        client = self._build_client()
        if not client.is_configured():
            yield event.plain_result("未配置 bilibili_cookie。")
            return
        try:
            info = await client.get_cookie_refresh_info()
            data = (info.get("data") or {}) if isinstance(info, dict) else {}
            yield event.plain_result(
                "Cookie 刷新状态\n"
                f"- code: {info.get('code')}\n"
                f"- message: {info.get('message')}\n"
                f"- need_refresh: {data.get('refresh')}\n"
                f"- timestamp: {data.get('timestamp')}\n"
                f"- refresh_token_configured: {client.has_refresh_token()}"
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("查询 Cookie 刷新状态失败")
            yield event.plain_result(f"查询失败：{e}")

    @filter.command("bili_refresh_cookie")
    async def bili_refresh_cookie(self, event: AstrMessageEvent):
        """手动刷新 Cookie，并写回插件配置。"""
        client = self._build_client()
        if not client.is_configured():
            yield event.plain_result("未配置 bilibili_cookie。")
            return
        if not client.has_refresh_token():
            yield event.plain_result("未配置 bilibili_refresh_token，无法刷新。")
            return
        try:
            result = await client.refresh_cookie()
            if result.get("ok"):
                self._update_runtime_auth(
                    cookie=result.get("new_cookie"),
                    refresh_token=result.get("new_refresh_token"),
                )
                yield event.plain_result(
                    "Cookie 刷新成功\n"
                    f"- refresh.code: {((result.get('refresh_result') or {}).get('code'))}\n"
                    f"- confirm.code: {((result.get('confirm_result') or {}).get('code'))}\n"
                    f"- refresh_token_updated: {bool(result.get('new_refresh_token'))}"
                )
            else:
                refresh_result = result.get("result") or result.get("refresh_result") or {}
                yield event.plain_result(
                    "Cookie 刷新失败\n"
                    f"- stage: {result.get('stage')}\n"
                    f"- code: {refresh_result.get('code')}\n"
                    f"- message: {refresh_result.get('message')}"
                )
        except Exception as e:  # noqa: BLE001
            logger.exception("刷新 Cookie 失败")
            yield event.plain_result(f"刷新失败：{e}")

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
            nav_data = (nav.get("data") or {}) if isinstance(nav, dict) else {}
            uname = nav_data.get("uname", "未知")
            mid = nav_data.get("mid", "未知")
            is_login = nav_data.get("isLogin", False)
            wbi_img = nav_data.get("wbi_img", {}) if isinstance(nav_data, dict) else {}
            has_wbi = bool(wbi_img.get("img_url")) and bool(wbi_img.get("sub_url"))
            videos = await client.get_video_list(uid=uid, page=1, page_size=5)
            videos_code = videos.get("code")
            videos_message = videos.get("message", "") if isinstance(videos, dict) else ""
            vlist = videos.get("data", {}).get("list", {}).get("vlist", []) if isinstance(videos, dict) else []
            sample_titles = [item.get("title", "") for item in vlist[:3] if isinstance(item, dict)]
            lines = [
                "B站探针结果",
                f"- nav.code: {nav_code}",
                f"- is_login: {is_login}",
                f"- uname: {uname}",
                f"- mid: {mid}",
                f"- csrf_present: {bool(client.csrf_token)}",
                f"- has_wbi_keys: {has_wbi}",
                f"- video_api.code: {videos_code}",
                f"- video_api.message: {videos_message}",
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
            lines.append(f"{flag} {item.user_name} | {item.video_title[:20]} | {item.time_text}\n{item.message[:120]}")
        yield event.plain_result("\n".join(lines))

    @filter.command("bili_scan_mentions")
    async def bili_scan_mentions(self, event: AstrMessageEvent):
        """仅展示命中 @我的评论。"""
        try:
            meta, previews = await self._scan_recent_mentions()
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
            lines.append(f"- {item.user_name} | {item.video_title[:24]} | {item.time_text}\n  comment_id={item.comment_id} bvid={item.bvid}\n  {item.message[:160]}")
        yield event.plain_result("\n".join(lines))

    @filter.command("bili_scan_debug")
    async def bili_scan_debug(self, event: AstrMessageEvent):
        """输出更详细的扫描调试信息。"""
        try:
            meta, previews = await self._scan_recent_mentions()
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
            lines.append(f"- {str(item.get('title', ''))[:30]} | bvid={item.get('bvid')} | comments={item.get('comment_count', 0)}")
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

    @filter.command("bili_msg_debug")
    async def bili_msg_debug(self, event: AstrMessageEvent):
        """查看消息中心触发源调试信息。"""
        try:
            meta, triggers = await self._scan_message_triggers()
        except Exception as e:  # noqa: BLE001
            logger.exception("消息中心扫描异常")
            yield event.plain_result(f"消息中心扫描失败：{e}")
            return
        lines = [
            "B站消息中心 Debug",
            f"- unread.code: {meta.get('unread_code')}",
            f"- unread.message: {meta.get('unread_message')}",
            f"- unread.at: {meta.get('unread_at', 0)}",
            f"- unread.reply: {meta.get('unread_reply', 0)}",
            f"- at.code: {meta.get('at_code')}",
            f"- at.message: {meta.get('at_message')}",
            f"- reply.code: {meta.get('reply_code')}",
            f"- reply.message: {meta.get('reply_message')}",
            f"- at_items: {meta.get('at_count', 0)}",
            f"- reply_items: {meta.get('reply_count', 0)}",
            f"- usable_triggers: {meta.get('trigger_count', 0)}",
            f"- at_cursor: {meta.get('at_cursor')}",
            f"- reply_cursor: {meta.get('reply_cursor')}",
        ]
        if not triggers:
            lines.append("- 当前消息中心里没有可用于回复的新触发项。")
        else:
            lines.append("")
            for item in triggers[:10]:
                lines.append(
                    f"- [{item.msg_kind}] {item.user_name} | msg_id={item.msg_id} | oid={item.oid} | root={item.root_id} | parent={item.parent_id}\n"
                    f"  title={item.title[:30]}\n"
                    f"  content={item.source_content[:120]}"
                )
        yield event.plain_result("\n".join(lines))

    @filter.command("bili_run_once")
    async def bili_run_once(self, event: AstrMessageEvent):
        """执行一轮自动回复流程。"""
        try:
            result = await self._process_one_cycle()
        except Exception as e:  # noqa: BLE001
            logger.exception("bili_run_once 失败")
            yield event.plain_result(f"执行失败：{e}")
            return
        lines = [
            "B站自动回复执行结果",
            f"- dry_run: {result['dry_run']}",
            f"- baseline_initialized: {result.get('baseline_initialized', False)}",
            f"- matched_candidates: {result['candidates']}",
            f"- handled_count: {len(result['processed'])}",
        ]
        for item in result["processed"][:5]:
            trigger = item.get("trigger", {})
            lines.append(
                f"- {item.get('status')} | {trigger.get('user_name')} | msg_id={trigger.get('msg_id')} | oid={trigger.get('oid')}\n"
                f"  reply={item.get('reply_text', '')[:160]}"
            )
        if result.get("baseline_initialized"):
            lines.append("- 已初始化消息基线；旧消息不会被回复。请在产生新 @/回复 后再次执行。")
        elif not result["processed"]:
            lines.append("- 本轮没有需要处理的新消息触发项。")
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
                prompt=("请把下面这条B站评论回复成自然、简短、像真人会说的话。" f"要求：不超过{max_chars}字。\n\n" f"评论：{prompt_text}"),
                system_prompt=system_prompt,
            )
            text = (llm_resp.completion_text or "").strip()
            if len(text) > max_chars:
                text = text[:max_chars]
            yield event.plain_result(f"Dry Run 回复：\n{reply_prefix}{text}")
        except Exception as e:  # noqa: BLE001
            logger.exception("LLM dry run 失败")
            yield event.plain_result(f"LLM dry run 失败：{e}")
