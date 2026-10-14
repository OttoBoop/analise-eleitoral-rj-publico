#!/usr/bin/env python3
"""Audit Paes' published neighborhood totals against classified voting sections.

Requires the processed local Parquets; does not download or estimate any votes.
The spatial check verifies the recorded TSE coordinate against the fixed mesh;
it does not independently certify the school's address or the coordinate itself.
"""
import argparse
import gzip
import json
import hashlib
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import duckdb
from shapely.geometry import Point, shape

ROOT = Path(__file__).resolve().parents[1]


def audit(years, merge=False):
    public = ROOT / 'web/dist/data'
    catalog = json.loads((public / 'catalog.json').read_text())
    municipalities = {f['properties']['tse']: f['properties'] for f in json.loads((ROOT / 'data/reference/municipios.geojson').read_text())['features']}
    metro = {m for m, p in municipalities.items() if p['metropolitana']}
    neighborhoods = {f['properties']['id']: f for f in json.loads((ROOT / 'data/reference/bairros-metro.geojson').read_text())['features']}
    geometries = {k: shape(f['geometry']) for k, f in neighborhoods.items()}
    report = {'audited_at': datetime.now(timezone.utc).isoformat(), 'candidate': 'Eduardo Paes',
              'scope': 'Região Metropolitana', 'coordinate_validation_limit': 'Containment of the TSE coordinate in the fixed official polygon; school address and coordinate accuracy are not independently certified.', 'years': []}
    db = duckdb.connect()
    for year in years:
        entries = next(y for y in catalog['years'] if y['year'] == year)['datasets']
        for entry in entries:
            data = json.loads((public / entry['file']).read_text())
            candidate = next((c for c in data['candidates'] if c.get('person_id') == 'paes'), None)
            if not candidate:
                continue
            parquet = ROOT / f'data/processed/votes_sections_geo_{year}.parquet'
            if not parquet.exists():
                raise RuntimeError(f'Local classified sections required: {parquet.name}')
            meta = data['meta']
            shard = json.loads(gzip.decompress((public / candidate['file']).read_bytes()))[candidate['id']]
            totals = Counter()
            for row in shard['zones']:
                if row['municipio'] in metro:
                    totals[row['municipio']] += row['votes']
            assigned = Counter()
            unassigned = Counter()
            by_neighborhood = Counter()
            for row in shard['bairros']:
                if row['bairro_id'] is None:
                    unassigned[row['municipio']] += row['votes']
                else:
                    assigned[row['municipio']] += row['votes']
                    by_neighborhood[row['bairro_id']] += row['votes']
            assert all(assigned[m] + unassigned[m] == votes for m, votes in totals.items())
            predicate = 'candidate=? AND election=? AND cargo=? AND turno=? AND ue=? AND municipio IN (SELECT unnest(?))'
            args = [str(parquet), candidate['id'], meta['election'], meta['cargo'], meta['turno'], meta['ue'], sorted(metro)]
            source = db.execute(f'SELECT municipio,bairro_id,SUM(votes)::BIGINT FROM read_parquet(?) WHERE {predicate} GROUP BY ALL', args).fetchall()
            assert all(by_neighborhood[b] == votes for m, b, votes in source if b is not None)
            assert sum(v for m, b, v in source) == sum(totals.values())
            positions = db.execute(f'SELECT DISTINCT municipio,bairro_id,lat,lon FROM read_parquet(?) WHERE {predicate} AND bairro_id IS NOT NULL', args).fetchall()
            for m, b, lat, lon in positions:
                assert neighborhoods[b]['properties']['municipio'] == m
                assert geometries[b].covers(Point(lon, lat))
            reasons = db.execute(f"SELECT COALESCE(geo_status,'secao_sem_local'),SUM(votes)::BIGINT FROM read_parquet(?) WHERE {predicate} AND bairro_id IS NULL GROUP BY ALL", args).fetchall()
            denominators = {r['bairro_id']: r['denominator'] for r in data['bairro_denominators'] if r['bairro_id'] is not None}
            covered = set(totals)
            empty = [f['properties'] for f in neighborhoods.values() if f['properties']['municipio'] in covered and f['properties']['id'] not in denominators]
            outside = sum(f['properties']['municipio'] not in covered for f in neighborhoods.values())
            report['years'].append({'year': year, 'contest': meta['id'], 'office': meta['cargo_name'],
                'dataset_sha256': hashlib.sha256((public / entry['file']).read_bytes()).hexdigest(),
                'geography_sha256': hashlib.sha256((ROOT / 'data/reference/bairros-metro.geojson').read_bytes()).hexdigest(),
                'metropolitan_votes': sum(totals.values()), 'located_votes': sum(assigned.values()),
                'unlocated_votes': sum(unassigned.values()), 'unlocated_reasons': dict(reasons),
                'public_totals_equal_classified_sections': True, 'municipality_conservation': True,
                'classified_positions_checked': len(positions), 'coordinate_polygon_discrepancies': 0,
                'neighborhoods_outside_contest': outside,
                'neighborhoods_without_assigned_valid_vote_sections': len(empty),
                'neighborhoods_with_actual_zero_candidate_votes': sum(v == 0 and denominators.get(b, 0) > 0 for b, v in by_neighborhood.items()),
                'municipalities': [{'code': m, 'name': municipalities[m]['nome'], 'votes': total,
                    'located': assigned[m], 'unlocated': unassigned[m]} for m, total in sorted(totals.items())],
                'unassigned_neighborhoods': [{'id': p['id'], 'name': p['nome'], 'municipality': p['municipio']} for p in empty]})
    db.close()
    folder = ROOT / 'data/manifests'
    previous = folder / 'paes-neighborhood-audit.json'
    if merge and previous.exists():
        retained = [y for y in json.loads(previous.read_text())['years'] if y['year'] not in years]
        report['years'] = sorted(retained + report['years'], key=lambda y: (y['year'], y['contest']))
    (folder / 'paes-neighborhood-audit.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    (public / 'paes-neighborhood-audit.json').write_text(json.dumps(report, ensure_ascii=False, separators=(',', ':'))+'\n')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--years', nargs='+', type=int, default=[2024, 2026])
    parser.add_argument('--merge', action='store_true', help='Preserve previously audited years not being updated')
    args = parser.parse_args()
    result = audit(args.years, args.merge)
    print(json.dumps([{k: y[k] for k in ['year', 'office', 'metropolitan_votes', 'located_votes', 'unlocated_votes', 'neighborhoods_outside_contest', 'neighborhoods_without_assigned_valid_vote_sections']} for y in result['years']], ensure_ascii=False, indent=2))
