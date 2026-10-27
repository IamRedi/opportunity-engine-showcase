/* A stale tab must never save one person's data into another person's profile. */
(() => {
  const meta=document.querySelector('meta[name="pilot-actor"]');if(!meta)return;
  const actor=meta.content,originalFetch=window.fetch.bind(window);
  window.fetch=(url,options={})=>{
    const target=new URL(typeof url==='string'?url:url.url,location.href);
    if(target.origin===location.origin){const headers=new Headers(options.headers||{});headers.set('X-Pilot-Actor',actor);options={...options,headers};}
    return originalFetch(url,options);
  };
  function mark(form){if(!form.querySelector('input[name="_pilot_actor"]')){const input=document.createElement('input');input.type='hidden';input.name='_pilot_actor';input.value=actor;form.append(input);}}
  document.querySelectorAll('form[method="post"]').forEach(mark);
  document.addEventListener('submit',e=>{if(e.target.method==='post')mark(e.target);},true);
  document.querySelector('#pilot-user')?.addEventListener('change',async e=>{
    if(window.OE?.hasPendingEdits?.()&&!confirm(window.OEI18n.t('Ka ndryshime të paruajtura. Të largohesh?'))){e.target.value=actor;return;}
    const response=await window.fetch('/api/pilot/switch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user_id:Number(e.target.value)})});
    if(response.ok)location.assign('/profile');else {e.target.value=actor;window.OE.toast(window.OEI18n.t('Përdoruesi ndryshoi. Rifresko faqen përpara ruajtjes.'));}
  });
})();
