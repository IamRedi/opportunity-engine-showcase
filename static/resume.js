/* Language editions are export documents, never profile edits. */
(() => {
  const form=document.querySelector('#resume-options');if(!form)return;
  const {fetch}=window.OE,{t:tr,ui}=window.OEI18n;
  const status=document.querySelector('#resume-status'),download=document.querySelector('#resume-download');
  const cancel=document.querySelector('#resume-cancel');let busy=false,activeJob='';
  form.addEventListener('change',()=>{download.hidden=true;status.textContent=tr('Zgjedhjet ndryshuan. Gjenero CV-në përpara shkarkimit.');});
  form.addEventListener('submit',async e=>{
    e.preventDefault();if(busy)return;busy=true;
    const fields=new FormData(form),button=e.submitter;button.disabled=true;download.hidden=true;
    status.textContent=tr('AI po shkruan CV-në dhe po kontrollon gjuhën e faktet…');
    const body={language:fields.get('cv_language'),skill_limit:Number(fields.get('skill_limit')),
      include_projects:fields.has('projects'),include_personal:fields.has('personal'),include_photo:fields.has('photo')};
    try{
      const d=await window.OEWork.run('resume_generation',body,job=>{activeJob=job.job_id||activeJob;cancel.hidden=window.OEWork.terminal.has(job.status);status.textContent=tr('AI po shkruan CV-në dhe po kontrollon gjuhën e faktet…')+' '+(job.progress_current||0)+'/'+(job.progress_total||0);});
      if(d.status!=='succeeded')throw new Error(d.message||tr(d.status==='superseded'?'Profili ndryshoi. Gjenero CV-në përsëri.':'Veprimi dështoi. Provo përsëri.'));
      form.dataset.unsaved='false';const query=new URLSearchParams(fields);query.set('lang',document.documentElement.lang);
      location.assign('/profile/resume?'+query.toString());
    }catch(error){status.textContent=error.message;}finally{busy=false;button.disabled=false;cancel.hidden=true;}
  });
  cancel.addEventListener('click',async()=>{if(!activeJob)return;cancel.disabled=true;try{const job=await window.OEWork.cancel(activeJob);status.textContent=tr(job.status==='cancelled'?'Puna u anulua.':'Anulimi u kërkua.');}catch(error){status.textContent=error.message;}finally{cancel.disabled=false;}});
})();
