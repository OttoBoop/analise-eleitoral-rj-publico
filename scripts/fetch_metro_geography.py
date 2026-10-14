#!/usr/bin/env python3
"""Prepare the fixed metropolitan bairro reference without inferring boundaries.

Run .venv/bin/python scripts/fetch_metro_geography.py [--refresh]. Original
downloads are cached; a refresh archives the previous bytes before replacing.
Canonical geometry is EPSG:4326. Display simplification never assigns votes.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import shutil
import zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode, urljoin
from zoneinfo import ZoneInfo

import requests
import pandas as pd
import shapefile
from pyproj import Transformer
from shapely import STRtree
from shapely.geometry import Point, mapping, shape
from shapely.ops import transform, unary_union
from shapely.validation import explain_validity

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / 'data/raw/geography'
REF = ROOT / 'data/reference'
NOW = lambda: datetime.now(ZoneInfo('America/Sao_Paulo')).isoformat(timespec='seconds')
IBGE = 'https://geoftp.ibge.gov.br/organizacao_do_territorio/malhas_territoriais/malhas_de_setores_censitarios__divisoes_intramunicipais/censo_2022/bairros/shp/UF/RJ_bairros_CD2022.zip'
MPRJ = 'https://geo.mprj.mp.br/arcgis/rest/services/Bairros_ERJ/FeatureServer/8'
SINFOR = 'https://sinfor.marica.rj.gov.br/server/rest/services/Limites_Administrativos/FeatureServer/0'
SINFOR_FIXED_MOMENT = 1777904313000
SINFOR_SNAPSHOT_ID = 'sinfor_marica_historico20260504'
RIOBONITO = 'https://riobonito.rj.gov.br/wp-content/mapa/index.html'
RIOBONITO_NEWS = 'https://riobonito.rj.gov.br/bairros-de-rio-bonito-ganha-novos-codigos-de-enderecamento-postal/'
PETROPOLIS = ('https://sig.petropolis.rj.gov.br/sig398/index.php/lizmap/service?'
              + urlencode({'repository': 'publico', 'project': 'acesso_abairramento_2025',
                           'SERVICE': 'WFS', 'VERSION': '1.0.0', 'REQUEST': 'GetFeature',
                           'TYPENAME': 'abairramento_proposta_2026', 'OUTPUTFORMAT': 'GeoJSON',
                           'SRSNAME': 'EPSG:4326'}))
PETROPOLIS_LEGAL = 'Proposta municipal de abairramento 2026; aprovação legal não comprovada. Referência cartográfica fixa, sem afirmar vigência histórica.'
QUEIMADOS_LAW = 'https://www.queimados.rj.leg.br/leis/legislacao-municipal/leis-complementares/lc-100-23-plc-128-23-msg-021-23-lei-bairros-altera-arts-2o-3o-e-7o-e-anexo-i-da-lc-068-14-inclui-os-anexos-v-e-vi-doq-120.pdf'
MPRJ_MUNICIPIOS = ('3301850', '3302007', '3302502', '3304144', '3305554')
MUNICIPAL_ONLY = {'3303609': 'Paracambi: sem malha poligonal válida de bairros disponível; usar locais efetivos e bairro informado pelo TSE, sem estimar limites.'}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.part')
    temporary.write_text(json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n', encoding='utf8')
    temporary.replace(path)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fetch(name, url, refresh=False):
    path = RAW / name
    if path.exists() and not refresh:
        return path
    response = requests.get(url, timeout=120, headers={'User-Agent': 'Mozilla/5.0 electoral-RJ-geography'})
    response.raise_for_status()
    if name.endswith(('.json', '.geojson')):
        response.json()
    if name.endswith('.zip'):
        with zipfile.ZipFile(io.BytesIO(response.content)) as z:
            if z.testzip():
                raise ValueError(f'ZIP CRC failure: {name}')
    if path.exists() and path.read_bytes() != response.content:
        archive = RAW / 'historical' / (path.stem + '-' + digest(path)[:12] + path.suffix)
        archive.parent.mkdir(exist_ok=True)
        if not archive.exists():
            shutil.copyfile(path, archive)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.part')
    temporary.write_bytes(response.content)
    temporary.replace(path)
    return path


def parse_geojson_variable(source):
    """Parse qgis2web's single JSON variable; JavaScript is never executed."""
    match = re.fullmatch(r'\s*var\s+json_[A-Za-z0-9_]+\s*=\s*(\{.*\})\s*;?\s*', source, flags=re.DOTALL)
    if not match:
        raise ValueError('Expected a single GeoJSON JSON variable')
    payload = json.loads(match.group(1))
    if payload.get('type') != 'FeatureCollection':
        raise ValueError('Not a GeoJSON FeatureCollection')
    return payload


