/* Durable work client: creation, resumable polling, cancel and retry. */
(() => {
  const terminal = new Set(['succeeded','failed','cancelled','superseded']);
  const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
  async function json(url, options={}) {
    const response = await window.OE.fetch(url, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(window.OEI18n.ui(data.error) || window.OEI18n.t('Veprimi dështoi. Provo përsëri.'));
    return data;
  }
  async function wait(job, onUpdate=()=>{}) {
    let current=job, pause=1000;
    while (true) {
      current=await json(current.progress_url || `/api/work/${current.job_id}`);
      onUpdate(current);
      if (terminal.has(current.status)) return current;
      await delay(pause);pause=Math.min(5000,Math.round(pause*1.5));
    }
  }
  async function run(type,input,onUpdate=()=>{}) {
    const job=await json('/api/work',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({type,input})});
    onUpdate(job);return wait(job,onUpdate);
  }
  async function cancel(jobId) {return json(`/api/work/${jobId}/cancel`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});}
  async function retry(jobId,onUpdate=()=>{}) {const job=await json(`/api/work/${jobId}/retry`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});onUpdate(job);return wait(job,onUpdate);}
  window.OEWork={run,wait,cancel,retry,terminal};
})();
