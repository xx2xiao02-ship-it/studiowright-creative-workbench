"""Local durable media archive. No provider calls or cloud project operations.

Content addressing / verified staged copies follow the old Flow2API library's
archival approach. Provenance remains a separate immutable record per source.
"""

import hashlib
import json
import math
import os
import shutil
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from PIL import Image
from material_tags import TagIndex, SEARCH_LABELS
from media_analysis import audio_duration


AUDIO_CATEGORIES = ("music", "sound_effect", "other")
ARCHIVE_MAX_BYTES = 1024**3
# Keep the displayed category and database filter identical for shared files.
AUDIO_CATEGORY_SQL = """
    COALESCE(
        (SELECT json_extract(o.details, '$.audio_category')
         FROM origins o WHERE o.asset_id=a.id
         AND json_extract(o.details, '$.audio_category') IN ('music', 'sound_effect', 'other')
         ORDER BY o.source, o.source_id LIMIT 1),
        CASE WHEN EXISTS(SELECT 1 FROM origins o WHERE o.asset_id=a.id AND o.source='flow_music')
             THEN 'music' ELSE 'other' END
    )
"""


def duration_bound(value):
    if value is None or value == "":
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("音频时长必须是非负秒数。") from None
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("音频时长必须是非负秒数。")
    return seconds


def digest(path):
    with Path(path).open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


