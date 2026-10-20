#!/usr/bin/env python3
"""Per-territory standings derived from the published candidate shards.

Writes data/rankings/<dataset>.json.gz next to the existing shards without
changing them: for each bairro and municipality, the two leading candidates
and the run-length encoded vote distribution, enough to place any candidate.
"""
from pathlib import Path
from collections import defaultdict
import gzip
import json

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / 'web/dist/data'


def standings(totals):
    """totals: {territory: {candidate: votes}} -> compact standings."""
    out = {}
    for territory, by_candidate in totals.items():
        ranked = sorted(((v, c) for c, v in by_candidate.items() if v > 0), key=lambda x: (-x[0], x[1]))
        if not ranked:
            continue
        runs = []
        for votes, _ in ranked:
            if runs and runs[-1][0] == votes:
                runs[-1][1] += 1
            else:
                runs.append([votes, 1])
        out[territory] = {'n': len(ranked), 'top': [[c, v] for v, c in ranked[:2]], 'runs': runs}
    return out


def position(entry, votes):
    """1-based position of a candidate with `votes` and how many share it."""
    above = sum(count for value, count in entry['runs'] if value > votes)
    tied = next((count for value, count in entry['runs'] if value == votes), 0)
    return above + 1, tied


def build_dataset(path):
    data = json.loads(path.read_text())
    meta = data['meta']
    shard_dir = DIST / 'candidates' / meta['id']
    bairro, municipio = defaultdict(lambda: defaultdict(int)), defaultdict(lambda: defaultdict(int))
    if shard_dir.exists():
        for shard in sorted(shard_dir.glob('*.json.gz')):
            for candidate, values in json.loads(gzip.decompress(shard.read_bytes())).items():
                for row in values.get('zones', []):
                    municipio[str(row['municipio'])][candidate] += int(row['votes'])
                for row in values.get('bairros', []) + values.get('bairros_informados', []):
                    if row.get('bairro_id') is not None:
                        bairro[str(row['bairro_id'])][candidate] += int(row['votes'])
    else:
        for row in data.get('zones', []):
            municipio[str(row['municipio'])][row['candidate']] += int(row['votes'])
        for row in data.get('bairros', []) + data.get('bairros_informados', []):
            if row.get('bairro_id') is not None:
                bairro[str(row['bairro_id'])][row['candidate']] += int(row['votes'])
    candidate_total = sum(sum(v.values()) for v in municipio.values())
    assert candidate_total == meta['total'], (meta['id'], candidate_total, meta['total'])
    return {'dataset': meta['id'], 'bairro': standings(bairro), 'municipio': standings(municipio)}


def main():
    target = DIST / 'rankings'
    target.mkdir(exist_ok=True)
    count = 0
    for path in sorted((DIST / 'datasets').glob('*.json')):
        payload = build_dataset(path)
        raw = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()
        (target / f"{payload['dataset']}.json.gz").write_bytes(gzip.compress(raw, compresslevel=9, mtime=0))
        count += 1
    print(f'Rankings: {count} disputas')


if __name__ == '__main__':
    main()
