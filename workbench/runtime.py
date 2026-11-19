"""Thread-safe process-only configuration for credentials and active models."""

import json
import os
import re
import threading
import uuid


def load_generator_profiles(settings, validate_base_url, provider_ids):
    profiles = []
    raw_profiles = settings.get("generator_profiles_json")
    has_current_profile_list = False
    if raw_profiles:
        try:
            values = json.loads(raw_profiles)
            if isinstance(values, list):
                has_current_profile_list = True
                seen = set()
                for index, raw in enumerate(values[:8]):
                    if not isinstance(raw, dict):
                        continue
                    profile_id = str(raw.get("id") or "model-{}".format(index + 1))[:80]
                    provider = str(raw.get("provider") or "custom")
                    protocol = str(raw.get("protocol") or "chat_completions")
                    model = str(raw.get("model") or "").strip()[:120]
                    try:
                        base_url = validate_base_url(raw.get("base_url") or "")
                    except ValueError:
                        continue
                    if (not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", profile_id)
                            or profile_id in seen or provider not in provider_ids
                            or protocol not in {"chat_completions", "responses"} or not model):
                        continue
                    seen.add(profile_id)
                    profiles.append({
                        "id": profile_id, "name": str(raw.get("name") or model)[:80],
                        "provider": provider, "base_url": base_url, "protocol": protocol,
                        "model": model, "api_key": "",
                    })
        except (TypeError, ValueError):
            profiles = []

    if not has_current_profile_list:
        model = settings.get("generator_model") or os.environ.get("XHS_LLM_MODEL", "")
        if model:
            provider = settings.get("generator_provider", "custom")
            if provider not in provider_ids:
                provider = "custom"
            profiles = [{
                "id": "legacy-default", "name": "当前文案模型", "provider": provider,
                "base_url": settings.get("generator_base_url") or os.environ.get(
                    "XHS_LLM_BASE_URL", "https://api.openai.com/v1"),
                "protocol": settings.get("generator_protocol", "chat_completions"),
                "model": model, "api_key": "",
            }]
            profiles[0]["base_url"] = validate_base_url(profiles[0]["base_url"])

    env_key = os.environ.get("XHS_LLM_API_KEY", "")
    env_model = os.environ.get("XHS_LLM_MODEL", "")
    env_base_url = os.environ.get("XHS_LLM_BASE_URL", "https://api.openai.com/v1")
    if profiles and env_key and not has_current_profile_list:
        profiles[0]["api_key"] = env_key
    elif profiles and env_key and env_model:
        try:
            env_base_url = validate_base_url(env_base_url)
        except ValueError:
            env_base_url = ""
        target = next((item for item in profiles
                       if item["model"] == env_model and item["base_url"] == env_base_url), None)
        if target:
            target["api_key"] = env_key
    return profiles


def generator_public_settings(runtime):
    profiles = runtime.list_generator_profiles_public()
    preferred = profiles[0] if profiles else {
        "id": "", "name": "", "provider": "custom",
        "base_url": "https://api.openai.com/v1", "protocol": "chat_completions",
        "model": "", "key_in_memory": False, "configured": False,
    }
    return {
        **preferred, "profiles": profiles, "auto_fallback": runtime.get_auto_fallback(),
        "configured": any(item["configured"] for item in profiles),
        "primary_configured": bool(profiles and profiles[0]["configured"]),
        "key_in_memory": bool(profiles and profiles[0]["key_in_memory"]),
    }


