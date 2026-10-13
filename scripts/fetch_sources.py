#!/usr/bin/env python3
"""Discover and fetch official TSE ZIPs, preserving originals and resuming partials.

Example: python scripts/fetch_sources.py --years 2024 2026 --workers 3
The manifest is updated after each completed source; failed sources stay explicit.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import csv
from datetime import datetime, timezone
import hashlib
import fcntl
import io
import json
import os
from pathlib import Path
import shutil
import threading
import time
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
API = "https://dadosabertos.tse.jus.br/api/3/action/package_show?id="


class RemoteZip(io.RawIOBase):
    """Seekable official HTTP ZIP using byte ranges; no national extraction."""
    def __init__(self, url):
        self.url, self.pos = url, 0
        self.cache_start, self.cache = 0, b""
        with urllib.request.urlopen(urllib.request.Request(url, headers={"Range": "bytes=0-0"}), timeout=90) as response:
            self.size = int(response.headers["Content-Range"].split("/")[-1])
            self.etag = response.headers.get("ETag")
            self.last_modified = response.headers.get("Last-Modified")
            response.read()

    def seekable(self): return True
    def readable(self): return True
    def tell(self): return self.pos
    def seek(self, offset, whence=0):
        self.pos = offset if whence == 0 else self.pos + offset if whence == 1 else self.size + offset
        return self.pos
    def read(self, count=-1):
        count = min(self.size - self.pos, count if count >= 0 else self.size)
        if count <= 0: return b""
        if self.cache_start <= self.pos and self.pos + count <= self.cache_start + len(self.cache):
            data = self.cache[self.pos - self.cache_start:self.pos - self.cache_start + count]
            self.pos += count
            return data
        request_count = min(self.size - self.pos, max(count, 2**20)) if count >= 4096 else count
        for attempt in range(5):
            try:
                headers = {"Range": f"bytes={self.pos}-{self.pos + request_count - 1}"}
                if self.etag: headers["If-Match"] = self.etag
                request = urllib.request.Request(self.url, headers=headers)
                with urllib.request.urlopen(request, timeout=120) as response:
                    if response.status != 206: raise RuntimeError("Range unsupported")
                    expected = f"bytes {self.pos}-{self.pos + request_count - 1}/{self.size}"
                    if response.headers.get("Content-Range") != expected:
                        raise RuntimeError(f"Incompatible remote ZIP range: {response.headers.get('Content-Range')} versus {expected}")
                    if self.etag and response.headers.get("ETag") != self.etag:
                        raise RuntimeError("Remote archive generation changed during extraction")
                    data = response.read()
                if len(data) != request_count: raise RuntimeError("Truncated remote ZIP range")
                if request_count >= 4096:
                    self.cache_start, self.cache = self.pos, data
                self.pos += count
                return data[:count]
            except Exception:
                if attempt == 4: raise
                time.sleep(1)


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, payload):
    """Atomic records permit readers and independent source collectors to coexist."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".json.tmp.{os.getpid()}")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def discover(year):
    out = []
    for package in (f"resultados-{year}", f"eleitorado-{year}"):
        with urllib.request.urlopen(API + package, timeout=90) as response:
            payload = json.load(response)
        if not payload.get("success"):
            raise RuntimeError(f"CKAN failed: {package}")
        meta = ROOT / "data/manifests/catalogs" / f"{package}.json"
        write_json(meta, payload)
        for resource in payload["result"]["resources"]:
            url = resource.get("url", "")
            name = url.rsplit("/", 1)[-1]
            target = None
            if name == f"votacao_candidato_munzona_{year}.zip":
                target = f"munzona_{year}.zip"
            elif name == f"votacao_secao_{year}_RJ.zip":
                target = f"secao_{year}_RJ.zip"
            elif name == f"votacao_secao_{year}_BR.zip" and year in (2018, 2022, 2026):
                target = f"secao_{year}_BR.zip"
            elif name == f"detalhe_votacao_secao_{year}.zip":
                target = f"detalhe_{year}.zip"
            elif name == f"eleitorado_local_votacao_{year}.zip":
                target = f"locais_{year}.zip"
            elif name == f"votacao_partido_munzona_{year}.zip":
                target = f"partido_{year}.zip"
            if target:
                out.append({"year": year, "path": f"data/raw/{target}", "url": url,
                            "source": f"https://dadosabertos.tse.jus.br/dataset/{package}",
                            "resource_id": resource.get("id"), "resource_name": resource.get("name"),
                            "catalog_modified_at": resource.get("last_modified") or resource.get("metadata_modified"),
                            "discovered_at": utcnow()})
    if len(out) != (6 if year in (2018, 2022, 2026) else 5):
        raise RuntimeError(f"Missing or duplicate resources for {year}: {out}")
    return out


