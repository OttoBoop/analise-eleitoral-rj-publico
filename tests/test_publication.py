import importlib.util
import json
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('publication',ROOT/'scripts/prepare_publication.py')
publication=importlib.util.module_from_spec(spec);spec.loader.exec_module(publication)


def make_source(root):
    for name,text in {'README.public.md':'Public atlas','requirements.txt':'pytest',
                      'web/index.html':'<h1>Public</h1>','web/dist/index.html':'Public',
                      'docs/prompts/private.txt':'PRIVATE SENTINEL',
                      '.git/private-history':'PRIVATE HISTORY',
                      'data/raw/private.csv':'PRIVATE RAW',
                      'data/reference/sample.json':json.dumps({'path':'/home/private/input.geojson'})}.items():
        p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text)


def test_export_excludes_private_documents_history_and_paths(tmp_path):
    source=tmp_path/'source';make_source(source)
    staging=tmp_path/'public';(staging/'.git').mkdir(parents=True)
    (staging/'.git/own-history').write_text('PUBLIC HISTORY')
    publication.prepare(staging,source)
    assert (staging/'.git/own-history').read_text()=='PUBLIC HISTORY'
    assert not (staging/'.git/private-history').exists()
    assert not (staging/'docs').exists()
    assert not (staging/'data/raw').exists()
    assert json.loads((staging/'data/reference/sample.json').read_text())['path']=='input.geojson'
    assert (staging/'README.md').read_text()=='Public atlas'
    assert not any('PRIVATE SENTINEL' in p.read_text() for p in staging.rglob('*') if p.is_file())


def test_export_rejects_symlinks_to_private_source(tmp_path):
    source=tmp_path/'source';make_source(source)
    p=source/'web/dist/hidden.txt';p.symlink_to(source/'docs/prompts/private.txt')
    with pytest.raises(RuntimeError,match='Non-public source'):
        publication.prepare(tmp_path/'public',source)
