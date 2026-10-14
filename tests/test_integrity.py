import importlib.util
import json
import gzip
from pathlib import Path

import pytest
import pandas as pd
import duckdb
from shapely.geometry import mapping, box
from shapely.strtree import STRtree

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pipeline", ROOT / "scripts/pipeline.py")
pipeline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pipeline)


def test_invalid_votes_are_not_fractionally_distributed():
    assert pipeline.valid_factor(100, 100) == 1
    assert pipeline.valid_factor(100, 0) == 0
    assert pipeline.valid_factor(100, 70) is None


def test_absence_and_zero_have_distinct_participation():
    assert pipeline.participation(0, 100) == 0
    assert pipeline.participation(0, 0) is None
    assert pipeline.participation(52, 100) == 52


def fake_geo(overlap=False):
    geoms = [box(-44, -23, -43, -22)]
    if overlap:
        geoms.append(box(-44, -23, -43, -22))
    return {"municipio_geoms": {"60011": box(-45, -24, -42, -21)},
            "tree": STRtree(geoms), "geom": geoms,
            "bairro_municipios": {"60011"},
            "bairros": {"features": [{"properties": {"id": f"3304557:test:{i}", "municipio": "60011"}, "geometry": mapping(g)} for i,g in enumerate(geoms)]}}


def test_invalid_or_ambiguous_coordinates_are_not_assigned():
    geo = fake_geo()
    assert pipeline.classify_location("#NULO", "-43.5", "60011", geo)[1] == "coordenada_ausente"
    assert pipeline.classify_location("0", "0", "60011", geo)[1] == "coordenada_invalida"
    assert pipeline.classify_location("-22.5", "-50", "60011", geo)[1] == "fora_municipio"
    assert pipeline.classify_location("-22.5", "-43.5", "60011", fake_geo(True))[0] is None
    assert pipeline.classify_location("-22.5", "-43.5", "60011", geo)[:2] == ("3304557:test:0", "coordenada_validada")


def test_neighborhoods_are_attributed_only_inside_their_own_municipality():
    geo = fake_geo()
    geo["municipio_geoms"]["60000"] = geo["municipio_geoms"]["60011"]
    geo["bairro_municipios"].add("60000")
    assert pipeline.classify_location("-22.5", "-43.5", "60000", geo)[:2] == (None, "fora_bairros")
    geo["bairro_municipios"].remove("60000")
    assert pipeline.classify_location("-22.5", "-43.5", "60000", geo)[1] == "sem_malha_bairros"


def test_per_municipality_blocks_preserve_authoritative_votes_without_estimating_places():
    official = [{"candidate": "x", "municipio": "60011", "votes": 100},
                {"candidate": "x", "municipio": "60000", "votes": 80},
                {"candidate": "y", "municipio": "60011", "votes": 0}]
    located = [{"candidate": "x", "municipio": "60011", "bairro_id": "3304557:ipp:001", "votes": 90},
               {"candidate": "x", "municipio": "60000", "bairro_id": "3300000:ibge:001", "votes": 80}]
    statuses = {"60011": "verificada", "60000": "bloqueada_divergencia"}
    rows, denoms, coverage = pipeline.geographic_aggregates(official, located, statuses)
    assert sum(r["votes"] for r in rows) == sum(r["denominator"] for r in denoms) == 180
    assert all(r["bairro_id"] is None for r in rows if r["municipio"] == "60000")
    assert next(r for r in coverage if r["municipio"] == "60011")["unlocated"] == 10
    assert next(r for r in coverage if r["municipio"] == "60000")["unlocated"] == 80
    assert any(r["candidate"] == "y" and r["votes"] == 0 for r in rows)
    assert not any(r["candidate"] == "absent" for r in rows)
    assert pipeline.municipality_geography_status(False, True) == "bloqueada_divergencia"
    assert pipeline.municipality_geography_status(True, False) == "sem_malha"
    assert pipeline.municipality_geography_status(True, True) == "verificada"
    with pytest.raises(ValueError, match="multiplied"):
        pipeline.geographic_aggregates(official, [{**located[0], "votes": 101}], statuses)


