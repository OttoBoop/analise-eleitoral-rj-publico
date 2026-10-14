/** Searchable shortcuts into the existing, election-specific candidate data. */
const $=id=>document.getElementById(id);
const norm=s=>String(s??'').normalize('NFD').replace(/[\u0300-\u036f]/g,'').toUpperCase().replace(/\s+/g,' ').trim();
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const nf=new Intl.NumberFormat('pt-BR');
function option(value,text){const o=document.createElement('option');o.value=value;o.textContent=text;return o;}

export function initPSD({fetchIndex,selectCandidate}){
  let candidates=null,loading=null,limit=50,busy=false;
  function setYears(){
    const old=$('psd-year').value;
    const years=[...new Set(candidates.filter(c=>c.cargo===$('psd-cargo').value).map(c=>c.year))].sort((a,b)=>b-a);
    $('psd-year').replaceChildren(...years.map(y=>option(y,y)));
    if(years.some(y=>String(y)===old))$('psd-year').value=old;
    $('psd-municipality-label').hidden=$('psd-cargo').value!=='13';
  }
  function render(){
    if(!candidates)return;
    const cargo=$('psd-cargo').value,year=$('psd-year').value,municipality=$('psd-municipality').value,q=norm($('psd-search').value);
    const matches=candidates.map((c,i)=>({...c,index:i})).filter(c=>c.cargo===cargo&&String(c.year)===year&&(cargo!=='13'||!municipality||c.ue===municipality)&&(!q||norm(`${c.name} ${c.full_name} ${c.number}`).includes(q))).sort((a,b)=>b.votes-a.votes||a.name.localeCompare(b.name));
    const shown=matches.slice(0,limit);
    $('psd-status').textContent=matches.length?`${nf.format(matches.length)} candidaturas · ${year} · PSD. Mostrando ${nf.format(shown.length)}.`:'Nenhuma candidatura do PSD encontrada nesta seleção.';
    $('psd-results').innerHTML=shown.map(c=>`<article class="psd-candidate"><div><strong>${esc(c.name)}</strong><p>${esc(c.cargo_name)} · ${c.year} · ${esc(c.ue_name)}</p><p>PSD · ${esc(c.number)} · ${nf.format(c.votes)} votos nominais válidos ${c.cargo==='13'?'no município':'no RJ'}</p></div><div class="psd-actions"><button class="secondary" data-psd-index="${c.index}" data-psd-side="a" aria-label="Usar ${esc(c.name)}, ${c.year}, ${esc(c.cargo_name)} como referência">Usar como referência</button><button class="secondary" data-psd-index="${c.index}" data-psd-side="b" aria-label="Comparar com ${esc(c.name)}, ${c.year}, ${esc(c.cargo_name)}">Usar como comparado</button></div></article>`).join('');
    $('psd-more').hidden=matches.length<=limit;
  }
  $('open-psd').addEventListener('click',async()=>{
    const panel=$('psd-panel');panel.hidden=!panel.hidden;$('open-psd').setAttribute('aria-expanded',String(!panel.hidden));
    if(panel.hidden)return;
    try{
      if(!candidates){
        loading??=fetchIndex().catch(e=>{loading=null;throw e;});
        candidates=(await loading).candidates;
        const municipalities=[...new Map(candidates.filter(c=>c.cargo==='13').map(c=>[c.ue,c.ue_name])).entries()].sort((a,b)=>a[1].localeCompare(b[1]));
        $('psd-municipality').replaceChildren(option('','Todos os municípios'),...municipalities.map(([code,name])=>option(code,name)));
        $('psd-municipality').value='60011';setYears();render();
      }
    }catch(e){$('psd-status').textContent=`Não foi possível carregar a lista: ${e.message}. Feche e abra para tentar novamente.`;}
  });
  $('close-psd').addEventListener('click',()=>{$('psd-panel').hidden=true;$('open-psd').setAttribute('aria-expanded','false');$('open-psd').focus();});
  $('psd-cargo').addEventListener('change',()=>{if(candidates){setYears();limit=50;render();}});
  for(const id of ['psd-year','psd-municipality'])$(id).addEventListener('change',()=>{limit=50;render();});
  $('psd-search').addEventListener('input',()=>{limit=50;render();});
  $('psd-more').addEventListener('click',()=>{limit+=50;render();});
  $('psd-results').addEventListener('click',async event=>{
    const button=event.target.closest('button[data-psd-index]');if(!button||busy)return;
    busy=true;const candidate=candidates[Number(button.dataset.psdIndex)];
    $('psd-results').querySelectorAll('button').forEach(b=>b.disabled=true);
    try{await selectCandidate(button.dataset.psdSide,candidate);$('psd-panel').hidden=true;$('open-psd').setAttribute('aria-expanded','false');$('open-psd').focus();}
    catch(e){$('psd-status').textContent=`Não foi possível selecionar a candidatura: ${e.message}.`;}finally{busy=false;$('psd-results').querySelectorAll('button').forEach(b=>b.disabled=false);}
  });
}
