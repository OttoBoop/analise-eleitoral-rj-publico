import gzip
import importlib.util
import json
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / 'web/dist/data'
spec = importlib.util.spec_from_file_location('build_rankings', ROOT / 'scripts/build_rankings.py')
rankings = importlib.util.module_from_spec(spec); spec.loader.exec_module(rankings)
FLAVIO, PEDRO, CONTEST = '190002538024', '190002537991', '2026-6259-7-1-RJ'
BOTAFOGO, COPACABANA = '3304557:ipp:020', '3304557:ipp:022'


def load(name):
    raw = (DIST / name).read_bytes()
    return json.loads(gzip.decompress(raw) if name.endswith('.gz') else raw)


def test_standings_handle_ties_zero_and_leader_gap():
    entry = rankings.standings({'b': {'x': 10, 'y': 10, 'z': 3, 'w': 0}})['b']
    assert entry['n'] == 3 and entry['runs'] == [[10, 2], [3, 1]]
    assert rankings.position(entry, 10) == (1, 2)
    assert rankings.position(entry, 3) == (3, 1)
    assert entry['top'] == [['x', 10], ['y', 10]]


needs_dist = pytest.mark.skipif(not (DIST / f'rankings/{CONTEST}.json.gz').exists(), reason='site não gerado')


@needs_dist
def test_published_standings_conserve_bairro_votes_and_leaders():
    data, ranking = load(f'datasets/{CONTEST}.json'), load(f'rankings/{CONTEST}.json.gz')
    den = {}
    for r in data['bairro_denominators']:
        den[r['bairro_id']] = den.get(r['bairro_id'], 0) + r['denominator']
    for bid, entry in ranking['bairro'].items():
        if bid in den:
            assert sum(v * k for v, k in entry['runs']) == den[bid]
        assert entry['top'][0][1] == entry['runs'][0][0]
        assert entry['n'] == sum(k for _, k in entry['runs'])
    municipal = {r['municipio']: 0 for r in data['denominators']}
    for r in data['denominators']:
        municipal[r['municipio']] += r['denominator']
    for m, entry in ranking['municipio'].items():
        assert sum(v * k for v, k in entry['runs']) == municipal[m]


@needs_dist
@pytest.mark.skipif(not (ROOT / 'data/processed/votes_sections_geo_2026.parquet').exists(), reason='Parquet local ausente')
def test_flavio_positions_match_section_level_votes():
    import duckdb
    ranking = load(f'rankings/{CONTEST}.json.gz')
    for bid in [BOTAFOGO, COPACABANA]:
        rows = duckdb.execute("""SELECT candidate, SUM(votes)::BIGINT v FROM read_parquet(?)
            WHERE cargo='7' AND turno='1' AND verified AND bairro_id=? GROUP BY ALL""",
            [str(ROOT / 'data/processed/votes_sections_geo_2026.parquet'), bid]).fetchall()
        votes = dict(rows)
        for cid in [FLAVIO, PEDRO]:
            expected = 1 + sum(1 for v in votes.values() if v > votes[cid])
            assert rankings.position(ranking['bairro'][bid], votes[cid])[0] == expected
        assert ranking['bairro'][bid]['top'][0][1] == max(votes.values())
