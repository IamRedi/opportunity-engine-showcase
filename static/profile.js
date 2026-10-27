/* Guided profile editors. Existing section objects retain extra fields/provenance. */
(() => {
  const { t: tr, ui } = window.OEI18n;
  const { fetch, esc, toast } = window.OE;
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const labels = {
    address: tr('Adresa'), birth_date: tr('Datëlindja'), age: tr('Mosha e deklaruar'), nationality: tr('Shtetësia'),
    desired_work: tr('Puna që kërkon'), availability: tr('Disponueshmëria'), travel: tr('Udhëtimi'), salary: tr('Pagesa e dëshiruar'), transport: tr('Transporti'),
    name: tr("Emri"),
    level: tr("Niveli"),
    title: tr("Titulli"),
    org: tr("Organizata"),
    period: tr("Periudha"),
    description: tr("Përshkrimi"),
    year: tr("Viti"),
    url: tr("Lidhja"),
    stack: tr("Teknologjitë"),
    details: tr("Detaje"),
  };
  const schemas = {
    interests: ['name','description'],
    personal: ['address','birth_date','age','nationality'],
    preferences: ['desired_work','availability','travel','salary','transport'],
    skills: ["name", "level"],
    languages: ["name", "level"],
    experience: ["title", "org", "period", "description"],
    education: ["title", "org", "period"],
    certifications: ["name", "org", "year", "details"],
    portfolio: ["title", "url", "description"],
    projects: ["title", "url", "description", "stack"],
  };
  const editors = new Map();
  const destinations = {
    roles: "profili",
    skills: "strukture",
    languages_level: "strukture",
    experience: "strukture",
    location_remote: "profili",
    salary: "preferencat",
    education: "strukture",
    certifications: "strukture",
    cv: "cv",
    products: "produkte",
    portfolio: "strukture",
    rate: "lokale",
    local_geo: "lokale",
    local_transport: "lokale",
    local_rate: "lokale",
    local_categories: "lokale",
    availability: "lokale",
    industries: "produkte",
    summary: "profili",
  };
  async function json(url, options) {
    const response = await fetch(url, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok)
      throw new Error(ui(data.error) || tr("Veprimi dështoi. Provo përsëri."));
    return data;
  }
  const write = (url, method, data) =>
    json(url, {
      method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
  $("#prepare-profile-language")?.addEventListener("click", async (event) => {
    const button = event.currentTarget;
    const status = $("#profile-language-status");
    button.disabled = true;
    status.textContent = tr("AI po përgatit profilin dhe po kontrollon gjuhën e faktet…");
    try {
      const result = await write("/api/profile/resume/generate", "POST", {
        language: button.dataset.language,
        skill_limit: 200,
        include_projects: true,
        include_personal: true,
        include_photo: false,
      });
      if (!result.ok)
        throw new Error(ui(result.reason) || tr("Veprimi dështoi. Provo përsëri."));
      location.reload();
    } catch (error) {
      status.textContent = error.message;
      button.disabled = false;
    }
  });
  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!confirm(form.dataset.confirm)) event.preventDefault();
    });
  });
  async function completion() {
    const host = $("#completion-content");
    if (!host) return;
    const mode = $("#completion-mode").value;
    try {
      const data = await json(`/api/profile/completeness?mode=${mode}`);
      if ($("#completion-mode").value !== mode) return;
      const ring = $(".profile-orbit");
      if (ring) {
        const previous = Number(ring.getAttribute("aria-valuenow"));
        ring.setAttribute("aria-valuenow", String(data.score));
        $(".orbit-value", ring).textContent = `${data.score}%`;
        requestAnimationFrame(() => $(".orbit-progress", ring).style.strokeDashoffset = String(100 - data.score));
        if (data.score > previous) {
          ring.classList.remove("celebrate");
          requestAnimationFrame(() => ring.classList.add("celebrate"));
        }
      }
      host.innerHTML = `<p class="sub">${data.missing.length ? tr("Hapat me më shumë vlerë:") : tr("Profili është plotësuar për këtë mënyrë.")}</p><ul>${data.missing
        .slice(0, 3)
        .map(
          (item) =>
            `<li><a href="#${destinations[item.key] || "profili"}">${esc(tr(item.label))} →</a></li>`,
        )
        .join("")}</ul>`;
    } catch {
      host.innerHTML = `<p class="form-error">${esc(tr("Plotësimi nuk u ngarkua."))}</p><button class="btn small js-retry-completion">${esc(tr("Provo përsëri"))}</button>`;
    }
  }
  $("#completion-mode")?.addEventListener("change", completion);
  document.addEventListener("click", (e) => {
    if (e.target.closest(".js-retry-completion")) completion();
  });
  completion();
  function dirty(editor) {
    editor.host.dataset.editorDirty = "true";
    $(".editor-state", editor.host).textContent = tr("Ndryshime të paruajtura");
  }
  function entryMarkup(section, entry, index) {
    const fields = schemas[section];
    return `<div class="entry-form" data-index="${index}"><span class="entry-provenance">${String(entry.source || "").startsWith("cv:") ? tr("Pranuar nga CV-ja") : tr("Të dhëna të profilit")} · ${index + 1}</span>${fields.map((key, i) => `<label>${labels[key]}${i === 0 && !["personal","preferences"].includes(section) ? " *" : ""}${key === "description" ? `<textarea name="${key}" rows="3">${esc(entry[key])}</textarea>` : `<input name="${key}" value="${esc(Array.isArray(entry[key]) ? entry[key].join(", ") : entry[key])}" ${i === 0 && !["personal","preferences"].includes(section) ? "required" : ""}>`}</label>`).join("")}<button class="linklike entry-remove" type="button" aria-label="${esc(tr("Hiq zërin"))} ${index + 1}">${esc(tr("Hiq zërin"))}</button></div>`;
  }
  function collect(editor) {
    return $$(".entry-form", editor.host).map((row) => {
      const previous = editor.entries[Number(row.dataset.index)] || {};
      const entry = { ...previous };
      $$("input,textarea", row).forEach((input) => {
        entry[input.name] =
          input.value ===
          (Array.isArray(previous[input.name])
            ? previous[input.name].join(", ")
            : String(previous[input.name] ?? ""))
            ? previous[input.name]
            : Array.isArray(previous[input.name])
              ? input.value
                  .split(",")
                  .map((s) => s.trim())
                  .filter(Boolean)
              : input.value;
      });
      return entry;
    });
  }
  function renderEditor(editor) {
    const section = editor.host.dataset.section;
    $(".section-count", editor.host).textContent = tr(
      editor.entries.length === 1 ? "{p0} zë" : "{p0} zëra",
      {
        p0: editor.entries.length,
      },
    );
    $(".editor-content", editor.host).innerHTML =
      `<form class="structured-form"><div class="entries">${editor.entries.map((entry, i) => entryMarkup(section, entry, i)).join("")}</div><div class="editor-actions"><button class="btn small entry-add" type="button">${esc(tr("+ Shto një zë"))}</button><button class="btn small primary" type="submit">${esc(tr("Ruaj ndryshimet"))}</button></div><p class="editor-state" role="status">${editor.entries.length ? tr("Të dhënat e ruajtura") : tr("Ende bosh. Shto zërin e parë.")}</p></form>`;
  }
  async function loadEditor(host) {
    if (host.dataset.editorDirty === "true") return;
    try {
      const data = await json(`/api/profile/sections/${host.dataset.section}`);
      if (host.dataset.editorDirty === "true") return;
      const editor = {
        host,
        entries: data.data,
        original: JSON.stringify(data.data),
      };
      editors.set(host.dataset.section, editor);
      renderEditor(editor);
    } catch {
      $(".editor-content", host).innerHTML =
        `<p class="form-error">${esc(tr("Të dhënat nuk u ngarkuan. Provo përsëri përpara ndryshimit."))}</p><button class="btn small editor-retry" type="button">${esc(tr("Provo përsëri"))}</button>`;
    }
  }
  $$(".section-editor").forEach(loadEditor);
  document.addEventListener("input", (e) => {
    const host = e.target.closest(".section-editor");
    if (host && editors.has(host.dataset.section))
      dirty(editors.get(host.dataset.section));
  });
  document.addEventListener("click", (e) => {
    const host = e.target.closest(".section-editor");
    if (!host) return;
    if (e.target.closest(".editor-retry")) {
      loadEditor(host);
      return;
    }
    const editor = editors.get(host.dataset.section);
    if (!editor) return;
    if (e.target.closest(".entry-add")) {
      editor.entries = collect(editor);
      editor.entries.push({});
      renderEditor(editor);
      dirty(editor);
      $(".entry-form:last-child input", host)?.focus();
    }
    if (e.target.closest(".entry-remove")) {
      const row = e.target.closest(".entry-form");
      if (
        $$("input,textarea", row).some((input) => input.value) &&
        !confirm(tr("Të hiqet ky zë? Ndryshimi zbatohet kur ruan seksionin."))
      )
        return;
      row.remove();
      editor.entries = collect(editor);
      renderEditor(editor);
      dirty(editor);
      $(".entry-add", host)?.focus();
    }
  });
  document.addEventListener("submit", async (e) => {
    if (!e.target.matches(".structured-form")) return;
    e.preventDefault();
    const host = e.target.closest(".section-editor"),
      editor = editors.get(host.dataset.section);
    const button = e.submitter;
    button.disabled = true;
    try {
      // Refuse to overwrite CV merges or changes made in another tab since load.
      const current = await json(
        `/api/profile/sections/${host.dataset.section}`,
      );
      if (JSON.stringify(current.data) !== editor.original)
        throw new Error(
          tr(
            "Ky seksion ka ndryshuar nga një CV ose një faqe tjetër. Kopjo ndryshimet e tua dhe rifresko përpara ruajtjes.",
          ),
        );
      const entries = collect(editor);
      await write(`/api/profile/sections/${host.dataset.section}`, "PUT", {
        entries,
      });
      editor.entries = entries;
      editor.original = JSON.stringify(entries);
      host.dataset.editorDirty = "false";
      $(".editor-state", host).textContent = tr("U ruajt ✓");
      $(".section-count", host).textContent = tr(
        entries.length === 1 ? "{p0} zë" : "{p0} zëra",
        {
          p0: entries.length,
        },
      );
      toast(tr("Seksioni u ruajt"));
      document.dispatchEvent(new Event('profile-saved'));
      completion();
    } catch (error) {
      $(".editor-state", host).textContent = error.message;
      toast(error.message, 6000);
    } finally {
      button.disabled = false;
    }
  });
  async function suggestions() {
    const host = $("#profile-suggestions");
    if (!host) return;
    try {
      const data = await json("/api/profile/suggestions");
      host.innerHTML = data.length
        ? data
            .map(
              (item) =>
                `<div class="suggestion-item"><strong>${esc(item.value)}</strong><p class="sub">${esc(ui(item.reason))}</p><a href="#strukture">${esc(tr("Rishiko aftësitë e tua →"))}</a></div>`,
            )
            .join("")
        : `<p class="sub">${esc(tr("Nuk ka sugjerime të tjera për momentin."))}</p>`;
    } catch {
      host.textContent = tr("Sugjerimet nuk u ngarkuan.");
    }
  }
  suggestions();
  document.addEventListener("profile-updated", () => {
    completion();
    suggestions();
    $$(".section-editor").forEach(loadEditor);
    // Accepted CV facts must not leave stale blank fields ready to overwrite
    // them on the next form save. Preserve any field the user is editing.
    json('/api/profile/context').then(data => {
      const form = document.querySelector('form[action="/profile"]');
      const values = {...data.core, summary:data.summary, experience:data.experience_text};
      for (const [key,value] of Object.entries(values)) {
        const input=form?.elements.namedItem(key);
        if (input && input.value === input.defaultValue) {
          input.value=value || ''; input.defaultValue=input.value;
        }
      }
      document.dispatchEvent(new Event('profile-saved'));
    }).catch(() => {});
  });
  const profileNav = $(".profile-nav nav");
  let mobileProfile;
  if (profileNav) {
    mobileProfile = document.createElement("select");
    mobileProfile.className = "mobile-profile-nav";
    mobileProfile.setAttribute("aria-label", tr("Seksioni i profilit"));
    mobileProfile.innerHTML = $$("a", profileNav)
      .map(
        (link) =>
          `<option value="${esc(link.hash)}">${esc(link.textContent)}</option>`,
      )
      .join("");
    profileNav.after(mobileProfile);
    mobileProfile.addEventListener("change", () => {
      location.hash = mobileProfile.value;
    });
  }
  function showSection() {
    const sections = $$(".profile-content>.psect");
    if (!sections.length) return;
    const key = location.hash.slice(1) || "pamja";
    const target = sections.find((s) => s.id === key) || sections[0];
    sections.forEach((section) => (section.hidden = section !== target));
    if (mobileProfile) mobileProfile.value = `#${target.id}`;
    $$(".profile-nav nav a").forEach((link) => {
      if (link.hash === `#${target.id}`)
        link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
  }
  window.addEventListener("hashchange", showSection);
  showSection();
  const gapForm = $("#gap-form");
  let gaps = [];
  let editingGap = null;
  const categories = {
    missing_skill: tr("Aftësi për të zhvilluar"),
    certification_missing: tr("Certifikim"),
    language_gap: tr("Gjuha"),
    experience_gap: tr("Përvoja"),
    education_gap: tr("Edukimi"),
    licence_missing: tr("Licenca"),
    availability_constraint: tr("Disponueshmëria"),
    travel_constraint: tr("Transporti"),
    work_mode_constraint: tr("Mënyra e punës"),
    other: tr("Tjetër"),
  };
  if (gapForm)
    $("[name=category]", gapForm).innerHTML = Object.entries(categories)
      .map(([key, label]) => `<option value="${key}">${label}</option>`)
      .join("");
  async function loadGaps() {
    if (!gapForm) return;
    try {
      gaps = await json("/api/profile/gaps");
      $("#gap-list").innerHTML = gaps.length
        ? gaps
            .map(
              (g) =>
                `<div class="gap-card" data-gap="${g.id}"><div class="gap-head"><strong>${esc(g.label)}</strong><span class="tchip miss">${g.severity === "hard" ? tr("Kusht") : g.severity === "soft" ? tr("Preferencë") : tr("Informacion")}</span></div><p>${esc(g.current_state)}${g.target_state ? ` → ${esc(g.target_state)}` : ""}</p><p>${esc(g.notes)}</p><div class="inline-actions"><button class="linklike gap-edit" type="button" data-id="${g.id}">${esc(tr("Ndrysho"))}</button><button class="linklike gap-delete" type="button" data-id="${g.id}">${esc(tr("Fshi"))}</button></div></div>`,
            )
            .join("")
        : `<p class="sub">${esc(tr("Ende asnjë kufizim i deklaruar."))}</p>`;
    } catch {
      $("#gap-list").innerHTML =
        `<p class="form-error">${esc(tr("Kufizimet nuk u ngarkuan."))}</p>`;
    }
  }
  loadGaps();
  gapForm?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const b = e.submitter;
    b.disabled = true;
    try {
      await write(
        editingGap ? `/api/profile/gaps/${editingGap}` : "/api/profile/gaps",
        editingGap ? "PATCH" : "POST",
        Object.fromEntries(new FormData(gapForm)),
      );
      editingGap = null;
      gapForm.dataset.unsaved = "false";
      gapForm.reset();
      b.textContent = tr("Shto kufizimin");
      await loadGaps();
      toast(tr("Kufizimi u ruajt"));
    } catch (error) {
      toast(error.message);
    } finally {
      b.disabled = false;
    }
  });
  document.addEventListener("click", async (e) => {
    const edit = e.target.closest(".gap-edit");
    if (edit) {
      const gap = gaps.find((g) => String(g.id) === edit.dataset.id);
      editingGap = gap.id;
      for (const key of [
        "label",
        "category",
        "severity",
        "current_state",
        "target_state",
        "willingness",
        "notes",
      ])
        $(`[name=${key}]`, gapForm).value = gap[key] || "";
      $("button[type=submit]", gapForm).textContent = tr("Ruaj kufizimin");
      $("[name=label]", gapForm).focus();
    }
    const del = e.target.closest(".gap-delete");
    if (del && confirm(tr("Të fshihet ky kufizim?"))) {
      try {
        await json(`/api/profile/gaps/${del.dataset.id}`, { method: "DELETE" });
        loadGaps();
      } catch (error) {
        toast(error.message);
      }
    }
  });
  document.addEventListener("panel-loaded", async (e) => {
    const host = $("[data-schedule]", e.target);
    if (!host) return;
    const form = $("form", host),
      button = $("button[type=submit]", form);
    button.disabled = true;
    try {
      const schedules = await json("/api/schedules");
      const schedule = schedules.find(
        (s) => String(s.profile_id) === host.dataset.schedule,
      );
      if (schedule)
        for (const key of [
          "enabled",
          "cadence",
          "run_window_time",
          "timezone",
          "min_match",
        ]) {
          const input = $(`[name=${key}]`, form);
          if (input.type === "checkbox") input.checked = Boolean(schedule[key]);
          else input.value = schedule[key] ?? input.value;
        }
      $(".schedule-state", form).textContent = schedule?.next_run_at
        ? tr("Planifikuar: {p0}", { p0: schedule.next_run_at })
        : tr("Nuk ka ekzekutim të planifikuar.");
      button.disabled = false;
    } catch (error) {
      $(".schedule-state", form).textContent = tr(
        "Planifikimi nuk u ngarkua. Rihap panelin përpara ndryshimit.",
      );
    }
  });
  document.addEventListener("submit", async (e) => {
    if (!e.target.matches(".js-schedule-form")) return;
    e.preventDefault();
    const form = e.target,
      button = e.submitter;
    button.disabled = true;
    try {
      const data = Object.fromEntries(new FormData(form));
      data.enabled = $("[name=enabled]", form).checked;
      data.min_match = Number(data.min_match);
      const saved = await write(
        `/api/schedules/${form.closest("[data-schedule]").dataset.schedule}`,
        "PUT",
        data,
      );
      form.dataset.dirty = "false";
      $(".schedule-state", form).textContent = saved.enabled
        ? tr("Aktiv · {p0}", { p0: saved.next_run_at || "manual" })
        : tr("Planifikimi është i çaktivizuar.");
      toast(tr("Planifikimi u ruajt"));
    } catch (error) {
      toast(error.message);
    } finally {
      button.disabled = false;
    }
  });
})();
