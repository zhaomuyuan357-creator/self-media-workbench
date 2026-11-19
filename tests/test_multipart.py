import io
import unittest

from workbench.multipart import PayloadTooLargeError, parse_multipart_stream


class ChunkOnlyStream:
    def __init__(self, payload):
        self.payload = io.BytesIO(payload)
        self.read_sizes = []

    def read(self, size=-1):
        if size < 0:
            raise AssertionError("multipart parser must not read the complete body")
        self.read_sizes.append(size)
        return self.payload.read(size)


def multipart(filename, content, boundary="upload-boundary"):
    body = (
        "--{}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{}\"\r\n"
        "Content-Type: application/octet-stream\r\n\r\n".format(boundary, filename).encode("ascii")
        + content
        + "\r\n--{}--\r\n".format(boundary).encode("ascii")
    )
    return body, {"Content-Type": "multipart/form-data; boundary={}".format(boundary)}


class MultipartTests(unittest.TestCase):
    def test_streams_file_in_bounded_chunks_and_spools_to_disk(self):
        content = b"video-data" * (1024 * 1024)
        body, headers = multipart("demo.mp4", content)
        stream = ChunkOnlyStream(body)
        parts = parse_multipart_stream(stream, headers, len(body), max_bytes=len(content) + 10)
        try:
            self.assertEqual(parts[0].filename, "demo.mp4")
            self.assertEqual(parts[0].file.read(), content)
            self.assertTrue(all(size <= 64 * 1024 for size in stream.read_sizes))
            self.assertTrue(parts[0].file._rolled)
        finally:
            parts[0].close()

    def test_rejects_oversized_file_and_bad_boundary(self):
        body, headers = multipart("cover.png", b"x" * 100)
        with self.assertRaises(PayloadTooLargeError):
            parse_multipart_stream(io.BytesIO(body), headers, len(body), max_bytes=50)

        with self.assertRaisesRegex(ValueError, "boundary"):
            parse_multipart_stream(io.BytesIO(body), {"Content-Type": "multipart/form-data"}, len(body), 1000)

    def test_rejects_multiple_file_fields(self):
        boundary = "multi"
        body = (
            b"--multi\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.mp4\"\r\n\r\na"
            b"\r\n--multi\r\nContent-Disposition: form-data; name=\"file\"; filename=\"b.mp4\"\r\n\r\nb"
            b"\r\n--multi--\r\n"
        )
        with self.assertRaisesRegex(ValueError, "一个"):
            parse_multipart_stream(
                io.BytesIO(body), {"Content-Type": "multipart/form-data; boundary=" + boundary}, len(body), 1024,
            )

    def test_boundary_prefix_inside_file_is_not_treated_as_a_delimiter(self):
        content = b"start\r\n--upload-boundaryXdata end"
        body, headers = multipart("demo.mp4", content)
        parts = parse_multipart_stream(io.BytesIO(body), headers, len(body), max_bytes=len(content) + 10)
        try:
            self.assertEqual(parts[0].file.read(), content)
        finally:
            parts[0].close()


if __name__ == "__main__":
    unittest.main()