def inspect_zip(path):
    with zipfile.ZipFile(path) as archive:
        bad = archive.testzip()
        if bad:
            raise RuntimeError(f"ZIP CRC failure: {bad}")
        members = [m for m in archive.infolist() if m.filename.lower().endswith(".csv")]
        preferred = next((m for m in members if "_RJ.csv" in m.filename), members[0] if members else None)
        result = {"members": [m.filename for m in archive.infolist()],
                  "uncompressed_bytes": sum(m.file_size for m in archive.infolist())}
        if preferred:
            with archive.open(preferred) as stream:
                reader = csv.reader(io.TextIOWrapper(stream, encoding="latin-1"), delimiter=";")
                header = next(reader)
                first = dict(zip(header, next(reader, [])))
            result.update({"inspected_member": preferred.filename, "header": header,
                           "generated_at": " ".join(first.get(k, "") for k in ("DT_GERACAO", "HH_GERACAO")).strip(),
                           "first_election": {k: first.get(k) for k in ("ANO_ELEICAO", "CD_ELEICAO", "DS_ELEICAO", "DT_ELEICAO", "NR_TURNO", "SG_UF")}})
        return result


def download(url, path):
    """Four independent resumable ranges bypass per-connection CDN throttling."""
    with urllib.request.urlopen(urllib.request.Request(url, headers={"Range": "bytes=0-0"}), timeout=90) as response:
        if response.status != 206:
            return download_single(url, path)
        size = int(response.headers["Content-Range"].split("/")[-1])
        response_headers = dict(response.headers)
        response.read()
    resume = path.with_suffix(".zip.resume.json")
    identity = {"url": url, "bytes": size, "etag": response_headers.get("ETag")}
    if resume.exists() and json.loads(resume.read_text()) != identity:
        for obsolete in path.parent.glob(path.name + ".segment*"): obsolete.unlink()
        if path.with_suffix(".zip.part").exists(): path.with_suffix(".zip.part").unlink()
    resume.write_text(json.dumps(identity))
    segments = 4
    width = (size + segments - 1) // segments
    old = path.with_suffix(".zip.part")
    first = path.with_suffix(".zip.segment0")
    if old.exists() and not first.exists() and old.stat().st_size <= width:
        old.replace(first)
    progress_lock = threading.Lock()
    milestones = set()
    def report_progress():
        with progress_lock:
            completed = sum(p.stat().st_size for p in path.parent.glob(path.name + ".segment*"))
            milestone = int(100 * completed / size) // 20 * 20
            if 0 < milestone < 100 and milestone not in milestones:
                milestones.add(milestone)
                print(f"progress {path.name}: {milestone}% ({completed:,}/{size:,} bytes)", flush=True)
    def fetch_segment(index):
        start, end = index * width, min((index + 1) * width, size) - 1
        part = path.with_suffix(f".zip.segment{index}")
        for attempt in range(5):
            offset = part.stat().st_size if part.exists() else 0
            if offset == end - start + 1: return part
            try:
                headers = {"Range": f"bytes={start + offset}-{end}"}
                if identity["etag"]: headers["If-Match"] = identity["etag"]
                request = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(request, timeout=120) as response:
                    if response.status != 206 or response.headers["Content-Range"] != f"bytes {start + offset}-{end}/{size}":
                        raise RuntimeError("Incompatible segment range")
                    if identity["etag"] and response.headers.get("ETag") != identity["etag"]:
                        raise RuntimeError("Remote archive generation changed during download")
                    with part.open("ab") as output:
                        while chunk := response.read(2**20):
                            output.write(chunk)
                            report_progress()
                if part.stat().st_size != end - start + 1: raise RuntimeError("Truncated segment")
                return part
            except Exception:
                if attempt == 4: raise
                time.sleep(min(2**attempt, 16))
    print(f"fetch {path.name}: 4 resumable ranges, {size:,} bytes", flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=segments) as executor:
        parts = list(executor.map(fetch_segment, range(segments)))
    with old.open("wb") as output:
        for part in parts:
            with part.open("rb") as source: shutil.copyfileobj(source, output, 8 * 2**20)
    old.replace(path)
    for part in parts: part.unlink()
    resume.unlink()
    return response_headers