def test_duckdb_pipeline_blocks_only_the_divergent_municipality(tmp_path, monkeypatch):
    processed=tmp_path/'data/processed'
    public=tmp_path/'web/dist/data'
    for path in [processed,public,tmp_path/'data/review',tmp_path/'data/reference']:
        path.mkdir(parents=True,exist_ok=True)
    (tmp_path/'data/review/correcoes_locais.csv').write_text('ano,turno,municipio,zona,local,latitude,longitude,bairro_id,fonte,justificativa\n')
    (tmp_path/'data/reference/candidaturas_destaque.csv').write_text('ano,cargo,sq_candidato,person_id\n')
    monkeypatch.setattr(pipeline,'ROOT',tmp_path)
    monkeypatch.setattr(pipeline,'PROCESSED',processed)
    monkeypatch.setattr(pipeline,'PUBLIC',public)
    common={'CD_ELEICAO':'1','DT_ELEICAO':'06/10/2024','CD_TIPO_ELEICAO':'2','DS_ELEICAO':'Teste',
            'CD_CARGO':'3','DS_CARGO':'GOVERNADOR','NR_TURNO':'1','SG_UE':'RJ','NM_UE':'RJ',
            'NR_ZONA':'1','NR_CANDIDATO':'10','NM_CANDIDATO':'Candidato','NM_URNA_CANDIDATO':'Candidato',
            'SG_PARTIDO':'P','NR_PARTIDO':'10','ST_VOTO_EM_TRANSITO':'N'}
    mun=[{**common,'CD_MUNICIPIO':m,'NM_MUNICIPIO':m,'SQ_CANDIDATO':c,
          'QT_VOTOS_NOMINAIS':str(v),'QT_VOTOS_NOMINAIS_VALIDOS':str(v)}
         for m,c,v in [('60011','10',100),('60011','20',0),('60000','10',80)]]
    sec=[{**r,'NR_VOTAVEL':'10','NR_SECAO':'1','NR_LOCAL_VOTACAO':'1',
          'QT_VOTOS':str(int(r['QT_VOTOS_NOMINAIS'])-(1 if r['CD_MUNICIPIO']=='60000' else 0))} for r in mun]
    locais=[{'NR_TURNO':'1','CD_MUNICIPIO':m,'NR_ZONA':'1','NR_SECAO':'1','NR_LOCAL_VOTACAO':'1',
             'NM_LOCAL_VOTACAO':'Escola','DS_ENDERECO':'Rua','NM_BAIRRO':'Centro',
             'NR_LATITUDE':'-22.5','NR_LONGITUDE':lon,'NR_SECAO_PRINCIPAL':'1',
             'CD_TIPO_SECAO_AGREGADA':'0','DT_ELEICAO':'06/10/2024'} for m,lon in [('60011','-43.5'),('60000','-41.5')]]
    paths={}
    for kind,rows in [('munzona',mun),('secao',sec),('locais',locais)]:
        paths[kind]=processed/f'{kind}.parquet'
        pd.DataFrame(rows).to_parquet(paths[kind])
    monkeypatch.setattr(pipeline,'ingest',lambda year,kind,force=False:paths.get(kind))
    geoms=[box(-44,-23,-43,-22),box(-42,-23,-41,-22)]
    props=[{'id':'3304557:ipp:001','nome':'Centro','municipio':'60011'},
           {'id':'3300000:teste:001','nome':'Centro','municipio':'60000'}]
    geo={'bairros':{'features':[{'properties':p,'geometry':mapping(g)} for p,g in zip(props,geoms)]},
         'by_id':{p['id']:p for p in props},'legacy_ids':{'001':props[0]['id']},
         'bairro_municipios':{'60011','60000'},'geom':geoms,'tree':STRtree(geoms),
         'municipio_geoms':dict(zip(['60011','60000'],geoms)),
         'municipios_by_tse':{'60011':{'metropolitana':True},'60000':{'metropolitana':True}}}
    result=pipeline.build_year(2024,geo)
    data=json.loads((public/result['datasets'][0]['file']).read_text())
    assert data['meta']['geography_status']=='parcial'
    assert {r['municipio']:r['status'] for r in data['coverage_by_municipio']}=={'60011':'verificada','60000':'bloqueada_divergencia'}
    assert data['coverage']['total']==data['coverage']['located']==100
    local_cov={r['municipio']:r for r in data['local_coverage_by_municipio']}
    assert local_cov['60011']['total']==local_cov['60011']['located']==100
    assert local_cov['60000']['total']==local_cov['60000']['unlocated']==80
    assert local_cov['60000']['located']==0
    shard=json.loads(gzip.decompress((public/data['candidates'][0]['file']).read_bytes()))
    assert any(r['bairro_id']=='3304557:ipp:001' for r in shard['10']['bairros'])
    assert all(r['bairro_id'] is None and r['votes']==80 for r in shard['10']['bairros'] if r['municipio']=='60000')


