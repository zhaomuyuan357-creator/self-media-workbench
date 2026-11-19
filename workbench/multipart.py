"""Bounded-memory parser for the workbench's single-file multipart uploads."""

import re
import tempfile
from email import message_from_bytes


CHUNK_SIZE = 64 * 1024
MAX_HEADER_BYTES = 64 * 1024
MAX_MULTIPART_OVERHEAD = 256 * 1024


class PayloadTooLargeError(ValueError):
    """The request or an uploaded file exceeded its configured limit."""


class UploadedPart:
    def __init__(self, field_name, filename, content_type, file_obj):
        self.field_name = field_name
        self.filename = filename
        self.content_type = content_type
        self.file = file_obj

    def close(self):
        self.file.close()


class _LimitedReader:
    def __init__(self, source, length):
        self.source = source
        self.remaining = length
        self.buffer = bytearray()

    def read(self, size):
        while len(self.buffer) < size and self.remaining:
            chunk = self.source.read(min(CHUNK_SIZE, self.remaining, size - len(self.buffer)))
            if not chunk:
                raise ValueError("上传请求提前结束")
            self.remaining -= len(chunk)
            self.buffer.extend(chunk)
        value = bytes(self.buffer[:size])
        del self.buffer[:size]
        return value

    def read_until(self, marker, max_bytes):
        collected = bytearray()
        while True:
            index = self.buffer.find(marker)
            if index >= 0:
                collected.extend(self.buffer[:index])
                if len(collected) > max_bytes:
                    raise ValueError("multipart 请求头过大")
                del self.buffer[:index + len(marker)]
                return bytes(collected)
            keep = max(0, len(marker) - 1)
            if len(self.buffer) > keep:
                count = len(self.buffer) - keep
                collected.extend(self.buffer[:count])
                del self.buffer[:count]
            if len(collected) > max_bytes:
                raise ValueError("multipart 请求头过大")
            if not self.remaining:
                raise ValueError("multipart 数据格式无效")
            chunk = self.source.read(min(CHUNK_SIZE, self.remaining))
            if not chunk:
                raise ValueError("上传请求提前结束")
            self.remaining -= len(chunk)
            self.buffer.extend(chunk)

    def copy_until(self, marker, output, max_bytes):
        written = 0
        while True:
            index = self.buffer.find(marker)
            if index >= 0:
                suffix_start = index + len(marker)
                while len(self.buffer) < suffix_start + 2 and self.remaining:
                    chunk = self.source.read(min(CHUNK_SIZE, self.remaining))
                    if not chunk:
                        raise ValueError("上传请求提前结束")
                    self.remaining -= len(chunk)
                    self.buffer.extend(chunk)
                suffix = bytes(self.buffer[suffix_start:suffix_start + 2])
                if suffix in {b"--", b"\r\n"}:
                    chunk = self.buffer[:index]
                    written += len(chunk)
                    if written > max_bytes:
                        raise PayloadTooLargeError("上传文件超过大小限制")
                    if output is not None:
                        output.write(chunk)
                    del self.buffer[:suffix_start]
                    return written
                flush_count = index + 1
                chunk = self.buffer[:flush_count]
                written += len(chunk)
                if written > max_bytes:
                    raise PayloadTooLargeError("上传文件超过大小限制")
                if output is not None:
                    output.write(chunk)
                del self.buffer[:flush_count]
                continue

            keep = max(0, len(marker) - 1)
            if len(self.buffer) > keep:
                count = len(self.buffer) - keep
                chunk = self.buffer[:count]
                written += len(chunk)
                if written > max_bytes:
                    raise PayloadTooLargeError("上传文件超过大小限制")
                if output is not None:
                    output.write(chunk)
                del self.buffer[:count]

            if not self.remaining:
                raise ValueError("multipart 文件缺少结束边界")
            chunk = self.source.read(min(CHUNK_SIZE, self.remaining))
            if not chunk:
                raise ValueError("上传请求提前结束")
            self.remaining -= len(chunk)
            self.buffer.extend(chunk)


def _boundary_from_headers(headers):
    content_type = headers.get("Content-Type", "")
    message = message_from_bytes(("Content-Type: " + content_type + "\r\n\r\n").encode("latin-1"))
    if message.get_content_type().lower() != "multipart/form-data":
        raise ValueError("请使用文件上传表单")
    boundary = message.get_param("boundary", header="content-type")
    if not boundary or len(boundary) > 70 or not re.fullmatch(r"[0-9A-Za-z'()+_,./:=? -]+", boundary):
        raise ValueError("multipart boundary 无效")
    return boundary.encode("ascii")


def _part_headers(raw):
    message = message_from_bytes(raw + b"\r\n")
    disposition = message.get("Content-Disposition", "")
    if message.get_content_disposition() != "form-data":
        raise ValueError("multipart 内容格式无效")
    field_name = message.get_param("name", header="content-disposition")
    filename = message.get_filename()
    content_type = message.get_content_type() if message.get("Content-Type") else "application/octet-stream"
    return field_name or "", filename, content_type


def parse_multipart_stream(stream, headers, content_length, max_bytes,
                           max_overhead=MAX_MULTIPART_OVERHEAD):
    """Parse one file field using bounded reads and a disk-backed spool file."""
    if content_length <= 0:
        raise ValueError("没有收到文件")
    if content_length > max_bytes + max_overhead:
        raise PayloadTooLargeError("上传请求超过大小限制")
    boundary = _boundary_from_headers(headers)
    reader = _LimitedReader(stream, content_length)
    opening = b"--" + boundary + b"\r\n"
    if reader.read(len(opening)) != opening:
        raise ValueError("multipart 起始 boundary 无效")

    delimiter = b"\r\n--" + boundary
    parts = []
    try:
        while True:
            raw_headers = reader.read_until(b"\r\n\r\n", MAX_HEADER_BYTES)
            field_name, filename, content_type = _part_headers(raw_headers)
            if not filename:
                reader.copy_until(delimiter, None, min(64 * 1024, max_bytes))
            else:
                if field_name != "file" or parts:
                    raise ValueError("只支持一个名为 file 的上传文件")
                safe_name = filename.replace("\\", "/").rsplit("/", 1)[-1].replace("\x00", "")
                ext = safe_name.rsplit(".", 1)[-1].lower() if "." in safe_name else ""
                if ext not in {"png", "jpg", "jpeg", "webp", "mp4", "mov", "m4v", "webm", "avi"}:
                    raise ValueError("文件类型不支持")
                file_limit = min(max_bytes, 32 * 1024 * 1024) if ext in {"png", "jpg", "jpeg", "webp"} else max_bytes
                spool = tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")
                try:
                    reader.copy_until(delimiter, spool, file_limit)
                    spool.seek(0)
                    parts.append(UploadedPart(field_name, safe_name, content_type, spool))
                except Exception:
                    spool.close()
                    raise

            suffix = reader.read(2)
            if suffix == b"--":
                trailing_length = len(reader.buffer) + reader.remaining
                if trailing_length > 2:
                    raise ValueError("multipart 结束标记后含有无效内容")
                trailing = reader.read(trailing_length)
                if trailing not in {b"", b"\r\n"}:
                    raise ValueError("multipart 结束标记后含有无效内容")
                break
            if suffix != b"\r\n":
                raise ValueError("multipart boundary 后缀无效")
        if len(parts) != 1:
            raise ValueError("请选择一个文件")
        return parts
    except Exception:
        for part in parts:
            part.close()
        raise
