import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".avi"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
IMAGE_UPLOAD_LIMIT = 32 * 1024 * 1024
VIDEO_UPLOAD_LIMIT = 300 * 1024 * 1024


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def json_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class DuplicatePublishError(Exception):
    def __init__(self, job_id):
        super().__init__("相同内容已有发布任务")
        self.job_id = job_id


class Repository:
    def __init__(self, data_dir=None):
        self.data_dir = Path(data_dir or os.environ.get(
            "XHS_WORKBENCH_DATA_DIR", Path(__file__).resolve().parents[1] / "data"
        )).expanduser().resolve()
        self.media_dir = self.data_dir / "media"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "workbench.sqlite3"
        self._initialize()

    def _connect(self):
        conn = sqlite3.connect(str(self.db_path), timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def _initialize(self):
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS accounts (
                    id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    current_profile_version INTEGER NOT NULL,
                    configured INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS persona_profiles (
                    account_id TEXT NOT NULL REFERENCES accounts(id),
                    version INTEGER NOT NULL,
                    profile_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(account_id, version)
                );
                CREATE TABLE IF NOT EXISTS assets (
                    id TEXT PRIMARY KEY,
                    original_name TEXT NOT NULL,
                    stored_name TEXT NOT NULL,
                    extension TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    media_type TEXT NOT NULL DEFAULT 'video',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS content_packages (
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL REFERENCES accounts(id),
                    persona_version INTEGER NOT NULL,
                    asset_id TEXT NOT NULL REFERENCES assets(id),
                    asset_ids_json TEXT NOT NULL DEFAULT '[]',
                    content_type TEXT NOT NULL DEFAULT 'video',
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    source_refs_json TEXT NOT NULL DEFAULT '[]',
                    fingerprint TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS publish_jobs (
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL REFERENCES accounts(id),
                    content_package_id TEXT NOT NULL REFERENCES content_packages(id),
                    publisher_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    verification_note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS publish_attempts (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES publish_jobs(id),
                    idempotency_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    result_json TEXT NOT NULL DEFAULT '{}',
                    error_text TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_created ON publish_jobs(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_packages_fingerprint ON content_packages(fingerprint);
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS monitor_snapshots (
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL REFERENCES accounts(id),
                    snapshot_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_monitor_created ON monitor_snapshots(created_at DESC);
                CREATE TABLE IF NOT EXISTS toapis_generations (
                    client_request_id TEXT PRIMARY KEY,
                    media_type TEXT NOT NULL,
                    model TEXT NOT NULL,
                    state TEXT NOT NULL,
                    task_id TEXT NOT NULL DEFAULT '',
                    poll_after_seconds INTEGER NOT NULL DEFAULT 5,
                    asset_id TEXT NOT NULL DEFAULT '',
                    error_text TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
            """)
            account_columns = {row["name"] for row in db.execute("PRAGMA table_info(accounts)")}
            if "configured" not in account_columns:
                db.execute("ALTER TABLE accounts ADD COLUMN configured INTEGER NOT NULL DEFAULT 0")
            package_columns = {row["name"] for row in db.execute("PRAGMA table_info(content_packages)")}
            if "source_refs_json" not in package_columns:
                db.execute("ALTER TABLE content_packages ADD COLUMN source_refs_json TEXT NOT NULL DEFAULT '[]'")
            if "asset_ids_json" not in package_columns:
                db.execute("ALTER TABLE content_packages ADD COLUMN asset_ids_json TEXT NOT NULL DEFAULT '[]'")
            if "content_type" not in package_columns:
                db.execute("ALTER TABLE content_packages ADD COLUMN content_type TEXT NOT NULL DEFAULT 'video'")
            asset_columns = {row["name"] for row in db.execute("PRAGMA table_info(assets)")}
            if "media_type" not in asset_columns:
                db.execute("ALTER TABLE assets ADD COLUMN media_type TEXT NOT NULL DEFAULT 'video'")
            existing = db.execute("SELECT id FROM accounts LIMIT 1").fetchone()
            if not existing:
                stamp = now_iso()
                profile = {
                    "positioning": "",
                    "audience": "",
                    "content_lane": "",
                    "tone": "",
                    "structure": "",
                    "boundaries": "",
                    "fact_rules": "",
                    "preferred_topics": [],
                    "publishing_defaults": {"visibility": "公开可见"},
                }
                db.execute(
                    "INSERT INTO accounts(id,display_name,current_profile_version,configured,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                    ("account-1", "", 1, 0, stamp, stamp),
                )
                db.execute(
                    "INSERT INTO persona_profiles(account_id,version,profile_json,created_at) VALUES(?,?,?,?)",
                    ("account-1", 1, json_text(profile), stamp),
                )
            defaults = {
                "publisher_mode": "mock",
                "mcp_endpoint": "http://127.0.0.1:18060/mcp",
            }
            for key, value in defaults.items():
                db.execute(
                    "INSERT OR IGNORE INTO app_settings(key,value,updated_at) VALUES(?,?,?)",
                    (key, value, now_iso()),
                )

    def get_settings(self):
        with self._connect() as db:
            rows = db.execute("SELECT key,value FROM app_settings").fetchall()
        return {row["key"]: row["value"] for row in rows}

    def save_settings(self, settings):
        stamp = now_iso()
        with self._connect() as db:
            for key, value in settings.items():
                db.execute(
                    "INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                    (key, str(value), stamp),
                )
        return self.get_settings()

    def get_account(self):
        with self._connect() as db:
            row = db.execute("SELECT * FROM accounts ORDER BY created_at LIMIT 1").fetchone()
            profile = db.execute(
                "SELECT * FROM persona_profiles WHERE account_id=? AND version=?",
                (row["id"], row["current_profile_version"]),
            ).fetchone()
        return {
            "id": row["id"], "display_name": row["display_name"],
            "current_profile_version": row["current_profile_version"],
            "configured": bool(row["configured"]),
            "profile": json.loads(profile["profile_json"]),
        }

    def save_account(self, display_name, profile):
        display_name = (display_name or "").strip()[:100]
        if not display_name:
            raise ValueError("请填写账号名称")
        if not isinstance(profile, dict):
            raise ValueError("账号定位格式不正确")
        allowed = {
            "positioning": 500, "audience": 500, "content_lane": 200,
            "tone": 500, "structure": 500, "boundaries": 1000,
            "fact_rules": 1000,
        }
        normalized = {}
        for key, limit in allowed.items():
            normalized[key] = str(profile.get(key, "")).strip()[:limit]
        topics = profile.get("preferred_topics", [])
        if isinstance(topics, str):
            topics = [part.strip().lstrip("#") for part in topics.split(",")]
        if not isinstance(topics, list):
            topics = []
        normalized["preferred_topics"] = [str(x).strip().lstrip("#")[:50] for x in topics if str(x).strip()][:20]
        publishing_defaults = profile.get("publishing_defaults", {})
        normalized["publishing_defaults"] = publishing_defaults if isinstance(publishing_defaults, dict) else {}
        configured = bool(
            display_name and normalized["content_lane"] and normalized["positioning"]
            and normalized["audience"] and normalized["tone"]
        )
        stamp = now_iso()
        with self._connect() as db:
            row = db.execute("SELECT * FROM accounts ORDER BY created_at LIMIT 1").fetchone()
            old = db.execute(
                "SELECT profile_json FROM persona_profiles WHERE account_id=? AND version=?",
                (row["id"], row["current_profile_version"]),
            ).fetchone()
            version = row["current_profile_version"]
            if json.loads(old["profile_json"]) != normalized:
                version += 1
                db.execute(
                    "INSERT INTO persona_profiles(account_id,version,profile_json,created_at) VALUES(?,?,?,?)",
                    (row["id"], version, json_text(normalized), stamp),
                )
            db.execute(
                "UPDATE accounts SET display_name=?,current_profile_version=?,configured=?,updated_at=? WHERE id=?",
                (display_name, version, int(configured), stamp, row["id"]),
            )
        return self.get_account()

    def list_assets(self):
        with self._connect() as db:
            rows = db.execute("SELECT * FROM assets ORDER BY created_at DESC").fetchall()
        return [self._asset_dict(row) for row in rows]

    @staticmethod
    def _asset_dict(row):
        return {
            "id": row["id"], "original_name": row["original_name"],
            "mime_type": row["mime_type"], "size_bytes": row["size_bytes"],
            "sha256": row["sha256"], "created_at": row["created_at"],
            "media_type": row["media_type"],
            "url": "/media/" + row["id"],
        }

    def save_asset(self, original_name, file_obj, mime_type):
        original_name = Path(original_name or "video").name.replace("\x00", "")[:240]
        extension = Path(original_name).suffix.lower()
        if extension not in VIDEO_EXTENSIONS | IMAGE_EXTENSIONS:
            raise ValueError("支持 PNG、JPEG、WebP 图片或 MP4、MOV、M4V、WebM、AVI 视频")
        media_type = "image" if extension in IMAGE_EXTENSIONS else "video"
        limit = IMAGE_UPLOAD_LIMIT if media_type == "image" else VIDEO_UPLOAD_LIMIT
        asset_id = uuid.uuid4().hex
        stored_name = asset_id + extension
        target = self.media_dir / stored_name
        digest = hashlib.sha256()
        size = 0
        try:
            with target.open("wb") as out:
                while True:
                    chunk = file_obj.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > limit:
                        limit_label = "32 MB" if media_type == "image" else "300 MB"
                        raise ValueError("单个{}不能超过 {}".format("图片" if media_type == "image" else "视频", limit_label))
                    digest.update(chunk)
                    out.write(chunk)
            if size == 0:
                raise ValueError("文件为空")
            if media_type == "image":
                with target.open("rb") as source:
                    header = source.read(12)
                signatures = {
                    ".png": header.startswith(b"\x89PNG\r\n\x1a\n"),
                    ".jpg": header.startswith(b"\xff\xd8\xff"),
                    ".jpeg": header.startswith(b"\xff\xd8\xff"),
                    ".webp": len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP",
                }
                if not signatures[extension]:
                    raise ValueError("图片扩展名与文件内容不匹配")
        except Exception:
            try:
                target.unlink()
            except OSError:
                pass
            raise
        stamp = now_iso()
        with self._connect() as db:
            db.execute(
                "INSERT INTO assets(id,original_name,stored_name,extension,mime_type,size_bytes,sha256,media_type,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (asset_id, original_name, stored_name, extension,
                 (mime_type or "application/octet-stream")[:100], size, digest.hexdigest(), media_type, stamp),
            )
        return self.get_asset(asset_id)

    def save_generated_image(self, image_bytes, mime_type="image/png"):
        mime_type = (mime_type or "").split(";", 1)[0].strip().lower()
        extensions = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
        extension = extensions.get(mime_type)
        if not extension:
            raise ValueError("只支持 PNG、JPEG 或 WebP 图片")
        if not isinstance(image_bytes, bytes) or not image_bytes:
            raise ValueError("图片内容为空")
        if len(image_bytes) > 25 * 1024 * 1024:
            raise ValueError("单张图片不能超过 25 MB")
        signatures = {
            "image/png": image_bytes.startswith(b"\x89PNG\r\n\x1a\n"),
            "image/jpeg": image_bytes.startswith(b"\xff\xd8\xff"),
            "image/webp": len(image_bytes) >= 12 and image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP",
        }
        if not signatures.get(mime_type):
            raise ValueError("图片格式与生图接口声明不符")
        asset_id = uuid.uuid4().hex
        stored_name = asset_id + extension
        target = self.media_dir / stored_name
        digest = hashlib.sha256(image_bytes).hexdigest()
        target.write_bytes(image_bytes)
        stamp = now_iso()
        try:
            with self._connect() as db:
                db.execute(
                    "INSERT INTO assets(id,original_name,stored_name,extension,mime_type,size_bytes,sha256,media_type,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (asset_id, "小红书配图" + extension, stored_name, extension, mime_type,
                     len(image_bytes), digest, "image", stamp),
                )
        except Exception:
            try:
                target.unlink()
            except OSError:
                pass
            raise
        return self.get_asset(asset_id)

    def save_generated_video(self, video_bytes, mime_type="video/mp4", original_name="ToAPIs 生成视频.mp4"):
        mime_type = (mime_type or "").split(";", 1)[0].strip().lower()
        extensions = {"video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm"}
        extension = extensions.get(mime_type)
        if not extension:
            raise ValueError("ToAPIs 返回的视频格式暂不支持")
        if not isinstance(video_bytes, bytes) or not video_bytes:
            raise ValueError("ToAPIs 返回的视频为空")
        if len(video_bytes) > 1024 * 1024 * 1024:
            raise ValueError("生成的视频不能超过 1 GB")
        if mime_type in {"video/mp4", "video/quicktime"}:
            valid = b"ftyp" in video_bytes[:64]
        else:
            valid = video_bytes.startswith(b"\x1aE\xdf\xa3")
        if not valid:
            raise ValueError("生成文件内容与视频格式不匹配")
        asset_id = uuid.uuid4().hex
        original_name = Path(original_name).name[:240] or ("ToAPIs 生成视频" + extension)
        stored_name = asset_id + extension
        target = self.media_dir / stored_name
        digest = hashlib.sha256(video_bytes).hexdigest()
        target.write_bytes(video_bytes)
        stamp = now_iso()
        try:
            with self._connect() as db:
                db.execute(
                    "INSERT INTO assets(id,original_name,stored_name,extension,mime_type,size_bytes,sha256,media_type,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (asset_id, original_name, stored_name, extension, mime_type,
                     len(video_bytes), digest, "video", stamp),
                )
        except Exception:
            try:
                target.unlink()
            except OSError:
                pass
            raise
        return self.get_asset(asset_id)

    def save_toapis_generation(self, client_request_id, media_type, model, state,
                               task_id="", poll_after_seconds=5, asset_id="", error_text=""):
        stamp = now_iso()
        with self._connect() as db:
            db.execute(
                "INSERT INTO toapis_generations(client_request_id,media_type,model,state,task_id,poll_after_seconds,asset_id,error_text,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(client_request_id) DO UPDATE SET "
                "state=excluded.state,task_id=excluded.task_id,poll_after_seconds=excluded.poll_after_seconds,"
                "asset_id=excluded.asset_id,error_text=excluded.error_text,updated_at=excluded.updated_at",
                (client_request_id, media_type, model, state, task_id,
                 max(1, min(int(poll_after_seconds or 5), 60)), asset_id, error_text[:500], stamp, stamp),
            )
        return self.get_toapis_generation(client_request_id)

    def create_toapis_generation(self, client_request_id, media_type, model, state="starting"):
        stamp = now_iso()
        with self._connect() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO toapis_generations(client_request_id,media_type,model,state,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (client_request_id, media_type, model, state, stamp, stamp),
            )
        return cursor.rowcount == 1, self.get_toapis_generation(client_request_id)

    def get_toapis_generation(self, client_request_id):
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM toapis_generations WHERE client_request_id=?", (client_request_id,),
            ).fetchone()
        if not row:
            return None
        value = dict(row)
        if value["asset_id"]:
            value["asset"] = self.get_asset(value["asset_id"])
        return value

    def list_toapis_generations(self, limit=10):
        with self._connect() as db:
            rows = db.execute(
                "SELECT client_request_id FROM toapis_generations ORDER BY updated_at DESC LIMIT ?",
                (max(1, min(int(limit), 50)),),
            ).fetchall()
        return [self.get_toapis_generation(row["client_request_id"]) for row in rows]

    def get_asset(self, asset_id):
        with self._connect() as db:
            row = db.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
        if not row:
            return None
        value = self._asset_dict(row)
        value["path"] = str(self.media_dir / row["stored_name"])
        value["extension"] = row["extension"]
        return value

    def asset_file(self, asset_id):
        asset = self.get_asset(asset_id)
        if not asset:
            return None, None
        path = Path(asset["path"]).resolve()
        if path.parent != self.media_dir.resolve() or not path.is_file():
            return None, None
        return path, asset

    def create_publish_job(self, asset_id, title, body, tags, publisher_id, source_refs=None,
                           asset_ids=None, content_type="video"):
        account = self.get_account()
        if not account["configured"]:
            raise ValueError("请先完成并保存账号定位，再进入内容制作")
        asset = self.get_asset(asset_id)
        if not asset:
            raise ValueError("请先准备笔记配图")
        content_type = content_type if content_type in {"video", "note"} else "video"
        media_ids = asset_ids if isinstance(asset_ids, list) else [asset_id]
        media_ids = list(dict.fromkeys(str(value) for value in media_ids if value))[:9]
        if not media_ids or media_ids[0] != asset_id:
            raise ValueError("请先准备笔记配图")
        media_assets = [self.get_asset(value) for value in media_ids]
        if any(item is None for item in media_assets):
            raise ValueError("笔记中的图片素材不存在")
        expected_media = "image" if content_type == "note" else "video"
        if any(item["media_type"] != expected_media for item in media_assets):
            raise ValueError("内容形态与素材类型不匹配")
        title = str(title or "").strip()[:20]
        body = str(body or "").strip()[:1000]
        if not title:
            raise ValueError("请填写标题")
        if not body:
            raise ValueError("请填写笔记正文")
        if isinstance(tags, str):
            tags = [x.strip().lstrip("#") for x in tags.split(",")]
        if not isinstance(tags, list):
            tags = []
        tags = [str(x).strip().lstrip("#")[:50] for x in tags if str(x).strip()][:10]
        if not isinstance(source_refs, list):
            source_refs = []
        source_refs = [
            {key: str(item.get(key, ""))[:limit] for key, limit in (
                ("source", 100), ("title", 300), ("url", 1000),
                ("published_at", 100), ("summary", 1800),
            )}
            for item in source_refs[:20] if isinstance(item, dict)
        ]
        signature = json_text({
            "account_id": account["id"], "content_type": content_type,
            "asset_sha256s": [item["sha256"] for item in media_assets],
            "title": title, "body": body, "tags": tags,
        })
        fingerprint = hashlib.sha256(signature.encode("utf-8")).hexdigest()
        stamp = now_iso()
        with self._connect() as db:
            duplicate = db.execute(
                "SELECT j.id,j.status FROM publish_jobs j "
                "JOIN content_packages c ON c.id=j.content_package_id "
                "WHERE c.fingerprint=? AND j.publisher_id=? AND j.status IN "
                "('submitting','submitted','under_review','verified_visible','unknown_manual_check') "
                "ORDER BY j.created_at DESC LIMIT 1",
                (fingerprint, publisher_id),
            ).fetchone()
            if duplicate:
                raise DuplicatePublishError(duplicate["id"])
            package_id = uuid.uuid4().hex
            job_id = uuid.uuid4().hex
            attempt_id = uuid.uuid4().hex
            idempotency_key = uuid.uuid4().hex
            db.execute(
                "INSERT INTO content_packages(id,account_id,persona_version,asset_id,asset_ids_json,content_type,title,body,tags_json,source_refs_json,fingerprint,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (package_id, account["id"], account["current_profile_version"], asset_id,
                 json_text(media_ids), content_type, title, body, json_text(tags), json_text(source_refs), fingerprint, stamp),
            )
            db.execute(
                "INSERT INTO publish_jobs(id,account_id,content_package_id,publisher_id,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (job_id, account["id"], package_id, publisher_id, "submitting", stamp, stamp),
            )
            db.execute(
                "INSERT INTO publish_attempts(id,job_id,idempotency_key,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (attempt_id, job_id, idempotency_key, "submitting", stamp, stamp),
            )
        return self.get_job(job_id), idempotency_key

    def update_publish_result(self, job_id, status, result=None, error=""):
        allowed = {"submitted", "under_review", "failed", "unknown_manual_check", "simulated"}
        if status not in allowed:
            raise ValueError("发布连接器返回了无效状态")
        stamp = now_iso()
        payload = json_text(result or {})
        with self._connect() as db:
            db.execute(
                "UPDATE publish_jobs SET status=?,updated_at=? WHERE id=?",
                (status, stamp, job_id),
            )
            db.execute(
                "UPDATE publish_attempts SET status=?,result_json=?,error_text=?,updated_at=? "
                "WHERE job_id=? AND created_at=(SELECT MAX(created_at) FROM publish_attempts WHERE job_id=?)",
                (status, payload, str(error or "")[:1000], stamp, job_id, job_id),
            )
        return self.get_job(job_id)

    def verify_job(self, job_id, outcome, note=""):
        mapping = {
            "visible": "verified_visible",
            "review": "under_review",
            "not_found": "failed",
        }
        if outcome not in mapping:
            raise ValueError("核验选项无效")
        status = mapping[outcome]
        stamp = now_iso()
        note = str(note or "").strip()[:1000]
        with self._connect() as db:
            row = db.execute("SELECT id,status FROM publish_jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise ValueError("没有找到该发布任务")
            if row["status"] not in {"submitted", "under_review", "unknown_manual_check"}:
                raise ValueError("当前任务状态不支持人工核验")
            db.execute(
                "UPDATE publish_jobs SET status=?,verification_note=?,updated_at=? WHERE id=?",
                (status, note, stamp, job_id),
            )
            db.execute(
                "UPDATE publish_attempts SET status=?,result_json=?,updated_at=? "
                "WHERE job_id=? AND created_at=(SELECT MAX(created_at) FROM publish_attempts WHERE job_id=?)",
                (status, json_text({"verification": outcome, "note": note}), stamp, job_id, job_id),
            )
        return self.get_job(job_id)

    def get_job(self, job_id):
        with self._connect() as db:
            row = db.execute(
                "SELECT j.*,c.title,c.body,c.tags_json,c.source_refs_json,c.asset_id,c.asset_ids_json,c.content_type,c.persona_version,a.original_name "
                "FROM publish_jobs j JOIN content_packages c ON c.id=j.content_package_id "
                "JOIN assets a ON a.id=c.asset_id WHERE j.id=?",
                (job_id,),
            ).fetchone()
            attempt = db.execute(
                "SELECT * FROM publish_attempts WHERE job_id=? ORDER BY created_at DESC LIMIT 1",
                (job_id,),
            ).fetchone()
        if not row:
            return None
        return self._job_dict(row, attempt)

    def list_jobs(self, limit=30):
        with self._connect() as db:
            rows = db.execute(
                "SELECT j.*,c.title,c.body,c.tags_json,c.source_refs_json,c.asset_id,c.asset_ids_json,c.content_type,c.persona_version,a.original_name "
                "FROM publish_jobs j JOIN content_packages c ON c.id=j.content_package_id "
                "JOIN assets a ON a.id=c.asset_id ORDER BY j.created_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
            results = []
            for row in rows:
                attempt = db.execute(
                    "SELECT * FROM publish_attempts WHERE job_id=? ORDER BY created_at DESC LIMIT 1",
                    (row["id"],),
                ).fetchone()
                results.append(self._job_dict(row, attempt))
        return results

    @staticmethod
    def _job_dict(row, attempt):
        try:
            source_refs = json.loads(row["source_refs_json"])
        except (KeyError, TypeError, json.JSONDecodeError):
            source_refs = []
        try:
            asset_ids = json.loads(row["asset_ids_json"])
        except (KeyError, TypeError, json.JSONDecodeError):
            asset_ids = [row["asset_id"]]
        if not asset_ids:
            asset_ids = [row["asset_id"]]
        return {
            "id": row["id"], "account_id": row["account_id"],
            "publisher_id": row["publisher_id"], "status": row["status"],
            "verification_note": row["verification_note"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "title": row["title"], "body": row["body"],
            "tags": json.loads(row["tags_json"]), "asset_id": row["asset_id"],
            "asset_ids": asset_ids, "content_type": row["content_type"],
            "source_refs": source_refs,
            "asset_name": row["original_name"], "persona_version": row["persona_version"],
            "attempt": {
                "id": attempt["id"], "status": attempt["status"],
                "result": json.loads(attempt["result_json"]),
                "error": attempt["error_text"], "created_at": attempt["created_at"],
            } if attempt else None,
        }

    def save_monitor_snapshot(self, snapshot):
        snapshot_id = uuid.uuid4().hex
        stamp = now_iso()
        # Persist only display metrics. The MCP session owns cookies/tokens;
        # even a malformed connector response must not put credentials in the DB.
        raw = snapshot if isinstance(snapshot, dict) else {}
        payload = {
            key: raw[key] for key in (
                "login_status", "message", "nickname", "red_id", "description",
                "metrics", "notes", "metrics_limit", "source", "observed_at",
            ) if key in raw
        }
        metrics = payload.get("metrics")
        if isinstance(metrics, dict):
            payload["metrics"] = {
                key: str(metrics[key])[:100] for key in (
                    "follows", "fans", "likes_and_saves", "loaded_notes",
                ) if key in metrics
            }
        else:
            payload.pop("metrics", None)
        notes = payload.get("notes")
        if isinstance(notes, list):
            payload["notes"] = [
                {key: str(item.get(key, ""))[:300] for key in (
                    "id", "title", "likes", "comments", "saves", "shares",
                ) if key in item}
                for item in notes[:50] if isinstance(item, dict)
            ]
        else:
            payload.pop("notes", None)
        payload["checked_at"] = stamp
        with self._connect() as db:
            account = db.execute("SELECT id FROM accounts ORDER BY created_at LIMIT 1").fetchone()
            db.execute(
                "INSERT INTO monitor_snapshots(id,account_id,snapshot_json,created_at) VALUES(?,?,?,?)",
                (snapshot_id, account["id"], json_text(payload), stamp),
            )
        return {"id": snapshot_id, **payload}

    def get_monitor_history(self, limit=30):
        with self._connect() as db:
            rows = db.execute(
                "SELECT id,snapshot_json,created_at FROM monitor_snapshots "
                "ORDER BY created_at DESC LIMIT ?", (int(limit),)
            ).fetchall()
        return [
            {"id": row["id"], **json.loads(row["snapshot_json"]), "checked_at": row["created_at"]}
            for row in rows
        ]
