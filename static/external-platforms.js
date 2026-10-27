/* External links only: no provider requests, profile upload or result ingestion. */
(() => {
  const supported = (card, kind, country) =>
    card.kinds.includes(kind) && (!card.countries.length || card.countries.includes(country));
  function searchURL(site, fields, terms, language) {
    const german = ['de', 'at', 'ch'].includes(fields.country);
    const suffix = (german ? {
      job: 'Stellenangebot', local_job: '(Minijob OR Teilzeit OR Nebenjob OR gesucht)',
      freelance: '(Projekt OR Auftrag)', product_lead: '(Ausschreibung OR Auftrag OR Projekt)'
    } : {
      job: 'job vacancy', local_job: '("part time" OR job OR wanted)',
      freelance: 'freelance project', product_lead: '("request for proposal" OR contract OR project)'
    })[fields.kind];
    const categories = fields.kind === 'local_job' ? (fields.categories || [])
      .slice(0, 3).filter(id => Object.hasOwn(terms, id))
      .map(id => '"' + terms[id][german ? 0 : 1] + '"') : [];
    const categoryQuery = categories.length ? '(' + categories.join(' OR ') + ')' : '';
    const url = new URL('https://www.google.com/search');
    url.searchParams.set('q', [site ? 'site:' + site : '', (fields.what || '').trim().slice(0,250),
      categoryQuery, (fields.where || '').trim().slice(0,150),
      new Intl.DisplayNames([german ? 'de' : 'en'], {type:'region'}).of(fields.country.toUpperCase()), suffix].filter(Boolean).join(' '));
    url.searchParams.set('gl', fields.country);
    url.searchParams.set('hl', ['sq','en','de'].includes(language) ? language : 'en');
    return url.href;
  }
  window.OEPlatforms = {supported, searchURL};
  const form = document.querySelector('#discovery-form');
  if (!form) return;
  const terms = JSON.parse(document.querySelector('#external-search-terms').textContent);
  function update() {
    const fields = Object.fromEntries(new FormData(form));
    fields.categories = new FormData(form).getAll('categories');
    document.querySelector('#external-web').href = searchURL('', fields, terms, document.documentElement.lang);
    document.querySelectorAll('[data-platform-kinds]').forEach(card => {
      card.hidden = !supported({kinds:card.dataset.platformKinds.split(' '),
        countries:card.dataset.platformCountries.split(' ').filter(Boolean)}, fields.kind, fields.country);
      const link = card.querySelector('.external-google');
      if (link) link.href = searchURL(link.dataset.site, fields, terms, document.documentElement.lang);
    });
  }
  form.addEventListener('input', update);
  form.addEventListener('change', update);
  update();
})();