class Library(TagIndex):
    def __init__(self, root, analysis_db=None):
        self.root = Path(root).resolve()
        self.analysis_db = Path(analysis_db) if analysis_db else None
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.root / "objects").mkdir(exist_ok=True)
        for category in AUDIO_CATEGORIES:
            (self.root / "objects" / "audio" / category).mkdir(parents=True, exist_ok=True)
        (self.root / "staging").mkdir(exist_ok=True)
        self.db = self.root / "library.db"
        with self.connect() as c:
            c.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS assets(
              id TEXT PRIMARY KEY, sha256 TEXT NOT NULL UNIQUE, relative_path TEXT NOT NULL,
              media_type TEXT NOT NULL, mime TEXT NOT NULL, size INTEGER NOT NULL,
              width INTEGER, height INTEGER, duration REAL, added REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS origins(
              source TEXT NOT NULL, source_id TEXT NOT NULL, asset_id TEXT NOT NULL,
              account_id TEXT, account_email TEXT, project_id TEXT, media_id TEXT,
              request_id TEXT, model TEXT, prompt TEXT, created TEXT, details TEXT,
              PRIMARY KEY(source,source_id));
            CREATE INDEX IF NOT EXISTS origin_asset ON origins(asset_id);
            CREATE TABLE IF NOT EXISTS tags(asset_id TEXT, value TEXT, PRIMARY KEY(asset_id,value));
            CREATE TABLE IF NOT EXISTS archive_queue(
              key TEXT PRIMARY KEY, path TEXT NOT NULL, metadata TEXT NOT NULL,
              state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER DEFAULT 0,
              next_try REAL DEFAULT 0, error TEXT);
            """)
        self.db.chmod(0o600)
        self.init_index()

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.db, timeout=15, uri=True)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    def archive(self, path, meta, expected=None, *, max_bytes=ARCHIVE_MAX_BYTES, report_created=False):
        source, source_id = meta["source"], str(meta["source_id"])
        path = Path(path).resolve()
        if not path.is_file() or not 0 < path.stat().st_size <= max_bytes:
            raise ValueError("素材文件不存在、为空或超过大小限制。")
        stage = self.root / "staging" / (uuid.uuid4().hex + ".part")
        try:
            with path.open("rb") as src, stage.open("xb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
                dst.flush()
                os.fsync(dst.fileno())
            sha = digest(stage)
            if expected and sha != expected:
                raise ValueError("素材完整性校验不一致，未导入。")
            width = height = None
            with stage.open("rb") as f:
                header = f.read(32)
            if header[:4] == b"RIFF" and header[8:12] == b"WAVE":
                kind, mime, ext = "audio", "audio/wav", ".wav"
            elif (
                len(header) >= 12
                and header[4:8] == b"ftyp"
                and header[8:12].startswith(b"M4A")
            ):
                # Flow Music 产出的 AAC 音轨（ISO 容器，主品牌 M4A）。
                kind, mime, ext = "audio", "audio/mp4", ".m4a"
            elif (
                len(header) >= 12
                and header[4:8] == b"ftyp"
                and not any(b in header[8:] for b in (b"avif", b"avis"))
            ):
                kind, mime, ext = ("video", "video/quicktime", ".mov") if header[8:12] == b"qt  " else (
                    "video", "video/mp4", ".mp4")
                size, offset, boxes = stage.stat().st_size, 0, set()
                with stage.open("rb") as f:
                    while offset < size:
                        f.seek(offset)
                        box = f.read(16)
                        if len(box) < 8:
                            raise ValueError("视频文件不完整。")
                        length = int.from_bytes(box[:4], "big")
                        minimum = 8
                        if length == 1:
                            length, minimum = int.from_bytes(box[8:16], "big"), 16
                        elif length == 0:
                            length = size - offset
                        if length < minimum or offset + length > size:
                            raise ValueError("视频文件不完整。")
                        boxes.add(box[4:8])
                        offset += length
                if not {b"moov", b"mdat"} <= boxes:
                    raise ValueError("视频缺少媒体数据。")
            elif header.startswith(b"\x1aE\xdf\xa3"):
                kind, mime, ext = "video", "video/webm", ".webm"
            else:
                with Image.open(stage) as im:
                    width, height = im.size
                    fmt = im.format
                    im.verify()
                if fmt not in {"JPEG", "PNG", "WEBP", "GIF", "BMP", "AVIF"}:
                    raise ValueError("不支持的素材格式。")
                kind, mime, ext = (
                    "image",
                    Image.MIME[fmt],
                    "." + {"JPEG": "jpg"}.get(fmt, fmt.lower()),
                )
            if meta.get("media_type") and meta["media_type"] != kind:
                raise ValueError("素材类型与原记录不一致。")
            duration = audio_duration(stage, mime) if kind == "audio" else meta.get("duration")
            category = meta.get("details", {}).get("audio_category")
            if category not in AUDIO_CATEGORIES:
                category = "music" if source == "flow_music" else "other"
            with self.connect() as c:
                c.execute("BEGIN IMMEDIATE")
                old = c.execute(
                    "SELECT asset_id FROM origins WHERE source=? AND source_id=?",
                    (source, source_id),
                ).fetchone()
                if old and old["asset_id"] != sha:
                    raise ValueError("同一来源的文件内容发生变化，已保留原始记录，未覆盖。")
                # Resolve the recorded path under the same transaction as the
                # insert, so concurrent sources agree on one object location.
                asset = c.execute("SELECT relative_path FROM assets WHERE id=?", (sha,)).fetchone()
                rel = Path(asset["relative_path"]) if asset else self.object_path(
                    sha, ext, category if kind == "audio" else None)
                dest = (self.root / rel).resolve()
                if not dest.is_relative_to(self.root / "objects"):
                    raise ValueError("素材存储位置无法确认。")
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.link(stage, dest)
                except FileExistsError:
                    if digest(dest) != sha:
                        raise ValueError("目标文件校验失败，已保留原文件，未覆盖。") from None
                created = c.execute(
                    "INSERT OR IGNORE INTO assets VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        sha,
                        sha,
                        rel.as_posix(),
                        kind,
                        mime,
                        dest.stat().st_size,
                        width or meta.get("width"),
                        height or meta.get("height"),
                        duration,
                        time.time(),
                    ),
                ).rowcount == 1
                if kind == "audio" and duration is not None:
                    c.execute("UPDATE assets SET duration=? WHERE id=? AND duration IS NULL", (duration, sha))
                columns = [
                    "account_id",
                    "account_email",
                    "project_id",
                    "media_id",
                    "request_id",
                    "model",
                    "prompt",
                    "created",
                ]
                values = [str(meta.get(k) or "") for k in columns]
                c.execute(
                    "INSERT OR IGNORE INTO origins VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        source,
                        source_id,
                        sha,
                        *values,
                        json.dumps(meta.get("details", {}), ensure_ascii=False),
                    ),
                )
                # Re-import can complete a missing email from the original source DB;
                # it cannot replace an existing identity or change project provenance.
                if meta.get("account_email"):
                    c.execute(
                        "UPDATE origins SET account_email=? WHERE source=? AND source_id=? AND account_email=''",
                        (meta["account_email"], source, source_id),
                    )
                for tag in meta.get("tags", []):
                    if isinstance(tag, str) and 0 < len(tag.strip()) <= 80:
                        c.execute("INSERT OR IGNORE INTO tags VALUES(?,?)", (sha, tag if source == "flow_music" else tag.strip()))
                if meta.get("structured_tags"):
                    manual = {r[0] for r in c.execute("SELECT dimension FROM manual_dimensions WHERE asset_id=?", (sha,))}
                    self.write_dimensions(c, sha, {d: v for d, v in meta["structured_tags"].items() if d not in manual}, "music_prompt" if source == "flow_music" else "legacy_import")
                c.execute("INSERT OR IGNORE INTO index_jobs(asset_id) VALUES(?)", (sha,))
                if meta.get("index_provenance"):
                    c.execute("UPDATE index_jobs SET provenance=?,state=? WHERE asset_id=? AND state='pending'", (json.dumps(meta["index_provenance"]), meta.get("index_status", "pending"), sha))
                if kind == "audio":
                    self.organize_audio(c, sha)
            return (sha, created) if report_created else sha
        finally:
            stage.unlink(missing_ok=True)

    @staticmethod
    def object_path(ident, extension, category=None):
        base = Path("objects")
        if category is not None:
            base = base / "audio" / category
        return base / ident[:2] / (ident + extension)

    def organize_audio(self, connection, ident):
        row = connection.execute(
            f"SELECT relative_path, {AUDIO_CATEGORY_SQL} FROM assets a WHERE id=? AND media_type='audio'", (ident,)
        ).fetchone()
        if not row:
            return False
        original = (self.root / row[0]).resolve()
        target = self.object_path(ident, original.suffix, row[1])
        if row[0] == target.as_posix():
            return False
        if not original.is_relative_to(self.root / "objects") or not original.is_file() or digest(original) != ident:
            raise ValueError("原音频文件无法通过校验，未更改分类位置。")
        destination = self.root / target
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(original, destination)
        except FileExistsError:
            if digest(destination) != ident:
                raise ValueError("音频分类目录有冲突，未覆盖原文件。") from None
        # Keep the old link available for readers and rollback. It shares the same bytes.
        connection.execute("UPDATE assets SET relative_path=? WHERE id=?", (target.as_posix(), ident))
        return True

    def organize_existing_audio(self):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            ids = [row[0] for row in connection.execute("SELECT id FROM assets WHERE media_type='audio'")]
            return sum(self.organize_audio(connection, ident) for ident in ids)

    def get(self, ident):
        with self.connect() as c:
            row = c.execute(
                f"SELECT a.*, CASE WHEN a.media_type='audio' THEN {AUDIO_CATEGORY_SQL} END AS audio_category "
                "FROM assets a WHERE a.id=?", (ident,)
            ).fetchone()
            if not row:
                return None
            result = dict(row)
            result.pop("relative_path")
            result["origins"] = [
                dict(r)
                for r in c.execute(
                    "SELECT * FROM origins WHERE asset_id=? ORDER BY source,source_id", (ident,)
                )
            ]
            for origin in result["origins"]:
                origin.pop("asset_id")
                origin["details"] = json.loads(origin["details"])
                if isinstance(origin["details"], dict) and "style_id" in origin["details"]:
                    origin["style_id"] = origin["details"]["style_id"]
            original_names = [origin["details"].get("original_name") for origin in result["origins"]
                              if isinstance(origin["details"], dict)]
            original_name = next((name for name in original_names if isinstance(name, str) and name), None)
            if original_name:
                result["original_name"] = original_name
            if result["media_type"] != "audio":
                result.pop("audio_category")
            result["tags"] = [
                r[0]
                for r in c.execute(
                    "SELECT value FROM tags WHERE asset_id=? ORDER BY value", (ident,)
                )
            ]
            result["url"] = f"/api/library/{ident}/file"
            result.update(self.dimension_data(c, ident))
            result["thumbnail"] = (
                f"/api/library/{ident}/thumbnail" if result["media_type"] == "image" else None
            )
            return result

    def backfill_audio_durations(self):
        with self.connect() as c:
            pending = c.execute(
                "SELECT id,mime FROM assets WHERE media_type='audio' AND duration IS NULL"
            ).fetchall()
        updated = 0
        for row in pending:
            path = self.file(row["id"])
            duration = audio_duration(path, row["mime"]) if path else None
            if duration is not None:
                with self.connect() as c:
                    updated += c.execute(
                        "UPDATE assets SET duration=? WHERE id=? AND duration IS NULL", (duration, row["id"])
                    ).rowcount
        return {"scanned": len(pending), "updated": updated}

    def file(self, ident):
        with self.connect() as c:
            row = c.execute("SELECT relative_path FROM assets WHERE id=?", (ident,)).fetchone()
        if not row:
            return None
        path = (self.root / row[0]).resolve()
        return path if path.is_relative_to(self.root / "objects") and path.is_file() else None

    def thumbnail(self, ident):
        path = self.file(ident)
        if not path:
            raise ValueError("素材文件不存在。")
        thumbs = self.root / "thumbnails"
        thumbs.mkdir(exist_ok=True)
        target = thumbs / (ident + ".jpg")
        if not target.exists():
            with Image.open(path) as im:
                im.thumbnail((640, 480))
                im = im.convert("RGB")
                temp = thumbs / (uuid.uuid4().hex + ".part")
                try:
                    im.save(temp, format="JPEG", quality=80)
                    os.replace(temp, target)
                finally:
                    temp.unlink(missing_ok=True)
        return target

    def search(self, q="", kind="", tag="", account="", page=1, *, dimensions=None,
               model="", project_id="", created_from="", created_to="", index_status="",
               limit=24, offset=None, account_id="", storage_status="", style_id="",
               audio_category="", duration_min=None, duration_max=None):
        if kind not in {"", "image", "video", "audio"} or page < 1:
            raise ValueError("筛选条件不正确。")
        filters, args = [], []
        if audio_category and audio_category not in AUDIO_CATEGORIES:
            raise ValueError("音频分类不正确。")
        minimum, maximum = duration_bound(duration_min), duration_bound(duration_max)
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ValueError("最短时长不能大于最长时长。")
        if audio_category or minimum is not None or maximum is not None:
            filters.append("a.media_type='audio'")
        if audio_category:
            filters.append(AUDIO_CATEGORY_SQL + "=?")
            args.append(audio_category)
        for operator, boundary in ((">=", minimum), ("<=", maximum)):
            if boundary is not None:
                filters.append(f"a.duration{operator}?")
                args.append(boundary)
        if len(q) > 500 or not 1 <= limit <= 100 or offset is not None and offset < 0:
            raise ValueError("检索长度或分页不正确。")
        for dim, value in (dimensions or {}).items():
            if dim not in SEARCH_LABELS or not isinstance(value, str) or len(value) > 100:
                raise ValueError("标签维度或长度不正确。")
            if value:
                filters.append("EXISTS(SELECT 1 FROM {labels} d WHERE d.asset_id=a.id AND d.dimension=? AND d.value=? COLLATE NOCASE)")
                args += [dim, value]
        # Source constraints belong to the same origin, never mix accounts/projects across duplicates.
        origin_filters, origin_args = [], []
        for field, value in (("model", model), ("project_id", project_id), ("account_id", account_id)):
            if value:
                if len(str(value)) > 200:
                    raise ValueError("来源筛选值过长。")
                origin_filters.append("o." + field + "=?")
                origin_args.append(str(value))
        for operator, value in ((">=", created_from), ("<=", created_to)):
            if value:
                if len(value) > 40:
                    raise ValueError("日期筛选值过长。")
                origin_filters.append("substr(o.created,1,10)" + operator + "substr(?,1,10)")
                origin_args.append(value)
        if account:
            origin_filters.append("(o.account_email=? OR o.source || ':' || o.account_id=?)")
            origin_args += [account, account]
        if style_id:
            if not isinstance(style_id, str) or len(style_id)>256:
                raise ValueError("画风 ID 不正确。")
            origin_filters.append("json_extract(o.details,'$.style_id')=?")
            origin_args.append(style_id)
        if origin_filters:
            filters.append("EXISTS(SELECT 1 FROM origins o WHERE o.asset_id=a.id AND " + " AND ".join(origin_filters) + ")")
            args += origin_args
        if index_status:
            if index_status not in {"pending", "processing", "ready", "failed"}:
                raise ValueError("索引状态不正确。")
            filters.append("EXISTS(SELECT 1 FROM index_jobs j WHERE j.asset_id=a.id AND j.state=?)")
            args.append(index_status)
        if storage_status and storage_status != "ready":
            if storage_status not in {"pending", "downloading", "failed"}:
                raise ValueError("保存状态不正确。")
            filters.append("0")
        if kind:
            filters.append("a.media_type=?")
            args.append(kind)
        if tag:
            filters.append("(EXISTS(SELECT 1 FROM tags t WHERE t.asset_id=a.id AND t.value=?) OR EXISTS(SELECT 1 FROM dimension_tags d WHERE d.asset_id=a.id AND d.source='music_prompt' AND d.value=?))")
            args.extend([tag,tag])
        if q:
            filters.append(
                "(EXISTS(SELECT 1 FROM origins o WHERE o.asset_id=a.id AND (instr(lower(coalesce(o.prompt,'')),lower(?))>0 OR instr(lower(coalesce(o.model,'')),lower(?))>0 OR instr(lower(coalesce(o.project_id,'')),lower(?))>0 OR instr(lower(coalesce(o.account_email,'')),lower(?))>0 OR instr(lower(coalesce(o.media_id,'')),lower(?))>0 OR instr(lower(json_extract(o.details,'$.original_path')),lower(?))>0 OR instr(lower(json_extract(o.details,'$.original_name')),lower(?))>0)) OR EXISTS(SELECT 1 FROM tags t WHERE t.asset_id=a.id AND instr(lower(t.value),lower(?))>0) OR EXISTS(SELECT 1 FROM {labels} d WHERE d.asset_id=a.id AND instr(lower(d.value),lower(?))>0))"
            )
            args.extend([q] * 9)
        where = " WHERE " + " AND ".join(filters) if filters else ""
        with self.connect() as c:
            prefix, labels = self.search_labels(c) if "{labels}" in where else ("", "dimension_tags")
            where = where.format(labels=labels)
            c.execute("BEGIN")
            total = c.execute(prefix + "SELECT count(*) FROM assets a" + where, args).fetchone()[0]
            ids = [
                r[0]
                for r in c.execute(
                    prefix + "SELECT a.id FROM assets a"
                    + where
                    + " ORDER BY a.added DESC,a.id LIMIT ? OFFSET ?",
                    (*args, limit, (page - 1) * limit if offset is None else offset),
                )
            ]
            stats = dict(c.execute("SELECT media_type,count(*) FROM assets GROUP BY media_type"))
            stats["bytes"] = c.execute("SELECT coalesce(sum(size),0) FROM assets").fetchone()[0]
            accounts = [
                r[0]
                for r in c.execute(
                    "SELECT DISTINCT CASE WHEN account_email!='' THEN account_email ELSE source || ':' || account_id END FROM origins ORDER BY 1"
                )
            ]
            tags = [r[0] for r in c.execute("SELECT value FROM tags UNION SELECT value FROM dimension_tags WHERE source='music_prompt' ORDER BY value")]
            pending = c.execute(
                "SELECT count(*) FROM archive_queue WHERE state!='done'"
            ).fetchone()[0]
            errors = c.execute(
                "SELECT count(*) FROM archive_queue WHERE error IS NOT NULL AND state!='done'"
            ).fetchone()[0]
        return {
            "items": [self.get(i) for i in ids],
            "total": total,
            "page": page,
            "pages": max(1, (total + limit - 1) // limit),
            "limit": limit,
            "offset": (page - 1) * limit if offset is None else offset,
            "facets": self.facets(),
            "stats": stats,
            "accounts": accounts,
            "tags": tags,
            "pending": pending,
            "errors": errors,
        }

    def set_tags(self, ident, tags):
        if (
            not isinstance(tags, list)
            or len(tags) > 30
            or any(not isinstance(t, str) or not 0 < len(t.strip()) <= 80 for t in tags)
        ):
            raise ValueError("最多填写 30 个标签，每个最多 80 字。")
        with self.connect() as c:
            if not c.execute("SELECT 1 FROM assets WHERE id=?", (ident,)).fetchone():
                raise ValueError("素材不存在。")
            c.execute("DELETE FROM tags WHERE asset_id=?", (ident,))
            c.executemany(
                "INSERT OR IGNORE INTO tags VALUES(?,?)", [(ident, t.strip()) for t in tags]
            )

    def enqueue_task(self, task, account, output):
        if task["kind"] not in {"image", "video"}:
            return
        result = task.get("result") or {}
        groups = [result] if result.get("files") else result.get("source_jobs")
        if not isinstance(groups, list):
            return
        for group in groups:
            if not isinstance(group, dict):
                continue
            paths, media = group.get("files"), group.get("media_ids")
            if not isinstance(paths, list):
                continue  # Older failed jobs may store files: null.
            media = media if isinstance(media, list) else []
            for index, value in enumerate(paths):
                if not isinstance(value, str) or not value:
                    continue
                path = Path(value).resolve()
                if not path.is_relative_to(Path(output).resolve()):
                    continue
                key = task["id"] + ":" + str(group.get("job_id", "result")) + ":" + str(index)
                meta = {
                    "source": "studiowright",
                    "source_id": key,
                    "account_id": task["profile"],
                    "account_email": (account or {}).get("email"),
                    "project_id": group.get("project_id") or task["project"],
                    "media_id": media[index] if len(paths) == len(media) else "",
                    "request_id": task["id"],
                    "model": task["params"].get("model"),
                    "prompt": task["params"].get("prompt"),
                    "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(task["created"])),
                    "media_type": task["kind"],
                    "details": {
                        "job_id": group.get("job_id"),
                        "stage": group.get("stage"),
                        "file_name": path.name,
                        **({"style_id": task["params"]["style_id"]} if "style_id" in task["params"] else {}),
                    },
                }
                with self.connect() as c:
                    c.execute(
                        "INSERT OR IGNORE INTO archive_queue(key,path,metadata) VALUES(?,?,?)",
                        (key, str(path), json.dumps(meta, ensure_ascii=False)),
                    )

    def drain(self):
        with self.connect() as c:
            rows = [
                dict(r)
                for r in c.execute(
                    "SELECT * FROM archive_queue WHERE state!='done' AND next_try<=? ORDER BY rowid LIMIT 4",
                    (time.time(),),
                )
            ]
        for row in rows:
            try:
                self.archive(row["path"], json.loads(row["metadata"]))
                with self.connect() as c:
                    c.execute(
                        "UPDATE archive_queue SET state='done',error=NULL WHERE key=?",
                        (row["key"],),
                    )
            except Exception:
                # No credential/path disclosure; archival never resubmits generation.
                with self.connect() as c:
                    c.execute(
                        "UPDATE archive_queue SET attempts=attempts+1,next_try=?,error=? WHERE key=?",
                        (
                            time.time() + min(3600, 30 * 2 ** min(row["attempts"], 7)),
                            "本地归档失败，请检查文件完整性和磁盘空间；不会重新生成。",
                            row["key"],
                        ),
                    )

    def reconcile(self, store, output):
        for row in store.rows(
            "SELECT * FROM flow_task_execution_rows WHERE kind IN ('image','video') AND result IS NOT NULL"
        ):
            task = store.decode(row)
            self.enqueue_task(task, store.account(task["profile"]), output)