def download_single(url, path):
    partial = path.with_suffix(".zip.part")
    # Range support is negotiated on each retry; ignored Range responses restart safely.
    for attempt in range(5):
        offset = partial.stat().st_size if partial.exists() else 0
        headers = {"User-Agent": "analise-eleitoral-rj/1.0"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120) as response:
                append = offset > 0 and response.status == 206
                if append and not response.headers.get("Content-Range", "").startswith(f"bytes {offset}-"):
                    raise RuntimeError("Server returned incompatible range")
                print(f"fetch {path.name}: {'resume ' + str(offset) if append else 'start'}", flush=True)
                with partial.open("ab" if append else "wb") as output:
                    while chunk := response.read(2**20):
                        output.write(chunk)
            partial.replace(path)
            return dict(response.headers)
        except Exception:
            if attempt == 4:
                raise
            time.sleep(min(2**attempt, 16))


def fetch(resource, downloads, refresh=False):
    # Priority fetches and the complete update may run concurrently, sharing one file safely.
    path = ROOT / resource["path"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".zip.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return fetch_locked(resource, downloads, refresh)


def fetch_locked(resource, downloads, refresh=False):
    result = dict(resource)
    path = ROOT / resource["path"]
    source_record = ROOT / "data/manifests/files" / (path.name + ".json")
    if source_record.exists():
        result = {**json.loads(source_record.read_text()), **result}
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if not path.exists() and path.name.startswith("munzona_"):
            original_name = resource["url"].rsplit("/", 1)[-1]
            original = downloads / original_name
            if original.is_file() and zipfile.is_zipfile(original):
                # Validate before linking; incomplete browser downloads remain untouched.
                try:
                    inspect_zip(original)
                except Exception as exc:
                    result["invalid_preserved_original"] = {"path": str(original), "error": str(exc)}
                else:
                    path.symlink_to(original)
                    result["preserved_original"] = str(original)
        if refresh and path.exists():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            history = ROOT / "data/raw/history" / path.name / stamp
            history.mkdir(parents=True, exist_ok=True)
            archived = history / path.name
            path.rename(archived)
            historical_record = {**result, "archived_path": str(archived.relative_to(ROOT)), "archived_at": utcnow()}
            record_dir = ROOT / "data/manifests/history" / path.name
            record_dir.mkdir(parents=True, exist_ok=True)
            write_json(record_dir / f"{stamp}.json", historical_record)
            result = dict(resource)
        if path.exists():
            try:
                metadata = inspect_zip(path)
            except Exception:
                if path.is_symlink():
                    path.unlink()
                else:
                    path.rename(path.with_suffix(".zip.invalid"))
                result["replaced_invalid_local_copy"] = True
                metadata = None
        else:
            metadata = None
        if metadata is None:
            # CDN can serve different archive generations for cached byte ranges.
            # A fixed query namespace keeps all ranges on a coherent snapshot.
            retrieval_url = resource["url"] + "?dataset_snapshot=" + datetime.now(timezone.utc).strftime("%Y%m%d")
            resume_identity = path.with_suffix(".zip.resume.json")
            if refresh:
                retrieval_url += "&collection=" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            elif resume_identity.exists():
                previous_url = json.loads(resume_identity.read_text()).get("url", "")
                if previous_url.split("?", 1)[0] == resource["url"]:
                    retrieval_url = previous_url
            result["retrieval_url"] = retrieval_url
            result["collected_at"] = utcnow()
            subset = False
            if path.name.startswith(("locais_", "detalhe_", "munzona_", "partido_")):
                with zipfile.ZipFile(RemoteZip(retrieval_url)) as remote:
                    selected = [m for m in remote.infolist() if m.filename.endswith("_RJ.csv") or
                                (path.name.startswith(("munzona_", "partido_")) and m.filename.endswith("_BR.csv"))]
                    print(f"inspect {path.name}: {len(remote.infolist())} archive members, {len(selected)} selected CSVs", flush=True)
                    if selected:
                        # Preserve the exact decompressed CSV bytes; zip-level hash is this derived archive.
                        staging = path.with_suffix(".zip.subset.part")
                        member_ranges = []
                        with zipfile.ZipFile(staging, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as output:
                            for member in selected:
                                with remote.open(member) as source, output.open(member.filename, "w", force_zip64=True) as target:
                                    shutil.copyfileobj(source, target, 2**20)
                                member_ranges.append({"member": member.filename, "original_crc32": f"{member.CRC:08x}",
                                                      "original_compressed_bytes": member.compress_size,
                                                      "original_header_offset": member.header_offset})
                        staging.replace(path)
                        result.update({"subset": "official_RJ_and_presidential_BR_members" if path.name.startswith(("munzona_", "partido_")) and any(m.filename.endswith("_BR.csv") for m in selected) else "official_RJ_members_only", "original_archive_bytes": remote.fp.size,
                                       "original_members": member_ranges, "http_etag": remote.fp.etag,
                                       "http_last_modified": remote.fp.last_modified})
                        subset = True
            if not subset:
                response_headers = download(retrieval_url, path)
                result["http_last_modified"] = response_headers.get("Last-Modified")
                result["http_etag"] = response_headers.get("ETag")
            metadata = inspect_zip(path)
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(8 * 2**20):
                digest.update(chunk)
        result.update(metadata)
        result.setdefault("collected_at", utcnow())
        result.update({"verified_at": utcnow(), "bytes": path.stat().st_size,
                       "sha256": digest.hexdigest(), "integrity": "zip_crc_verified"})
        result.pop("error", None)
        print(f"ready {path.name}: {result['bytes']:,} bytes, CRC verified", flush=True)
    except Exception as exc:
        result.update({"collected_at": utcnow(), "integrity": "failed", "error": str(exc)})
        print(f"failed {path.name}: {exc}", flush=True)
    write_json(source_record, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--years", nargs="+", type=int, default=[2024, 2026, 2018, 2020, 2022])
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--downloads", type=Path, default=Path.home() / "Downloads")
    parser.add_argument("--refresh", action="store_true", help="Archive existing sources and fetch a new official snapshot")
    parser.add_argument("--kinds", nargs="+", choices=["munzona", "secao", "detalhe", "locais", "partido"])
    args = parser.parse_args()
    manifest_path = ROOT / "data/manifests/sources.json"
    old = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    records = {r["path"]: r for r in old.get("sources", [])}
    jobs = []
    for year in args.years:
        jobs.extend(r for r in discover(year) if not args.kinds or Path(r["path"]).name.split("_")[0] in args.kinds)
    # Both small geographic sources and section votes become available early.
    rank = {"locais": 0, "secao": 1, "munzona": 2, "partido": 2, "detalhe": 3}
    jobs.sort(key=lambda r: (rank[Path(r["path"]).name.split("_")[0]] == 3, args.years.index(r["year"]), rank[Path(r["path"]).name.split("_")[0]], "_BR" in r["path"]))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(args.workers, 6))) as executor:
        futures = [executor.submit(fetch, job, args.downloads, args.refresh) for job in jobs]
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            for saved in (ROOT / "data/manifests/files").glob("*.json"):
                other = json.loads(saved.read_text())
                records[other["path"]] = other
            records[result["path"]] = result
            payload = {"updated_at": utcnow(), "sources": sorted(records.values(), key=lambda r: r["path"])}
            write_json(manifest_path, payload)
    return int(any(r["integrity"] == "failed" for r in records.values()))


if __name__ == "__main__":
    raise SystemExit(main())
