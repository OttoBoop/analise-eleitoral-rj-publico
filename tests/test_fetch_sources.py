"""Downloader checks against a local HTTP range server, without network fixtures."""
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import zipfile

SPEC = importlib.util.spec_from_file_location("fetch_sources", Path(__file__).resolve().parents[1] / "scripts/fetch_sources.py")
fetch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fetch)


class RangeServer(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        start, end = map(int, self.headers["Range"].removeprefix("bytes=").split("-"))
        data = self.server.payload
        self.send_response(206)
        self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("ETag", '"fixture-1"')
        self.end_headers()
        self.wfile.write(data[start:end + 1])


class FetchSourcesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), RangeServer)
        content = io.BytesIO()
        with zipfile.ZipFile(content, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("votes_RJ.csv", 'ANO_ELEICAO;SG_UF\n2024;RJ\n')
        cls.server.payload = content.getvalue()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/source.zip"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_segmented_download_is_identical_and_removes_segments(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "source.zip"
            fetch.download(self.url, target)
            self.assertEqual(target.read_bytes(), self.server.payload)
            self.assertEqual(list(Path(directory).iterdir()), [target])

    def test_valid_partial_segment_resumes(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "source.zip"
            target.with_suffix(".zip.segment0").write_bytes(self.server.payload[:11])
            fetch.download(self.url, target)
            self.assertEqual(target.read_bytes(), self.server.payload)

    def test_changed_archive_identity_discards_old_segments(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "source.zip"
            target.with_suffix(".zip.resume.json").write_text(json.dumps({"url": self.url, "bytes": 2, "etag": '"old"'}))
            target.with_suffix(".zip.segment0").write_bytes(b"wrong content")
            fetch.download(self.url, target)
            self.assertEqual(target.read_bytes(), self.server.payload)

    def test_remote_zip_seek_and_member_crc(self):
        with zipfile.ZipFile(fetch.RemoteZip(self.url)) as archive:
            self.assertEqual(archive.read("votes_RJ.csv"), b"ANO_ELEICAO;SG_UF\n2024;RJ\n")

    def test_remote_zip_rejects_changed_generation(self):
        remote = fetch.RemoteZip(self.url)
        remote.etag = '"old"'
        with patch.object(fetch.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "generation changed"):
                remote.read(5)

    def test_prefetch_preserves_position_and_serves_following_bytes(self):
        original = self.server.payload
        try:
            self.server.payload = bytes(range(256)) * 256
            remote = fetch.RemoteZip(self.url)
            self.assertEqual(remote.read(5000), self.server.payload[:5000])
            self.assertEqual(remote.tell(), 5000)
            self.assertGreater(len(remote.cache), 5000)
            self.assertEqual(remote.read(5000), self.server.payload[5000:10000])
            self.assertEqual(remote.tell(), 10000)
            remote.seek(100)
            self.assertEqual(remote.read(100), self.server.payload[100:200])
        finally:
            self.server.payload = original

    def test_bad_original_is_preserved_and_replaced_with_official_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            downloads = root / "Downloads"
            downloads.mkdir()
            invalid = bytearray(self.server.payload)
            invalid[43] ^= 1  # compressed member; central directory remains intact
            original = downloads / "source.zip"
            original.write_bytes(invalid)
            resource = {"year": 2024, "path": "data/raw/munzona_2024.zip", "url": self.url, "source": self.url}
            with patch.object(fetch, "ROOT", root):
                result = fetch.fetch(resource, downloads)
            self.assertEqual(result["integrity"], "zip_crc_verified")
            self.assertIn("invalid_preserved_original", result)
            self.assertEqual(original.read_bytes(), bytes(invalid))
            with zipfile.ZipFile(root / resource["path"]) as archive:
                self.assertEqual(archive.read("votes_RJ.csv"), b"ANO_ELEICAO;SG_UF\n2024;RJ\n")

    def test_refresh_archives_previous_zip_and_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            downloads = root / "Downloads"
            downloads.mkdir()
            resource = {"year": 2024, "path": "data/raw/munzona_2024.zip", "url": self.url, "source": self.url}
            with patch.object(fetch, "ROOT", root):
                first = fetch.fetch(resource, downloads)
                previous = (root / resource["path"]).read_bytes()
                second = fetch.fetch(resource, downloads, refresh=True)
            self.assertEqual(second["integrity"], "zip_crc_verified")
            self.assertNotEqual(first["retrieval_url"], second["retrieval_url"])
            archived = [p for p in (root / "data/raw/history").rglob("*.zip") if p.is_file()]
            self.assertEqual(len(archived), 1)
            self.assertEqual(archived[0].read_bytes(), previous)
            metadata = list((root / "data/manifests/history").rglob("*.json"))
            self.assertEqual(json.loads(metadata[0].read_text())["sha256"], first["sha256"])

    def test_party_subset_includes_presidential_br_and_excludes_brasil_duplicates(self):
        original = self.server.payload
        content = io.BytesIO()
        with zipfile.ZipFile(content, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, value in [("votes_RJ.csv", "2026;RJ;7"), ("votes_BR.csv", "2026;RJ;1"), ("votes_BRASIL.csv", "2026;RJ;7")]:
                archive.writestr(name, "ANO_ELEICAO;SG_UF;CD_CARGO\n" + value + "\n")
        try:
            self.server.payload = content.getvalue()
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                downloads = root / "Downloads"
                downloads.mkdir()
                resource = {"year": 2026, "path": "data/raw/partido_2026.zip", "url": self.url, "source": self.url}
                with patch.object(fetch, "ROOT", root):
                    result = fetch.fetch(resource, downloads)
                self.assertEqual(result["integrity"], "zip_crc_verified")
                self.assertEqual(result["subset"], "official_RJ_and_presidential_BR_members")
                with zipfile.ZipFile(root / resource["path"]) as archive:
                    self.assertEqual(set(archive.namelist()), {"votes_RJ.csv", "votes_BR.csv"})
        finally:
            self.server.payload = original


if __name__ == "__main__":
    unittest.main()
