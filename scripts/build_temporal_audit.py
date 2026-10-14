#!/usr/bin/env python3
"""Observed effective-location composition changes, distinct from legal rezoning."""
import json
from pathlib import Path
import pandas as pd
from pipeline import ROOT, PUBLIC, code, norm, save_json


def main():
    years={}
    for p in sorted((ROOT/"data/processed").glob("locations_*.parquet")):
        year=int(p.stem.split('_')[-1])
        d=pd.read_parquet(p)
        years[year]=d.loc[d.turno.eq('1')]
    overrides=pd.read_csv(ROOT/'data/review/zonas_verificadas.csv',dtype=str,keep_default_na=False)
    results=[]
    yy=sorted(years)
    pairs=set(zip(yy,yy[1:]))
    if 2024 in years and 2026 in years:pairs.add((2024,2026))
    for a,b in sorted(pairs):
        aa,bb=years[a],years[b]
        keys=set(zip(aa.municipio,aa.zona))|set(zip(bb.municipio,bb.zona))
        for municipio,zona in sorted(keys,key=lambda k:(k[0],int(k[1]))):
            def locs(df):
                rows=df.loc[df.zona.eq(zona)&df.municipio.eq(municipio)].drop_duplicates(['local','nome','endereco'])
                return {norm(r.nome)+'|'+norm(r.endereco) for r in rows.itertuples()}
            left,right=locs(aa),locs(bb)
            status='nao_verificada';source='';note='A composição observada não comprova continuidade dos limites legais.'
            matched=overrides.loc[overrides.ano_a.eq(str(a))&overrides.ano_b.eq(str(b))&overrides.municipio.eq(municipio)&overrides.zona.eq(zona)]
            if not matched.empty:
                r=matched.iloc[0]
                if r.status not in ['verificada','alterada','nao_verificada'] or not r.fonte or not r.justificativa:
                    raise ValueError('Zone override requires valid status, source and justification')
                status,source,note=r.status,r.fonte,r.justificativa
            results.append({'ano_a':a,'ano_b':b,'turno':'1','municipio':municipio,'zona':zona,'status':status,
                            'fonte':source,'nota':note,'locais_a':len(left),'locais_b':len(right),
                            'locais_comuns':len(left&right),'adicionados':len(right-left),'removidos':len(left-right),
                            'composicao_observada_alterada':left!=right})
    save_json(PUBLIC/'zone-comparisons.json',{'comparisons':results,'method':'Identidade por nome e endereço normalizados; mudanças de nome também podem alterar a correspondência. Não prova rezoneamento legal.'})
    pd.DataFrame(results).to_csv(ROOT/'data/manifests/zone-comparisons.csv',index=False)
    print(f'{len(results)} zone comparisons audited')


if __name__=='__main__':main()
