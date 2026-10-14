export const TERRITORY_STATUS={
  ok:'Votos disponíveis',
  zero:'Zero votos, com votação da disputa disponível',
  fora_disputa:'Fora da abrangência desta disputa',
  sem_candidatura:'Sem candidatura selecionada',
  disputa_indisponivel:'Disputa ou turno indisponível',
  sem_malha:'Sem malha de bairros validada; consulte locais de votação e resultado municipal',
  bloqueada_divergencia:'Detalhamento bloqueado por divergência eleitoral',
  fora_cobertura:'Fora da cobertura geográfica de bairros',
  geografia_indisponivel:'Ligação entre seções e bairros indisponível',
  sem_secoes_localizadas:'Sem seções associadas ao polígono; localização ou inventário em revisão',
  sem_local_cadastrado:'Sem local cadastrado dentro do polígono da referência nesta eleição e turno',
  sem_votacao_associada:'Local cadastrado sem votação nominal associada nesta base; vínculo em revisão',
  sem_votos_validos:'Sem votos nominais válidos da disputa neste território',
  dados_indisponiveis:'Dados indisponíveis neste território',
};

export function territoryStatus({hasDispute,hasCandidate,inDispute,local,geographyStatus,aggregate}){
  if(!hasDispute)return 'disputa_indisponivel';
  if(!hasCandidate)return 'sem_candidatura';
  if(!inDispute)return 'fora_disputa';
  if(local&&geographyStatus!=='verificada')return ['sem_malha','bloqueada_divergencia','fora_cobertura'].includes(geographyStatus)?geographyStatus:'geografia_indisponivel';
  if(!aggregate)return local?'sem_secoes_localizadas':'dados_indisponiveis';
  if(aggregate.denominator===0)return 'sem_votos_validos';
  return aggregate.votes===0?'zero':'ok';
}
