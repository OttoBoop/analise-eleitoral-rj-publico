#!/usr/bin/env python3
"""One-command local refresh, validation and clean GitHub Pages preparation."""
import subprocess
import sys
import argparse
import shutil
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--years',nargs='+',type=int,choices=[2018,2020,2022,2024,2026],default=[2018,2020,2022,2024,2026])
parser.add_argument('--refresh',action='store_true',help='Collect a new TSE snapshot, preserving the previous generation')
parser.add_argument('--prepare-publication',action='store_true',help='Run browser QA and export the public GitHub checkout')
parser.add_argument('--publish',action='store_true',help='After validation, push the public checkout to trigger GitHub Pages')
args=parser.parse_args()
years=[str(y) for y in args.years]
for command in [
    [sys.executable,"scripts/fetch_sources.py","--years",*years]+(['--refresh'] if args.refresh else []),
    [sys.executable,"scripts/fetch_geography.py"],
    [sys.executable,"scripts/fetch_metro_geography.py"],
    [sys.executable,"scripts/pipeline.py","--years",*years],
    [sys.executable,"scripts/build_temporal_audit.py"],
    [sys.executable,"scripts/build_site.py"],
    [sys.executable,"-m","pytest","-q"],
]:
    subprocess.run(command,cwd=ROOT,check=True)
if args.prepare_publication or args.publish:
    node=shutil.which('node')
    if not node:
        bundled=ROOT.parent/'.tools/node/bin/node'
        node=str(bundled) if bundled.is_file() else None
    if not node:
        raise RuntimeError('Instale Node.js para validar as cores antes da publicação.')
    subprocess.run([node,'--test',*map(str,sorted((ROOT/'tests').glob('*.mjs')))],cwd=ROOT,check=True)
    subprocess.run([sys.executable,'scripts/browser_qa.py'],cwd=ROOT,check=True)
    subprocess.run([sys.executable,'scripts/prepare_publication.py']+(['--push'] if args.publish else []),cwd=ROOT,check=True)
print("Dados e aplicação validados. A publicação usa o repositório público e o GitHub Actions Pages.")
