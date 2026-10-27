/* Shared presentation catalog. Never translate user content or API enum values. */
(() => {
  const dictionary = window.__I18N__ || {};
  const t = (source, values = {}) => {
    const text = Object.hasOwn(dictionary, source)
      ? dictionary[source]
      : source;
    return String(text ?? "").replace(/\{(\w+)\}/g, (all, key) =>
      Object.hasOwn(values, key) ? String(values[key]) : all,
    );
  };
  // Only system messages use these patterns. Never pass CV text or job descriptions here.
  const patterns = (window.__UI_PATTERNS__ || []).map(source => {
    let expression = '^', cursor = 0;
    const escape = value => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    for (const match of source.matchAll(/\{(\w+)\}/g)) {
      expression += escape(source.slice(cursor, match.index));
      expression += `(?<${match[1]}>${['n','total'].includes(match[1]) ? '[\\d.,]+' : '.+'})`;
      cursor = match.index + match[0].length;
    }
    return {source, regex: new RegExp(expression + escape(source.slice(cursor)) + '$')};
  });
  const ui = source => {
    source = String(source ?? '');
    if (Object.hasOwn(dictionary, source)) return t(source);
    for (const pattern of patterns) {
      const match = source.match(pattern.regex);
      if (match) {
        if (pattern.source === '{n}/{total} faqe punëdhënësi dështuan: {value}') match.groups.value = ui(match.groups.value);
        return t(pattern.source, match.groups);
      }
    }
    return source.includes(' · ') ? source.split(' · ').map(ui).join(' · ') : source;
  };
  const sourceLabel = id => Object.hasOwn(window.__SOURCE_LABELS__ || {}, id) ? window.__SOURCE_LABELS__[id] : t(id);
  window.OEI18n = { t, ui, sourceLabel };
  const form = document.getElementById("language-form");
  form?.addEventListener("submit", (e) => {
    e.preventDefault();
    const url = new URL(location.href);
    url.searchParams.set("lang", form.elements.lang.value);
    if (form.elements.cv_language) url.searchParams.set('cv_language', form.elements.cv_language.value);
    const cvForm = document.getElementById('resume-options');
    if (cvForm?.id === 'resume-options') {
      for (const key of ['cv_language','skill_limit','configured','projects','photo','personal']) url.searchParams.delete(key);
      for (const [key,value] of new FormData(cvForm)) url.searchParams.set(key,value);
    }
    // A flash belongs to the previous action and may already be translated.
    url.searchParams.delete('msg');
    // Keep the displayed selection honest if an unsaved-edit guard cancels navigation.
    form.elements.lang.value = document.documentElement.lang;
    if (window.OE?.hasPendingEdits()) {
      window.OE.toast(
        t("Ruaj ndryshimet përpara se të ndryshosh gjuhën."),
        5000,
      );
      return;
    }
    location.assign(url.href);
  });
})();
