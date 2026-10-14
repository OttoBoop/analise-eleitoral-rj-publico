import test from 'node:test';
import assert from 'node:assert/strict';
import {territoryStatus} from '../web/territory-status.js';
const known={hasDispute:true,hasCandidate:true,inDispute:true,local:true,geographyStatus:'verificada'};
test('municipal candidacy cannot become zero outside its electorate',()=>{
  assert.equal(territoryStatus({...known,inDispute:false,aggregate:{votes:0,denominator:100}}),'fora_disputa');
});
test('unassigned polygons and absent geography remain distinct from actual zero votes',()=>{
  assert.equal(territoryStatus(known),'sem_secoes_localizadas');
  assert.equal(territoryStatus({...known,geographyStatus:'sem_malha'}),'sem_malha');
  assert.equal(territoryStatus({...known,aggregate:{votes:0,denominator:100}}),'zero');
  assert.equal(territoryStatus({...known,aggregate:{votes:0,denominator:0}}),'sem_votos_validos');
});
test('missing candidacy and blocked reconciliation never become a valid result',()=>{
  assert.equal(territoryStatus({...known,hasCandidate:false}),'sem_candidatura');
  assert.equal(territoryStatus({...known,geographyStatus:'bloqueada_divergencia',aggregate:{votes:10,denominator:100}}),'bloqueada_divergencia');
  assert.equal(territoryStatus({...known,aggregate:{votes:10,denominator:100}}),'ok');
});