def test_geography_identity_and_explicit_groups():
    for fn, expected in [("municipios.geojson",92),("bairros.geojson",167)]:
        data=json.loads((ROOT / "data/reference" / fn).read_text())
        assert len(data["features"]) == expected
        assert len({f["properties"]["id"] for f in data["features"]}) == expected
    mun=json.loads((ROOT / "data/reference/municipios.geojson").read_text())["features"]
    rio=next(f for f in mun if f["properties"]["tse"]=="60011")
    assert rio["properties"]["id"]=="3304557"
    assert len({f["properties"]["regiao"] for f in mun})==8
    assert sum(bool(f["properties"]["metropolitana"]) for f in mun)==22
    bairros=json.loads((ROOT / "data/reference/bairros.geojson").read_text())["features"]
    assert len({f["properties"]["macro"] for f in bairros})==6
    assert all(f["properties"]["macro"]=="Barra/Jacarepaguá" for f in bairros if str(f["properties"]["ap"])=="4")


def test_public_assets_do_not_contain_private_documents():
    root=ROOT/"web/dist"
    assert (root/"index.html").exists()
    for p in root.rglob("*"):
        assert not any(c in p.parts for c in ["docs","prompts","planos",".git","raw","processed"])
        assert "transcricao" not in p.name.lower()
        assert "marcel" not in p.name.lower()


def test_all_published_aggregates_conserve_nominal_valid_votes():
    files=list((ROOT/"web/dist/data/datasets").glob("*.json"))
    assert files, "Run the pipeline first"
    for p in files:
        data=json.loads(p.read_text())
        if data.get('sharded'):
            shards={}
            for candidate in data['candidates']:
                filename=candidate['file']
                if filename not in shards:
                    shards[filename]=json.loads(gzip.decompress((ROOT/'web/dist/data'/filename).read_bytes()))
            data['zones']=[r for shard in shards.values() for v in shard.values() for r in v['zones']]
            data['bairros']=[r for shard in shards.values() for v in shard.values() for r in v['bairros']]
            data['locals']=[r for shard in shards.values() for v in shard.values() for r in v.get('locals',[])]
        assert sum(r["votes"] for r in data["zones"])==sum(r["denominator"] for r in data["denominators"])
        assert sum(c["votes"] for c in data["candidates"])==data["meta"]["total"]
        assert all(r["votes"]>=0 for r in data["zones"])
        if data.get('local_denominators'):
            assert sum(r['votes'] for r in data['locals'])==data['meta']['total']
            assert sum(r['denominator'] for r in data['local_denominators'])==data['meta']['total']
            candidate_zones={}
            candidate_locals={}
            for row in data['zones']:
                k=(row['candidate'],row['municipio'],row['zona'])
                candidate_zones[k]=candidate_zones.get(k,0)+row['votes']
            for row in data['locals']:
                k=(row['candidate'],row['municipio'],row['zona'])
                candidate_locals[k]=candidate_locals.get(k,0)+row['votes']
            assert candidate_locals==candidate_zones
            points=json.loads((ROOT/f"web/dist/data/zones-{data['meta']['year']}.json").read_text())['locations']
            point_ids=[r['id'] for r in points]
            assert len(point_ids)==len(set(point_ids))
            valid_ids={r['id'] for r in points if r['lat'] is not None and r['lon'] is not None}
            for cov in data['local_coverage_by_municipio']:
                assert cov['located']+cov['unlocated']==cov['total']
                assert sum(r['denominator'] for r in data['local_denominators'] if r['municipio']==cov['municipio'] and r['local_id'] in valid_ids)==cov['located']
        if "coverage_by_municipio" in data and data["coverage_by_municipio"]:
            covs = {r["municipio"]: r for r in data["coverage_by_municipio"]}
            for municipio, cov in covs.items():
                assert cov["located"]+cov["unlocated"]==cov["total"]
                assert sum(r["votes"] for r in data["bairros"] if r["municipio"]==municipio)==cov["total"]
                assert sum(r["denominator"] for r in data["bairro_denominators"] if r["municipio"]==municipio)==cov["total"]
                assert sum(r["denominator"] for r in data["denominators"] if r["municipio"]==municipio)==cov["total"]
                if cov["status"]!="verificada":
                    assert all(r["bairro_id"] is None for r in data["bairros"] if r["municipio"]==municipio)
            candidate_bairro = {}
            candidate_zones = {}
            for row in data["bairros"]:
                key=(row["candidate"],row["municipio"])
                candidate_bairro[key]=candidate_bairro.get(key,0)+row["votes"]
            for row in data["zones"]:
                key=(row["candidate"],row["municipio"])
                candidate_zones[key]=candidate_zones.get(key,0)+row["votes"]
            for key, total in candidate_bairro.items():
                assert total==candidate_zones.get(key,0)
            if "60011" in covs:
                assert data["coverage"]==covs["60011"]
        elif data["meta"]["geography_status"]=="verificada":
            cov=data["coverage"]
            assert cov["located"]+cov["unlocated"]==cov["total"]
            assert sum(r["votes"] for r in data["bairros"])==cov["total"]
            assert sum(r["denominator"] for r in data["bairro_denominators"])==cov["total"]
        else:
            assert not data["bairros"]


