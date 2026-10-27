(() => {
  const form=document.querySelector('#discovery-form');if(!form)return;
  const {esc}=window.OE,{t,ui,sourceLabel}=window.OEI18n;
  const workStatuses={dispatch_pending:t('work_status_dispatch_pending'),queued:t('work_status_queued'),running:t('work_status_running'),succeeded:t('work_status_succeeded'),failed:t('work_status_failed'),cancelled:t('work_status_cancelled'),superseded:t('work_status_superseded'),pending:t('work_status_pending')};
  const workSteps={draft:t('work_step_draft'),validate:t('work_step_validate'),analyze:t('work_step_analyze'),summarize:t('work_step_summarize')};
  const progress=document.querySelector('#discovery-progress'),link=document.querySelector('#discovery-results');
  const cancel=document.querySelector('#discovery-cancel'),retry=document.querySelector('#discovery-retry');
  let busy=false,activeJob='',lastMode='job';
  function localMode(){const local=form.elements.kind.value==='local_job';document.querySelector('#local-discovery').hidden=!local;form.elements.product_id.closest('label').hidden=form.elements.kind.value!=='product_lead';form.elements.product_id.disabled=form.elements.kind.value!=='product_lead';document.querySelectorAll('[data-source-kinds]').forEach(row=>row.hidden=!row.dataset.sourceKinds.split(' ').includes(form.elements.kind.value));}
  form.elements.kind.addEventListener('change',localMode);localMode();
  form.elements.product_id.addEventListener('change',()=>{if(form.elements.product_id.value){form.elements.kind.value='product_lead';localMode();}});
  form.elements.country.addEventListener('change',()=>{form.elements.where.value='';});
  function targets(){const kind=form.elements.kind.value,country=form.elements.country.value;form.querySelectorAll('[data-target-kinds]').forEach(option=>{const countries=option.dataset.targetCountries.split(' ').filter(Boolean);option.disabled=option.hidden=!option.dataset.targetKinds.split(' ').includes(kind)||(countries.length>0&&!countries.includes(country));});if(form.elements.web_target.selectedOptions[0]?.disabled)form.elements.web_target.value='';}
  form.addEventListener('change',targets);targets();
  document.querySelectorAll('.js-select-platform').forEach(button=>button.addEventListener('click',()=>{if(busy)return;form.elements.web_target.value=button.dataset.site;targets();form.elements.web_target.focus();form.scrollIntoView({behavior:'smooth',block:'start'});form.requestSubmit(form.querySelector('button[type="submit"]'));}));
  function render(job){
    activeJob=job.job_id||activeJob;const terminal=window.OEWork.terminal.has(job.status);
    cancel.hidden=terminal;retry.hidden=job.status!=='failed';
    const current=job.progress_current||0,total=job.progress_total||0;
    let html=`<p><strong>${esc(t('Puna po vazhdon edhe nëse e mbyll këtë faqe.'))}</strong> ${current}/${total}</p>`;
    if(job.steps)html+=job.steps.map(step=>`<p>${step.status==='succeeded'?'✓':step.status==='running'?'→':step.status==='failed'?'!':'·'} ${esc(step.step_type.startsWith('source:')?sourceLabel(step.step_type.slice(7)):(workSteps[step.step_type]||step.step_type))} · ${esc(workStatuses[step.status]||step.status)}</p>`).join('');
    if(job.result?.runs?.length){html=job.result.runs.map(row=>`<p>${row.status==='ok'?'✓':'!'} ${esc(sourceLabel(row.source))} · ${row.discovered} ${esc(t('rezultate'))} · ${row.new_count} ${esc(t('Të reja'))}${row.error?' · '+esc(ui(row.error)):''}</p>`).join('');html+=`<strong>${job.result.discovered} ${esc(t('rezultate'))} · ${job.result.new} ${esc(t('Të reja'))} · ${job.result.duplicates} ${esc(t('Të ruajtura më parë'))}</strong>`;}
    if(job.message)html+=`<p class="form-error">${esc(job.message)}</p>`;
    if(job.status==='cancelled')html+=`<p>${esc(t('Puna u anulua.'))}</p>`;
    if(job.status==='superseded')html+=`<p>${esc(t('Profili ndryshoi. Nis kërkimin përsëri me të dhënat e reja.'))}</p>`;
    progress.innerHTML=html;
    if(job.status==='succeeded'){link.href='/?mode='+({job:'jobs',freelance:'freelance',local_job:'local',product_lead:'products'}[lastMode]);link.hidden=false;}
  }
  form.addEventListener('submit',async e=>{
    e.preventDefault();if(busy)return;busy=true;const button=e.submitter;button.disabled=true;link.hidden=true;retry.hidden=true;
    const fields=Object.fromEntries(new FormData(form));fields.categories=form.elements.kind.value==='local_job'?new FormData(form).getAll('categories'):[];fields.language=document.documentElement.lang;lastMode=fields.kind;
    try{const job=await window.OEWork.run('profile_discovery',fields,render);render(job);}
    catch(error){progress.textContent=error.message;}finally{busy=false;button.disabled=false;cancel.hidden=true;}
  });
  cancel.addEventListener('click',async()=>{if(!activeJob)return;cancel.disabled=true;try{render(await window.OEWork.cancel(activeJob));}catch(error){progress.textContent=error.message;}finally{cancel.disabled=false;}});
  retry.addEventListener('click',async()=>{if(!activeJob||busy)return;busy=true;retry.disabled=true;try{render(await window.OEWork.retry(activeJob,render));}catch(error){progress.textContent=error.message;}finally{busy=false;retry.disabled=false;}});
})();