def properties(municipio, source, code, name, extra=None):
    return {'id': f"{municipio['id']}:{source}:{code}", 'nome': str(name).strip(),
            'municipio': municipio['tse'], 'municipio_ibge': municipio['id'],
            'municipio_nome': municipio['nome'], 'tipo': 'bairro', 'source': source,
            **(extra or {})}


def checked_feature(props, geometry, review):
    reason = None
    if not props['nome']:
        reason = 'sem_nome_bairro_residuo_nao_classificado'
    elif not geometry:
        reason = 'geometria_ausente'
    else:
        geom = shape(geometry)
        if geom.is_empty:
            reason = 'geometria_vazia'
        elif geom.geom_type not in ('Polygon', 'MultiPolygon'):
            reason = 'geometria_nao_poligonal'
        elif not geom.is_valid:
            reason = 'geometria_invalida: ' + explain_validity(geom)
    if reason:
        review.append({**props, 'situacao': 'quarentena', 'motivo': reason})
        return None
    return {'type': 'Feature', 'id': props['id'], 'properties': props, 'geometry': geometry}


def petropolis_features(payload, municipio, review):
    """Read the named municipal proposal without inventing codes or legal status.

    The WFS identifier zero is a valid bairro code. A changed feature count or
    repeated/missing identifier requires an explicit new reference review.
    """
    if payload.get('type') != 'FeatureCollection' or len(payload.get('features', [])) != 45:
        raise ValueError('Petrópolis proposal snapshot must contain exactly 45 features')
    codes, result = set(), []
    for feature in payload['features']:
        p = feature.get('properties', {})
        code = p.get('id_0')
        if code is None or str(code).strip() == '' or str(code) in codes:
            raise ValueError('Missing or repeated Petrópolis bairro identifier')
        code = str(code)
        codes.add(code)
        candidate = checked_feature(properties(municipio, 'pmp_proposta2026', code,
                                    p.get('nome') or p.get('bairro') or '',
                                    {'reference_status': 'proposta_municipal_2026',
                                     'legal_validity': PETROPOLIS_LEGAL}),
                                    feature.get('geometry'), review)
        if candidate:
            result.append(candidate)
    return result


def audit_overlaps(features):
    """Keep valid source polygons; flag overlap area instead of editing edges.

    A location covered by more than one polygon must remain ambiguous downstream.
    No tolerance is used to silently turn an overlap into a match.
    """
    rows = []
    for municipio in sorted({f['properties']['municipio_ibge'] for f in features}):
        members = [f for f in features if f['properties']['municipio_ibge'] == municipio]
        geometries = [shape(f['geometry']) for f in members]
        tree = STRtree(geometries)
        for i, geom in enumerate(geometries):
            for j in tree.query(geom, predicate='intersects'):
                j = int(j)
                if j <= i:
                    continue
                area = geom.intersection(geometries[j]).area
                if area > 0:
                    rows.append({'municipio_ibge': municipio, 'bairro_a': members[i]['id'],
                                 'bairro_b': members[j]['id'], 'area_graus2': area,
                                 'situacao': 'sobreposicao_revisar_pontos_sem_atribuicao_automatica'})
    return rows


