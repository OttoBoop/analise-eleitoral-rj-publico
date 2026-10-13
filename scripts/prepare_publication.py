#!/usr/bin/env python3
"""Export public code/data with independent history, then optionally push Pages."""
from pathlib import Path
import argparse
import hashlib
import json
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_REPOSITORY = 'OttoBoop/analise-eleitoral-rj-publico'
SCRIPTS = ['fetch_sources.py','fetch_geography.py','fetch_metro_geography.py','pipeline.py',
           'build_temporal_audit.py','build_site.py','browser_qa.py','update.py','prepare_publication.py']
MANIFESTS = ['sources.json','vendors.json','headers.json','headers-partido.json',
             'coleta-validacao.json','validation.json','zone-comparisons.csv']
FORBIDDEN = {'docs','prompts','planos','raw','processed','.git','.openai','__pycache__',
             '.pytest_cache','.venv','outputs','logs'}


def public_files(root=ROOT):
    items=[(root/n,Path(n)) for n in ['requirements.txt','requirements-dev.txt','pytest.ini']]
    items += [(root/'README.public.md',Path(n)) for n in ['README.md','README.public.md']]
    items += [(root/'scripts'/n,Path('scripts')/n) for n in SCRIPTS]
    for folder in ['data/reference','web/dist','tests','.github/workflows']:
        for p in sorted((root/folder).rglob('*')):
            rel=p.relative_to(root)
            if p.is_file() and not any(c in FORBIDDEN for c in rel.parts):
                items.append((p,rel))
    if (root/'web').exists():
        items += [(p,p.relative_to(root)) for p in sorted((root/'web').iterdir())
                  if p.is_file() and p.suffix in {'.html','.css','.js','.mjs'}]
    items += [(root/'data/manifests'/n,Path('data/manifests')/n) for n in MANIFESTS]
    items += [(p,p.relative_to(root)) for p in sorted((root/'data/manifests').glob('audit-*.json'))]
    items += [(root/'data/review'/n,Path('data/review')/n) for n in ['correcoes_locais.csv','zonas_verificadas.csv']]
    return [(p,r) for p,r in items if p.exists()]


def sanitize(value):
    if isinstance(value,dict):
        return {k:sanitize(v) for k,v in value.items()
                if k not in {'preserved_original','invalid_preserved_original','error'}}
    if isinstance(value,list):
        return [sanitize(v) for v in value]
    if isinstance(value,str) and value.startswith(('/home/','/tmp/')):
        return Path(value).name
    return value


def prepare(staging=None,root=ROOT):
    staging=Path(staging) if staging else root/'outputs/public-repository'
    staging.mkdir(parents=True,exist_ok=True)
    if (staging/'.git').is_symlink():
        raise RuntimeError('Public checkout cannot share private Git metadata')
    for p in staging.iterdir():
        if p.name=='.git':
            continue
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        else:
            p.unlink()
    for source,rel in public_files(root):
        if source.is_symlink() or any(c in FORBIDDEN for c in rel.parts):
            raise RuntimeError(f'Non-public source: {rel}')
        target=staging/rel;target.parent.mkdir(parents=True,exist_ok=True)
        if source.suffix=='.json' and rel.parts[0]=='data':
            target.write_text(json.dumps(sanitize(json.loads(source.read_text())),ensure_ascii=False,separators=(',',':'))+'\n')
        else:
            shutil.copyfile(source,target)
    (staging/'.gitignore').write_text('.venv/\n__pycache__/\n.pytest_cache/\ndata/raw/\ndata/processed/\noutputs/\nlogs/\ndocs/\nweb/.openai/\n*.part\n.env*\n')
    files=[p for p in staging.rglob('*') if p.is_file() and '.git' not in p.relative_to(staging).parts]
    manifest={'repository':PUBLIC_REPOSITORY,'files':len(files),
              'sha256':{str(p.relative_to(staging)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}}
    folder=root/'data/manifests';folder.mkdir(parents=True,exist_ok=True)
    (folder/'public-export.json').write_text(json.dumps(manifest,indent=2)+'\n')
    return staging,manifest


def publish(staging):
    def run(*args):
        return subprocess.run(args,cwd=staging,check=True,capture_output=True,text=True).stdout.strip()
    if not (staging/'.git').exists():
        run('git','init','--initial-branch','main')
        run('git','remote','add','origin',f'https://github.com/{PUBLIC_REPOSITORY}.git')
    if Path(run('git','rev-parse','--show-toplevel')).resolve()!=staging.resolve():
        raise RuntimeError('Public checkout must have independent history')
    if run('git','remote','get-url','origin')!=f'https://github.com/{PUBLIC_REPOSITORY}.git':
        raise RuntimeError('Unexpected public repository remote')
    run('git','add','--all')
    if run('git','status','--porcelain'):
        run('git','-c','user.name=OttoBoop','-c','user.email=80711736+OttoBoop@users.noreply.github.com',
            'commit','-q','-m','Publish validated public atlas source and data')
    run('git','push','origin','main')
    return run('git','rev-parse','HEAD')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--push',action='store_true',help='Commit and push the clean public checkout')
    args=parser.parse_args();staging,report=prepare()
    sha=publish(staging) if args.push else None
    print(json.dumps({'checkout':str(staging),'repository':PUBLIC_REPOSITORY,
                      'files':report['files'],'commit_sha':sha},indent=2))


if __name__=='__main__':
    main()
