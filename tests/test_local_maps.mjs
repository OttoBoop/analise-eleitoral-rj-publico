import test from 'node:test';
import assert from 'node:assert/strict';
import {reportedBairroId,localFeatures,localValues,exportLocalFeatures,sameLocalElection} from '../web/local-maps.js';
const meta={year:2024,election:'619',date:'06/10/2024',turno:'1'},locations=[
  {id:'m:1:10:1',municipio:'m',zona:'1',local:'10',turno:'1',lat:-22,lon:-43,bairro_raw:'Centro'},
  {id:'m:1:11:1',municipio:'m',zona:'1',local:'11',turno:'1',lat:-22.1,lon:-43.1,bairro_raw:'Centro'},
  {id:'m:2:12:1',municipio:'m',zona:'2',local:'12',turno:'1',lat:null,lon:null,bairro_raw:'Lages'},
];
const data={meta,local_denominators:locations.map((p,i)=>({local_id:p.id,denominator:100*(i+1)})),locals:locations.map((p,i)=>({local_id:p.id,candidate:'c',votes:i===2?0:10*(i+1)})),local_status_by_municipio:[{municipio:'m',status:'verificada'}]};
const build=(d=data,candidate='c')=>localFeatures({locations,data:d,candidate,inScope:()=>true});
test('point values belong to each location, not repeated zone totals',()=>{
  const points=build();assert.deepEqual(points.features.map(f=>f.properties.votes),[10,20]);
  assert.equal(points.features.reduce((sum,f)=>sum+f.properties.votes,0),30);
  assert.equal(points.features[0].properties.territory_id,'m:1');
});
test('real zero, absence, blocked geography and missing coordinates stay distinct',()=>{
  assert.equal(localValues(data,'c').get('m:2:12:1').votes,0);
  assert.equal(build(data,null).features[0].properties.votes,null);
  assert.equal(build(data,null).features[0].properties.denominator,100);
  assert.equal(build({...data,local_status_by_municipio:[{municipio:'m',status:'bloqueada_divergencia'}]}).features[0].properties.votes,null);
  assert.equal(build().features.length,2);
});
test('reported neighborhood homonyms are scoped by municipality and explicitly point based',()=>{
  assert.equal(reportedBairroId('m','  São  José '),'tse:m:SAO JOSE');
  assert.notEqual(reportedBairroId('m','Centro'),reportedBairroId('n','Centro'));
  const points=localFeatures({locations,data,candidate:'c',inScope:()=>true,level:'bairro',fallbackMunicipalities:new Set(['m'])});
  assert.equal(points.features[0].properties.territory_id,'tse:m:CENTRO');assert.equal(points.features[0].geometry.type,'Point');
});
test('exports combine same-election values without repeating any zone totals',()=>{
  const a=build(),b=build({...data,locals:data.locals.map(r=>({...r,votes:r.votes+1}))});
  const features=exportLocalFeatures({a,b},{a:meta,b:meta},{a:'A',b:'B'},-1);
  assert.equal(features.length,2);assert.equal(features.reduce((n,f)=>n+f.properties.votos_referencia,0),30);
  assert.equal(features.reduce((n,f)=>n+f.properties.votos_comparados,0),32);assert.equal(features[0].properties.diferenca_votos,-1);
});
test('different-election coordinates are exported separately without temporal comparison',()=>{
  const features=exportLocalFeatures({a:build(),b:build()},{a:meta,b:{...meta,year:2026}},{a:'2024',b:'2026'});
  assert.equal(features.length,4);assert(features.every(f=>f.properties.diferenca_votos===null));
  assert.equal(features.reduce((n,f)=>n+(f.properties.votos_referencia||0),0),30);
});
test('presidential and regional contest IDs can share a polling event; turns and supplementary types cannot',()=>{
  assert.equal(sameLocalElection({...meta,election:'295',cargo:'1'},{...meta,election:'297',cargo:'3'}),true);
  assert.equal(sameLocalElection(meta,{...meta,turno:'2'}),false);
  assert.equal(sameLocalElection({...meta,election_type:'2'},{...meta,election_type:'1'}),false);
});
test('registered locations without nominal denominators never inherit zone availability or become zero',()=>{
  const registered={id:'m:1:99:1',municipio:'m',zona:'1',local:'99',turno:'1',lat:-22.2,lon:-43.2,bairro_raw:'Centro'},points=localFeatures({locations:[...locations,registered],data,candidate:'c',inScope:()=>true}),p=points.features.at(-1).properties;
  assert.equal(p.votes,null);assert.equal(p.denominator,null);assert.equal(p.result_status,'sem_votacao_associada');
  const exported=exportLocalFeatures({a:points},{a:meta},{a:'Candidato',b:'Sem seleção'}).at(-1).properties;
  assert.match(exported.situacao_referencia,/sem votação nominal associada/);assert.equal(exported.votacao_nominal_associada_referencia,false);
  const outside=localFeatures({locations:[registered],data:{...data,meta:{...meta,cargo:'11',ue:'other'}},candidate:'c',inScope:()=>true});assert.equal(outside.features[0].properties.result_status,'fora_disputa');
  assert.equal(localFeatures({locations:[registered],data,candidate:null,inScope:()=>true}).features[0].properties.result_status,'sem_candidatura');
});