def audit_municipality_locations(features, municipality, reference, years=(2018, 2020, 2022, 2024, 2026)):
    """Verify a fixed reference against original TSE ZIP rows, not treated joins.

    This proves spatial coverage of the archived election inventory. It does not
    certify a school's address, historical legal boundaries, or that a future
    second-turn inventory has actually been used in an election.
    """
    municipio_ibge, municipio_tse = municipality['properties']['id'], municipality['properties']['tse']
    members = [f for f in features if f['properties']['municipio_ibge'] == municipio_ibge]
    geometries = [shape(f['geometry']) for f in members]
    tree = STRtree(geometries)
    boundary = shape(municipality['geometry'])
    reports = []
    columns = ['SG_UF', 'CD_MUNICIPIO', 'DT_ELEICAO', 'NR_TURNO', 'NR_ZONA', 'NR_SECAO',
               'NR_LOCAL_VOTACAO', 'NM_LOCAL_VOTACAO', 'DS_ENDERECO', 'NR_LATITUDE', 'NR_LONGITUDE']
    for year in years:
        path = ROOT / f'data/raw/locais_{year}.zip'
        records = []
        with zipfile.ZipFile(path) as archive:
            csv_members = [n for n in archive.namelist() if n.lower().endswith('.csv')]
            for name in csv_members:
                with archive.open(name) as stream:
                    for chunk in pd.read_csv(stream, sep=';', encoding='latin-1', dtype=str,
                                             keep_default_na=False, usecols=columns, chunksize=100_000):
                        records.extend(chunk.loc[chunk.SG_UF.eq('RJ') & chunk.CD_MUNICIPIO.eq(municipio_tse)].to_dict('records'))
        outcomes, local_records = Counter(), {}
        sections = set()
        for row in records:
            key = (row['DT_ELEICAO'], row['NR_TURNO'], row['NR_ZONA'], row['NR_LOCAL_VOTACAO'])
            section = (*key[:3], row['NR_SECAO'])
            sections.add(section)
            status, matches = 'coordenada_invalida', []
            try:
                lat, lon = float(row['NR_LATITUDE'].replace(',', '.')), float(row['NR_LONGITUDE'].replace(',', '.'))
                point = Point(lon, lat)
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    status = 'coordenada_invalida'
                elif not boundary.covers(point):
                    status = 'fora_municipio'
                else:
                    matches = [int(i) for i in tree.query(point) if geometries[int(i)].covers(point)]
                    status = 'coordenada_coberta_por_um_bairro' if len(matches) == 1 else 'bairro_ambiguo' if matches else 'fora_bairros'
            except (TypeError, ValueError):
                lat, lon = None, None
            outcomes[status] += 1
            detail = {'data': key[0], 'turno': key[1], 'zona': key[2], 'local': key[3],
                      'nome': row['NM_LOCAL_VOTACAO'], 'endereco': row['DS_ENDERECO'], 'lat': lat, 'lon': lon,
                      'situacao': status, 'bairros_candidatos': [members[i]['id'] for i in matches]}
            point_key = (*key, row['NR_LATITUDE'], row['NR_LONGITUDE'])
            local_records[point_key] = detail
        distinct_keys = {key[:4] for key in local_records}
        key_counts = Counter(key[:4] for key in local_records)
        reports.append({'ano': year, 'arquivo_original': str(path.relative_to(ROOT)), 'sha256': digest(path),
                        'linhas_secao': len(records), 'chaves_secao_distintas': len(sections),
                        'locais_por_data_turno': len(distinct_keys),
                        'locais_por_data_turno_com_coordenadas_divergentes': sum(v > 1 for v in key_counts.values()),
                        'situacoes_linhas': dict(outcomes), 'locais': list(local_records.values())})
    return {'referencia': reference, 'municipio_ibge': municipio_ibge, 'municipio': municipio_tse,
            'metodo': 'Leitura independente dos ZIPs TSE originais, cobertura estrita do município e de exatamente um polígono; sem tolerância, ajustes ou estimativas.',
            'limites': 'Cobertura espacial do cadastro; não comprova endereço físico, vigência legal histórica ou uso de locais cadastrados para turnos futuros.',
            'anos': reports}


