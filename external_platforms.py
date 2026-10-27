"""Reviewed external discovery destinations, not ingestion connectors.

Scope checked against official pages on 2026-09-13. Empty countries means
international availability, not a promise of results in every country.
"""

def entry(name, url, site, kinds, countries, note):
    return dict(name=name, url=url, site=site, kinds=kinds.split(),
                countries=countries.split(), note=note)


PLATFORMS = [
    entry('Kleinanzeigen', 'https://www.kleinanzeigen.de/s-jobs/c102',
          'kleinanzeigen.de/s-anzeige/', 'job local_job', 'de', 'Njoftime pune dhe minijob'),
    entry('Arbeitsagentur', 'https://www.arbeitsagentur.de/jobsuche/',
          'arbeitsagentur.de/jobsuche/', 'job local_job', 'de', 'Njoftime pune dhe minijob'),
    entry('Haushaltsjob-Börse', 'https://www.haushaltsjob-boerse.de/',
          'haushaltsjob-boerse.de', 'local_job', 'de', 'Shtëpi, kopsht, kafshë dhe ndihmë në përditshmëri'),
    entry('LinkedIn Jobs', 'https://www.linkedin.com/jobs/',
          'linkedin.com/jobs/view/', 'job freelance', '', 'Punë profesionale dhe kontrata'),
    entry('Indeed DE', 'https://de.indeed.com/',
          'de.indeed.com', 'job local_job', 'de', 'Njoftime pune dhe minijob'),
    entry('Stepstone DE', 'https://www.stepstone.de/',
          'stepstone.de', 'job local_job', 'de', 'Punë profesionale dhe kontrata'),
    entry('INTERAMT', 'https://interamt.de/',
          'interamt.de', 'job', 'de', 'Punësim në sektorin publik'),
    entry('service.bund.de', 'https://service.bund.de/',
          'service.bund.de', 'job product_lead', 'de', 'Punësim publik dhe tendera'),
    entry('freelancermap', 'https://www.freelancermap.de/projekte',
          'freelancermap.de/projekt/', 'freelance product_lead', 'de at ch', 'Projekte për profesionistë dhe agjenci'),
    entry('freelance.de', 'https://www.freelance.de/projekte/',
          'freelance.de', 'freelance product_lead', 'de at ch', 'Projekte për profesionistë dhe agjenci'),
    entry('Freelancer', 'https://www.freelancer.de/jobs/search',
          'freelancer.com/projects/', 'freelance product_lead', '', 'Projekte për profesionistë dhe agjenci'),
    entry('TED', 'https://ted.europa.eu/en/browse-by-business-opportunity',
          'ted.europa.eu', 'product_lead', '', 'Tendera publikë; kontrollo kushtet e pjesëmarrjes'),
    entry('jobs.ch', 'https://www.jobs.ch/en/',
          'jobs.ch', 'job local_job', 'ch', 'Punë profesionale dhe kontrata'),
    entry('karriere.at', 'https://www.karriere.at/',
          'karriere.at', 'job local_job', 'at', 'Punë profesionale dhe kontrata'),
    entry('Pawshake', 'https://www.pawshake.de/become-a-sitter',
          '', 'local_job', 'de', 'Ofrimi i shërbimeve të kujdesit për kafshë'),
    entry('Nebenan', 'https://dein.nebenan.de/lp/tierbetreuung',
          '', 'local_job', 'de', 'Ndihmë në lagje; jo çdo njoftim është punë me pagesë'),
]
