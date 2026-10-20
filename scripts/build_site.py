#!/usr/bin/env python3
"""Assemble only public assets; private docs are never copied into dist."""
from pathlib import Path
import hashlib
import gzip
import json
import shutil
import requests
import build_rankings
from shapely.geometry import shape, mapping
from shapely.ops import unary_union

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
    for name in ['geographic-evidence.json','zone-geometry-attempts.json']:
        source = ROOT / 'data/manifests' / name
        if source.exists():
            # Keep the same deterministic public representation as the audit.
            (dist/'data'/name).write_text(json.dumps(json.loads(source.read_text()),ensure_ascii=False,
                                                    allow_nan=False,separators=(',',':'))+'\n')
    coverage_reference=ROOT/'data/reference/petropolis_local_coverage.json'
    if coverage_reference.exists():
        shutil.copyfile(coverage_reference,dist/'data/petropolis-local-coverage.json')
    catalog_path = dist/'data/catalog.json'
    if catalog_path.exists():
        catalog = json.loads(catalog_path.read_text())
        for year in catalog['years']:
            inventory = dist/'data'/f"zones-{year['year']}.json"
            if inventory.exists():
                compact = inventory.with_suffix('.json.gz')
                compact.write_bytes(gzip.compress(inventory.read_bytes(),compresslevel=6,mtime=0))
                year['locations_file'] = compact.name
        catalog_path.write_text(json.dumps(catalog,ensure_ascii=False,separators=(',',':'))+'\n')
        inventories = {}
        for path in (dist/'data/datasets').glob('*.json'):
            data=json.loads(path.read_text())
            year=data['meta']['year']
            if year not in inventories:
                inventory=dist/'data'/f'zones-{year}.json'
                records=json.loads(inventory.read_text())['locations'] if inventory.exists() else []
                inventories[year]={r['id'] for r in records if r.get('id') and r.get('lat') is not None and r.get('lon') is not None}
            data['valid_local_ids']=sorted({r['local_id'] for r in data.get('local_denominators',[]) if r['local_id'] in inventories[year]})
            path.write_text(json.dumps(data,ensure_ascii=False,separators=(',',':')))
    (dist / 'data/psd-candidates.json').write_text(
        json.dumps({'party': 'PSD', 'candidates': psd}, ensure_ascii=False, separators=(',', ':'))+'\n'
    )
    municipalities = json.loads((ROOT / 'data/reference/municipios.geojson').read_text())['features']
    groups = {}
    for feature in municipalities:
        prop = feature['properties']
        key = 'Rio de Janeiro (capital)' if prop['tse'] == '60011' else 'Metropolitana (sem a capital)' if prop['regiao'] == 'Metropolitana' else prop['regiao']
        groups.setdefault(key, []).append(feature)
    regions = {'type': 'FeatureCollection', 'classification': 'Regiões CEPERJ com capital separada da Metropolitana',
               'features': [{'type': 'Feature', 'properties': {'id': key, 'nome': key,
                    'municipios': [f['properties']['tse'] for f in items]},
                    'geometry': mapping(unary_union([shape(f['geometry']) for f in items]).simplify(0.00015, preserve_topology=True))}
                    for key, items in sorted(groups.items())]}
    (dist / 'data/regiao-capital-separada.geojson').write_text(json.dumps(regions, ensure_ascii=False, separators=(',', ':'))+'\n')
    # Standings per bairro/município, derived from the shards without changing them.
    build_rankings.main()
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