def save_generator_profiles(value, runtime, repository, validate_base_url, provider_ids):
    values = value.get("profiles")
    api_key_profile_id = str(value.get("api_key_profile_id") or "")[:80]
    api_key = str(value.get("api_key") or "").strip()[:2000]
    clear_api_key = value.get("clear_api_key") is True
    if not isinstance(values, list) or len(values) > 8:
        raise ValueError("模型方案数量需要在 0 到 8 个之间")

    old_by_id = {item["id"]: item for item in runtime.get_active_generator_credentials()}
    profiles, seen = [], set()
    for index, raw in enumerate(values):
        if not isinstance(raw, dict):
            raise ValueError("模型方案格式无效")
        profile_id = str(raw.get("id") or uuid.uuid4().hex)[:80]
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", profile_id) or profile_id in seen:
            raise ValueError("模型方案编号无效或重复")
        seen.add(profile_id)
        name = str(raw.get("name") or "").strip()[:80]
        provider = str(raw.get("provider") or "custom").strip()
        base_url = validate_base_url(raw.get("base_url") or "")
        model = str(raw.get("model") or "").strip()[:120]
        protocol = str(raw.get("protocol") or "chat_completions").strip()
        if not name:
            raise ValueError("请填写模型方案名称")
        if not model:
            raise ValueError("请填写模型名称")
        if provider not in provider_ids:
            raise ValueError("不支持的文案模型服务商类型")
        if protocol not in {"chat_completions", "responses"}:
            raise ValueError("文案模型接口协议无效")
        old = old_by_id.get(profile_id, {})
        key = old.get("api_key", "")
        endpoint_changed = base_url != old.get("base_url") or provider != old.get("provider")
        if profile_id == api_key_profile_id:
            if clear_api_key:
                key = ""
            elif api_key:
                key = api_key
        if endpoint_changed and not (profile_id == api_key_profile_id and api_key and not clear_api_key):
            key = ""
        profiles.append({
            "id": profile_id, "name": name, "provider": provider, "base_url": base_url,
            "protocol": protocol, "model": model, "api_key": key,
        })

    auto_fallback = value.get("auto_fallback") is not False
    stored_profiles = [{key: item[key] for key in (
        "id", "name", "provider", "base_url", "protocol", "model",
    )} for item in profiles]
    settings = {
        "generator_profiles_json": json.dumps(stored_profiles, ensure_ascii=False),
        "generator_auto_fallback": "1" if auto_fallback else "0",
    }
    if profiles:
        primary = profiles[0]
        settings.update({
            "generator_base_url": primary["base_url"], "generator_model": primary["model"],
            "generator_protocol": primary["protocol"], "generator_provider": primary["provider"],
        })
    else:
        settings.update({key: "" for key in (
            "generator_base_url", "generator_model", "generator_protocol", "generator_provider",
        )})
    repository.save_settings(settings)
    runtime.save_generator_profiles(profiles, auto_fallback)
    return generator_public_settings(runtime)


class RuntimeConfigManager:
    def __init__(self, generator_profiles=None, auto_fallback=True, toapis=None):
        self._lock = threading.RLock()
        self._generator_profiles = generator_profiles if isinstance(generator_profiles, list) else []
        self._auto_fallback = bool(auto_fallback)
        self._toapis = toapis if isinstance(toapis, dict) else dict(toapis or {})

    def save_generator_profiles(self, profiles, auto_fallback):
        with self._lock:
            self._generator_profiles[:] = [dict(item) for item in profiles]
            self._auto_fallback = bool(auto_fallback)

    def list_generator_profiles_public(self):
        with self._lock:
            return [{key: item[key] for key in (
                "id", "name", "provider", "base_url", "protocol", "model",
            )} | {
                "key_in_memory": bool(item.get("api_key")),
                "configured": bool(item.get("model") and item.get("api_key")),
            } for item in self._generator_profiles]

    def get_active_generator_credentials(self):
        with self._lock:
            return [dict(item) for item in self._generator_profiles]

    def get_auto_fallback(self):
        with self._lock:
            return self._auto_fallback

    def get_toapis(self):
        with self._lock:
            return dict(self._toapis)

    def save_toapis(self, values):
        with self._lock:
            self._toapis.clear()
            self._toapis.update(values)

    def update_toapis(self, **values):
        with self._lock:
            self._toapis.update(values)
            return dict(self._toapis)

    def redact(self, message):
        with self._lock:
            for item in self._generator_profiles:
                key = item.get("api_key")
                if key:
                    message = message.replace(key, "[已隐藏]")
            key = self._toapis.get("api_key")
            if key:
                message = message.replace(key, "[已隐藏]")
            return message
