"""OpenAI-compatible image generation adapter used for note artwork."""

import base64
import json
import urllib.error
import urllib.request

from workbench.generators import validate_base_url


class ImageGeneratorError(Exception):
    pass


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class OpenAICompatibleImageGenerator:
    def __init__(self, base_url, model, api_key, timeout=180):
        self.base_url = validate_base_url(base_url)
        self.model = (model or "").strip()
        self.api_key = api_key or ""
        self.timeout = timeout
        if not self.model:
            raise ValueError("请填写生图模型名称")
        if not self.api_key:
            raise ValueError("请先在设置中配置生图 API Key")

    def generate(self, prompt, size="1024x1024"):
        prompt = str(prompt or "").strip()[:4000]
        if not prompt:
            raise ValueError("请填写配图描述")
        if size not in {"1024x1024", "1024x1536", "1536x1024"}:
            raise ValueError("图片尺寸无效")
        payload = {
            "model": self.model,
            "prompt": prompt,
            "n": 1,
            "size": size,
            "response_format": "b64_json",
        }
        request = urllib.request.Request(
            self.base_url + "/images/generations",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.api_key,
            },
            method="POST",
        )
        try:
            opener = urllib.request.build_opener(_NoRedirectHandler)
            with opener.open(request, timeout=self.timeout) as response:
                data = response.read(35 * 1024 * 1024 + 1)
                if len(data) > 35 * 1024 * 1024:
                    raise ImageGeneratorError("生图接口返回内容过大")
                result = json.loads(data.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("error", {}).get("message", "")
            except Exception:
                detail = ""
            raise ImageGeneratorError("生图接口返回 HTTP {}{}".format(
                exc.code, ("：" + str(detail)[:300]) if detail else ""
            ))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ImageGeneratorError("无法连接生图接口：{}".format(exc))
        except (ValueError, UnicodeDecodeError):
            raise ImageGeneratorError("生图接口返回了无法识别的响应")

        try:
            encoded = result["data"][0]["b64_json"]
            image_bytes = base64.b64decode(encoded, validate=True)
        except (KeyError, IndexError, TypeError, ValueError):
            raise ImageGeneratorError("生图接口没有返回图片 Base64 数据；请使用支持 b64_json 的接口")
        if not image_bytes or len(image_bytes) > 25 * 1024 * 1024:
            raise ImageGeneratorError("生成的图片为空或超过 25 MB")
        content_type = str(result.get("data", [{}])[0].get("mime_type") or "image/png").lower()
        if content_type not in {"image/png", "image/jpeg", "image/webp"}:
            content_type = "image/png"
        return image_bytes, content_type
