"""Hanime1.me 协议化适配器（HTML 爬取模式）。

基于 Laravel + Bootstrap 的 H 动画聚合站，包含搜索、详情页。
无 Cloudflare 防护，可直接通过 HTTP 代理访问。
"""
from __future__ import annotations

import base64
import os
import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, quote, urlparse, parse_qs

import requests as std_requests
from bs4 import BeautifulSoup

from protocol.base import ProtocolProvider

HANIME1_CONFIG_KEY = "hanime1"
HANIME1_PLUGIN_ID = "video.hanime1"
HANIME1_PLATFORM = "Hanime1"
HANIME1_HOST_ID_PREFIX = "HN1"

DEFAULT_DOMAIN = "https://hanime1.me"
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

SUPPORTED_CAPABILITIES = frozenset(
    {
        "catalog.search",
        "catalog.detail",
        "playback.proxy.url",
        "playback.proxy.stream",
        "playback.sources.build",
        "transport.http.request",
        "health.query.status",
    }
)

_HTML_PARSERS = ("lxml", "html.parser")


def _make_soup(html: Any) -> BeautifulSoup:
    """优先 lxml，缺失时回退标准库解析器（Android/Chaquopy 环境更稳）。"""
    for parser in _HTML_PARSERS:
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return BeautifulSoup(html, "html.parser")