def audit_petropolis_locations(features, municipality, years=(2018, 2020, 2022, 2024, 2026)):
    return audit_municipality_locations(features, municipality, 'pmp_proposta2026', years)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--refresh', action='store_true')
    parser.add_argument('--audit-petropolis', action='store_true', help='Conferir os locais diretamente nos ZIPs TSE dos cinco anos')
    parser.add_argument('--audit-marica', action='store_true', help='Conferir a recuperação histórica SINFOR nos ZIPs TSE dos cinco anos')
    args = parser.parse_args()
    RAW.mkdir(parents=True, exist_ok=True)
    manifest_path = REF / 'metro_geography_sources.json'
    previous = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    if args.refresh and manifest_path.exists():
        history = RAW / 'historical'
        history.mkdir(exist_ok=True)
        archived_manifest = history / ('metro_geography_sources-' + digest(manifest_path)[:12] + '.json')
        if not archived_manifest.exists():
            shutil.copyfile(manifest_path, archived_manifest)
    prior = {s['id']: s for s in previous.get('sources', [])}
    sources, reviews, features = [], [], []
    municipal_features = json.loads((REF / 'municipios.geojson').read_text())['features']
    municipios = {f['properties']['id']: f['properties'] for f in municipal_features if f['properties']['metropolitana']}
    assert len(municipios) == 22

    def source(sid, name, url, generation=None, kind='json', publisher=None, legal=None):
        path = fetch(name, url, args.refresh)
        sha = digest(path)
        old = prior.get(sid, {})
        sources.append({'id': sid, 'file': str(path.relative_to(ROOT)), 'url': url,
                        'generation': generation, 'collected_at': old.get('collected_at') if old.get('sha256') == sha else NOW(),
                        'bytes': path.stat().st_size, 'sha256': sha,
                        'integrity': 'verified_zip_crc' if kind == 'zip' else 'parsed_json' if kind == 'json' else 'retrieved_document',
                        'publisher': publisher, 'legal_validity': legal or 'não confirmada para todas as eleições',
                        'license': 'Dados públicos; atribuir o órgão de origem; licença específica não informada no serviço' if publisher != 'IBGE' else 'IBGE; atribuição'} )
        return path

    # Current IPP snapshot already frozen by the capital fetcher, unchanged.
    capital = json.loads((REF / 'bairros.geojson').read_text())['features']
    assert len(capital) == 167
    capital_source = next(s for s in json.loads((REF / 'geography_sources.json').read_text())['sources'] if s['id'] == 'bairros_ipp')
    sources.append({**capital_source, 'id': 'ipp_capital', 'publisher': 'IPP/Prefeitura do Rio de Janeiro',
                    'legal_validity': 'Referência fixa existente; data legal da versão não informada'})
    for f in capital:
        p = f['properties']
        candidate = checked_feature(properties(municipios['3304557'], 'ipp', p['id'], p['nome'],
                                     {**{k: v for k, v in p.items() if k not in ('id', 'nome')}, 'legacy_id': p['id']}), f['geometry'], reviews)
        if candidate:
            features.append(candidate)

    ibge_path = source('ibge_bairros_2022', 'RJ_bairros_CD2022.zip', IBGE, 'Censo 2022; arquivo publicado em 12/11/2024', 'zip', 'IBGE')
    with zipfile.ZipFile(ibge_path) as z:
        stem = next(n[:-4] for n in z.namelist() if n.endswith('.shp'))
        reader = shapefile.Reader(shp=io.BytesIO(z.read(stem + '.shp')), shx=io.BytesIO(z.read(stem + '.shx')),
                                  dbf=io.BytesIO(z.read(stem + '.dbf')), encoding='utf8')
        transformer = Transformer.from_crs(z.read(stem + '.prj').decode(), 'EPSG:4326', always_xy=True)
        for sr in reader.iterShapeRecords():
            p = sr.record.as_dict()
            if p['CD_MUN'] not in municipios or p['CD_MUN'] == '3304557' or p['CD_MUN'] in MUNICIPAL_ONLY:
                continue
            geometry = mapping(transform(transformer.transform, shape(sr.shape.__geo_interface__)))
            candidate = checked_feature(properties(municipios[p['CD_MUN']], 'ibge', p['CD_BAIRRO'], p['NM_BAIRRO']), geometry, reviews)
            if candidate:
                features.append(candidate)

    source('mprj_layer_metadata', 'mprj_bairros_layer8_metadata.json', MPRJ + '?f=pjson', publisher='MPRJ')
    for municipio in MPRJ_MUNICIPIOS:
        url = MPRJ + '/query?' + urlencode({'where': f'cd_mun7={municipio}', 'outFields': '*', 'outSR': '4326', 'f': 'geojson'})
        path = source(f'mprj_{municipio}', f'mprj_bairros_{municipio}.geojson', url, publisher='MPRJ')
        payload = json.loads(path.read_text())
        if payload.get('exceededTransferLimit'):
            raise ValueError('Incomplete ArcGIS query: ' + municipio)
        for f in payload['features']:
            p = f['properties']
            if str(p['cd_mun7']) != municipio:
                raise ValueError('Unexpected municipality in MPRJ response')
            candidate = checked_feature(properties(municipios[municipio], 'mprj', str(p['cod_bairro']), p['bairro']), f['geometry'], reviews)
            if candidate:
                features.append(candidate)

    source('queimados_lc100_2023', 'queimados_lc100_2023.pdf', QUEIMADOS_LAW, '30/06/2023', 'pdf', 'Câmara/Prefeitura de Queimados',
           'Lei altera limites de 32 bairros; malha MPRJ não possui certificação de atualização a esta lei')
    reviews.append({'id': '3304144:mprj:versao', 'nome': 'Queimados — malha MPRJ',
                    'municipio': municipios['3304144']['tse'], 'municipio_ibge': '3304144', 'source': 'mprj',
                    'situacao': 'vigencia_legal_nao_confirmada',
                    'motivo': 'LC100/2023 modifica limites. Preservada uma versão MPRJ identificada por hash; não declarada como malha legal atualizada.'})

    source('sinfor_layer_metadata', 'marica_sinfor_metadata.json', SINFOR + '?f=pjson', publisher='Prefeitura de Maricá/SINFOR')
    url = SINFOR + '/query?' + urlencode({'where': '1=1', 'outFields': '*', 'outSR': '4326', 'f': 'geojson'})
    path = source('sinfor_marica', 'marica_sinfor_bairros.geojson', url, publisher='Prefeitura de Maricá/SINFOR')
    current_sinfor = json.loads(path.read_text())
    if current_sinfor.get('exceededTransferLimit'):
        raise ValueError('Incomplete SINFOR response')
    url = SINFOR + '/query?' + urlencode({'where':'1=1', 'outFields':'*', 'outSR':'4326',
                                          'returnGeometry':'true', 'historicMoment':SINFOR_FIXED_MOMENT, 'f':'geojson'})
    path = source(SINFOR_SNAPSHOT_ID, 'marica_sinfor_bairros_historico20260504.geojson', url,
                  'Consulta histórica do serviço: 04/05/2026 14:18:33 UTC (11:18:33 Brasília)',
                  publisher='Prefeitura de Maricá/SINFOR',
                  legal='Snapshot municipal completo imediatamente anterior à remoção da geometria de Jardim Atlântico Central; vigência histórica dos limites não confirmada.')
    payload = json.loads(path.read_text())
    if payload.get('exceededTransferLimit') or len(payload.get('features', [])) != 49:
        raise ValueError('Incomplete fixed historical SINFOR snapshot')
    current_by_code = {f['properties']['cod_bairro']:f for f in current_sinfor['features']}
    for f in current_sinfor['features']:
        if not f.get('geometry'):
            reviews.append({**properties(municipios['3302700'],'sinfor',str(f['properties']['cod_bairro']),f['properties']['bairro']),
                'situacao':'recuperada_por_snapshot_municipal_historico_completo', 'motivo':'geometria_ausente',
                'justificativa':'A geometria ausente do snapshot atual foi recuperada da versão histórica do próprio SINFOR. Todos os bairros de Maricá usam o mesmo instante histórico, sem misturar versões.',
                'source_snapshot_id':SINFOR_SNAPSHOT_ID,'historic_moment':SINFOR_FIXED_MOMENT})
    for f in payload['features']:
        p = f['properties']
        if p['cod_bairro'] not in current_by_code:
            raise ValueError('Unknown bairro code in historical SINFOR snapshot')
        candidate = checked_feature(properties(municipios['3302700'], 'sinfor', str(p['cod_bairro']), p['bairro'],
                    {'source_snapshot_id':SINFOR_SNAPSHOT_ID,'historic_moment':SINFOR_FIXED_MOMENT,
                     'reference_timestamp':'2026-05-04T14:18:33Z'}), f['geometry'], reviews)
        if candidate:
            features.append(candidate)

    path = source('petropolis_proposta2026', 'petropolis_abairramento_proposta2026.geojson', PETROPOLIS,
                  'Proposta de abairramento 2026; data de geração do vetor não informada pelo serviço',
                  publisher='Prefeitura de Petrópolis/SIG', legal=PETROPOLIS_LEGAL)
    features.extend(petropolis_features(json.loads(path.read_text()), municipios['3303906'], reviews))
    reviews.append({'id': '3303906:pmp_proposta2026:versao', 'nome': 'Petrópolis — proposta municipal 2026',
                    'municipio': municipios['3303906']['tse'], 'municipio_ibge': '3303906', 'source': 'pmp_proposta2026',
                    'situacao': 'proposta_municipal_vigencia_legal_nao_confirmada', 'motivo': PETROPOLIS_LEGAL})

    path = source('riobonito_index', 'riobonito_mapa.html', RIOBONITO, kind='html', publisher='Prefeitura de Rio Bonito')
    source('riobonito_noticia_2026', 'riobonito_bairros_2026.html', RIOBONITO_NEWS, 'Notícia municipal 2026; data de geração cartográfica não informada', 'html', 'Prefeitura de Rio Bonito')
    scripts = re.findall(r'(?:src|href)=[\"\'](data/[^\"\']+\.js)', path.read_text())
    scripts = [p for p in scripts if not p.startswith('data/municipios_')]
    if not scripts:
        raise ValueError('Rio Bonito map no longer provides bairro data scripts')
    # Fetch independent assets concurrently, then record metadata in source order.
    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(lambda p: fetch('riobonito/' + p, urljoin(RIOBONITO, p), args.refresh), scripts))
    for relative in scripts:
        # Already fetched above; source() must not refresh the same asset twice.
        saved_refresh = args.refresh
        args.refresh = False
        try:
            asset = source('riobonito_' + Path(relative).stem, 'riobonito/' + relative,
                           urljoin(RIOBONITO, relative), kind='js', publisher='Prefeitura de Rio Bonito')
        finally:
            args.refresh = saved_refresh
        payload = parse_geojson_variable(asset.read_text())
        for index, f in enumerate(payload['features']):
            code = Path(relative).stem + (f':{index}' if len(payload['features']) > 1 else '')
            candidate = checked_feature(properties(municipios['3304300'], 'riobonito', code, f['properties']['Name']), f['geometry'], reviews)
            if candidate:
                features.append(candidate)

    assert len({f['id'] for f in features}) == len(features)
    features.sort(key=lambda f: f['id'])
    overlaps = audit_overlaps(features)
    output = {'type': 'FeatureCollection', 'name': 'Bairros metropolitanos — referência fixa por fonte',
              'crs': {'type': 'name', 'properties': {'name': 'urn:ogc:def:crs:OGC:1.3:CRS84'}}, 'features': features}
    write_json(REF / 'bairros-metro.geojson', output)
    display = {**output, 'features': [{**f, 'geometry': mapping(shape(f['geometry']).simplify(0.00008, preserve_topology=True))} for f in features],
               'cartography': {'display_only': True, 'simplification_tolerance_degrees': 0.00008,
                               'canonical': 'data/reference/bairros-metro.geojson'}}
    write_json(REF / 'display/bairros-metro.geojson', display)
    write_json(REF / 'metro_geography_review.json', {'features': reviews, 'overlaps': overlaps,
               'policy': 'Não reparar geometrias, estimar limites nem atribuir automaticamente pontos cobertos por múltiplos bairros.'})
    if args.audit_petropolis:
        municipality = next(f for f in municipal_features if f['properties']['id'] == '3303906')
        audit = audit_petropolis_locations(features, municipality)
        write_json(REF / 'petropolis_local_coverage.json', audit)
    if args.audit_marica:
        municipality = next(f for f in municipal_features if f['properties']['id'] == '3302700')
        audit = audit_municipality_locations(features, municipality, SINFOR_SNAPSHOT_ID)
        write_json(REF / 'marica_local_coverage.json', audit)
    rows = []
    for code, municipio in sorted(municipios.items()):
        members = [f for f in features if f['properties']['municipio_ibge'] == code]
        rejected = [r for r in reviews if r.get('municipio_ibge') == code and r['situacao'] == 'quarentena']
        boundary = shape(next(f['geometry'] for f in municipal_features if f['id'] == code))
        union = unary_union([shape(f['geometry']) for f in members]) if members else None
        rows.append({'municipio_ibge': code, 'municipio': municipio['tse'], 'nome': municipio['nome'],
                     'bairros': len(members), 'sources': sorted({f['properties']['source'] for f in members}),
                     'quarentena': len(rejected), 'situacao': 'somente_municipio' if code in MUNICIPAL_ONLY else 'malha_parcial_ou_vigencia_nao_confirmada',
                     'area_municipal_coberta_fracao': union.intersection(boundary).area / boundary.area if union is not None else None,
                     'motivo': PETROPOLIS_LEGAL if code == '3303906' else MUNICIPAL_ONLY.get(code, 'Cobertura eleitoral será calculada pelos locais efetivamente atribuídos; área coberta não é cobertura de votos.'),
                     'fallback': 'locais_efetivos_e_bairro_informado_tse' if code == '3303609' else None})
    manifest = {'version': 'metropolitana-v3', 'collected_at': NOW(), 'territorial_reference': 'IPP snapshot existente / IBGE bairros2022 / MPRJ snapshot / SINFOR snapshot municipal completo de 04/05/2026 14:18:33 UTC / Prefeitura de Rio Bonito snapshot2026 / proposta municipal de Petrópolis2026 (aprovação legal não comprovada)',
                'sources': sources, 'municipios': rows, 'audit': {'municipios_escopo': 22, 'municipios_com_bairros': len({f['properties']['municipio_ibge'] for f in features}),
                'bairros_validos': len(features), 'source_counts': dict(Counter(f['properties']['source'] for f in features)),
                'quarentena': sum(r['situacao'] == 'quarentena' for r in reviews), 'overlap_pairs': len(overlaps),
                'municipios_somente_municipal': sorted(MUNICIPAL_ONLY)},
                'products': [{'file': 'data/reference/' + name, 'bytes': (REF / name).stat().st_size, 'sha256': digest(REF / name)} for name in ['bairros-metro.geojson', 'display/bairros-metro.geojson', 'metro_geography_review.json', 'petropolis_local_coverage.json', 'marica_local_coverage.json'] if (REF / name).exists()]}
    write_json(manifest_path, manifest)
    with (REF / 'bairros_metro_classificacao.csv').open('w', encoding='utf8', newline='') as out:
        fields = ['id', 'nome', 'municipio', 'municipio_ibge', 'municipio_nome', 'tipo', 'source', 'legacy_id', 'ra', 'rp', 'ap', 'macro']
        writer = csv.DictWriter(out, fieldnames=fields, extrasaction='ignore')
        writer.writeheader(); writer.writerows(f['properties'] for f in features)
    print(json.dumps(manifest['audit'], ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
