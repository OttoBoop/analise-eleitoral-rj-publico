#!/usr/bin/env python3
"""Fetch and validate official fixed geography for the electoral RJ project.

Run .venv/bin/python scripts/fetch_geography.py [--refresh] [--tre].
Default caches downloaded bytes; --refresh creates a new current snapshot.
Geometric operations need pyshp, pyproj and shapely. No election votes are
assigned here and TRE files do not certify temporal comparability.
"""
from __future__ import annotations
import argparse, csv, hashlib, io, json, re, unicodedata, urllib.parse, urllib.request, zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import shapefile
from pyproj import Transformer
from shapely.geometry import mapping, shape
from shapely.ops import transform, unary_union

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / 'data/raw/geography'
REF = ROOT / 'data/reference'
SOURCES = {
 'bairros_ipp': ('bairros_ipp_original.geojson', 'https://pgeo3.rio.rj.gov.br/arcgis/rest/services/Cartografia/Limites_administrativos/MapServer/4/query?where=1%3D1&outFields=*&outSR=4326&f=geojson', None, 'Prefeitura/IPP; copyright municipal; licença aberta específica não informada no endpoint'),
 'bairros_ipp_metadata': ('bairros_ipp_metadata.json', 'https://pgeo3.rio.rj.gov.br/arcgis/rest/services/Cartografia/Limites_administrativos/MapServer/4?f=pjson', None, 'Prefeitura da Cidade do Rio de Janeiro'),
 'municipios_ibge': ('RJ_Municipios_2025.zip', 'https://geoftp.ibge.gov.br/organizacao_do_territorio/malhas_territoriais/malhas_municipais/municipio_2025/UFs/RJ/RJ_Municipios_2025.zip', '2025', 'IBGE; dados públicos com atribuição'),
 'regioes_ceperj': ('limites_intermunicipais_2025.zip', 'https://www.rj.gov.br/ceperj/sites/default/files/arquivos-paginas/limites_intermunicipais_2025.zip', '2025', 'CEPERJ; licença específica não identificada na página'),
 'codigos_tse_ibge': ('municipio_tse_ibge.zip', 'https://cdn.tse.jus.br/estatistica/sead/odsele/municipio_tse_ibge/municipio_tse_ibge.zip', None, 'Creative Commons Attribution, conforme catálogo TSE'),
}
REGIONS = {
 'Região Metropolitana': 'Metropolitana',
 'Região Noroeste Fluminense': 'Noroeste Fluminense',
 'Região Norte Fluminense': 'Norte Fluminense',
 'Região das Baixadas Litorâneas': 'Baixadas Litorâneas',
 'Região Serrana': 'Serrana',
 'Região Centro-Sul Fluminense': 'Centro-Sul Fluminense',
 'Região do Médio Paraíba': 'Médio Paraíba',
 'Região da Costa Verde': 'Costa Verde',
}
# Explicit correspondence using official RP codes; this is an analytical
# classification agreed for this project, not a new official division.
MACROS = {'1.1':'Centro', '2.1':'Zona Sul', '2.2':'Grande Tijuca',
 '3.1':'Zona Norte','3.2':'Zona Norte','3.3':'Zona Norte','3.4':'Zona Norte',
 '3.5':'Zona Norte','3.6':'Zona Norte','3.7':'Zona Norte',
 '4.1':'Barra/Jacarepaguá','4.2':'Barra/Jacarepaguá',
 '5.1':'Zona Oeste','5.2':'Zona Oeste','5.3':'Zona Oeste','5.4':'Zona Oeste'}
TRE_BASE = 'https://www.tre-rj.jus.br/transparencia-e-prestacao-de-contas/tecnologia-da-informacao-e-comunicacao/'
TRE_FILES = {
 'tre_capital_individual.zip': 'rezoneamento-individual-das-zonas-eleitorais-da-capital',
 'tre_capital_unidas.zip':'rezoneamento_capital_zonas_unidas',
 'tre_interior_individual.zip':'rezoneamento_interior_shapes_individuais',
 'tre_interior_unidas.zip':'rezoneamento_interior_zonas_unidas',
}

def key(s):
 return ''.join(c for c in unicodedata.normalize('NFKD', str(s)) if not unicodedata.combining(c)).casefold().strip()

def fetch(name, url, refresh):
 p = RAW / name
 if refresh or not p.exists():
  req = urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0 (electoral-geography-research)'})
  tmp = p.with_suffix(p.suffix+'.part')
  with urllib.request.urlopen(req, timeout=90) as response, tmp.open('wb') as out:
   while chunk := response.read(1024*1024): out.write(chunk)
  if name.endswith('.zip'):
   with zipfile.ZipFile(tmp) as z:
    bad = z.testzip()
    if bad: raise ValueError(f'ZIP corrupt: {bad}')
  elif name.endswith(('.json','.geojson')): json.loads(tmp.read_text())
  tmp.replace(p)
 return p

def zipped_reader(path):
 z = zipfile.ZipFile(path)
 stem = next(n[:-4] for n in z.namelist() if n.endswith('.shp'))
 r = shapefile.Reader(shp=io.BytesIO(z.read(stem+'.shp')), shx=io.BytesIO(z.read(stem+'.shx')), dbf=io.BytesIO(z.read(stem+'.dbf')), encoding='utf8')
 return z, r

