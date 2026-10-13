#!/usr/bin/env python3
"""Browser checks, including a project Pages prefix. No fixed browser dependency.
Use ATLAS_QA_URL for an already deployed URL and CHROMIUM_PATH for a browser override.
"""
from pathlib import Path
from functools import partial
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
import csv,gzip,json,os,shutil,struct,threading,zipfile
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
            assert len(initial['rows'])>140 and any(r['delta'] is not None for r in initial['rows'])
            assert initial['semantics']['growth'] and initial['semantics']['sign']==1
            assert page.locator('#party-a,.nav[data-view="historico"],.nav[data-view="zonas"]').count()==0
            page.screenshot(path=str(OUT/'desktop.png'),full_page=True)
            for button,name in [('download-csv','comparacao.csv'),('download-xlsx','comparacao.xlsx'),('download-geo','comparacao.geojson'),('download-png','mapas.png')]:
                with page.expect_download(timeout=120000) as info:page.locator('#'+button).click()
                info.value.save_as(OUT/name);assert (OUT/name).stat().st_size>100
            assert zipfile.is_zipfile(OUT/'comparacao.xlsx')
            geo=json.loads((OUT/'comparacao.geojson').read_text())['features'];assert len(geo)>140
            with (OUT/'comparacao.csv').open(encoding='utf-8-sig') as f:exported=list(csv.DictReader(f,delimiter=';'))
            assert len(exported)==len(initial['rows'])
            workbook=load_workbook(OUT/'comparacao.xlsx',data_only=True)
            sheet=list(workbook['Comparação'].values)
            assert list(sheet[0])==list(exported[0])
            assert len(sheet)-1==len(exported)
            for excel,csv_record in zip(sheet[1:],exported):
                assert excel[0]==csv_record['Território']
                for cell,text_value in zip(excel[1:],list(csv_record.values())[1:]):
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
                assert not {'a','b','sa','sb'}.intersection(properties)
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
            assert page.evaluate('document.querySelector("#level option[value=ap]").disabled'),page.evaluate('({scope:document.querySelector("#scope").value,level:document.querySelector("#level").value,rows:atlasState.rows.length})')
            assert page.locator('#municipal-coverage summary').count()==1
            page.screenshot(path=str(OUT/'metro.png'),full_page=True)
            page.locator('#preset-paes').click();page.wait_for_function('atlasState.a?.votes==='+str(paes_a))
            municipality_geo=json.loads((ROOT/'web/dist/data/municipios.geojson').read_text())
            petropolis=next(f['properties']['tse'] for f in municipality_geo['features'] if f['properties']['nome']=='Petrópolis')
            page.locator('#scope').select_option('municipio');page.locator('#municipality').select_option(petropolis)
            page.wait_for_function('atlasState.level==="municipio" && atlasState.scope==="municipio"')
            paes=person(b,'paes')
            with gzip.open(ROOT/'web/dist/data'/paes['file'],'rt') as f:paes_zones=json.load(f)[paes['id']]['zones']
            petropolis_votes=sum(r['votes'] for r in paes_zones if r['municipio']==petropolis)
            page.wait_for_function('atlasState.b?.votes==='+str(petropolis_votes))
            assert page.locator('#level option[value="bairro"]').get_attribute('disabled') is not None
            page.locator('#preset-paes').click();page.wait_for_function('atlasState.a?.votes==='+str(paes_a))
            page.locator('#year-a').select_option('2020');page.wait_for_function('atlasState.labels.a.includes("2020")')
            page.locator('#turn-a').select_option('2')
            _,second=contest(2020,'11',turn='2')
            second_votes=person(second,'paes')['capital_votes']
            page.wait_for_function('atlasState.a?.votes==='+str(second_votes))
            page.locator('#year-b').select_option('2024');page.wait_for_function('atlasState.labels.b.includes("2024")')
            page.locator('#turn-b').select_option('2');page.wait_for_function('atlasState.b===null')
            assert page.locator('#share-b').inner_text()=='—'
            page.locator('#preset-paes').click();page.wait_for_function('atlasState.a?.votes==='+str(paes_a))
            page.locator('#year-a').select_option('2022');page.wait_for_function('atlasState.a===null');assert page.locator('#share-a').inner_text()=='—'
            page.locator('.nav[data-view="metodologia"]').click();page.wait_for_function('document.querySelector("#source-table tbody").children.length>10')
            mobile=browser.new_page(viewport={'width':390,'height':844});mobile.on('pageerror',lambda e:errors.append(str(e)))
            mobile.goto(url,wait_until='domcontentloaded');mobile.wait_for_function('window.atlasState?.b',timeout=120000)
            assert not mobile.locator('#control-panel').get_attribute('open')
            assert mobile.evaluate('document.documentElement.scrollWidth<=innerWidth')
            mobile.locator('#tab-b').click();mobile.wait_for_function('document.querySelector(".map-card.mobile-active").dataset.map==="b"')
            mobile.locator('#tab-d').click();mobile.wait_for_function('document.querySelector(".map-card.mobile-active").dataset.map==="d"')
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
            assert not errors,errors
            (OUT/'report.json').write_text(json.dumps({'passed':True,'url':url,'browser_errors':errors,'rows':len(initial['rows']),'scenarios':['colors','exports','synchronization','viewport','Ramagem','Flavio','Niteroi','metro','absence','mobile-tabs','Pages-prefix','Petropolis-municipal','second-turn-2020','unavailable-turn-2024','linked-highlights','mobile-PNG-polygons']},indent=2))
            browser.close()
    finally:
        if server:server.shutdown();server.server_close()
    print('Browser QA passed: colors, independent selectors, synchronized maps, metros, exports, mobile, Pages prefix.')
if __name__=='__main__':main()