def test_presidential_votes_are_present_without_duplicate_imports():
    catalog=json.loads((ROOT/"web/dist/data/catalog.json").read_text())
    for year in catalog["years"]:
        if year["status"]=="processed" and year["year"] in (2018,2022,2026):
            assert any(d["cargo"]=="1" for d in year["datasets"])
            assert all(d["ue"]=="BR" for d in year["datasets"] if d["cargo"]=="1")


def test_version_exclusions_require_three_exact_valid_vote_totals():
    for path in (ROOT/"web/dist/data").glob("audit-*.json"):
        audit=json.loads(path.read_text())
        for row in audit.get("unmatched_section_contests",[]):
            if row["excluded_from_current_valid"]:
                assert row["candidate_valid"]==row["party_valid"]==row["section_valid"]
            elif row["municipio"]=="60011" and row["votes"]:
                contest="-".join(str(v) for v in [audit['year'],row['election'],row['cargo'],row['turno'],row['ue']])
                dataset=json.loads((ROOT/'web/dist/data/datasets'/f'{contest}.json').read_text())
                if dataset.get('coverage_by_municipio'):
                    assert next(c for c in dataset['coverage_by_municipio'] if c['municipio']=='60011')['status']=='bloqueada_divergencia'
                else:
                    assert dataset['meta']['geography_status']=='bloqueada_divergencia'


def test_metro_neighborhood_ids_keep_homonyms_and_sources_distinct():
    path=ROOT/'data/reference/bairros-metro.geojson'
    if not path.exists():
        pytest.skip('Metropolitan source not built yet')
    features=json.loads(path.read_text())['features']
    ids=[f['properties']['id'] for f in features]
    assert len(ids)==len(set(ids))
    municipalities={f['properties']['tse']:f['properties'] for f in json.loads((ROOT/'data/reference/municipios.geojson').read_text())['features']}
    for f in features:
        prop=f['properties']
        assert len(prop['id'].split(':'))==3
        assert prop['id'].split(':')[0]==str(prop['municipio_ibge'])
        assert str(prop['municipio_ibge'])==str(municipalities[prop['municipio']]['id'])
        assert municipalities[prop['municipio']]['metropolitana']
        if prop['municipio']!='60011':
            assert not any(prop.get(level) for level in ['ra','rp','ap','macro'])
    excluded={m for m,p in municipalities.items() if p['nome'] == 'Paracambi'}
    assert not any(f['properties']['municipio'] in excluded for f in features)


