"""Spatial source invariants: no inferred boundaries or homonym collisions."""
import importlib.util
import json
from pathlib import Path

import pytest
from shapely.geometry import Point, Polygon, mapping, shape

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
    assert len(covered) == 21
    assert '3303906' in covered
    assert '3303609' not in covered
    assert manifest['audit']['municipios_somente_municipal'] == ['3303609']
    review = read('metro_geography_review.json')['features']
    assert any(r['source'] == 'sinfor' and r['motivo'] == 'geometria_ausente' for r in review)
    bad_rb = [r for r in review if r['source'] == 'riobonito' and r['situacao'] == 'quarentena']
    assert {r['nome'].casefold() for r in bad_rb} == {'nova cidade', 'viçosa'}
    assert any(r['municipio_ibge'] == '3304144' and r['situacao'] == 'vigencia_legal_nao_confirmada' for r in review)


def test_petropolis_uses_explicit_proposal_reference():
    features = [f for f in read('bairros-metro.geojson')['features']
                if f['properties']['municipio_ibge'] == '3303906']
    assert len(features) == 45
    assert all(f['properties']['source'] == 'pmp_proposta2026' for f in features)
    assert all(f['properties']['reference_status'] == 'proposta_municipal_2026' for f in features)
    assert all('aprovação legal não comprovada' in f['properties']['legal_validity'] for f in features)
    assert {f['id'].split(':')[-1] for f in features} == {str(i) for i in range(1, 46)}
    source = next(s for s in read('metro_geography_sources.json')['sources'] if s['id'] == 'petropolis_proposta2026')
    assert 'abairramento_proposta_2026' in source['url']
    assert source['legal_validity'] == geo.PETROPOLIS_LEGAL


def test_petropolis_changed_or_duplicate_identifiers_require_review():
    municipio = {'id': '3303906', 'tse': '58777', 'nome': 'Petrópolis'}
    geometry = mapping(Polygon([(0, 0), (1, 0), (1, 1), (0, 1)]))
    payload = {'type': 'FeatureCollection', 'features': [
        {'type': 'Feature', 'properties': {'id_0': i, 'nome': f'Bairro {i}'}, 'geometry': geometry}
        for i in range(45)]}
    result = geo.petropolis_features(payload, municipio, [])
    assert len(result) == 45
    assert result[0]['id'] == '3303906:pmp_proposta2026:0'
    with pytest.raises(ValueError, match='exactly 45'):
        geo.petropolis_features({**payload, 'features': payload['features'][:-1]}, municipio, [])
    payload['features'][44]['properties']['id_0'] = 0
    with pytest.raises(ValueError, match='repeated'):
        geo.petropolis_features(payload, municipio, [])


def test_paracambi_missing_mesh_records_honest_point_and_reported_bairro_fallback():
    municipio = next(m for m in read('metro_geography_sources.json')['municipios'] if m['municipio_ibge'] == '3303609')
    assert municipio['bairros'] == 0
    assert municipio['fallback'] == 'locais_efetivos_e_bairro_informado_tse'
    assert 'sem estimar limites' in municipio['motivo']


def test_petropolis_original_zip_inventories_have_unambiguous_spatial_coverage():
    audit = read('petropolis_local_coverage.json')
    assert {year['ano'] for year in audit['anos']} == {2018, 2020, 2022, 2024, 2026}
    valid_ids = {f['id'] for f in read('bairros-metro.geojson')['features']
                 if f['properties']['municipio_ibge'] == '3303906'}
    source_manifest = json.loads((ROOT / 'data/manifests/sources.json').read_text())['sources']
    for year in audit['anos']:
        manifest = next(source for source in source_manifest if source['path'] == year['arquivo_original'])
        assert year['sha256'] == manifest['sha256']
        assert year['linhas_secao'] == year['chaves_secao_distintas'] > 0
        assert year['situacoes_linhas'] == {'coordenada_coberta_por_um_bairro': year['linhas_secao']}
        assert year['locais_por_data_turno_com_coordenadas_divergentes'] == 0
        assert len(year['locais']) == year['locais_por_data_turno']
        assert all(len(row['bairros_candidatos']) == 1 and row['bairros_candidatos'][0] in valid_ids
                   for row in year['locais'])


def test_marica_uses_one_complete_official_historical_snapshot():
    features = [f for f in read('bairros-metro.geojson')['features']
                if f['properties']['municipio_ibge'] == '3302700']
    assert len(features) == 49
    assert all(f['properties']['source_snapshot_id'] == geo.SINFOR_SNAPSHOT_ID for f in features)
    assert all(f['properties']['historic_moment'] == geo.SINFOR_FIXED_MOMENT for f in features)
    recovered = next(f for f in features if f['id'] == '3302700:sinfor:47')
    assert shape(recovered['geometry']).covers(Point(-42.9536927, -22.9570068))
    source = next(s for s in read('metro_geography_sources.json')['sources'] if s['id'] == geo.SINFOR_SNAPSHOT_ID)
    assert f'historicMoment={geo.SINFOR_FIXED_MOMENT}' in source['url']
    assert 'Snapshot municipal completo' in source['legal_validity']
    current = ROOT / 'data/raw/geography/marica_sinfor_bairros.geojson'
    if current.exists():
        by = {str(f['properties']['cod_bairro']):f for f in json.loads(current.read_text())['features']}
        assert by['47']['geometry'] is None
        assert all(f['geometry'] == by[f['id'].split(':')[-1]]['geometry']
                   for f in features if f['id'] != '3302700:sinfor:47')


def test_marica_recovery_covers_all_five_original_inventories_without_ambiguity():
    audit = read('marica_local_coverage.json')
    assert {year['ano'] for year in audit['anos']} == {2018,2020,2022,2024,2026}
    sources = json.loads((ROOT / 'data/manifests/sources.json').read_text())['sources']
    for year in audit['anos']:
        manifest = next(s for s in sources if s['path'] == year['arquivo_original'])
        assert year['sha256'] == manifest['sha256']
        assert year['linhas_secao'] == year['chaves_secao_distintas'] > 0
        assert year['situacoes_linhas'] == {'coordenada_coberta_por_um_bairro':year['linhas_secao']}
        assert year['locais_por_data_turno_com_coordenadas_divergentes'] == 0
        if year['ano'] in [2022,2024,2026]:
            recovered = [r for r in year['locais'] if r['local']=='1627' and r['zona']=='55']
            assert recovered
            assert all(r['bairros_candidatos'] == ['3302700:sinfor:47'] for r in recovered)


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
