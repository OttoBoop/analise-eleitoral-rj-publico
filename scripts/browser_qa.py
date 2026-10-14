#!/usr/bin/env python3
"""Browser checks, including a project Pages prefix. No fixed browser dependency.
Use ATLAS_QA_URL for an already deployed URL and CHROMIUM_PATH for a browser override.
"""
from pathlib import Path
from functools import partial
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
import csv,gzip,json,os,shutil,struct,threading,time,zipfile
from openpyxl import load_workbook
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'outputs/browser-qa'

def main():
    OUT.mkdir(parents=True,exist_ok=True)
    catalog=json.loads((ROOT/'web/dist/data/catalog.json').read_text())
    def contest(year,cargo,ue='60011',turn='1'):
        ds=next(y for y in catalog['years'] if y['year']==year)['datasets']
        d=next(d for d in ds if d['cargo']==cargo and d['turno']==turn and d['ue'] in (ue,'RJ'))
        return d,json.loads((ROOT/'web/dist/data'/d['file']).read_text())
    def person(data,key):return next(c for c in data['candidates'] if c.get('person_id')==key)
    _,a=contest(2024,'11');_,b=contest(2026,'3')
    paes_a,paes_b=person(a,'paes')['capital_votes'],person(b,'paes')['capital_votes']
    prefix='/analise-eleitoral-rj-publico/'
    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self,*args):pass
        def translate_path(self,path):
            if path.startswith(prefix):path=path[len(prefix)-1:]
            return super().translate_path(path)
    server=None
    url=os.getenv('ATLAS_QA_URL')
    if not url:
        server=ThreadingHTTPServer(('127.0.0.1',0),partial(QuietHandler,directory=str(ROOT/'web/dist')))
        threading.Thread(target=server.serve_forever,daemon=True).start()
        url=f'http://127.0.0.1:{server.server_port}{prefix}'
    try:
        with sync_playwright() as p:
            exe=os.getenv('CHROMIUM_PATH') or shutil.which('chromium') or shutil.which('chromium-browser')
            kwargs={'headless':True}
            if exe:kwargs['executable_path']=exe
            browser=p.chromium.launch(**kwargs)
            page=browser.new_page(viewport={'width':1440,'height':1000})
            errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(url,wait_until='domcontentloaded')
            page.wait_for_function('window.atlasState && atlasState.b && atlasState.a',timeout=120000)
            initial=page.evaluate('atlasState');assert initial['a']['votes']==paes_a and initial['b']['votes']==paes_b
            assert page.locator('#level option[value="regiao"]').get_attribute('disabled') is not None
            assert page.locator('#level option[value="ap"]').count()==1
            assert len(initial['rows'])>140 and any(r['delta'] is not None for r in initial['rows'])
            assert initial['semantics']['growth'] and initial['semantics']['sign']==1
            selected_candidate=page.locator('#candidate-a').input_value()
            page.locator('#search-a').fill('candidatura inexistente zzz')
            assert page.locator('#candidate-a').input_value()==selected_candidate
            assert page.locator('#candidate-a option:checked').inner_text().startswith('Selecionado:')
            assert page.evaluate('atlasState.a.votes')==paes_a
            page.locator('#search-a').fill('')
            assert page.locator('#candidate-a option').count()==len(a['candidates'])+1

            assert page.locator('#party-a,.nav[data-view="historico"],.nav[data-view="zonas"]').count()==0
            # Year requests can finish in a different order, including a return
            # to the same dataset ID. Only the most recent selection may commit.
            page.evaluate('''()=>{for(const year of ['2020','2018','2024']){const control=document.querySelector('#year-a');control.value=year;control.dispatchEvent(new Event('change',{bubbles:true}));}}''')
            page.wait_for_function('atlasState.selections.a.year===2024 && atlasState.a?.votes==='+str(paes_a)+' && !document.querySelector("#download-csv").disabled')
            assert page.locator('#year-a').input_value()=='2024'
            assert page.locator('#dataset-a').input_value()==page.evaluate('atlasState.selections.a.dataset_id')
            assert '2024' in page.evaluate('atlasState.labels.a')
            # Different uncached candidate shards deliberately arrive slowly.
            # An old callback must not expose zero votes or unlock exports.
            council_dataset,council_data=contest(2024,'13')
            page.locator('#dataset-a').select_option(council_dataset['id'])
            page.wait_for_function('atlasState.selections.a.dataset_id==='+json.dumps(council_dataset['id'])+' && atlasState.a===null')
            choices=[council_data['candidates'][index] for index in (40,80,120)]
            delayed_files={candidate['file']:delay for candidate,delay in zip(choices,[.8,.5,.2])}
            pattern='**/data/candidates/2024-*/**'
            def delay_candidate(route):
                relative=route.request.url.split('/data/',1)[-1]
                if relative in delayed_files:
                    time.sleep(delayed_files[relative])
                    route.fulfill(body=(ROOT/'web/dist/data'/relative).read_bytes(),content_type='application/octet-stream')
                else:route.continue_()
            page.route(pattern,delay_candidate)
            page.evaluate('''(target)=>{window.raceFailures=[];window.raceExpected=target;window.raceObserver=new MutationObserver(()=>{if(!document.querySelector('#download-csv').disabled&&(atlasState.selections.a.candidate!==target.id||atlasState.a?.votes!==target.votes))raceFailures.push({candidate:atlasState.selections.a.candidate,votes:atlasState.a?.votes});});raceObserver.observe(document.querySelector('#download-csv'),{attributes:true,attributeFilter:['disabled']});}''',{'id':choices[-1]['id'],'votes':choices[-1]['votes']})
            page.evaluate('''(ids)=>{for(const id of ids){const control=document.querySelector('#candidate-a');control.value=id;control.dispatchEvent(new Event('change',{bubbles:true}));}}''',[candidate['id'] for candidate in choices])
            page.wait_for_function('atlasState.selections.a.candidate==='+json.dumps(choices[-1]['id'])+' && atlasState.a?.votes==='+str(choices[-1]['votes'])+' && !document.querySelector("#download-csv").disabled')
            assert page.evaluate('raceFailures')==[]
            assert page.locator('#candidate-a').input_value()==choices[-1]['id']
            page.evaluate('raceObserver.disconnect()');page.unroute(pattern,delay_candidate)
            page.locator('#preset-paes').click();page.wait_for_function('atlasState.scope==="capital" && atlasState.a?.votes==='+str(paes_a)+' && atlasState.b?.votes==='+str(paes_b))

            page.wait_for_selector('.rival-candidate')
            assert page.locator('.rival-candidate').count()==5
            assert page.locator('.rivals-panel').bounding_box()['y']<page.locator('#control-panel').bounding_box()['y']
            for i,(year,name,side) in enumerate([(2018,'WITZEL','a'),(2022,'CASTRO','b'),(2026,'RUAS','b')]):
                _,rival_data=contest(year,'3')
                rival=next(c for c in rival_data['candidates'] if name in c['name'])
                other='b' if side=='a' else 'a'
                before=page.evaluate('atlasState')[other]
                page.locator(f'[data-rival="{i}"][data-rival-side="{side}"]').click()
                page.wait_for_function(f'atlasState.labels.{side}.includes("{name}") && atlasState.{side}?.votes==={rival["capital_votes"]}')
                assert page.evaluate('atlasState')[other]==before
                assert page.locator(f'#candidate-{side} option').count()==len(rival_data['candidates'])+1
                assert abs(page.evaluate(f'atlasState.{side}.share')-100*rival['capital_votes']/rival_data['meta']['capital_total'])<1e-9
                if year==2022:
                    freixo=next(c for c in rival_data['candidates'] if 'FREIXO' in c['name'])
                    page.locator('#candidate-b').select_option(freixo['id'])
                    page.wait_for_function('atlasState.b?.votes==='+str(freixo['capital_votes']))
                    assert page.evaluate('atlasState.a')==before
            for index,year,name in [(3,2024,'RAMAGEM'),(4,2020,'CRIVELLA')]:
                _,municipal=contest(year,'11')
                rival=next(c for c in municipal['candidates'] if name in c['name'])
                before=page.evaluate('atlasState.a')
                page.locator(f'[data-rival="{index}"][data-rival-side="b"]').click()
                page.wait_for_function(f'atlasState.labels.b.includes("{name}") && atlasState.b?.votes==={rival["votes"]}')
                assert page.evaluate('atlasState.a')==before
                assert page.evaluate('atlasState.scope')=='city'
                assert 'Prefeito' in page.evaluate('atlasState.labels.b')
                assert page.locator('#candidate-b option').count()==len(municipal['candidates'])+1
            page.locator('#preset-paes').click()
            page.wait_for_function('atlasState.a?.votes==='+str(paes_a)+' && atlasState.b?.votes==='+str(paes_b))
            assert page.locator('#rivals-status').inner_text()==''
            page.screenshot(path=str(OUT/'desktop.png'),full_page=True)
            page.evaluate('''()=>{const capture=window.html2canvas;window.html2canvas=async(...args)=>{window.atlasExportLocked=document.querySelector('#year-a').disabled&&document.querySelector('#scope').disabled&&document.querySelector('#preset-flavio').disabled&&document.querySelector('#comparar').inert;return capture(...args);};}''')
            for button,name in [('download-csv','comparacao.csv'),('download-xlsx','comparacao.xlsx'),('download-geo','comparacao.geojson'),('download-png','mapas.png')]:
                with page.expect_download(timeout=120000) as info:page.locator('#'+button).click()
                info.value.save_as(OUT/name);assert (OUT/name).stat().st_size>100
            assert page.evaluate('window.atlasExportLocked') is True
            assert not page.locator('#year-a').is_disabled()
            assert page.evaluate('document.querySelector("#comparar").inert') is False
            assert zipfile.is_zipfile(OUT/'comparacao.xlsx')
            geo_export=json.loads((OUT/'comparacao.geojson').read_text())
            assert 'Simplificados' in geo_export['geometria']['poligonos']
            assert 'integral versionada' in geo_export['geometria']['atribuicao_votos']
            geo=geo_export['features'];assert len(geo)>140
            with (OUT/'comparacao.csv').open(encoding='utf-8-sig') as f:exported=list(csv.DictReader(f,delimiter=';'))
            assert len(exported)==len(initial['rows'])
            workbook=load_workbook(OUT/'comparacao.xlsx',data_only=True)
            sheet=list(workbook['Comparação'].values)
            assert list(sheet[0])==list(exported[0])
            assert len(sheet)-1==len(exported)
            for excel,csv_record in zip(sheet[1:],exported):
                assert excel[0]==csv_record['Território']
                for cell,(key,text_value) in zip(excel[1:],list(csv_record.items())[1:]):
                    if key.startswith(('Situação','Evidência')):
                        assert (cell or '')==text_value
                        continue
                    expected=float(text_value) if text_value else None
                    assert cell is None if expected is None else abs(cell-expected)<1e-9
            metadata=' '.join(str(v) for row in workbook['Metadados'].values for v in row if v is not None)
            assert '2024' in metadata and '2026' in metadata and 'PAES' in metadata.upper()
            for row in initial['rows']:
                record=next(r for r in exported if r['Território']==row['name'])
                for prefix_key,field in [('Votos de referência','a'),('Votos comparados','b')]:
                    key=next(k for k in record if k.startswith(prefix_key));assert (float(record[key]) if record[key] else None)==row[field]
                properties=next(f for f in geo if f['properties']['id']==row['id'])['properties']
                for key,field in [('votos_referencia','a'),('votos_comparados','b'),('participacao_referencia_pct','sa'),('participacao_comparada_pct','sb'),('diferenca_participacao_pp','delta'),('diferenca_votos','dv')]:
                    assert properties[key]==row[field]
                assert properties['candidatura_referencia']==initial['labels']['a']
                assert properties['candidatura_comparada']==initial['labels']['b']
                assert properties['situacao_referencia']==next(v for k,v in record.items() if k.startswith('Situação de referência'))
                assert properties['situacao_comparada']==next(v for k,v in record.items() if k.startswith('Situação comparada'))
                assert not {'a','b','sa','sb'}.intersection(properties)
            page.locator('#map-a').scroll_into_view_if_needed()
            map_box=page.locator('#map-a').bounding_box()
            old_center=page.evaluate('atlasMaps.a.getCenter()')
            pointer_x=map_box['x']+map_box['width']/2;pointer_y=map_box['y']+map_box['height']/2
            page.mouse.move(pointer_x,pointer_y);page.mouse.down()
            page.mouse.move(pointer_x+50,pointer_y+30,steps=10);page.mouse.up()
            page.wait_for_function('(old)=>atlasMaps.a.getCenter().distanceTo(L.latLng(old.lat,old.lng))>1 && Object.values(atlasMaps).every(m=>m.getCenter().equals(atlasMaps.a.getCenter(),1e-10))',arg=old_center)
            page.evaluate('()=>{atlasMaps.a.setView([-22.91,-43.27],12,{animate:false});}')
            page.wait_for_function('Object.values(atlasMaps).every(m=>m.getZoom()===12)')
            center=page.evaluate('atlasMaps.a.getCenter()')
            page.locator('#metric').select_option('votes')
            page.wait_for_function('document.querySelector("#legend-range").textContent.includes("Escala comum")')
            page.wait_for_timeout(300)
            assert page.evaluate('atlasMaps.a.getZoom()')==12
            assert page.evaluate('Object.values(atlasMaps).every(m=>Math.abs(m.getCenter().lat-atlasMaps.a.getCenter().lat)<1e-9 && Math.abs(m.getCenter().lng-atlasMaps.a.getCenter().lng)<1e-9)')
            assert abs(page.evaluate('atlasMaps.a.getCenter().lat')-center['lat'])<.0001
            page.locator('#metric').select_option('share')
            row_id=page.locator('#comparison-table tbody tr[data-id]').first.get_attribute('data-id')
            page.locator('#comparison-table tbody tr[data-id]').first.dispatch_event('mouseenter')
            assert page.evaluate('(id)=>Object.values(atlasFeatureLayers).every(ls=>ls.get(id)?.options.weight===2.5)',row_id)
            page.locator('#comparison-table tbody tr[data-id]').first.dispatch_event('mouseleave')
            # The same candidacy has a genuine zero difference, never '-0'.
            page.locator('#year-b').select_option('2024');page.wait_for_function('atlasState.selections.b.year===2024 && atlasState.b?.votes==='+str(paes_a))
            assert page.locator('#delta-share').inner_text()=='0,00 p.p.'
            assert page.locator('#delta-votes').inner_text()=='0'
            assert all(r['delta'] in (None,0) and r['dv'] in (None,0) for r in page.evaluate('atlasState.rows'))
            assert '-0,00' not in page.locator('#comparison-table').inner_text()
            # Same-year candidates: common scales and semantic reference advantage.
            page.locator('#year-b').select_option('2024')
            page.wait_for_function('atlasState.labels.b.includes("2024")')
            ramagem=next(c for c in a['candidates'] if 'RAMAGEM' in c['name'].upper())
            page.locator('#candidate-b').select_option(ramagem['id'])
            page.wait_for_function('atlasState.semantics.advantage')
            assert 'RAMAGEM' in page.locator('#map-title-b').inner_text().upper()
            assert page.locator('#legend-a .legend-ends').inner_text()==page.locator('#legend-b .legend-ends').inner_text()
            assert page.locator('#legend-b .oranges').count()==1
            page.screenshot(path=str(OUT/'duel.png'),full_page=True)
            # Same SQ in the same year is one candidacy even without a
            # cross-year person mapping. Cached geometry must refresh values,
            # popup labels, palette and the style restored after highlighting.
            shared_id=page.evaluate('atlasState.rows.find(r=>r.a>0 && r.b>0).id')
            page.evaluate('(id)=>{window.ramagemGeometry=atlasFeatureLayers.b.get(id);}',shared_id)
            seq=page.evaluate('atlasState.render_seq')
            page.locator('#candidate-a').select_option(ramagem['id'])
            page.wait_for_function('(seq)=>atlasState.render_seq>seq && atlasState.semantics.same && document.querySelector("#map-loading").hidden',arg=seq)
            assert not page.evaluate('atlasState.semantics.growth')
            assert page.locator('#legend-b .blues').count()==1
            assert page.locator('#delta-share').inner_text()=='0,00 p.p.' and page.locator('#delta-votes').inner_text()=='0'
            assert page.evaluate('(id)=>atlasFeatureLayers.b.get(id)===window.ramagemGeometry',shared_id)
            page.locator('#comparison-table tbody tr[data-id]').first.dispatch_event('mouseenter')
            page.locator('#comparison-table tbody tr[data-id]').first.dispatch_event('mouseleave')
            assert page.evaluate('(id)=>atlasFeatureLayers.a.get(id).options.fillColor===atlasFeatureLayers.b.get(id).options.fillColor',shared_id)
            page.evaluate('(id)=>{atlasFeatureLayers.b.get(id).openPopup();}',shared_id)
            assert 'RAMAGEM' in page.locator('#map-b .leaflet-popup-content').inner_text()
            assert 'EDUARDO PAES' not in page.locator('#map-b .leaflet-popup-content').inner_text()
            page.evaluate('()=>{atlasMaps.b.closePopup();}')
            with page.expect_download() as info:page.locator('#download-geo').click()
            info.value.save_as(OUT/'empate-ramagem.geojson')
            tie=json.loads((OUT/'empate-ramagem.geojson').read_text())['features']
            assert all(f['properties']['diferenca_votos'] in (None,0) and f['properties']['diferenca_participacao_pp'] in (None,0) for f in tie)
            assert all('RAMAGEM' in f['properties']['candidatura_referencia'] and 'RAMAGEM' in f['properties']['candidatura_comparada'] for f in tie)
            page.locator('#preset-flavio').click();page.wait_for_function('atlasState.labels.a.includes("FLÁVIO") || atlasState.labels.a.includes("FLAVIO")')
            page.wait_for_function('atlasState.labels.b.includes("2026") && atlasState.a && atlasState.b')
            assert any(0<abs(r['delta'])<.01 for r in page.evaluate('atlasState.rows') if r['delta'] is not None)
            # Prefeitura municipality changed independently, and paired municipalities have no difference.
            niteroi,_=contest(2024,'11','58653')
            page.locator('#preset-paes').click();page.wait_for_function('atlasState.a?.votes==='+str(paes_a))
            page.locator('#dataset-a').select_option(niteroi['id'])
            page.wait_for_function('atlasState.scope==="city" && atlasState.labels.a.includes("2024")')
            assert 'Niterói' in page.locator('#map-context').inner_text()
            cand=page.locator('#candidate-a option').nth(1).get_attribute('value');page.locator('#candidate-a').select_option(cand)
            page.wait_for_function('atlasState.a !== null')
            assert page.evaluate('atlasFeatureLayers.a.size')>40
            _,niteroi_data=contest(2024,'11','58653')
            niteroi_c=next(c for c in niteroi_data['candidates'] if c['id']==cand)
            assert page.evaluate('atlasState.a.votes')==niteroi_c['votes']
            page.locator('#year-b').select_option('2024');page.wait_for_function('atlasState.labels.b.includes("2024")')
            assert page.locator('#delta-share').inner_text()=='—'
            assert 'municípios distintos' in page.locator('#notice').inner_text()
            page.locator('#preset-paes').click();page.wait_for_function('atlasState.a?.votes==='+str(paes_a))
            page.locator('#scope').select_option('metro');page.wait_for_function('atlasState.scope==="metro"')
            metro_rows=page.evaluate('atlasState.rows')
            metro_count=len(metro_rows)
            metro_geometry=json.loads((ROOT/'web/dist/data/bairros-metro.geojson').read_text())['features']
            assert metro_count>=len(metro_geometry)
            assert any(r['id'].startswith('tse:') for r in metro_rows)
            assert any(r['status_b']=='sem_secoes_localizadas' for r in metro_rows)
            assert sum(r['status_a']=='fora_disputa' for r in metro_rows)>800
            assert all(r['a'] is None for r in metro_rows if r['status_a']=='fora_disputa')
            assert all(r['b'] is None for r in metro_rows if r['status_b']=='sem_secoes_localizadas')
            assert 'ausência não significa zero' in page.locator('#notice').inner_text()
            missing=next(r for r in metro_rows if r['name']=='Cafubá · Niterói')
            assert missing['status_a']=='fora_disputa' and missing['status_b']=='sem_secoes_localizadas'
            page.evaluate('(id)=>{atlasFeatureLayers.b.get(id).openPopup();}',missing['id'])
            assert 'inventário em revisão' in page.locator('#map-b .leaflet-popup-content').inner_text()
            assert 'Fora da abrangência' in page.locator('#map-b .leaflet-popup-content').inner_text()
            page.evaluate('()=>{atlasMaps.b.closePopup();}')
            petropolis_rows=[r for r in metro_rows if r['name'].endswith('· Petrópolis')]
            assert len(petropolis_rows)==45
            assert sum(r['b'] or 0 for r in petropolis_rows)==58673
            assert not any(r['status_b']=='sem_malha' for r in petropolis_rows)
            page.locator('#territory').select_option(missing['id']);page.wait_for_function('atlasState.rows.length===1')
            with page.expect_download(timeout=120000) as info:page.locator('#download-csv').click()
            info.value.save_as(OUT/'bairro-sem-local.csv')
            with (OUT/'bairro-sem-local.csv').open(encoding='utf-8-sig') as f:missing_export=next(csv.DictReader(f,delimiter=';'))
            assert next(v for k,v in missing_export.items() if k.startswith('Votos comparados'))==''
            assert 'inventário em revisão' in next(v for k,v in missing_export.items() if k.startswith('Situação comparada'))
            page.locator('#territory').select_option('');page.wait_for_function('atlasState.rows.length==='+str(metro_count))
            assert page.locator('#comparison-table tbody tr[data-id]').count()==50
            page.locator('#table-next').click();assert '51–100' in page.locator('#table-page-info').inner_text()
            page.locator('#table-prev').click()
            page.wait_for_function('[...document.getElementById("level").options].find(option=>option.value==="ap")?.disabled===true')
            assert page.locator('#municipal-coverage summary').count()==1
            page.screenshot(path=str(OUT/'metro.png'),full_page=True)
            page.locator('#scope').select_option('state');page.locator('#level').select_option('regiao');page.wait_for_function('atlasState.level==="regiao" && atlasState.rows.length===9')
            regional_rows=page.evaluate('atlasState.rows')
            assert sum(r['b'] or 0 for r in regional_rows)==person(b,'paes')['votes']
            capital_row=next(r for r in regional_rows if r['id']=='Rio de Janeiro (capital)')
            assert capital_row['b']==paes_b
            metro_row=next(r for r in regional_rows if r['id']=='Metropolitana (sem a capital)')
            assert metro_row['a'] is None and metro_row['status_a']=='fora_disputa'
            assert metro_row['b']==2934854-paes_b
            page.screenshot(path=str(OUT/'regions-capital-separated.png'),full_page=True)
            page.locator('#preset-paes').click();page.wait_for_function('atlasState.a?.votes==='+str(paes_a))
            municipality_geo=json.loads((ROOT/'web/dist/data/municipios.geojson').read_text())
            petropolis=next(f['properties']['tse'] for f in municipality_geo['features'] if f['properties']['nome']=='Petrópolis')
            page.locator('#scope').select_option('municipio');page.locator('#municipality').select_option(petropolis)
            page.wait_for_function('atlasState.level==="bairro" && atlasState.scope==="municipio" && atlasState.rows.length===45')
            paes=person(b,'paes')
            with gzip.open(ROOT/'web/dist/data'/paes['file'],'rt') as f:paes_zones=json.load(f)[paes['id']]['zones']
            petropolis_votes=sum(r['votes'] for r in paes_zones if r['municipio']==petropolis)
            page.wait_for_function('atlasState.b?.votes==='+str(petropolis_votes))
            assert page.locator('#level option[value="bairro"]').get_attribute('disabled') is None
            assert sum(r['b'] or 0 for r in page.evaluate('atlasState.rows'))==petropolis_votes
            page.screenshot(path=str(OUT/'petropolis-bairros.png'),full_page=True)
            paracambi=next(f['properties']['tse'] for f in municipality_geo['features'] if f['properties']['nome']=='Paracambi')
            page.locator('#municipality').select_option(paracambi)
            page.wait_for_function('atlasState.rows.length===5 && atlasPointFeatures.b.features.length===17')
            assert all(r['id'].startswith('tse:') for r in page.evaluate('atlasState.rows'))
            assert 'não há malha' in page.locator('#notice').inner_text()
            assert sum(r['b'] or 0 for r in page.evaluate('atlasState.rows'))==sum(f['properties']['votes'] or 0 for f in page.evaluate('atlasPointFeatures.b.features'))
            page.screenshot(path=str(OUT/'paracambi-locais.png'),full_page=True)
            page.locator('#preset-paes').click();page.wait_for_function('atlasState.a?.votes==='+str(paes_a))
            page.locator('#year-a').select_option('2020');page.wait_for_function('atlasState.labels.a.includes("2020")')
            page.locator('#turn-a').select_option('2')
            _,second=contest(2020,'11',turn='2')
            second_votes=person(second,'paes')['capital_votes']
            page.wait_for_function('atlasState.a?.votes==='+str(second_votes))
            page.locator('#year-b').select_option('2024');page.wait_for_function('atlasState.labels.b.includes("2024")')
            page.locator('#turn-b').select_option('2');page.wait_for_function('atlasState.b===null && atlasState.selections.b.has_dispute===false')
            assert page.evaluate('atlasState.selections.b.requested_ue')=='60011'
            assert page.evaluate('atlasState.selections.b.requested_year')==2024
            assert page.evaluate('atlasState.selections.b.requested_turn')=='2'
            assert page.locator('#dataset-b').input_value()==''
            assert 'turno indisponível' in page.evaluate('atlasState.labels.b')
            assert 'Rio de Janeiro' in page.evaluate('atlasState.labels.b')
            assert all(r['status_b']=='disputa_indisponivel' for r in page.evaluate('atlasState.rows'))
            assert page.locator('#share-b').inner_text()=='—'
            page.locator('#preset-paes').click();page.wait_for_function('atlasState.a?.votes==='+str(paes_a))
            page.locator('#year-a').select_option('2022');page.wait_for_function('atlasState.a===null');assert page.locator('#share-a').inner_text()=='—'
            assert page.evaluate('atlasState.selections.a.has_dispute') is True
            assert page.evaluate('atlasState.selections.a.has_candidate') is False
            assert page.locator('#dataset-a').input_value()!=''
            assert page.evaluate('atlasState.labels.a').startswith('Sem candidatura')
            # PSD shortcuts select one election-specific candidacy at a time.
            page.locator('#preset-paes').click();page.wait_for_function('atlasState.a?.votes==='+str(paes_a))
            psd=json.loads((ROOT/'web/dist/data/psd-candidates.json').read_text())['candidates']
            page.locator('#open-psd').click();page.wait_for_selector('.psd-candidate')
            assert page.locator('#psd-year').input_value()=='2024'
            page.locator('#psd-search').fill('marcio ribeiro')
            councilor=next(c for c in psd if c['year']==2024 and c['cargo']=='13' and c['ue']=='60011' and c['name']=='MARCIO RIBEIRO')
            assert page.locator('.psd-candidate').count()==1
            page.locator('.psd-candidate [data-psd-side="a"]').click()
            page.wait_for_function('atlasState.a?.votes==='+str(councilor['votes']))
            assert page.evaluate('atlasState.b.votes')==paes_b
            assert '2024' in page.evaluate('atlasState.labels.a') and 'Vereador' in page.evaluate('atlasState.labels.a')
            _,council_data=contest(2024,'13')
            assert abs(page.evaluate('atlasState.a.share')-100*councilor['votes']/council_data['meta']['capital_total'])<1e-9
            assert page.locator('#candidate-a option').count()>len([c for c in council_data['candidates'] if c['party']=='PSD'])
            page.locator('#open-psd').click();page.locator('#psd-cargo').select_option('6');page.locator('#psd-search').fill('soranz')
            deputy=next(c for c in psd if c['year']==2026 and c['cargo']=='6' and 'SORANZ' in c['name'])
            _,deputy_data=contest(2026,'6')
            expected_deputy=next(c['capital_votes'] for c in deputy_data['candidates'] if c['id']==deputy['candidate'])
            assert page.locator('#psd-municipality-label').is_hidden()
            page.locator('.psd-candidate [data-psd-side="b"]').click()
            page.wait_for_function('atlasState.b?.votes==='+str(expected_deputy))
            assert page.evaluate('atlasState.a.votes')==councilor['votes']
            page.locator('#open-psd').click();page.locator('#psd-cargo').select_option('7');page.locator('#psd-year').select_option('2022');page.locator('#psd-search').fill('claudio caiado')
            _,state_deputy_data=contest(2022,'7')
            state_deputy=next(c for c in state_deputy_data['candidates'] if c['party']=='PSD' and c['name']=='CLAUDIO CAIADO')
            page.locator('.psd-candidate [data-psd-side="a"]').click();page.wait_for_function('atlasState.a?.votes==='+str(state_deputy['capital_votes']))
            assert page.evaluate('atlasState.b.votes')==expected_deputy
            page.locator('#open-psd').click();page.locator('#psd-cargo').select_option('13');page.locator('#psd-year').select_option('2024');page.locator('#psd-municipality').select_option('58653');page.locator('#psd-search').fill('')
            niteroi_psd=next(c for c in psd if c['year']==2024 and c['cargo']=='13' and c['ue']=='58653')
            page.locator('#psd-search').fill(niteroi_psd['name'])
            page.locator('.psd-candidate [data-psd-side="a"]').first.click()
            page.wait_for_function('atlasState.scope==="municipio" && atlasState.a?.votes==='+str(niteroi_psd['votes']))
            assert page.locator('#municipality').input_value()=='58653'
            page.locator('#open-psd').click();page.locator('#psd-search').fill('candidatura inexistente qqq');assert page.locator('.psd-candidate').count()==0
            page.locator('#close-psd').click()
            page.locator('#preset-paes').click();page.wait_for_function('atlasState.a?.votes==='+str(paes_a))
            page.locator('.nav[data-view="metodologia"]').click();page.wait_for_function('document.querySelector("#source-table tbody").children.length>10')
            assert page.locator('#geographic-cases details').count()==25
            page.locator('.brand').click();page.wait_for_function('document.querySelector("#comparar").classList.contains("active")')
            page.go_back();page.wait_for_function('document.querySelector("#metodologia").classList.contains("active")')
            page.go_forward();page.wait_for_function('document.querySelector("#comparar").classList.contains("active")')
            mobile=browser.new_page(viewport={'width':390,'height':844},is_mobile=True,has_touch=True);mobile.on('pageerror',lambda e:errors.append(str(e)))
            mobile.context.new_cdp_session(mobile).send('Emulation.setCPUThrottlingRate',{'rate':4})
            mobile.goto(url,wait_until='domcontentloaded');mobile.wait_for_function('window.atlasState?.b',timeout=120000)
            assert not mobile.locator('#control-panel').get_attribute('open')
            assert mobile.evaluate('document.documentElement.scrollWidth<=innerWidth')
            assert mobile.locator('#open-psd').is_visible()
            assert mobile.locator('#preset-flavio.featured-candidate').is_visible()
            assert mobile.locator('#preset-flavio').bounding_box()['y']<mobile.locator('#comparar .page-head').bounding_box()['y']
            # A touch selection followed by lazy tabs either keeps an active
            # highlight synchronized or clears it through the normal mouseout.
            centro=mobile.evaluate('atlasState.rows.find(r=>r.name.toUpperCase().startsWith("CENTRO ·") && r.a>0 && r.b>0).id')
            mobile.evaluate('(id)=>{atlasMaps.a.fitBounds(atlasFeatureLayers.a.get(id).getBounds(),{maxZoom:12,animate:false});}',centro)
            mobile.locator('#map-a').scroll_into_view_if_needed()
            point=mobile.evaluate('(id)=>atlasMaps.a.latLngToContainerPoint(atlasFeatureLayers.a.get(id).getCenter())',centro)
            mobile.locator('#map-a').tap(position={'x':point['x'],'y':point['y']})
            highlighted=mobile.evaluate('[...atlasFeatureLayers.a].find(([id,layer])=>layer.options?.weight===2.5)?.[0]')
            assert highlighted
            for side in ['b','d','a']:
                mobile.locator('#tab-'+side).tap()
                if mobile.locator('#comparison-table tr.highlighted').count():
                    assert mobile.evaluate('([side,id])=>atlasFeatureLayers[side].get(id).options.weight===2.5',[side,highlighted])
                else:
                    assert mobile.evaluate('(id)=>Object.values(atlasFeatureLayers).every(index=>index.get(id)?.options?.weight!==2.5)',highlighted)
            mobile.locator('#preset-flavio').click();mobile.wait_for_function('atlasState.labels.a.includes("FLÁVIO") || atlasState.labels.a.includes("FLAVIO")')
            mobile.locator('#preset-paes').click();mobile.wait_for_function('atlasState.a?.votes==='+str(paes_a))
            mobile.wait_for_selector('.rival-candidate')
            assert mobile.locator('.rival-candidate').count()==5
            assert mobile.evaluate('document.documentElement.scrollWidth<=innerWidth')
            mobile.locator('[data-rival="2"][data-rival-side="b"]').click()
            ruas=next(c for c in b['candidates'] if 'RUAS' in c['name'])
            mobile.wait_for_function('atlasState.b?.votes==='+str(ruas['capital_votes']))
            assert mobile.evaluate('atlasState.a.votes')==paes_a
            mobile.locator('#preset-paes').click();mobile.wait_for_function('atlasState.b?.votes==='+str(paes_b))
            mobile.locator('#open-psd').click();mobile.wait_for_selector('.psd-candidate')
            mobile.locator('#psd-search').fill('marcio ribeiro')
            assert mobile.evaluate('document.documentElement.scrollWidth<=innerWidth')
            mobile.screenshot(path=str(OUT/'psd-mobile.png'),full_page=True)
            mobile.locator('.psd-candidate [data-psd-side="a"]').click()
            mobile.wait_for_function('atlasState.a?.votes==='+str(councilor['votes']))
            assert mobile.evaluate('atlasState.b.votes')==paes_b
            mobile.locator('#preset-paes').click();mobile.wait_for_function('atlasState.a?.votes==='+str(paes_a))
            mobile.locator('#tab-b').click();mobile.wait_for_function('document.querySelector(".map-card.mobile-active").dataset.map==="b"')
            mobile.locator('#tab-d').click();mobile.wait_for_function('document.querySelector(".map-card.mobile-active").dataset.map==="d"')
            mobile.locator('#scope').select_option('metro');mobile.wait_for_function('atlasState.scope==="metro" && atlasState.rows.length==='+str(metro_count))
            assert mobile.locator('#comparison-table tbody tr[data-id]').count()==50
            assert mobile.evaluate('atlasFeatureLayers.d.size')==len(metro_geometry)
            assert mobile.evaluate('atlasFeatureLayers.a.size===0 && atlasFeatureLayers.b.size===0')
            mobile.locator('#level').select_option('zona');mobile.wait_for_function('atlasState.level==="zona"')
            assert mobile.locator('#map-d').is_visible()
            assert mobile.locator('#download-png').is_enabled() and mobile.locator('#download-geo').is_enabled()
            assert 'Diferença por zona indisponível' in mobile.locator('[data-map="d"] .map-state').inner_text()
            mobile.locator('#tab-b').click();mobile.wait_for_function('atlasFeatureLayers.b.size>49')
            assert len(mobile.evaluate('atlasPointFeatures.b.features'))>2000
            assert mobile.locator('#map-b canvas').count()>0
            # Reused markers must lose their old grouping parents; the new
            # zone index keeps every polling location exactly once.
            for metric in ['votes','share','votes','share']:
                mobile.evaluate('''()=>{const [id,group]=atlasFeatureLayers.b.entries().next().value;window.previousZoneId=id;window.previousZoneGroup=group;window.previousZoneCount=group.getLayers().length;window.previousZoneMarker=group.getLayers()[0];}''')
                seq=mobile.evaluate('atlasState.render_seq')
                mobile.locator('#metric').select_option(metric)
                mobile.wait_for_function('(seq)=>atlasState.render_seq>seq && document.querySelector("#map-loading").hidden',arg=seq)
                assert mobile.evaluate('previousZoneGroup.getLayers().length===0')
                assert mobile.evaluate('atlasFeatureLayers.b.get(previousZoneId).getLayers().length===previousZoneCount')
                assert mobile.evaluate('atlasFeatureLayers.b.get(previousZoneId).getLayers().includes(previousZoneMarker)')
            corrected=mobile.evaluate('atlasPointFeatures.b.features.find(f=>f.properties.id==="60011:211:1341:1").properties')
            mobile.evaluate('''(point)=>{atlasFeatureLayers.b.get(point.territory_id).getLayers().find(layer=>layer.feature.properties.id===point.id).openPopup();}''',corrected)
            coordinate_links=mobile.locator('#map-b .leaflet-popup-content a').evaluate_all('els=>els.filter(el=>el.textContent.startsWith("Fonte da coordenada / correção")).map(el=>el.getAttribute("href"))')
            assert len(coordinate_links)==2 and all(';https' not in url for url in coordinate_links)
            assert set(coordinate_links)=={url.strip() for url in corrected['source'].split(';') if url.strip().startswith(('https://','http://'))}
            mobile.evaluate('()=>{atlasMaps.b.closePopup();}')
            # A registered polling location can lack nominal-vote linkage in
            # this contest even when its zone has votes. Keep that state local.
            unlinked=mobile.evaluate('atlasPointFeatures.b.features.find(f=>f.properties.result_status==="sem_votacao_associada").properties')
            unlinked_reason='Local cadastrado sem votação nominal associada nesta base; vínculo em revisão'
            assert unlinked['votes'] is None and unlinked['denominator'] is None
            assert next(r for r in mobile.evaluate('atlasState.rows') if r['id']==unlinked['territory_id'])['b']>0
            mobile.evaluate('''(point)=>{atlasFeatureLayers.b.get(point.territory_id).getLayers().find(layer=>layer.feature.properties.id===point.id).openPopup();}''',unlinked)
            assert unlinked_reason in mobile.locator('#map-b .leaflet-popup-content').inner_text()
            assert 'Votos disponíveis' not in mobile.locator('#map-b .leaflet-popup-content').inner_text()
            mobile.evaluate('()=>{atlasMaps.b.closePopup();}')
            with mobile.expect_download(timeout=120000) as info:mobile.locator('#download-geo').click()
            info.value.save_as(OUT/'zonas-locais-mobile.geojson')
            zone_points=json.loads((OUT/'zonas-locais-mobile.geojson').read_text())['features']
            assert zone_points and all(f['geometry']['type']=='Point' for f in zone_points)
            assert all(f['properties']['diferenca_votos'] is None for f in zone_points)
            assert sum(f['properties']['votos_referencia'] or 0 for f in zone_points)==paes_a
            assert sum(f['properties']['votos_comparados'] or 0 for f in zone_points)==sum(f['properties']['votes'] or 0 for f in mobile.evaluate('atlasPointFeatures.b.features'))
            exported_unlinked=next(f['properties'] for f in zone_points if f['properties']['local_id']==unlinked['id'] and f['properties'].get('ano_comparado')==2026)
            assert exported_unlinked['votos_comparados'] is None and exported_unlinked['denominador_comparado'] is None
            assert exported_unlinked['situacao_comparada']==unlinked_reason
            assert exported_unlinked['votacao_nominal_associada_comparada'] is False
            with mobile.expect_download(timeout=120000) as info:mobile.locator('#download-xlsx').click()
            info.value.save_as(OUT/'zonas-locais-mobile.xlsx')
            zone_workbook=load_workbook(OUT/'zonas-locais-mobile.xlsx',data_only=True)
            local_rows=list(zone_workbook['Locais'].values)
            local_records=[dict(zip(local_rows[0],row)) for row in local_rows[1:]]
            excel_unlinked=next(row for row in local_records if row['local_id']==unlinked['id'] and row.get('ano_comparado')==2026)
            assert excel_unlinked['situacao_comparada']==unlinked_reason and excel_unlinked['votacao_nominal_associada_comparada'] is False
            assert excel_unlinked['votos_comparados'] is None
            assert sum(row['votos_comparados'] or 0 for row in local_records)==sum(f['properties']['votos_comparados'] or 0 for f in zone_points)
            # Exercise a real touch drag, zoom button and successive lazy tabs.
            mobile.locator('#map-b').scroll_into_view_if_needed()
            map_box=mobile.locator('#map-b').bounding_box()
            old_center=mobile.evaluate('atlasMaps.b.getCenter()')
            pointer_x=map_box['x']+map_box['width']/2;pointer_y=map_box['y']+map_box['height']/2
            touch=mobile.context.new_cdp_session(mobile)
            touch.send('Input.dispatchTouchEvent',{'type':'touchStart','touchPoints':[{'x':pointer_x,'y':pointer_y}]})
            for step in range(1,9):
                touch.send('Input.dispatchTouchEvent',{'type':'touchMove','touchPoints':[{'x':pointer_x+step*5,'y':pointer_y+step*3}]})
                mobile.wait_for_timeout(20)
            touch.send('Input.dispatchTouchEvent',{'type':'touchEnd','touchPoints':[]})
            mobile.wait_for_function('(old)=>atlasMaps.b.getCenter().distanceTo(L.latLng(old.lat,old.lng))>1 && Object.values(atlasMaps).every(m=>m.getCenter().equals(atlasMaps.b.getCenter(),1e-10))',arg=old_center)
            frame=mobile.evaluate('({center:atlasMaps.b.getCenter(),zoom:atlasMaps.b.getZoom()})')
            mobile.locator('#map-b .leaflet-control-zoom-in').click()
            mobile.wait_for_function('(zoom)=>Object.values(atlasMaps).every(m=>m.getZoom()===zoom+1)',arg=frame['zoom'])
            frame=mobile.evaluate('({center:atlasMaps.b.getCenter(),zoom:atlasMaps.b.getZoom()})')
            for side in ['d','a','b']:
                mobile.locator('#tab-'+side).click()
                assert mobile.evaluate('([side,frame])=>atlasMaps[side].getZoom()===frame.zoom && atlasMaps[side].getCenter().equals(L.latLng(frame.center.lat,frame.center.lng),1e-10)',[side,frame])
            row_id=mobile.locator('#comparison-table tbody tr[data-id]').first.get_attribute('data-id')
            mobile.locator('#comparison-table tbody tr[data-id]').first.dispatch_event('mouseenter')
            assert mobile.evaluate('(id)=>atlasFeatureLayers.b.get(id).getLayers().every(l=>l.options.weight===2.5)',row_id)
            mobile.locator('#comparison-table tbody tr[data-id]').first.dispatch_event('mouseleave')
            assert all(r['delta'] is None for r in mobile.evaluate('atlasState.rows'))
            assert mobile.locator('#download-csv').is_enabled()
            mobile.screenshot(path=str(OUT/'mobile-zones-map.png'),full_page=True)
            mobile.locator('#tab-d').click()
            mobile.locator('#level').select_option('bairro');mobile.wait_for_function('atlasState.level==="bairro" && atlasState.rows.length==='+str(metro_count))
            assert mobile.locator('#map-d').is_visible() and mobile.locator('#download-png').is_enabled()
            mobile.locator('#scope').select_option('capital');mobile.wait_for_function('atlasState.scope==="capital" && atlasState.rows.length===167')
            assert mobile.locator('#level option[value="regiao"]').get_attribute('disabled') is not None
            mobile.wait_for_timeout(300)
            assert mobile.evaluate('atlasMaps.d.getZoom()<10')
            mobile.evaluate('window.scrollTo(0,0)')
            mobile.wait_for_timeout(100)
            mobile.screenshot(path=str(OUT/'mobile.png'),full_page=True)
            with mobile.expect_download(timeout=120000) as info:mobile.locator('#download-png').click()
            info.value.save_as(OUT/'mapas-mobile.png')
            assert struct.unpack('>II',(OUT/'mapas-mobile.png').read_bytes()[16:24])[0]>=1800
            from PIL import Image
            image=Image.open(OUT/'mapas-mobile.png').convert('RGB')
            for start in [0,image.width//3,2*image.width//3]:
                pixels=[image.getpixel((x,y)) for x in range(start+10,min(start+image.width//3-10,image.width),4) for y in range(180,min(900,image.height),4)]
                assert sum(1 for r,g,b in pixels if (b-r>25 or r-b>25) and max(r,g,b)-min(r,g,b)>40)>100,'PNG map panel lacks colored polygons'
            assert mobile.evaluate('document.documentElement.scrollWidth<=innerWidth')
            mobile.wait_for_selector('[data-rival-side]')
            untouched=mobile.evaluate('atlasState.a.votes')
            mobile.locator('[data-rival="2"][data-rival-side="b"]').click()
            mobile.wait_for_function('atlasState.labels.b.includes("DOUGLAS RUAS")')
            assert mobile.evaluate('atlasState.a.votes')==untouched
            mobile.locator('#control-panel summary').click()
            assert mobile.locator('#candidate-b option').count()>3
            mobile.locator('#preset-paes').click();mobile.wait_for_function('atlasState.a?.votes==='+str(paes_a))
            assert not errors,errors
            (OUT/'report.json').write_text(json.dumps({'passed':True,'url':url,'browser_errors':errors,'rows':len(initial['rows']),'scenarios':['colors','exports','synchronization','viewport','Ramagem','Flavio','Niteroi','metro','absence','mobile-tabs','Pages-prefix','Petropolis-45-bairros','Paracambi-points-reported-bairro','second-turn-2020','unavailable-turn-2024','linked-highlights','mobile-PNG-polygons','PSD-vereador','PSD-federal','PSD-estadual','PSD-Niteroi','PSD-mobile','PSD-all-party-denominator','metro-outside-election','metro-unassigned-neighborhoods','missing-votes-export','Flavio-featured','rival-shortcuts','rival-independent-selectors','rival-all-candidates','rival-mobile','mobile-zone-point-map','zone-point-exports','zone-point-highlights','unlinked-local-popup-and-exports','mobile-lazy-maps','pagination-full-exports','regional-capital-separated','rapid-year-requests','rapid-candidate-shards','export-lock-while-loading','PNG-capture-selection-lock','search-selected-option','brand-and-hash-navigation','signed-zero-tie','unavailable-turn-preserves-city']},indent=2))
            browser.close()
    finally:
        if server:server.shutdown();server.server_close()
    print('Browser QA passed: colors, independent selectors, synchronized maps, metros, exports, mobile, Pages prefix.')
if __name__=='__main__':main()
