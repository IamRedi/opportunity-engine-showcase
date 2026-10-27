/* One reviewed profile, reused by onboarding, applications and discovery. */
(() => {
  const {fetch,esc,toast}=window.OE, {t:tr,ui}=window.OEI18n;
  const $=s=>document.querySelector(s);
  const locale=document.documentElement.lang||'sq';
  const local=v=>v?.[locale]||v?.en||'';
  async function request(url,body) {
    const r=await fetch(url,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const d=await r.json(); if(d.needs_confirmation)return d;if(!r.ok||d.ok===false)throw new Error(ui(d.reason||d.error||'Veprimi dështoi. Provo përsëri.'));return d;
  }
  const adjacent=$('#adjacent-opportunities');
  if(adjacent)request('/api/profile/adjacent-opportunities?mode='+encodeURIComponent(adjacent.dataset.mode||'jobs')).then(d=>{
    if(!d.items.length)return;
    document.getElementById('adjacent-count').textContent=d.items.length;
    document.getElementById('adjacent-grid').innerHTML=d.items.map(i=>`<article><span class="concept-state">${esc(tr('Një lidhje tjetër me profilin tënd'))}</span><h4><a href="/jobs/${Number(i.id)}" data-drawer="${Number(i.id)}">${esc(i.title)}</a></h4><p class="sub">${esc(i.location||'')} · ${Number(i.score)}%</p>${i.reasons.slice(0,1).map(reason=>`<p>${esc(local(reason.rationale))}</p>${reason.basis==='interest'?`<small>${esc(tr('Bazuar në interes; përvoja duhet verifikuar.'))}</small>`:''}`).join('')}</article>`).join('');
    adjacent.hidden=false;
  }).catch(()=>{});
  document.addEventListener('click',async e=>{
    const b=e.target.closest('.js-app-profile-learn');if(!b)return;
    if(b.closest('.appcard')?.dataset.dirty==='true'){toast(tr('Ruaj ndryshimet e tua në draft përpara kontrollit të profilit.'));return;}
    b.disabled=true;const text=b.textContent;b.textContent=tr('Po kontrollohen plotësimet…');
    try{await request(`/api/applications/${b.dataset.app}/profile-suggestions`,{});location.assign('/profile#nis');}
    catch(error){toast(error.message);}finally{b.disabled=false;b.textContent=text;}
  });
  if(!$('#nis'))return;
  const titles={contact:'Kontaktet',skills:'Aftësi & teknologji',languages:'Gjuhët',experience:'Përvoja e punës',education:'Edukimi',certifications:'Certifikimet',projects:'Projektet',portfolio:'Portfolio',interests:'Hobi & interesa',personal:'Të dhëna personale · opsionale',preferences:'Çfarë kërkon'};
  const fields={name:'Emri',title:'Titulli',email:'Email',phone:'Telefon',location:'Vendndodhja',summary:'Përmbledhje',level:'Niveli',org:'Organizata',period:'Periudha',description:'Përshkrimi',year:'Viti',details:'Detaje',url:'Lidhja',stack:'Teknologjitë',address:'Adresa',birth_date:'Datëlindja',age:'Mosha e deklaruar',nationality:'Shtetësia',desired_work:'Puna që kërkon',availability:'Disponueshmëria',travel:'Udhëtimi',salary:'Pagesa e dëshiruar',transport:'Transporti'};
  let preview=null,schemas={},defaults={},busy=false;
  const status=$('#intake-status'),review=$('#intake-review'),host=$('#intake-items');
  function confirmChanges(conflicts) {
    const dialog=$('#intake-confirm');
    $('#intake-conflicts').innerHTML=conflicts.map(c=>`<article><strong>${esc(tr(fields[c.field]||c.field))}</strong><p><span>${esc(tr('Aktualisht'))}</span>${esc(c.before)}</p><p><span>${esc(tr('Pas ndryshimit'))}</span>${esc(c.after)}</p></article>`).join('');
    return new Promise(resolve=>{dialog.returnValue='cancel';dialog.addEventListener('close',()=>resolve(dialog.returnValue==='save'),{once:true});dialog.showModal();});
  }
  function row(item) {
    const card=document.createElement('article');card.className='intake-item';card.dataset.section=item.section;
    if(item.id!==undefined)card.dataset.id=item.id;
    const selected=item.selected!==false;
    const current=Object.keys(item.fields).filter(k=>item.current?.[k]).map(k=>`${tr(fields[k]||k)}: ${item.current[k]}`).join(' · ');
    card.innerHTML=`<label class="intake-item-heading"><input class="intake-select" type="checkbox" ${selected?'checked':''}>${esc(tr(titles[item.section]))}</label>${current?`<p class="intake-existing">${esc(tr('Në profil aktualisht'))}: ${esc(current)}</p>`:''}<div class="intake-item-fields">${Object.entries(item.fields).map(([key,value])=>`<label>${esc(tr(fields[key]||key))}${['description','summary'].includes(key)?`<textarea data-field="${esc(key)}" rows="3" maxlength="4000" ${selected?'':'disabled'}>${esc(value)}</textarea>`:`<input data-field="${esc(key)}" value="${esc(value)}" maxlength="4000" ${selected?'':'disabled'}>`}</label>`).join('')}</div>${item.evidence&&item.evidence!=='account'?`<details class="intake-evidence"><summary>${esc(tr('Shiko tekstin burim'))}</summary><p>${esc(item.evidence)}</p></details>`:''}`;
    card.querySelector('.intake-select').addEventListener('change',e=>card.querySelectorAll('[data-field]').forEach(input=>input.disabled=!e.target.checked));
    return card;
  }
  function render(record) {
    preview=record;host.replaceChildren(...record.items.map(row));review.hidden=false;
    status.textContent=tr('Rishiko të dhënat. Fushat e papërzgjedhura nuk ruhen.');
  }
  request('/api/profile/intake').then(d=>{
    schemas=d.schemas;defaults=d.defaults;
    $('#intake-contact-fields').innerHTML=['name','email','phone','location'].map(key=>`<label>${esc(tr(fields[key]))}<input data-core="${key}" value="${esc(defaults[key]||'')}" maxlength="300"></label>`).join('');
    $('#intake-add-section').innerHTML=Object.entries(titles).map(([key,label])=>`<option value="${key}">${esc(tr(label))}</option>`).join('');
    if(d.preview)render(d.preview);
    if(!location.hash&&!defaults.name)location.hash='nis';
  }).catch(error=>status.textContent=error.message);
  $('#intake-photo').addEventListener('click',()=>$('#portrait-dialog').showModal());
  $('#intake-description-form').addEventListener('submit',async e=>{
    e.preventDefault();if(busy)return;busy=true;const b=e.submitter;b.disabled=true;
    status.textContent=tr('AI po organizon përshkrimin tënd…');
    try{const d=await request('/api/profile/intake/preview',{text:$('#intake-text').value});render(d.preview);if(d.warning)status.textContent+=' '+tr(d.warning);review.scrollIntoView({behavior:'smooth',block:'start'});}
    catch(error){status.textContent=error.message;}finally{busy=false;b.disabled=false;}
  });
  $('#intake-add').addEventListener('click',()=>{
    const section=$('#intake-add-section').value;
    const card=row({section,fields:Object.fromEntries((schemas[section]||[]).map(k=>[k,''])),selected:true});
    host.append(card);card.querySelector('input[data-field]')?.focus();
  });
  review.addEventListener('submit',async e=>{
    e.preventDefault();if(busy||!preview)return;busy=true;const b=e.submitter;b.disabled=true;
    const items=[...host.children].filter(card=>card.querySelector('.intake-select').checked).map(card=>({
      ...(card.dataset.id===undefined?{}:{id:card.dataset.id}),section:card.dataset.section,
      fields:Object.fromEntries([...card.querySelectorAll('[data-field]')].map(input=>[input.dataset.field,input.value]))}));
    const core=Object.fromEntries([...document.querySelectorAll('[data-core]')].filter(input=>input.value.trim()&&input.value!==String(defaults[input.dataset.core]||'')).map(input=>[input.dataset.core,input.value]));
    if(Object.keys(core).length)items.push({section:'contact',fields:core});
    status.textContent=tr('Po ruhet profili dhe po ndërtohen lidhjet…');
    try{let d=await request('/api/profile/intake/commit',{preview_id:preview.id,items});
      if(d.needs_confirmation){
        if(!await confirmChanges(d.conflicts)){status.textContent=tr('Ndryshimet nuk u ruajtën. Profili ekzistues mbetet siç ishte.');return;}
        d=await request('/api/profile/intake/commit',{preview_id:preview.id,items,allow_updates:true});
      }
      $('#intake-description-form').dataset.unsaved='false';review.dataset.unsaved='false';
      const message=tr('Profili u ruajt. Mund ta plotësosh kur të duash.')+(d.analysis?.ok?'':' '+tr('Rifresko lidhjet me AI'));
      location.assign('/profile?msg='+encodeURIComponent(message)+'#pamja');
    }catch(error){status.textContent=error.message;}finally{busy=false;b.disabled=false;}
  });
})();