def test_transferred_sections_use_own_effective_building_and_keep_original_codes():
    db=duckdb.connect()
    locations=pd.DataFrame([
        {'turno':'1','municipio':'60011','zona':'230','secao':sec,'local':loc,
         'local_original':'1112','nome_normalizado':pipeline.norm(name),
         'endereco_normalizado':pipeline.norm(addr)}
        for sec,loc,name,addr in [('50','1066','Escola Jorge Zarur','Rua A, 1'),
                                 ('52','1279','Escola Mulheres Quafá','Rua B, 2'),
                                 ('53','1279','Escola Mulheres Quafá','Rua B, 2'),
                                 ('69','1279','Escola Mulheres Quafá','Rua B, 2')]])
    sections=pd.DataFrame([
        {'CD_ELEICAO':'619','CD_TIPO_ELEICAO':'2','NR_TURNO':'1','CD_MUNICIPIO':'60011',
         'NR_ZONA':'230','NR_SECAO':sec,'NR_LOCAL_VOTACAO':loc,
         'NM_LOCAL_VOTACAO':name,'DS_LOCAL_VOTACAO_ENDERECO':addr}
        for sec,loc,name,addr in [('50','1112','Escola Jorge Zarur','Rua A, 1'),
                                 ('52','1112','Escola Mulheres Quafá','Rua B, 2'),
                                 ('53','1112','Prédio original','Rua anterior'),
                                 ('69','1066','Escola Jorge Zarur','Rua A, 1')]])
    db.register('loc',locations);db.execute('CREATE TABLE locations AS SELECT * FROM loc')
    db.register('sec_raw',sections)
    pipeline.resolve_section_locations(db,set(sections.columns))
    result={r[0]:r[1:] for r in db.execute('SELECT secao,result_local,effective_local,link_method FROM resolved_section_locations').fetchall()}
    assert result['50']==('1112','1066','transferencia_original_confirmada')
    assert result['52']==('1112','1279','transferencia_original_confirmada')
    assert result['53']==('1112',None,'ligacao_local_pendente')
    assert result['69']==('1066',None,'ligacao_local_pendente')
    assert db.execute('SELECT COUNT(*) FROM resolved_section_locations').fetchone()[0]==4
    db.close()


def test_known_2024_transfer_regression_matches_documented_original_section_votes():
    audit=json.loads((ROOT/'web/dist/data/audit-2024.json').read_text())
    rows=[r for r in audit['transferred_votes'] if r['municipio']=='60011' and r['turno']=='1']
    assert {r['cargo']:(r['sections'],r['votes']) for r in rows}=={'11':(19,4123),'13':(19,3972)}
    dataset=json.loads((ROOT/'web/dist/data/datasets/2024-619-11-1-60011.json').read_text())
    paes=next(c for c in dataset['candidates'] if c.get('person_id')=='paes')
    detail=json.loads(gzip.decompress((ROOT/'web/dist/data'/paes['file']).read_bytes()))[paes['id']]
    assert sum(r['votes'] for r in detail['bairros'] if r['bairro_id'] is None)==0
    assert sum(r['votes'] for r in detail['bairros'] if r['bairro_id'] is not None)==paes['votes']


def test_paes_2022_is_not_a_zero_candidate():
    catalog=json.loads((ROOT/"web/dist/data/catalog.json").read_text())
    year=next(y for y in catalog["years"] if y["year"]==2022)
    if year["status"]!="processed":
        pytest.skip("2022 not yet processed")
    for d in year["datasets"]:
        data=json.loads((ROOT/"web/dist/data"/d["file"]).read_text())
        assert not any(c["name"]=="EDUARDO PAES" for c in data["candidates"])


def test_regional_presentation_separates_capital_without_losing_municipalities():
    regions=json.loads((ROOT/'web/dist/data/regiao-capital-separada.geojson').read_text())['features']
    assert len(regions)==9
    members=[m for f in regions for m in f['properties']['municipios']]
    municipalities=json.loads((ROOT/'data/reference/municipios.geojson').read_text())['features']
    assert len(members)==len(set(members))==92
    assert set(members)=={f['properties']['tse'] for f in municipalities}
    capital=next(f for f in regions if f['properties']['id']=='Rio de Janeiro (capital)')
    metro=next(f for f in regions if f['properties']['id']=='Metropolitana (sem a capital)')
    assert capital['properties']['municipios']==['60011']
    assert len(metro['properties']['municipios'])==21 and '60011' not in metro['properties']['municipios']
