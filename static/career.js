/* Career profile: evidence-backed search concepts and private portraits. */
(() => {
  if (!document.querySelector('#pamja')) return;
  const {fetch,esc,toast,hasPendingEdits} = window.OE;
  const {t:tr,ui} = window.OEI18n;
  const $ = s => document.querySelector(s);
  const aiAvailable=!$('#enrich-profile').disabled;
  const locale = document.documentElement.lang || 'sq';
  let photoCvId = null, changedOverview = false, analyzing = false;
  $('#edit-portrait').addEventListener('click',()=>$('#portrait-dialog').showModal());
  $('#close-portrait').addEventListener('click',()=>$('#portrait-dialog').close());
  const local = values => values?.[locale] || values?.en || '';
  async function request(url, method='GET', data) {
    const options = {method};
    if (data instanceof FormData) options.body=data;
    else if (data !== undefined) { options.headers={'Content-Type':'application/json'}; options.body=JSON.stringify(data); }
    const r = await fetch(url,options), d=await r.json();
    if (!r.ok || d.ok === false) throw new Error(ui(d.error || d.reason || 'Veprimi dështoi. Provo përsëri.'));
    return d;
  }
  function clean() {
    if (!hasPendingEdits()) return true;
    toast(tr('Ruaj ndryshimet e profilit përpara përpunimit të CV-së.'));
    return false;
  }
  async function concepts() {
    const host=$('#career-concepts');
    try {
      const d=await request('/api/profile/intelligence');
      $('#refresh-intelligence').disabled=!aiAvailable||analyzing||d.analysis_current;
      $('#enrich-profile').disabled=!aiAvailable||analyzing||d.enrichment_current;
      if(d.enrichment_current) $('#enrich-status').textContent=tr('Profili është i përditësuar. Nuk nevojitet përpunim tjetër me AI.');
      else if($('#enrich-status').textContent===tr('Profili është i përditësuar. Nuk nevojitet përpunim tjetër me AI.')) $('#enrich-status').textContent='';
      const labels={role:tr('Drejtime pune'),skill:tr('Aftësi të transferueshme'),tool:tr('Mjete dhe teknologji'),domain:tr('Fusha profesionale'),task:tr('Çfarë di të bësh')};
      host.innerHTML=(d.stale ? `<p class="intelligence-stale">${esc(tr('Profili ka ndryshuar. Rifresko lidhjet; deri atëherë nuk ndikojnë në përputhje.'))}</p>` : '') + (d.concepts.length ? Object.entries(labels).map(([category,label]) => {
        const items=d.concepts.filter(c=>c.category===category);
        if(!items.length) return '';
        return `<div class="concept-group"><h4>${esc(label)}</h4><div class="concept-grid">${items.map(c=>`<article class="concept-card ${c.status==='dismissed'?'concept-dismissed':''}"><span class="concept-state">${esc(tr(c.status==='confirmed'?'Prioritet për kërkim':c.status==='dismissed'?'E çaktivizuar':'Lidhje nga AI · aktive'))}</span>${c.adjacent?`<span class="adjacent-badge">${esc(tr('Jashtë drejtimit kryesor'))}</span>`:''}<h5>${esc(local(c.labels))}</h5><p>${esc(local(c.rationale))}</p>${c.basis==='interest'?`<p class="sub">${esc(tr('Bazuar në interes; përvoja duhet verifikuar.'))}</p>`:''}${local(c.checks)?`<p class="connection-check">${esc(tr('Për t’u verifikuar'))}: ${esc(local(c.checks))}</p>`:''}<details><summary>${esc(tr('Pse lidhet me mua?'))}</summary>${c.evidence.map(q=>`<blockquote>${esc(q)}</blockquote>`).join('')}<div class="concept-terms">${c.terms.slice(0,8).map(t=>`<span>${esc(t)}</span>`).join('')}</div></details><div class="concept-actions"><button class="linklike" type="button" data-concept="${esc(c.id)}" data-status="${c.status==='dismissed'?'active':c.status==='confirmed'?'active':'confirmed'}">${esc(tr(c.status==='dismissed'?'Riaktivizo':c.status==='confirmed'?'Hiq përparësinë':'Jep përparësi'))}</button>${c.status!=='dismissed'?`<button class="linklike" type="button" data-concept="${esc(c.id)}" data-status="dismissed">${esc(tr('Nuk më përshtatet'))}</button>`:''}</div></article>`).join('')}</div></div>`;
      }).join('') : `<div class="intelligence-empty"><span aria-hidden="true">✧</span><p>${esc(tr('Lidhjet e tua profesionale shfaqen këtu pas analizës me AI.'))}</p></div>`);
      $('#discover-profile').disabled=!d.concepts.some(c=>c.category==='role'&&c.status!=='dismissed');
    } catch(e) {host.innerHTML=`<p class="form-error">${esc(e.message)}</p>`;}
  }
  concepts();
  document.addEventListener('click',async e=>{
    const b=e.target.closest('[data-concept]'); if(!b) return;
    b.disabled=true;
    try {await request('/api/profile/intelligence/'+b.dataset.concept,'POST',{status:b.dataset.status}); await concepts(); toast(tr('Lidhjet e kërkimit u përditësuan.'));}
    catch(error){toast(error.message);}finally{b.disabled=false;}
  });
  async function analyze(enrich) {
    if(analyzing||!clean()) return;
    analyzing=true;
    const buttons=[$('#enrich-profile'),$('#refresh-intelligence')];buttons.forEach(b=>b.disabled=true);
    const status=$('#enrich-status');status.textContent=tr('AI po lexon përvojën dhe po ndërton lidhjet…');
    try {
      const d=enrich?await request('/api/profile/enrich','POST'):await window.OEWork.run('profile_intelligence',{language:locale},job=>{status.textContent=tr('AI po lexon përvojën dhe po ndërton lidhjet…')+' '+(job.progress_current||0)+'/'+(job.progress_total||0);});
      if(!enrich&&d.status!=='succeeded')throw new Error(d.message||tr(d.status==='superseded'?'Profili ndryshoi. Rifresko lidhjet përsëri.':'Analiza e profilit dështoi. Provo përsëri.'));
      if(enrich&&!d.cached){ location.assign('/profile?msg='+encodeURIComponent(tr('Profili u pasurua. Rishiko faktet dhe lidhjet e reja.'))+'#pamja'); return; }
      await concepts();status.textContent=tr('Lidhjet e kërkimit u përditësuan.');
      if(d.cached)status.textContent=tr('Të dhënat nuk kanë ndryshuar. U përdor rezultati i ruajtur, pa kërkesë të re te AI.');
    } catch(error){status.textContent=error.message;}
    finally{analyzing=false;buttons.forEach(b=>b.disabled=false);await concepts();}
  }
  $('#enrich-profile').addEventListener('click',()=>analyze(true));
  $('#refresh-intelligence').addEventListener('click',()=>analyze(false));
  $('#discover-profile').addEventListener('click',async e=>{
    const b=e.currentTarget;b.disabled=true;const status=$('#discovery-status');status.textContent=tr('Po kërkohen oferta sipas drejtimeve të tua…');
    try{
      const d=await window.OEWork.run('profile_discovery',{profile_based:true,language:locale},job=>{status.textContent=tr('Po kërkohen oferta sipas drejtimeve të tua…')+' '+(job.progress_current||0)+'/'+(job.progress_total||0);});
      if(d.status!=='succeeded')throw new Error(d.message||tr(d.status==='superseded'?'Profili ndryshoi. Nis kërkimin përsëri me të dhënat e reja.':'Kërkimi dështoi'));
      status.textContent=tr('U shtuan {count} oferta të reja.',{count:d.result.new||0});
    }catch(error){status.textContent=error.message;}finally{b.disabled=false;}
  });
  function portrait(url) {
    document.querySelectorAll('.orbit-face,.hero-portrait').forEach(host=>{
      host.querySelector('.portrait-initials')?.remove();host.querySelector('img')?.remove();
      if(url){const img=document.createElement('img');img.className='js-portrait';img.src=url;img.alt=tr('Fotoja e profilit');host.prepend(img);}
      else{const span=document.createElement('span');span.className='portrait-initials';span.textContent=$('.orbit-name').textContent.trim().slice(0,1)||'?';host.prepend(span);}
    });
  }
  $('#portrait-form').addEventListener('submit',async e=>{
    e.preventDefault();const b=e.submitter;b.disabled=true;
    try{const d=await request('/api/profile/photo','POST',new FormData(e.target));portrait(d.url);e.target.reset();$('#portrait-dialog').close();toast(tr('Fotoja u ruajt.'));}
    catch(error){toast(error.message);}finally{b.disabled=false;}
  });
  $('#remove-portrait').addEventListener('click',async()=>{
    try{await request('/api/profile/photo','DELETE');portrait(null);toast(tr('Fotoja u hoq.'));}catch(error){toast(error.message);}
  });
  $('#cv-photos').addEventListener('click',async e=>{
    const b=e.currentTarget;b.disabled=true;const host=$('#portrait-candidates');host.textContent=tr('Po kërkohen imazhet…');
    try{const d=await request('/api/profile/photo/candidates');photoCvId=d.cv_id;host.innerHTML=d.items.length?`<p class="sub">${esc(tr('Zgjidh foton tënde. Imazhet e tjera mund të jenë logo ose dekorime.'))}</p><div class="photo-options">${d.items.map((p,i)=>`<button type="button" data-photo="${p.index}" aria-label="${esc(tr('Përdor foton'))} ${i+1}"><img src="${esc(p.url)}" alt="${esc(tr('Imazh nga CV-ja'))} ${i+1}"></button>`).join('')}</div>`:`<p class="sub">${esc(tr('Nuk u gjet foto e veçuar në CV. Ngarko një foto personale.'))}</p>`;}
    catch(error){host.textContent=error.message;}finally{b.disabled=false;}
  });
  $('#portrait-candidates').addEventListener('click',async e=>{
    const b=e.target.closest('[data-photo]');if(!b)return;b.disabled=true;
    try{const d=await request('/api/profile/photo/from-cv','POST',{index:Number(b.dataset.photo),cv_id:photoCvId});portrait(d.url);$('#portrait-dialog').close();toast(tr('Fotoja u ruajt.'));}
    catch(error){toast(error.message);}finally{b.disabled=false;}
  });
  document.addEventListener('profile-saved',()=>{
    changedOverview=true;concepts();
    const ring=$('.profile-orbit');ring.classList.remove('celebrate');requestAnimationFrame(()=>ring.classList.add('celebrate'));
  });
  window.addEventListener('hashchange',()=>{if(changedOverview&&location.hash==='#pamja'&&!hasPendingEdits())location.reload();});
})();
