#!/usr/bin/env python3
"""Exercise all compatible territory controls on desktop and a slower phone."""
import json
import os
import shutil
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'outputs/browser-qa'


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    server = None
    url = os.getenv('ATLAS_QA_URL')
    if not url:
        class Quiet(SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Quiet, directory=str(ROOT/'web/dist')))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f'http://127.0.0.1:{server.server_port}/'
    results, errors = [], []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, executable_path=os.getenv('CHROMIUM_PATH') or shutil.which('chromium'))
            for mobile in [False, True]:
                context = browser.new_context(viewport={'width':390,'height':844} if mobile else {'width':1440,'height':1000},
                                              is_mobile=mobile,has_touch=mobile,accept_downloads=True)
                page = context.new_page()
                page.on('pageerror',lambda e:errors.append(str(e)))
                if mobile:
                    cdp=context.new_cdp_session(page)
                    cdp.send('Emulation.setCPUThrottlingRate',{'rate':4})
                page.goto(url, wait_until='domcontentloaded')
                page.wait_for_function('window.atlasState?.a && atlasState?.b && document.querySelector("#map-loading").hidden',timeout=120000)
                def wait_render(previous, scope=None, level=None, metric=None, year=None):
                    page.wait_for_function('''({previous,scope,level,metric,year}) =>
                        window.atlasState?.render_seq > previous &&
                        document.querySelector('#map-loading').hidden &&
                        document.querySelector('#loading').classList.contains('hidden') &&
                        (!scope || atlasState.scope === scope) &&
                        (!level || atlasState.level === level) &&
                        (!metric || atlasState.metric === metric) &&
                        (!year || atlasState.labels.a.includes(year))''',
                        arg={'previous':previous,'scope':scope,'level':level,'metric':metric,'year':year},
                        timeout=120000)
                def sequence():
                    return page.evaluate('atlasState.render_seq')
                page.locator('#control-panel').evaluate('(el)=>el.open=true')
                for scope in ['capital','state','metro','metro-other','municipio']:
                    previous=sequence()
                    page.locator('#scope').select_option(scope)
                    wait_render(previous,scope=scope)
                    levels = page.locator('#level option:not([disabled])').evaluate_all('(els)=>els.map(e=>e.value)')
                    for level in levels:
                        previous=sequence()
                        page.locator('#level').select_option(level)
                        wait_render(previous,scope=scope,level=level)
                        for metric in ['share','votes']:
                            start=time.monotonic()
                            previous=sequence()
                            page.locator('#metric').select_option(metric)
                            wait_render(previous,scope=scope,level=level,metric=metric)
                            state=page.evaluate('atlasState')
                            assert state['rows'],f'Empty compatible selection {scope}/{level}'
                            assert len({r['id'] for r in state['rows']})==len(state['rows'])
                            for row in state['rows']:
                                for field in ['a','b','sa','sb','delta','dv']:
                                    value=row[field]
                                    assert value is None or isinstance(value,(int,float))
                            assert not page.evaluate('document.documentElement.scrollWidth>innerWidth+1'),f'Overflow {scope}/{level}'
                            assert page.locator('#loading .error').count()==0
                            if level=='zona':
                                assert page.evaluate('atlasPointFeatures.a.features.length>0 || atlasPointFeatures.b.features.length>0')
                            if mobile:
                                for side in ['b','d','a']:
                                    page.locator(f'.map-tabs button[data-map="{side}"]').click()
                                    assert page.locator(f'.map-card[data-map="{side}"]').is_visible()
                            results.append({'device':'phone-cpu4' if mobile else 'desktop','scope':scope,'level':level,'metric':metric,
                                            'rows':len(state['rows']),'seconds':round(time.monotonic()-start,3)})
                for year in ['2018','2020','2022','2024','2026']:
                    previous=sequence()
                    page.locator('#year-a').select_option(year)
                    wait_render(previous,year=year)
                    # Even absence of the previously selected person is a valid state.
                    assert page.locator('#loading .error').count()==0
                    results.append({'device':'phone-cpu4' if mobile else 'desktop','year':year,'label':page.evaluate('atlasState.labels.a')})
                page.screenshot(path=str(OUT/('matrix-phone.png' if mobile else 'matrix-desktop.png')),full_page=True)
                context.close()
            browser.close()
        assert not errors,errors
        (OUT/'matrix-report.json').write_text(json.dumps({'passed':True,'url':url,'cases':results,'browser_errors':errors},ensure_ascii=False,indent=2)+'\n')
        print(json.dumps({'passed':True,'cases':len(results),'browser_errors':errors}))
    finally:
        if server:
            server.shutdown()
            server.server_close()


if __name__=='__main__':
    main()
