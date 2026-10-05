"""Hanime1 插件协议契约测试（API_INTEGRATION_STANDARD §10）。

全部离线：不联网、不启动后端，只校验清单、Provider 契约与纯函数行为。
运行方式（在项目根目录）：python -m pytest comic_backend/third_party/hanime1/tests -q
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys

import pytest

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_ROOT = os.path.abspath(os.path.join(PLUGIN_DIR, "..", ".."))
PROJECT_ROOT = os.path.dirname(BACKEND_ROOT)
for _root in (BACKEND_ROOT, PROJECT_ROOT):
    if _root not in sys.path:
        sys.path.insert(0, _root)

protocol_base = pytest.importorskip("protocol.base")

MANIFEST_PATH = os.path.join(PLUGIN_DIR, "ultimate-plugin.json")
PROVIDER_PATH = os.path.join(PLUGIN_DIR, "ultimate_provider.py")

PLUGIN_ID = "video.hanime1"
ENTRYPOINT = "./ultimate_provider.py:Hanime1Provider"
PROTOCOL_VERSIONS = {"1.0", "1.1", "2.0"}
FIELD_TYPES = {"boolean", "text", "password", "textarea", "number"}
SECRET_FIELDS: tuple = ()
REQUIRED_FIELDS = ("domain",)
RUNTIME_DEPENDENCIES = ("requests", "beautifulsoup4", "lxml")


@pytest.fixture(scope="module")
def manifest() -> dict:
    with open(MANIFEST_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture(scope="module")
def plugin_module():
    spec = importlib.util.spec_from_file_location("_hanime1_plugin_under_test", PROVIDER_PATH)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


@pytest.fixture()
def provider(manifest, plugin_module):
    return plugin_module.Hanime1Provider(manifest=manifest, manifest_path=MANIFEST_PATH)


def _capability_keys(manifest: dict) -> set:
    return {str(item.get("key") or "").strip() for item in manifest["capabilities"]}


def _configuration_fields(manifest: dict) -> list:
    return [
        field
        for section in manifest["configuration"]["sections"]
        for field in section.get("fields") or []
    ]


# ---------- 清单契约 ----------

def test_manifest_minimum_contract(manifest):
    assert manifest["protocol_version"] in PROTOCOL_VERSIONS
    plugin = manifest["plugin"]
    assert plugin["id"] == PLUGIN_ID
    assert plugin["entrypoint"] == ENTRYPOINT
    assert plugin["config_key"] == "hanime1"
    assert plugin["version"]
    assert manifest["media_types"] == ["video"]
    assert manifest["identity"]["host_id_prefix"] == "HN1"
    assert manifest["identity"]["platform_label"] == "Hanime1"


def test_capabilities_match_provider_constant(manifest, plugin_module):
    assert _capability_keys(manifest) == set(plugin_module.SUPPORTED_CAPABILITIES)


def test_capability_dispatch_covers_declared_set(plugin_module):
    with open(PROVIDER_PATH, "r", encoding="utf-8") as handle:
        source = handle.read()
    dispatched = set(re.findall(r'capability\s*==\s*"([^"]+)"', source))
    assert dispatched == set(plugin_module.SUPPORTED_CAPABILITIES)


def test_configuration_field_types_are_supported(manifest):
    for field in _configuration_fields(manifest):
        assert field["type"] in FIELD_TYPES, field


def test_configuration_credential_block_is_consistent(manifest):
    credential = manifest["configuration"]["credential"]
    fields = _configuration_fields(manifest)
    boolean_fields = {field["key"] for field in fields if field["type"] == "boolean"}
    assert credential["enabled_field"] in boolean_fields
    assert set(credential.get("required_fields") or []) <= {field["key"] for field in fields}
    assert credential["required_fields"] == list(REQUIRED_FIELDS)
    assert credential["disabled_message"]


def test_secret_fields_are_flagged(manifest):
    secrets = {field["key"] for field in _configuration_fields(manifest) if field.get("secret")}
    assert secrets == set(SECRET_FIELDS)


def test_packaging_declares_dependency_pool_entries(manifest):
    packaging = manifest["packaging"]
    assert packaging["android"]["enabled"] is True
    declared = set()
    for platform in ("android", "external", "pyinstaller"):
        requirements = packaging[platform]["pip_requirements"]
        assert requirements and all(str(item).strip() for item in requirements)
        declared |= {str(item).strip().lower().replace("_", "-") for item in requirements}
    for name in RUNTIME_DEPENDENCIES:
        assert name in declared


def test_presentation_declares_mobile_aspect_ratio(manifest):
    cover = manifest["presentation"]["media_card"]["cover"]
    assert cover["aspect_ratio"]
    assert cover["mobile_aspect_ratio"]


# ---------- Provider 契约 ----------

def test_provider_inherits_protocol_provider(provider):
    assert isinstance(provider, protocol_base.ProtocolProvider)
    for method in ("execute", "normalize_config", "serialize_public_config", "get_query_status", "build_client"):
        assert callable(getattr(provider, method))


def test_undeclared_capability_is_rejected(provider):
    with pytest.raises(ValueError):
        provider.execute("catalog.by_code", {}, {}, {"enabled": True})


def test_enabled_defaults_to_disabled(provider):
    assert provider.normalize_config({})["enabled"] is False
    assert provider.get_query_status({})["configured"] is False


def test_health_status_answers_while_disabled(provider):
    status = provider.execute("health.query.status", {}, {}, {})
    assert set(status) >= {"configured", "message", "missing_fields"}
    assert status["configured"] is False


def test_disabled_provider_refuses_query(provider):
    with pytest.raises(RuntimeError):
        provider.execute("catalog.search", {"keyword": "x"}, {}, {"enabled": False})


def test_detail_requires_video_id(provider):
    with pytest.raises(RuntimeError):
        provider.execute(
            "catalog.detail", {}, {}, {"enabled": True, "domain": "https://hanime1.me"}
        )


def test_video_summary_uses_manifest_host_prefix(provider, manifest):
    summary = provider._to_video_summary({"video_id": "12345"})
    assert summary["host_id"] == f'{manifest["identity"]["host_id_prefix"]}12345'


def test_strip_host_prefix(provider):
    assert provider._strip_host_prefix("HN112345") == "12345"
    assert provider._strip_host_prefix("12345") == "12345"
    assert provider._strip_host_prefix("HN1") == "HN1"


# ---------- 代理与播放源契约 ----------

def test_resolve_proxy_target_uses_domain_and_path(provider):
    target = provider._resolve_proxy_target(
        {"domain": "vdownload.hembed.com", "path": "media/1/index.m3u8", "query_string": "t=123"}
    )
    assert target == "https://vdownload.hembed.com/media/1/index.m3u8?t=123"


def test_resolve_proxy_target_prefers_body_url(provider):
    assert provider._resolve_proxy_target({"body_url": "https://x/y", "domain": "z"}) == "https://x/y"


def test_proxy_response_exposes_host_contract(plugin_module):
    class _FakeResponse:
        content = b"payload"
        status_code = 206
        headers = {"Content-Type": "video/mp4"}

        def iter_content(self, chunk_size=1):
            yield self.content

        def close(self):
            self.closed = True

    wrapper = plugin_module._ProxyResponse(_FakeResponse())
    assert wrapper.body == b"payload"
    assert wrapper.content == b"payload"
    assert wrapper.status_code == 206
    assert wrapper.headers["Content-Type"] == "video/mp4"
    assert b"".join(wrapper.iter_content(4)) == b"payload"
    wrapper.close()


def test_proxy_stream_returns_wrapper_with_body(provider):
    class _FakeSession:
        def request(self, method, url, **kwargs):
            class _Response:
                content = b"stream-body"
                status_code = 200
                headers = {}

                def close(self):
                    pass

            return _Response()

    result = provider._handle_proxy_stream(
        _FakeSession(), {"timeout_seconds": 5}, {"domain": "vdownload.hembed.com", "path": "a/b.mp4"}
    )
    assert result.body == b"stream-body"
    assert result.status_code == 200


def test_proxy_stream_requires_target(provider):
    with pytest.raises(ValueError):
        provider._handle_proxy_stream(object(), {}, {})


def test_proxy_video_url_uses_injected_base_path(provider):
    url = provider._to_proxy_video_url("https://vdownload.hembed.com/a.mp4", "/api/v1/video")
    assert url.startswith("/api/v1/video/proxy2?url=")
    assert provider._to_proxy_video_url("https://cdn.other.com/a.mp4", "/api/v1/video") == "https://cdn.other.com/a.mp4"


def test_build_sources_reuses_detail_and_injected_base_path(provider, monkeypatch):
    captured = {}

    def _fake_detail(session, config, params):
        captured.update(params)
        return {"videos": [{"video_sources": [{"src": "https://vdownload.hembed.com/x.mp4", "size": "720p"}]}]}

    monkeypatch.setattr(provider, "_handle_detail", _fake_detail)
    sources = provider._handle_build_sources(
        object(), {}, {"code": "HN1999", "proxy_base_path": "/api/v1/video"}
    )
    assert captured == {"video_id": "999"}
    assert sources and sources[0]["streams"][0]["url"].startswith("/api/v1/video/proxy2?url=")


def test_make_soup_parses_with_available_parser(plugin_module):
    soup = plugin_module._make_soup("<html><body><p>ok</p></body></html>")
    assert soup.find("p").get_text() == "ok"