def write_json(path, data):
 path.write_text(json.dumps(data, ensure_ascii=False, separators=(',',':'))+'\n', encoding='utf8')

def write_csv(path, rows, fields=None):
 with path.open('w', encoding='utf8', newline='') as f:
  w = csv.DictWriter(f, fieldnames=fields or list(rows[0])); w.writeheader(); w.writerows(rows)

def main():
 parser = argparse.ArgumentParser(description=__doc__)
 parser.add_argument('--refresh', action='store_true')
 parser.add_argument('--tre', action='store_true', help='Also download the TRE reference malhas; their election validity remains unverified')
 parser.add_argument('--historical-bairros', type=Path, help='Preserve an earlier local IPP shapefile as a separate snapshot; it never replaces the current reference')
 args = parser.parse_args(); RAW.mkdir(parents=True,exist_ok=True); REF.mkdir(parents=True,exist_ok=True)
 # Preserve original collection timestamps for cached sources.
 previous = {}
 mf = REF/'geography_sources.json'
 if mf.exists(): previous = {x['id']:x for x in json.loads(mf.read_text()).get('sources',[])}
 sources=[]
 for sid,(name,url,generation,license_) in SOURCES.items():
  print('Fetching',sid,flush=True); p=fetch(name,url,args.refresh); digest=hashlib.sha256(p.read_bytes()).hexdigest()
  old=previous.get(sid,{})
  collected = old.get('collected_at') if old.get('sha256')==digest else datetime.now(ZoneInfo('America/Sao_Paulo')).isoformat(timespec='seconds')
  sources.append({'id':sid,'file':str(p.relative_to(ROOT)),'url':url,'generation':generation,'collected_at':collected,'bytes':p.stat().st_size,'sha256':digest,'integrity':'verified_zip_crc' if name.endswith('.zip') else 'parsed_json','license':license_})
 original=json.loads((RAW/'bairros_ipp_original.geojson').read_text())
 features=[]; bairros=[]
 for f in original['features']:
  p=f['properties']; cod=str(p['codbairro']).zfill(3); rp=p['cod_rp'].strip()
  props={'id':cod,'nome':p['nome'].strip(),'ra':str(p['codra']),'ra_nome':p['regiao_adm'].strip(),'rp':rp,'rp_nome':p['rp'].strip(),'ap':p['area_plane'].strip(),'macro':MACROS[rp]}
  features.append({'type':'Feature','id':cod,'properties':props,'geometry':f['geometry']});bairros.append(props)
 assert len({r['id'] for r in bairros})==len(bairros)
 features.sort(key=lambda f:f['id']);bairros.sort(key=lambda r:r['id'])
 write_json(REF/'bairros.geojson',{'type':'FeatureCollection','name':'IPP — referência territorial fixa','features':features})
 write_csv(REF/'bairros_classificacao.csv',bairros)
 z,r=zipped_reader(RAW/'limites_intermunicipais_2025.zip')
 ceperj={key(x['MUNICN']):x for x in (rec.as_dict() for rec in r.records())}
 assert len(ceperj)==92
 with zipfile.ZipFile(RAW/'municipio_tse_ibge.zip') as tz:
  tse=list(csv.DictReader(io.StringIO(tz.read('municipio_tse_ibge.csv').decode('latin1')),delimiter=';'))
 tse=[x for x in tse if x['SG_UF']=='RJ'];tse_by_id={x['CD_MUNICIPIO_IBGE']:x for x in tse}
 assert len(tse_by_id)==92
 for s in sources:
  if s['id']=='codigos_tse_ibge':s['generation']=f"{tse[0]['DT_GERACAO']} {tse[0]['HH_GERACAO']}"
 z,r=zipped_reader(RAW/'RJ_Municipios_2025.zip')
 transformer=Transformer.from_crs('EPSG:4674','EPSG:4326',always_xy=True)
 municipal=[];municipios=[];anomalies=[]
 for sr in r.iterShapeRecords():
  p=sr.record.as_dict();id_=p['CD_MUN'];cp=ceperj[key(p['NM_MUN'])];ts=tse_by_id[id_]
  regiao=REGIONS[cp['REGGOV']]
  props={'id':id_,'nome':p['NM_MUN'],'tse':ts['CD_MUNICIPIO_TSE'],'regiao':regiao,'metropolitana':regiao=='Metropolitana'}
  geom=mapping(transform(transformer.transform,shape(sr.shape.__geo_interface__)))
  municipal.append({'type':'Feature','id':id_,'properties':props,'geometry':geom})
  municipios.append({**props,'ibge':id_})
  if str(cp['cd7'])!=id_:
   anomalies.append({'municipio':p['NM_MUN'],'ibge_ceperj_original':str(cp['cd7']),'ibge_validado':id_,'acao':'Código IBGE2025 e TSE; CEPERJ usada apenas para REGGOV por nome municipal'})
 assert len(municipal)==92 and len({x['tse']for x in municipios})==92
 assert next(x['tse'] for x in municipios if x['id']=='3304557')=='60011'
 municipal.sort(key=lambda f:f['id']);municipios.sort(key=lambda x:x['nome'])
 write_json(REF/'municipios.geojson',{'type':'FeatureCollection','name':'IBGE — Malha municipal2025 + CEPERJ2025/TSE','features':municipal})
 write_csv(REF/'municipio_codigos.csv',municipios)
 write_csv(REF/'regioes_governo.csv',[{'ibge':x['id'],'tse':x['tse'],'municipio':x['nome'],'regiao':x['regiao'],'metropolitana':x['metropolitana']}for x in municipios])
 write_csv(REF/'ceperj_codigos_divergentes.csv',anomalies,['municipio','ibge_ceperj_original','ibge_validado','acao'])
 # These dissolved territories are analytical unions of the fixed bairro
 # polygons, never intersections or fractional allocation of zone votes.
 for field in ['ra','rp','ap','macro']:
  groups={}
  for f in features:groups.setdefault(f['properties'][field],[]).append(f)
  dissolved=[]
  for id_,members in groups.items():
   geometry=unary_union([shape(f['geometry']) for f in members])
   name=members[0]['properties'].get(field+'_nome',id_)
   dissolved.append({'type':'Feature','id':id_,'properties':{'id':id_,'nome':name,'bairros':[m['properties']['id']for m in members]},'geometry':mapping(geometry)})
  write_json(REF/f'{field}.geojson',{'type':'FeatureCollection','features':dissolved})
 # Compact cartography is for rendering/export only. The canonical files
 # above remain full precision for point-in-polygon and spatial review.
 display = REF/'display'; display.mkdir(exist_ok=True)
 for name in ['bairros','municipios','ra','rp','ap','macro']:
  cartography=json.loads((REF/f'{name}.geojson').read_text())
  for f in cartography['features']:
   f['geometry']=mapping(shape(f['geometry']).simplify(0.00008,preserve_topology=True))
  cartography['cartography']={'display_only':True,'simplification_tolerance_degrees':0.00008,'canonical':f'data/reference/{name}.geojson'}
  write_json(display/f'{name}.geojson',cartography)
 if args.historical_bairros:
  path=args.historical_bairros; reader=shapefile.Reader(str(path),encoding='utf8')
  projection=path.with_suffix('.prj').read_text()
  tr=Transformer.from_crs(projection,'EPSG:4326',always_xy=True)
  historical=[]
  for sr in reader.iterShapeRecords():
   p=sr.record.as_dict();rp=str(p['cod_rp']).strip();id_=str(p['codbairro']).zfill(3)
   props={'id':id_,'nome':p['nome'].strip(),'ra':str(p['codra']),'ra_nome':p['regiao_adm'].strip(),'rp':rp,'rp_nome':p['rp'].strip(),'ap':p['area_plane'].strip(),'macro':MACROS[rp]}
   historical.append({'type':'Feature','id':id_,'properties':props,'geometry':mapping(transform(tr.transform,shape(sr.shape.__geo_interface__)))})
  history=REF/'historical';history.mkdir(exist_ok=True)
  write_json(history/'bairros_local_anterior.geojson',{'type':'FeatureCollection','generation':None,'provenance':'local prior IPP download; legal validity date unverified','features':historical})
  write_json(history/'bairros_local_anterior_sources.json',{'source_path':str(path),'generation':None,'legal_validity':'unverified','features':len(historical),'components':[{'path':str(path.with_suffix(ext)),'sha256':hashlib.sha256(path.with_suffix(ext).read_bytes()).hexdigest()}for ext in ['.shp','.shx','.dbf','.prj','.cpg'] if path.with_suffix(ext).exists()]})
 if args.tre:
  for name,slug in TRE_FILES.items():
   url=TRE_BASE+'arquivos-delimitacao-das-zonas-eleitorais/'+slug+'/@@download/file'
   try:
    print('Fetching',name,flush=True);p=fetch(name,url,args.refresh)
    sources.append({'id':name[:-4],'file':str(p.relative_to(ROOT)),'url':url,'generation':None,'collected_at':datetime.now(ZoneInfo('America/Sao_Paulo')).isoformat(timespec='seconds'),'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'integrity':'verified_zip_crc','license':'TRE-RJ público; licença específica não identificada','validity':'não verificada para eleições2018–2026'})
   except Exception as e: print(f'TRE source {name}: {type(e).__name__}: {e}',flush=True)
 report={'bairros':len(features),'municipios':len(municipal),'macrocapital':dict(Counter(x['macro']for x in bairros)),'regioes_governo':dict(Counter(x['regiao']for x in municipios)),'ceperj_codigo_municipal_divergente':len(anomalies),'invalid_bairros':[f['id']for f in features if not shape(f['geometry']).is_valid],'invalid_municipios':[f['id']for f in municipal if not shape(f['geometry']).is_valid],'unverified_zone_comparisons':'all; number repetition alone is not evidence'}
 write_json(mf,{'territorial_reference':'IPP current snapshot / IBGE2025 / CEPERJ2025 / TSE current correspondence','sources':sources,'audit':report})
 print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