class _ProxyResponse:
    """把第三方响应包装成宿主消费的形态。

    - ``/api/v1/video/proxy/<domain>/<path>`` 读 ``.body`` / ``.status_code`` / ``.headers``
    - ``/api/v1/video/proxy2`` 走 ``.iter_content`` 流式转发
    """

    def __init__(self, response: Any):
        self._response = response

    @property
    def body(self) -> bytes:
        return self._response.content

    @property
    def content(self) -> bytes:
        return self._response.content

    @property
    def status_code(self) -> int:
        return int(getattr(self._response, "status_code", 0) or 0)

    @property
    def headers(self):
        return getattr(self._response, "headers", {}) or {}

    def iter_content(self, chunk_size: int = 262144):
        return self._response.iter_content(chunk_size=chunk_size)

    def close(self) -> None:
        close = getattr(self._response, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _as_int(value: Any, default: int, minimum: int = 1, maximum: int = 10000) -> int:
    try:
        parsed = int(float(value))
    except Exception:
        parsed = default
    if parsed < minimum:
        return minimum
    if parsed > maximum:
        return maximum
    return parsed


def _normalize_domain(value: Any) -> str:
    text = str(value or "").strip().rstrip("/")
    return text or DEFAULT_DOMAIN


def _is_hanime1_cdn_url(url: str) -> bool:
    if not url:
        return False
    lower = url.lower()
    return "hembed.com" in lower or "hanime1.me" in lower


def _abs_url(url: str, domain: str) -> str:
    """将相对路径转为绝对 URL。"""
    if not url:
        return url
    url = url.strip()
    if url.startswith("http://") or url.startswith("https://") or url.startswith("//"):
        return url
    if url.startswith("/"):
        return f"{domain.rstrip('/')}{url}"
    return f"{domain.rstrip('/')}/{url}"


def _extract_code_from_title(title: str) -> str:
    """从标题中尝试提取番号，如 SIRO-4960、SSNI-001 等。"""
    m = re.search(r'[A-Za-z]{2,8}[-_][\d]{2,6}', title)
    if m:
        return m.group(0)
    return ""


class Hanime1Provider(ProtocolProvider):
    """Hanime1.me 协议化适配器（直接爬取模式）。

    Hanime1 是基于 Laravel 的 H 动画聚合站，包含搜索、详情页。
    无 Cloudflare 防护，直接 HTTP 请求即可访问。
    图片和视频资源托管在 vdownload.hembed.com CDN。
    """

    def normalize_config(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        raw = dict(payload or {})
        normalized: Dict[str, Any] = {}
        normalized["enabled"] = _as_bool(raw.get("enabled"), False)
        normalized["domain"] = _normalize_domain(raw.get("domain"))
        normalized["timeout_seconds"] = _as_int(
            raw.get("timeout_seconds"), DEFAULT_TIMEOUT_SECONDS, 1, 600
        )
        normalized["proxy"] = str(raw.get("proxy") or "").strip()
        normalized["user_agent"] = (
            str(raw.get("user_agent") or "").strip() or DEFAULT_USER_AGENT
        )
        return normalized

    def serialize_public_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        normalized = self.normalize_config(config)
        public = dict(normalized)
        # 代理字段已保留供 UI 编辑
        public["proxy_configured"] = bool(
            str((config or {}).get("proxy") or "").strip()
        )
        return public

    def get_query_status(self, config: Dict[str, Any]) -> Dict[str, Any]:
        normalized = self.normalize_config(config)
        enabled = _as_bool(normalized.get("enabled"), False)
        domain = str(normalized.get("domain") or "").strip()
        configured = bool(enabled and domain)
        return {
            "configured": configured,
            "message": (
                "" if configured else "Hanime1 未启用或站点域名未配置。"
            ),
            "missing_fields": [] if domain else ["domain"],
        }

    # ---------- 内部工具 ----------

    def _build_session(self, config: Dict[str, Any]) -> std_requests.Session:
        session = std_requests.Session()
        session.headers.update({
            "User-Agent": str(config.get("user_agent") or DEFAULT_USER_AGENT),
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;"
                "q=0.9,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,ja;q=0.7",
            "Referer": "https://hanime1.me/",
            "DNT": "1",
        })
        proxy = str(config.get("proxy") or "").strip()
        if proxy:
            session.proxies.update({"http": proxy, "https": proxy})
        return session

    def _request(
        self,
        session: std_requests.Session,
        config: Dict[str, Any],
        url: str,
        *,
        retry: int = 2,
        accept_json: bool = False,
    ):
        """发送 GET 请求，支持重试和 JSON 响应。

        如果配置了代理但请求失败，会自动回退到无代理重试。
        """
        timeout = _as_int(
            config.get("timeout_seconds"), DEFAULT_TIMEOUT_SECONDS, 1, 600
        )
        has_proxy = bool(str(config.get("proxy") or "").strip())

        for attempt in range(retry):
            try:
                response = session.get(
                    url,
                    timeout=timeout,
                    allow_redirects=True,
                )
                if response.status_code == 200:
                    if accept_json:
                        try:
                            return response.json()
                        except Exception:
                            return response.text
                    return response.text
                if response.status_code in (403, 503):
                    if attempt < retry - 1:
                        time.sleep(1)
                    continue
                if response.status_code == 404:
                    return None
            except Exception:
                if attempt < retry - 1:
                    time.sleep(1)
                    continue

        # 如果配置了代理，回退到无代理重试
        if has_proxy:
            no_proxy_config = dict(config)
            no_proxy_config["proxy"] = ""
            fallback_session = self._build_session(no_proxy_config)
            try:
                response = fallback_session.get(
                    url,
                    timeout=timeout,
                    allow_redirects=True,
                )
                if response.status_code == 200:
                    if accept_json:
                        try:
                            return response.json()
                        except Exception:
                            return response.text
                    return response.text
                if response.status_code == 404:
                    return None
            except Exception:
                pass

        return None

    def _domain(self, config: Dict[str, Any]) -> str:
        return str(config.get("domain") or DEFAULT_DOMAIN).rstrip("/")

    # ---------- 能力入口 ----------

    def _declared_capabilities(self) -> set:
        """以清单声明为准；清单缺少 capabilities 时回退到代码内置能力集。"""
        raw = None
        if isinstance(self.manifest, dict):
            raw = self.manifest.get("capabilities")
        declared = {
            str((item or {}).get("key") or "").strip()
            for item in (raw or [])
            if isinstance(item, dict)
        }
        declared.discard("")
        return declared or set(SUPPORTED_CAPABILITIES)

    def execute(
        self,
        capability: str,
        params: Dict[str, Any],
        context: Dict[str, Any],
        config: Dict[str, Any],
    ) -> Any:
        normalized = self.normalize_config(config)
        if capability not in self._declared_capabilities():
            raise ValueError(f"不支持的能力: {capability}")

        if capability == "health.query.status":
            return self.get_query_status(config)

        if not _as_bool(normalized.get("enabled"), False):
            raise RuntimeError("Hanime1 插件未启用。")

        session = self._build_session(normalized)

        if capability == "catalog.search":
            return self._handle_search(session, normalized, params)
        if capability == "catalog.detail":
            return self._handle_detail(session, normalized, params)
        if capability == "playback.proxy.url":
            return self._handle_proxy_url(session, normalized, params)
        if capability == "playback.proxy.stream":
            return self._handle_proxy_stream(session, normalized, params)
        if capability == "playback.sources.build":
            return self._handle_build_sources(session, normalized, params)
        if capability == "transport.http.request":
            return self._handle_http_request(session, normalized, params)

        raise ValueError(f"不支持的能力: {capability}")

    # ---------- 能力实现 ----------

    def _handle_search(
        self,
        session: std_requests.Session,
        config: Dict[str, Any],
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        keyword = str(params.get("keyword") or params.get("query") or "").strip()
        page = _as_int(params.get("page"), 1, 1, 10000)
        if not keyword:
            return {"videos": [], "total": 0, "page": page}

        domain = self._domain(config)

        # 搜索 URL 格式: /search?query=keyword&page=N
        search_url = f"{domain}/search?query={quote(keyword)}&page={page}"
        html = self._request(session, config, search_url)
        if not html:
            return {"videos": [], "total": 0, "page": page}

        videos, total_pages = self._parse_search_results(str(html), domain)
        has_next = page < total_pages

        return {
            "page": page,
            "has_next": has_next,
            "total_pages": total_pages,
            "videos": videos,
            "keyword": keyword,
        }

    def _handle_detail(
        self,
        session: std_requests.Session,
        config: Dict[str, Any],
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        video_id = str(params.get("video_id") or params.get("id") or "").strip()
        if not video_id:
            raise RuntimeError("catalog.detail 缺少 video_id 参数。")

        # 移除 host_id 前缀
        raw_id = video_id
        prefix_upper = HANIME1_HOST_ID_PREFIX.upper()
        if raw_id.upper().startswith(prefix_upper):
            raw_id = raw_id[len(prefix_upper):]

        domain = self._domain(config)

        # 详情页 URL: /watch?v={id}
        detail_url = f"{domain}/watch?v={raw_id}"
        html = self._request(session, config, detail_url)
        if not html:
            return {"videos": [], "found": False, "video_id": video_id}

        detail = self._parse_detail_page(str(html), raw_id, domain)
        if not detail:
            return {"videos": [], "found": False, "video_id": video_id}

        return {"videos": [detail]}

    def _handle_proxy_url(
        self,
        session: std_requests.Session,
        config: Dict[str, Any],
        params: Dict[str, Any],
    ):
        """处理 playback.proxy.url — 代理图片/视频资源请求。

        使用 stream=True 支持流式传输，后端可边下载视频边转发给浏览器，
        避免等待完整文件下载后才开始播放。
        """
        method = str(params.get("method") or "GET").upper()
        query_string = str(params.get("query_string") or "").strip()
        body_url = str(params.get("body_url") or "").strip()
        incoming_headers = dict(params.get("incoming_headers") or {})

        # 从 query_string 中提取目标 URL（base64 编码）
        target_url = body_url
        if not target_url and query_string:
            parsed = parse_qs(query_string)
            url_param = parsed.get("url", [])
            if url_param:
                encoded = url_param[0]
                try:
                    target_url = base64.b64decode(encoded).decode("utf-8")
                except Exception:
                    target_url = encoded

        if not target_url:
            raise ValueError("proxy.url: missing target URL")

        timeout = _as_int(
            config.get("timeout_seconds"), DEFAULT_TIMEOUT_SECONDS, 1, 600
        )

        # 带 Referer 请求目标资源
        req_headers = {}
        if incoming_headers.get("Range"):
            req_headers["Range"] = incoming_headers["Range"]
        if _is_hanime1_cdn_url(target_url):
            req_headers["Referer"] = "https://hanime1.me/"

        try:
            response = session.get(
                target_url,
                headers=req_headers,
                timeout=timeout,
                stream=True,
            )
            return response
        except Exception:
            # 如果有代理配置但失败，回退到无代理
            has_proxy = bool(str(config.get("proxy") or "").strip())
            if has_proxy:
                no_proxy_config = dict(config)
                no_proxy_config["proxy"] = ""
                fallback_session = self._build_session(no_proxy_config)
                response = fallback_session.get(
                    target_url,
                    headers=req_headers,
                    timeout=timeout,
                    stream=True,
                )
                return response
            raise

    @staticmethod
    def _strip_host_prefix(value: str) -> str:
        """宿主可能传入 HN1<id> 形式的 ID，这里还原为站内 ID。"""
        text = str(value or "").strip()
        prefix = HANIME1_HOST_ID_PREFIX
        if text.upper().startswith(prefix) and len(text) > len(prefix):
            stripped = text[len(prefix):].strip("_- ")
            if stripped:
                return stripped
        return text

    @staticmethod
    def _resolve_proxy_target(params: Dict[str, Any]) -> str:
        """按协议优先用 body_url / query_string，其次用 domain + path 拼装目标 URL。"""
        body_url = str(params.get("body_url") or "").strip()
        if body_url:
            return body_url

        query_string = str(params.get("query_string") or "").strip()
        if query_string:
            parsed = parse_qs(query_string)
            url_param = parsed.get("url", [])
            if url_param:
                encoded = url_param[0]
                try:
                    return base64.b64decode(encoded).decode("utf-8")
                except Exception:
                    return encoded

        domain = str(params.get("domain") or "").strip()
        path = str(params.get("path") or "").strip()
        if not domain:
            return ""
        base = domain if domain.startswith(("http://", "https://")) else f"https://{domain}"
        target = f"{base.rstrip('/')}/{path.lstrip('/')}" if path else base.rstrip("/")
        return f"{target}?{query_string}" if query_string else target

    def _handle_proxy_stream(
        self,
        session: std_requests.Session,
        config: Dict[str, Any],
        params: Dict[str, Any],
    ) -> _ProxyResponse:
        """处理 playback.proxy.stream — 代理宿主的 /proxy/<domain>/<path> 路由。

        宿主按 ``proxy_result.body / .status_code / .headers`` 消费该结果，
        因此返回 _ProxyResponse 包装对象，而不是裸的 requests.Response。
        """
        method = str(params.get("method") or "GET").upper()
        target_url = self._resolve_proxy_target(params)

        if not target_url:
            raise ValueError("proxy.stream: missing target URL")

        timeout = _as_int(
            config.get("timeout_seconds"), DEFAULT_TIMEOUT_SECONDS, 1, 600
        )

        req_headers = {"Referer": "https://hanime1.me/"}
        try:
            response = session.request(
                method,
                target_url,
                headers=req_headers,
                timeout=timeout,
                stream=True,
            )
            return _ProxyResponse(response)
        except Exception:
            # 如果有代理配置但失败，回退到无代理
            has_proxy = bool(str(config.get("proxy") or "").strip())
            if has_proxy:
                no_proxy_config = dict(config)
                no_proxy_config["proxy"] = ""
                fallback_session = self._build_session(no_proxy_config)
                response = fallback_session.request(
                    method,
                    target_url,
                    headers=req_headers,
                    timeout=timeout,
                    stream=True,
                )
                return _ProxyResponse(response)
            raise

    def _to_proxy_video_url(self, src_url: str, proxy_base_path: str = "") -> str:
        """将 CDN 视频 URL 包装为后端代理 URL。

        hanime1 的视频文件托管在 vdownload.hembed.com CDN，
        浏览器直接访问会因 CORS/Referer 限制失败，需通过后端代理转发。
        """
        if not src_url:
            return ""
        # 只对 vdownload.hembed.com 域名的 URL 进行代理
        if "hembed.com" not in src_url.lower() and "hanime1.me" not in src_url.lower():
            return src_url
        try:
            base = (proxy_base_path or "/api/v1/video").rstrip("/")
            encoded = base64.b64encode(src_url.encode("utf-8")).decode("utf-8")
            return f"{base}/proxy2?url={encoded}"
        except Exception:
            return src_url

    def _handle_build_sources(
        self,
        session: std_requests.Session,
        config: Dict[str, Any],
        params: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """处理 playback.sources.build — 构建视频播放源列表。

        从视频详情页解析 <video> 标签中的 <source> 元素，
        提取不同画质的视频 URL，返回标准化的播放源格式。

        注意：视频源 URL 通过代理包装，避免浏览器因 CORS/Referer
        限制无法直接加载 vdownload.hembed.com CDN 资源。
        """
        code = str(params.get("code") or params.get("video_id") or "").strip()
        if not code:
            return []
        video_id = self._strip_host_prefix(code)

        # 复用详情页解析获取视频源
        detail_result = self._handle_detail(session, config, {"video_id": video_id})
        videos = detail_result.get("videos", [])
        if not videos:
            return []

        video = videos[0]
        video_sources = video.get("video_sources", [])
        if not video_sources:
            return []

        sources: List[Dict[str, Any]] = []
        for i, vs in enumerate(video_sources):
            src_url = str(vs.get("src", "")).strip()
            if not src_url:
                continue

            size_label = str(vs.get("size", "")).strip() or f"画质{i+1}"
            resolution_label = size_label
            if not size_label:
                resolution_label = "原始"

            # 将 CDN URL 包装为代理 URL，解决浏览器跨域问题
            proxy_url = self._to_proxy_video_url(
                src_url, str(params.get("proxy_base_path") or "")
            )

            sources.append({
                "key": f"hanime1_{i}",
                "name": f"Hanime1 {size_label}" if size_label else f"Hanime1 源{i+1}",
                "available": True,
                "type": "direct",
                "source": "hanime1",
                "episode_index": i,
                "currentResolution": resolution_label,
                "streams": [
                    {
                        "resolution": resolution_label,
                        "url": proxy_url,
                        "type": "direct",
                        "source": "hanime1",
                    }
                ],
            })

        return sources

    def _handle_http_request(
        self,
        session: std_requests.Session,
        config: Dict[str, Any],
        params: Dict[str, Any],
    ):
        """处理 transport.http.request — 通用 HTTP 请求。"""
        method = str(params.get("method") or "GET").upper()
        url = str(params.get("url") or "").strip()
        req_headers = dict(params.get("headers") or {})
        stream = bool(params.get("stream", False))
        timeout = _as_int(
            params.get("timeout") or config.get("timeout_seconds"),
            DEFAULT_TIMEOUT_SECONDS, 1, 600,
        )
        allow_redirects = bool(params.get("allow_redirects", True))

        if not url:
            raise ValueError("http.request: missing URL")

        if _is_hanime1_cdn_url(url) and "Referer" not in req_headers:
            req_headers["Referer"] = "https://hanime1.me/"

        response = session.request(
            method=method,
            url=url,
            headers=req_headers,
            timeout=timeout,
            stream=stream,
            allow_redirects=allow_redirects,
        )
        return response

    # ---------- 数据转换 ----------

    def _to_video_summary(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """搜索结果项转为宿主统一视频摘要格式。"""
        return {
            "video_id": item.get("video_id", ""),
            "title": item.get("title", ""),
            "code": item.get("code", ""),
            "cover_url": item.get("cover_url", ""),
            "date": item.get("date", ""),
            "platform": HANIME1_PLATFORM,
            "host_id": f'{HANIME1_HOST_ID_PREFIX}{item.get("video_id", "")}',
        }

    # ---------- 页面解析 ----------

    def _parse_search_results(
        self, html: str, domain: str
    ) -> tuple[List[Dict[str, Any]], int]:
        """解析搜索结果的 HTML，返回 (video列表, 总页数)。"""
        soup = _make_soup(html)
        results: List[Dict[str, Any]] = []

        # 搜索结果结构:
        # div.video-item-container > div.horizontal-card > a.video-link
        for card in soup.select("div.horizontal-card"):
            parsed = self._parse_search_card(card, domain)
            if parsed:
                results.append(parsed)

        # 获取总页数: 从分页表单中提取
        total_pages = 1
        page_form = soup.select_one("form#skip-page-form")
        if page_form:
            page_input = page_form.select_one("input[name='page']")
            if page_input:
                # 获取 oninput 中的最大值参数
                oninput = str(page_input.get("oninput", "") or "")
                m = re.search(r'validateNumberInput\([^,]+,\s*\d+,\s*(\d+)\)', oninput)
                if m:
                    total_pages = int(m.group(1))

        return results, total_pages

    def _parse_search_card(self, card, domain: str) -> Optional[Dict[str, Any]]:
        """解析单个搜索结果卡片（horizontal-card）。"""
        link = card.select_one("a.video-link")
        if not link:
            return None

        href = str(link.get("href", "")).strip()
        if not href or "/watch" not in href:
            return None

        # 提取视频 ID
        video_id = ""
        parsed_url = urlparse(href)
        qs = parse_qs(parsed_url.query)
        video_id = qs.get("v", [""])[0] if "v" in qs else ""

        if not video_id:
            return None

        # 缩略图
        cover_url = ""
        thumb = card.select_one("img.main-thumb")
        if thumb:
            for attr in ("src", "data-src", "data-original"):
                src = str(thumb.get(attr, "")).strip()
                if src and not src.startswith("data:"):
                    cover_url = src
                    break

        # 标准化封面 URL
        if cover_url and not cover_url.startswith("http"):
            cover_url = _abs_url(cover_url, domain)

        # 标题
        title = ""
        title_div = card.select_one("div.title")
        if title_div:
            title = title_div.get_text(strip=True)

        # 时长
        duration = ""
        duration_div = card.select_one("div.duration")
        if duration_div:
            duration = duration_div.get_text(strip=True)

        # 统计信息（点赞率、观看次数）
        stats_text = ""
        stats = card.select_one("div.stats-container")
        if stats:
            stats_text = stats.get_text(" ", strip=True)

        # 从标题提取番号
        code = _extract_code_from_title(title) or video_id

        return {
            "video_id": video_id,
            "title": title,
            "code": code,
            "cover_url": cover_url,
            "duration": duration,
            "stats_text": stats_text,
        }

    def _parse_detail_page(
        self, html: str, video_id: str, domain: str
    ) -> Optional[Dict[str, Any]]:
        """解析视频详情页 HTML。"""
        soup = _make_soup(html)

        # ====== 标题 ======
        title = ""
        title_tag = (
            soup.select_one("h3#shareBtn-title")
            or soup.select_one("h1")
            or soup.select_one("title")
        )
        if title_tag:
            title = title_tag.get_text(strip=True)
            # 去掉站点名后缀
            title = re.sub(r'\s*[-–]\s*H動漫.*$', '', title).strip()
            title = re.sub(r'\s*[-–]\s*Hanime1.*$', '', title).strip()

        if not title:
            return None

        # 番号
        code = _extract_code_from_title(title) or video_id

        # ====== 封面/海报 ======
        cover_url = ""
        video_tag = soup.select_one("video#player")
        if video_tag:
            cover_url = str(video_tag.get("poster", "")).strip()

        # 备选: og:image
        if not cover_url:
            og_img = soup.select_one('meta[property="og:image"]')
            if og_img:
                cover_url = str(og_img.get("content", "")).strip()

        # ====== 视频源 URL ======
        video_sources: List[Dict[str, Any]] = []
        if video_tag:
            for source in video_tag.select("source"):
                src = str(source.get("src", "")).strip()
                typ = str(source.get("type", "")).strip()
                size = str(source.get("size", "")).strip()
                if src:
                    video_sources.append({
                        "src": src,
                        "type": typ,
                        "size": size,
                    })

        # 没有 source 时，检查 preload link
        if not video_sources:
            preload_link = soup.select_one('link[rel="preload"][as="video"]')
            if preload_link:
                href = str(preload_link.get("href", "")).strip()
                typ = str(preload_link.get("type", "")).strip()
                if href:
                    video_sources.append({
                        "src": href,
                        "type": typ or "video/mp4",
                        "size": "",
                    })

        # ====== 标签 ======
        tags: List[str] = []
        # 方式1: meta keywords
        meta_kw = soup.select_one('meta[name="keywords"]')
        if meta_kw:
            kw_content = str(meta_kw.get("content", "")).strip()
            tags = [t.strip() for t in kw_content.split(",") if t.strip()]
        # 方式2: .video-tags-wrapper .single-video-tag > a
        if not tags:
            for tag_link in soup.select(".video-tags-wrapper .single-video-tag > a"):
                name = tag_link.get_text(strip=True)
                # 去掉末尾的 (N) 计数
                name = re.sub(r'\s*\(\d+\)\s*$', '', name).strip()
                if name and name not in tags:
                    tags.append(name)

        # ====== 制作方/艺术家 ======
        maker = ""
        maker_tag = soup.select_one("a#video-artist-name")
        if maker_tag:
            maker = maker_tag.get_text(strip=True)

        # ====== 类型 ======
        genre = ""
        genre_tag = soup.select_one('.hidden-xs > a[href*="genre="]')
        if genre_tag:
            genre = genre_tag.get_text(strip=True)

        # ====== 日期和观看次数 ======
        date = ""
        views = ""
        for info_div in soup.select("div.video-details-wrapper"):
            text = info_div.get_text(" ", strip=True)
            # 尝试匹配日期: 2022-10-15
            date_m = re.search(r'(\d{4}[-/]\d{2}[-/]\d{2})', text)
            if date_m:
                date = date_m.group(1)
            # 尝试匹配观看次数
            views_m = re.search(r'观看次数[：:]\s*([\d.]+[万]?次?)', text)
            if views_m:
                views = views_m.group(1)
            if date and views:
                break

        # ====== 描述 ======
        description = ""
        desc_div = soup.select_one("div.video-caption-text.caption-ellipsis")
        if desc_div:
            description = desc_div.get_text(strip=True)

        # ====== 预览截图 ======
        thumbnail_images: List[str] = []
        # 详情页一般没有额外的预览截图列表，但如果有相关推荐，可以提取封面
        for rel_card in soup.select("div.horizontal-card img.main-thumb"):
            src = str(rel_card.get("src", "")).strip()
            if src and not src.startswith("data:") and src != cover_url:
                if src not in thumbnail_images:
                    thumbnail_images.append(src)

        thumbnail_images = thumbnail_images[:10]

        # ====== 资源 URLs（用于代理） ======
        resource_urls: List[str] = []
        if cover_url:
            resource_urls.append(cover_url)
        resource_urls.extend(thumbnail_images)
        for vs in video_sources:
            resource_urls.append(vs["src"])

        result = {
            "video_id": video_id,
            "code": code,
            "title": title,
            "date": date,
            "views": views,
            "maker": maker,
            "genre": genre,
            "tags": tags,
            "description": description,
            "cover_url": cover_url,
            "thumbnail_images": thumbnail_images,
            "video_sources": video_sources,
            "resource_urls": resource_urls,
            "platform": HANIME1_PLATFORM,
            "host_id": f'{HANIME1_HOST_ID_PREFIX}{video_id}',
        }
        return result
