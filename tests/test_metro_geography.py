"""Spatial source invariants: no inferred boundaries or homonym collisions."""
import importlib.util
import json
from pathlib import Path

import pytest
from shapely.geometry import Polygon, mapping, shape

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('metro_geo', ROOT / 'scripts/fetch_metro_geography.py')
geo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(geo)


def read(name):
    return json.loads((ROOT / 'data/reference' / name).read_text())


def test_bairro_identity_uses_municipality_source_and_code():
    first = geo.properties({'id': '3303302', 'tse': '58653', 'nome': 'Niterói'}, 'ibge', '1', 'Centro')
    second = geo.properties({'id': '3304300', 'tse': '58858', 'nome': 'Rio Bonito'}, 'riobonito', '1', 'Centro')
    assert first['id'] != second['id']
    assert first['id'] == '3303302:ibge:1'
    assert isinstance(first['municipio'], str)


def test_javascript_is_parsed_without_execution():
    assert geo.parse_geojson_variable('var json_centro_1 = {"type":"FeatureCollection","features":[]};')['features'] == []
    with pytest.raises(ValueError):
        geo.parse_geojson_variable('var json_centro_1 = {"type":"FeatureCollection","features":[]}; process.exit();')


def test_bad_geometry_is_quarantined_without_repair():
    review = []
    p = geo.properties({'id': '3304300', 'tse': '58858', 'nome': 'Rio Bonito'}, 'riobonito', '1', 'Nova Cidade')
    bowtie = mapping(Polygon([(0, 0), (1, 1), (1, 0), (0, 1), (0, 0)]))
    assert geo.checked_feature(p, bowtie, review) is None
    assert 'geometria_invalida' in review[0]['motivo']
    assert geo.checked_feature(p, None, review) is None
    assert review[1]['motivo'] == 'geometria_ausente'


def test_metro_canonical_ids_and_capital_retention():
    features = read('bairros-metro.geojson')['features']
    assert len(features) == len({f['id'] for f in features})
    assert all(f['id'] == f['properties']['id'] for f in features)
    assert all(f['id'].split(':')[0] == f['properties']['municipio_ibge'] for f in features)
    assert all(shape(f['geometry']).is_valid for f in features)
    ipp = {f['properties']['legacy_id']: f for f in features if f['properties']['source'] == 'ipp'}
    original = read('bairros.geojson')['features']
    assert len(ipp) == len(original) == 167
    assert all(ipp[f['id']]['geometry'] == f['geometry'] for f in original)
    assert all(ipp[f['id']]['properties']['ap'] == f['properties']['ap'] for f in original)


def test_municipal_only_and_explicit_quarantine():
    manifest = read('metro_geography_sources.json')
    features = read('bairros-metro.geojson')['features']
    covered = {f['properties']['municipio_ibge'] for f in features}
    assert len(manifest['municipios']) == 22
    assert len(covered) == 20
    assert not {'3303906', '3303609'} & covered
    review = read('metro_geography_review.json')['features']
    assert any(r['source'] == 'sinfor' and r['motivo'] == 'geometria_ausente' for r in review)
    bad_rb = [r for r in review if r['source'] == 'riobonito' and r['situacao'] == 'quarentena']
    assert {r['nome'].casefold() for r in bad_rb} == {'nova cidade', 'viçosa'}
    assert any(r['municipio_ibge'] == '3304144' and r['situacao'] == 'vigencia_legal_nao_confirmada' for r in review)


def test_source_versions_and_download_hashes_are_recorded():
    manifest = read('metro_geography_sources.json')
    for source in manifest['sources']:
        assert source['url'].startswith('https://')
        assert source['bytes'] > 0
        assert len(source['sha256']) == 64
        assert source['collected_at']
        raw = ROOT / source['file']
        if raw.exists():
            assert geo.digest(raw) == source['sha256']


def test_overlaps_are_reported_without_editing_source():
    p1 = {'id': '1', 'properties': {'municipio_ibge': '3303302'}, 'geometry': mapping(Polygon([(0, 0), (2, 0), (2, 2), (0, 2)]))}
    p2 = {'id': '2', 'properties': {'municipio_ibge': '3303302'}, 'geometry': mapping(Polygon([(1, 1), (3, 1), (3, 3), (1, 3)]))}
    before = json.dumps([p1, p2], sort_keys=True)
    result = geo.audit_overlaps([p1, p2])
    assert len(result) == 1 and result[0]['area_graus2'] == 1
    assert json.dumps([p1, p2], sort_keys=True) == before
