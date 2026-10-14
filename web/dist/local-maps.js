/* Effective polling locations, never electoral offices or inferred boundaries. */
import {TERRITORY_STATUS} from './territory-status.js';
export const normalizeBairro=value=>String(value??'').normalize('NFD').replace(/[\u0300-\u036f]/g,'').toUpperCase().replace(/\s+/g,' ').trim();
export const reportedBairroId=(municipio,bairro)=>`tse:${municipio}:${normalizeBairro(bairro)}`;
export const zoneId=location=>`${location.municipio}:${location.zona}`;
export function usableCoordinates(location){
  if(location.lat==null||location.lon==null||location.lat===''||location.lon==='')return false;
  const lat=Number(location.lat),lon=Number(location.lon);
  return Number.isFinite(lat)&&Number.isFinite(lon)&&lat>=-90&&lat<=90&&lon>=-180&&lon<=180&&!(lat===0&&lon===0)&&!['fora_municipio','coordenada_invalida','sem_coordenada','invalid_coordinates','outside_municipality'].includes(location.coordinate_status||location.point_status||location.status);
}
export function localValues(data,candidate){
  const values=new Map();
  for(const row of data?.local_denominators||[]){
    if(row.local_id==null)continue;
    const id=String(row.local_id),v=values.get(id)||{votes:0,denominator:0};
    v.denominator+=Number(row.denominator);values.set(id,v);
  }
  for(const row of data?.locals||[]){
    if(row.local_id==null||String(row.candidate)!==String(candidate))continue;
    const v=values.get(String(row.local_id));if(v)v.votes+=Number(row.votes);
  }
  for(const value of values.values())value.share=value.denominator>0?100*value.votes/value.denominator:null;
  return values;
}
export function sameLocalElection(a,b){
  // Presidential and regional contests can have distinct TSE election IDs on
  // the same polling day. Locations are compared by the actual polling event.
  return !!a&&!!b&&['year','date','turno','election_type'].every(key=>String(a[key])===String(b[key]));
}
export function localFeatures({locations,data,candidate,inScope,level='zona',fallbackMunicipalities=new Set()}){
  const values=localValues(data,candidate),blocked=new Set((data?.local_status_by_municipio||[]).filter(r=>r.status!=='verificada').map(r=>String(r.municipio))),features=[];
  for(const location of locations||[]){
    const municipio=String(location.municipio);
    if(!inScope(municipio)||String(location.turno)!==String(data?.meta.turno)||!usableCoordinates(location))continue;
    if(level==='bairro'&&!fallbackMunicipalities.has(municipio))continue;
    const bairro=location.bairro_raw||'Bairro não informado',territoryId=level==='zona'?zoneId(location):reportedBairroId(municipio,bairro),value=!blocked.has(municipio)?values.get(String(location.id)):null;
    const inContest=!['11','13'].includes(String(data?.meta.cargo))||String(data.meta.ue)===municipio;
    const resultStatus=!candidate?'sem_candidatura':!inContest?'fora_disputa':blocked.has(municipio)?'bloqueada_divergencia':!value?'sem_votacao_associada':value.denominator===0?'sem_votos_validos':value.votes===0?'zero':'ok';
    features.push({type:'Feature',geometry:{type:'Point',coordinates:[Number(location.lon),Number(location.lat)]},properties:{...location,id:String(location.id),territory_id:territoryId,kind:'local_votacao',bairro_raw:bairro,votes:candidate?value?.votes??null:null,denominator:value?.denominator??null,share:candidate?value?.share??null:null,blocked:blocked.has(municipio),result_status:resultStatus}});
  }
  return {type:'FeatureCollection',features};
}
export function exportLocalFeatures(collections,metas,labels,sign=1){
  const joined=new Map(),same=sameLocalElection(metas.a,metas.b);
  for(const side of ['a','b'])for(const feature of collections[side]?.features||[]){
    if(feature.geometry.type!=='Point')continue;
    const p=feature.properties,key=`${same?'same':side}:${p.id}`;
    let target=joined.get(key);
    if(!target){target={type:'Feature',geometry:feature.geometry,properties:{id:key,local_id:p.id,municipio:p.municipio,zona:p.zona,local:p.local,nome:p.nome,endereco:p.endereco,bairro_informado_tse:p.bairro_raw,bairro_id:p.bairro_id??null,tipo_geometria:'local cadastrado no TSE (ponto)',fonte:p.source??null,metodo_vinculo:p.link_method??null,votos_referencia:null,votos_comparados:null,participacao_referencia_pct:null,participacao_comparada_pct:null,situacao_referencia:null,situacao_comparada:null}};joined.set(key,target);}
    const suffix=side==='a'?'referencia':'comparados',shareSuffix=side==='a'?'referencia':'comparada';
    target.properties[`votos_${suffix}`]=p.votes;
    target.properties[`participacao_${shareSuffix}_pct`]=p.share;
    target.properties[`candidatura_${side==='a'?'referencia':'comparada'}`]=labels[side];
    target.properties[`ano_${side==='a'?'referencia':'comparado'}`]=metas[side]?.year;
    target.properties[`turno_${side==='a'?'referencia':'comparado'}`]=metas[side]?.turno;
    target.properties[`denominador_${side==='a'?'referencia':'comparado'}`]=p.denominator;
    target.properties[`situacao_${side==='a'?'referencia':'comparada'}`]=TERRITORY_STATUS[p.result_status]||TERRITORY_STATUS.dados_indisponiveis;
    target.properties[`votacao_nominal_associada_${side==='a'?'referencia':'comparada'}`]=['ok','zero','sem_votos_validos'].includes(p.result_status);
  }
  for(const feature of joined.values()){
    const p=feature.properties;
    p.diferenca_participacao_pp=same&&p.participacao_referencia_pct!=null&&p.participacao_comparada_pct!=null?sign*(p.participacao_comparada_pct-p.participacao_referencia_pct):null;
    p.diferenca_votos=same&&p.votos_referencia!=null&&p.votos_comparados!=null?sign*(p.votos_comparados-p.votos_referencia):null;
    p.comparavel=p.diferenca_votos!=null;
  }
  return [...joined.values()];
}
