#!/usr/bin/env python3
"""Assemble only public assets; private docs are never copied into dist."""
from pathlib import Path
import hashlib
import json
import shutil
import requests

ROOT = Path(__file__).resolve().parents[1]
VENDORS = {
    "leaflet.js": "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js",
    "leaflet.css": "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css",
    "xlsx.full.min.js": "https://cdn.sheetjs.com/xlsx-0.20.3/package/dist/xlsx.full.min.js",
    "html2canvas.min.js": "https://cdn.jsdelivr.net/npm/html2canvas@1.4.1/dist/html2canvas.min.js",
}


def main():
    dist = ROOT / "web/dist"
    (dist / "vendor").mkdir(parents=True, exist_ok=True)
    manifest = []
    for name, url in VENDORS.items():
        target = dist / "vendor" / name
        if not target.exists():
            response = requests.get(url, timeout=90)
            response.raise_for_status()
            target.write_bytes(response.content)
        manifest.append({"file": name, "url": url, "sha256": hashlib.sha256(target.read_bytes()).hexdigest()})
    for source in (ROOT / "web").iterdir():
        if source.is_file() and source.suffix in {'.html', '.css', '.js', '.mjs'}:
            shutil.copyfile(source, dist / source.name)
    # Build a small directory from the already validated candidate metadata.
    # Voting distributions remain in the existing on-demand candidate shards.
    psd = []
    for path in sorted((dist / 'data/datasets').glob('*.json')):
        data = json.loads(path.read_text())
        meta = data['meta']
        if meta['cargo'] not in {'13', '6', '7'} or meta['turno'] != '1':
            continue
        for candidate in data['candidates']:
            if candidate['party'] != 'PSD':
                continue
            psd.append({
                'dataset': meta['id'], 'candidate': candidate['id'],
                'name': candidate['name'], 'full_name': candidate['full_name'],
                'number': candidate['number'], 'year': meta['year'],
                'cargo': meta['cargo'], 'cargo_name': meta['cargo_name'],
                'ue': meta['ue'], 'ue_name': meta['ue_name'],
                'votes': candidate['votes'],
            })
    (dist / 'data').mkdir(exist_ok=True)
    (dist / 'data/psd-candidates.json').write_text(
        json.dumps({'party': 'PSD', 'candidates': psd}, ensure_ascii=False, separators=(',', ':'))+'\n'
    )
    (ROOT / "data/manifests/vendors.json").write_text(json.dumps(manifest, indent=2))
    manifest_path = ROOT / "web/.openai/hosting.json"
    if manifest_path.exists():
        hosting = json.loads(manifest_path.read_text())
        hosting["static"] = {"directory": "dist"}
        manifest_path.write_text(json.dumps(hosting, indent=2)+"\n")
    private = [p for p in dist.rglob("*") if p.is_file() and any(x in p.parts for x in ["docs", "prompts", "planos", ".git", "raw", "processed"])]
    if private:
        raise RuntimeError(f"Private artifacts found in public output: {private}")
    print(f"Public build: {sum(p.is_file() for p in dist.rglob('*'))} files")


if __name__ == "__main__":
    main()
