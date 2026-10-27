/* Opportunity Engine: authenticated requests and focused workspace interactions. */
(() => {
  const { t: tr, ui, sourceLabel } = window.OEI18n;
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];

  const esc = (value) =>
    String(value ?? "").replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );
  // Keep the server's double-submit CSRF boundary for JSON and HTML forms.
  const fetch = async (url, options = {}) => {
    const headers = new Headers(options.headers || {});
    const target = new URL(url, location.href);
    if (target.origin === location.origin) headers.set('X-UI-Language', document.documentElement.lang);
    if (
      target.origin === location.origin &&
      !["GET", "HEAD"].includes((options.method || "GET").toUpperCase())
    ) {
      const csrf = document.cookie
        .split("; ")
        .find((c) => c.startsWith("oe_csrf="));
      if (csrf)
        headers.set(
          "X-CSRF-Token",
          decodeURIComponent(csrf.slice("oe_csrf=".length)),
        );
    }
    const response = await window.fetch(url, { ...options, headers });
    if (response.status === 401 && location.pathname !== "/login")
      toast(
        tr("Sesioni ka përfunduar. Kyçu përsëri për të ruajtur ndryshimet."),
        6000,
      );
    return response;
  };
  window.OE = {
    fetch,
    esc,
    toast: (...args) => toast(...args),
    hasPendingEdits,
  };

  /* ---------------------------------------------------------- drawer */
  const overlay = $("#overlay");
  const drawer = $("#drawer");
  const drawerBody = $("#drawer-body");
  let jobId = null;
  let lastFocus = null;
  let panelRequest = 0;
  let panelAbort = null;

  const SKELETON = `
      <div class="skel" role="status" aria-label="${esc(tr("Po ngarkohet paneli"))}">
        <div class="sk sk-title"></div><div class="sk sk-line"></div><div class="sk sk-line w60"></div>
        <div class="sk sk-btn"></div><div class="sk sk-block"></div><div class="sk sk-block"></div>
      </div>`;

  function showDrawer() {
    closePopovers();
    [$(".topbar"), $("main")].forEach((el) => {
      if (el) el.inert = true;
    });
    overlay.hidden = false;
    drawer.hidden = false;
    document.body.classList.add("drawer-open");
    requestAnimationFrame(() => drawer.classList.add("open"));
    const closeBtn = $("#drawer-close");
    if (closeBtn) closeBtn.focus();
  }

  async function openPanelURL(url, refresh = false) {
    if (
      !refresh &&
      !drawer.hidden &&
      hasUnsaved(drawerBody) &&
      !confirm(tr("Ka ndryshime të paruajtura. Të largohesh?"))
    )
      return;
    if (!drawer.hidden && !(await flushNotes())) return;
    if (drawer.hidden) {
      const active = document.activeElement;
      const pop = active?.closest(".popover");
      lastFocus = pop ? $(`[aria-controls="${pop.id}"]`) || active : active;
    }
    const requestId = ++panelRequest;
    panelAbort?.abort();
    panelAbort = new AbortController();
    showDrawer();
    drawerBody.innerHTML = SKELETON;
    drawerBody.setAttribute("aria-busy", "true");
    drawer.setAttribute(
      "aria-label",
      url.includes("/profiles/")
        ? tr("Profili i kërkimit")
        : tr("Detajet e mundësisë"),
    );
    try {
      const response = await fetch(url, { signal: panelAbort.signal });
      if (!response.ok || response.redirected)
        throw new Error(
          tr("Ngarkimi dështoi. Kontrollo lidhjen dhe sesionin."),
        );
      const html = await response.text();
      if (requestId !== panelRequest || drawer.hidden) return;
      drawerBody.innerHTML = html;
      jobId = $("[data-job]", drawerBody)?.dataset.job || null;
      drawerBody.scrollTop = 0;
      drawerBody.dispatchEvent(
        new CustomEvent("panel-loaded", { bubbles: true }),
      );
    } catch (error) {
      if (error.name === "AbortError" || requestId !== panelRequest) return;
      drawerBody.innerHTML = `<div class="empty" role="alert"><h3>${esc(tr("Nuk u hap dot."))}</h3><p class="sub">${esc(tr("Kontrollo lidhjen ose kyçu përsëri. Mund ta provosh sërish nga lista."))}</p></div>`;
    } finally {
      if (requestId === panelRequest) drawerBody.removeAttribute("aria-busy");
    }
  }

  function openDrawer(id) {
    return openPanelURL(`/api/jobs/${id}/panel`);
  }

  function hasUnsaved(host) {
    return (
      $$(
        ".appcard[data-dirty=true], .js-profile-form[data-dirty=true], .js-schedule-form[data-dirty=true]",
        host,
      ).length > 0
    );
  }
  async function closeDrawer() {
    if (drawer.hidden) return;
    if (
      hasUnsaved(drawerBody) &&
      !confirm(tr("Ka ndryshime të paruajtura. Të mbyllet paneli?"))
    )
      return;
    if (!(await flushNotes())) return;
    ++panelRequest;
    panelAbort?.abort();
    drawer.classList.remove("open");
    drawer.hidden = true;
    overlay.hidden = true;
    document.body.classList.remove("drawer-open");
    [$(".topbar"), $("main")].forEach((el) => {
      if (el) el.inert = false;
    });
    drawerBody.innerHTML = "";
    jobId = null;
    if (lastFocus?.isConnected) lastFocus.focus();
  }

  document.addEventListener("click", (e) => {
    const a = e.target.closest("a[data-drawer]");
    if (a && !e.ctrlKey && !e.metaKey && !e.shiftKey && !e.altKey) {
      e.preventDefault();
      openDrawer(a.dataset.drawer);
      return;
    }
    const row = e.target.closest(".row[data-id]");
    if (row && !e.target.closest("a,button,select,input,textarea")) {
      openDrawer(row.dataset.id);
      return;
    }
    if (e.target.closest("#drawer-close") || e.target === overlay) {
      closeDrawer();
      return;
    }
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Tab" && !drawer.hidden) {
      const focusable = $$(
        "button,a[href],input,select,textarea,summary,[tabindex='0']",
        drawer,
      ).filter((el) => !el.disabled && el.getClientRects().length);
      const first = focusable[0],
        last = focusable.at(-1);
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last?.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first?.focus();
      }
      return;
    }
    if (e.key === "Escape") {
      const activePop = $$(".popover").find((p) => !p.hidden);
      if (activePop) {
        const trigger = $(`[aria-controls="${activePop.id}"]`);
        closePopovers();
        trigger?.focus();
        return;
      }
    }
    if (e.key === "Escape" && !drawer.hidden) {
      closeDrawer();
      return;
    }
    const tag = document.activeElement && document.activeElement.tagName;
    const typing = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
    if (
      (e.key === "/" ||
        ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k")) &&
      !typing
    ) {
      const q = $("#q");
      if (q) {
        e.preventDefault();
        q.focus();
      }
    }
    if (
      e.key === "Enter" &&
      document.activeElement &&
      document.activeElement.classList &&
      document.activeElement.classList.contains("row")
    ) {
      e.preventDefault();
      openDrawer(document.activeElement.dataset.id);
    }
  });

  /* ------------------------------------------- status / shënime / fshirje */
  async function saveField(id, payload) {
    try {
      const r = await fetch(`/api/jobs/${id}/update`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      return r.ok ? r.json() : null;
    } catch {
      return null;
    }
  }

  function syncStatusUI(id, status) {
    const LABELS = window.__STATUS_LABELS__ || {};
    const previous = $(`.row[data-id="${id}"]`)?.dataset.status;
    if (previous && previous !== status)
      $$(".statusstrip a[data-status]").forEach((link) => {
        const count = $("b", link),
          delta =
            link.dataset.status === status
              ? 1
              : link.dataset.status === previous
                ? -1
                : 0;
        if (count && delta)
          count.textContent = Math.max(0, Number(count.textContent) + delta);
      });
    $$(`.row[data-id="${id}"]`).forEach((row) => {
      row.dataset.status = status;
      const pill = $(".statuspill", row);
      if (pill) {
        pill.className = `statuspill st-${status.replace(/ /g, "-")}`;
        pill.textContent = LABELS[status] || status;
      }
      const save = $(".js-quick-save", row);
      if (save) {
        save.hidden = !["new", "saved", "lead_saved"].includes(status);
        save.classList.toggle("on", status !== "new");
        save.setAttribute("aria-pressed", String(status !== "new"));
      }
    });
    $$(`[data-job="${id}"] .js-status`).forEach((b) =>
      b.classList.toggle("on", b.dataset.status === status),
    );
    $$(`[data-job="${id}"] .js-status-select`).forEach(
      (sel) => (sel.value = status),
    );
  }

  document.addEventListener("click", async (e) => {
    const summary = e.target.closest(".js-job-summary");
    if (summary) {
      const id = summary.dataset.job || jobId;
      if (!id) return;
      summary.disabled = true;
      const original = summary.innerHTML;
      summary.textContent = tr("Po krijohet përmbledhja…");
      try {
        const response = await fetch(`/api/jobs/${id}/summary`, { method: "POST" });
        const body = await response.json().catch(() => ({}));
        if (!response.ok || !body.ok) throw new Error(body.error || body.reason || "");
        await openPanelURL(`/api/jobs/${id}/panel`, true);
        toast(body.cached ? tr("Përmbledhja e ruajtur u përdor") : tr("Përmbledhja u krijua"));
      } catch (error) {
        summary.disabled = false;
        summary.innerHTML = original;
        toast(ui(error.message) || tr("Përmbledhja nuk u krijua. Provo përsëri."), 6000);
      }
      return;
    }
    const st = e.target.closest(".js-status");
    if (st) {
      const host = st.closest("[data-job]");
      const id = host ? host.dataset.job : jobId;
      if (!id) return;
      st.disabled = true;
      const res = await saveField(id, { status: st.dataset.status });
      if (res) {
        syncStatusUI(id, st.dataset.status);
        toast(tr("Statusi u ruajt"));
      } else toast(tr("Statusi nuk u ruajt. Provo përsëri."));
      st.disabled = false;
      return;
    }
    const del = e.target.closest(".js-delete");
    if (del && confirm(tr("Të fshihet përgjithmonë?"))) {
      const id = del.dataset.job || jobId;
      const response = await fetch(`/jobs/${id}/delete`, { method: "POST" });
      if (!response.ok) {
        toast(tr("Fshirja dështoi"));
        return;
      }
      const row = document.querySelector(`.row[data-id="${id}"]`);
      if (row) row.remove();
      if (drawer.hidden) location.assign("/");
      else closeDrawer();
    }
  });

  document.addEventListener("change", async (e) => {
    if (e.target.classList.contains("js-status-select")) {
      const host = e.target.closest("[data-job]");
      const id = host ? host.dataset.job : jobId;
      if (!id) return;
      const res = await saveField(id, { status: e.target.value });
      if (res) {
        syncStatusUI(id, e.target.value);
        toast(tr("Statusi u ruajt"));
      } else {
        e.target.value =
          $(`.row[data-id="${id}"]`)?.dataset.status || e.target.value;
        toast(tr("Statusi nuk u ruajt."));
      }
    }
  });

  const noteSaves = new WeakMap();
  async function saveNote(ta) {
    if (noteSaves.has(ta)) {
      await noteSaves.get(ta);
    }
    if (ta.value === ta.defaultValue) return true;
    const id = ta.closest("[data-job]")?.dataset.job;
    if (!id) return false;
    const value = ta.value,
      hint = ta.parentElement.querySelector(".savehint");
    if (hint) hint.textContent = tr("Po ruhet…");
    const save = saveField(id, { notes: value });
    noteSaves.set(ta, save);
    const result = await save;
    noteSaves.delete(ta);
    if (result) {
      ta.defaultValue = value;
      if (hint) hint.textContent = tr("U ruajt ✓");
      return true;
    }
    if (hint)
      hint.textContent = tr("Nuk u ruajt. Provo përsëri përpara mbylljes.");
    toast(tr("Shënimet nuk u ruajtën. Provo përsëri."), 6000);
    return false;
  }
  async function flushNotes() {
    for (const ta of $$("textarea[data-autosave]", drawerBody))
      if (!(await saveNote(ta))) return false;
    return true;
  }
  document.addEventListener("focusout", (e) => {
    const ta = e.target.closest?.("textarea[data-autosave]");
    if (ta) saveNote(ta);
  });

  /* ----------------------------------------------------------- toast */
  let toastTimer;
  function toast(msg, ms = 2800) {
    const t = $("#toast");
    if (!t) return;
    t.textContent = tr(msg);
    t.hidden = false;
    requestAnimationFrame(() => t.classList.add("show"));
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      t.classList.remove("show");
      setTimeout(() => (t.hidden = true), 300);
    }, ms);
  }

  /* ------------------------------------------------- burimet & kërkimi */
  function setPopover(id, btnId, open) {
    const p = $(id);
    const b = $(btnId);
    if (!p || !b) return;
    if (open) {
      $$(".popover").forEach((other) => {
        if (other !== p) {
          other.hidden = true;
          $(`[aria-controls="${other.id}"]`)?.setAttribute(
            "aria-expanded",
            "false",
          );
        }
      });
    }
    p.hidden = !open;
    b.setAttribute("aria-controls", p.id);
    b.setAttribute("aria-expanded", String(open));
    if (open)
      requestAnimationFrame(() => p.querySelector("a,button,input")?.focus());
  }

  function closePopovers() {
    setPopover("#sources-pop", "#btn-sources", false);
    setPopover("#profiles-pop", "#btn-profiles", false);
    setPopover("#notif-pop", "#btn-bell", false);
  }

  async function fetchSource(src, btn) {
    try {
      const r = await fetch(`/api/search/${src}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      const d = await r.json();
      if (!r.ok) {
        if (btn) {
          btn.textContent = tr("gabim");
        }
        toast(`${sourceLabel(src)}: ${ui(d.error || "gabim")}`);
        return 0;
      }
      if (btn) {
        btn.textContent = `+${d.new}`;
        setTimeout(() => {
          btn.disabled = false;
          btn.textContent = tr("Kërko");
        }, 2500);
      }
      return d.new;
    } catch {
      if (btn) {
        btn.textContent = tr("pa akses");
      }
      toast(tr("{p0}: burimi i papërdorshëm", { p0: sourceLabel(src) }));
      return 0;
    } finally {
      if (btn) {
        btn.disabled = false;
        if (![tr("Kërko")].includes(btn.textContent))
          setTimeout(() => {
            btn.textContent = tr("Kërko");
          }, 2500);
      }
    }
  }

  document.addEventListener("click", (e) => {
    if (e.target.closest("#btn-sources")) {
      setPopover("#sources-pop", "#btn-sources", $("#sources-pop").hidden);
      return;
    }
    if (e.target.closest("#btn-profiles")) {
      setPopover("#profiles-pop", "#btn-profiles", $("#profiles-pop").hidden);
      return;
    }
    if (!e.target.closest(".popwrap")) closePopovers();
    const f = e.target.closest(".js-src-fetch");
    if (f && !f.disabled) {
      f.disabled = true;
      f.textContent = "…";
      fetchSource(f.dataset.src, f).then((n) => {
        if (n > 0) setTimeout(() => location.reload(), 900);
      });
      return;
    }
    // profilet: hap formën në drawer
    const edit = e.target.closest(".js-prof-edit");
    if (edit) {
      openPanelURL(`/api/profiles/${edit.dataset.id}/panel`);
      return;
    }
    const neu = e.target.closest(".js-prof-new");
    if (neu) {
      openPanelURL("/api/profiles/new/panel");
      return;
    }
    const run = e.target.closest(".js-prof-run");
    if (run && !run.disabled) {
      run.disabled = true;
      run.textContent = "…";
      fetch(`/api/profiles/${run.dataset.id}/run`, { method: "POST" })
        .then((r) => r.json())
        .then((d) => {
          run.disabled = false;
          run.textContent = tr("Kërko");
          if (d.results) {
            const failed = d.results.filter((x) => !x.ok);
            if (d.new_total > 0) {
              toast(
                tr("{p0} mundësi të reja nga {p1}/{p2} burime", {
                  p0: d.new_total,
                  p1: d.ok_sources,
                  p2: d.results.length,
                }),
              );
              setTimeout(() => location.reload(), 800);
            } else if (failed.length === d.results.length) {
              toast(tr("Të gjitha burimet dështuan"));
            } else {
              toast(
                tr("Asnjë rezultat i reja") +
                  (failed.length
                    ? tr(" ({p0} burime me gabim)", { p0: failed.length })
                    : ""),
              );
            }
          } else {
            toast(ui(d.error) || "gabim");
          }
        })
        .catch(() => {
          run.disabled = false;
          run.textContent = tr("Kërko");
          toast(tr("Kërkimi dështoi"));
        });
    }
  });

  // aktivizim/çaktivizim profili
  document.addEventListener("change", async (e) => {
    const t = e.target;
    if (t.classList && t.classList.contains("js-prof-active")) {
      const id = t.dataset.id;
      t.disabled = true;
      try {
        const r = await fetch(`/api/profiles/${id}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ active: t.checked }),
        });
        if (r.ok) {
          const row = document.querySelector(`.prof-row[data-row="${id}"]`);
          if (row) row.classList.toggle("off", !t.checked);
          toast(
            t.checked ? tr("Profili u aktivizua") : tr("Profili u çaktivizua"),
          );
        } else {
          t.checked = !t.checked;
          toast(tr("Ndryshimi nuk u ruajt."));
        }
      } catch {
        t.checked = !t.checked;
        toast(tr("Gabim rrjeti"));
      } finally {
        t.disabled = false;
      }
    }
  });

  // fshirje profili nga formë në drawer
  document.addEventListener("click", async (e) => {
    const del = e.target.closest(".js-prof-delete");
    if (
      !del ||
      !confirm(tr("Të fshihet profili? (mundësitë e gjetura mbeten)"))
    )
      return;
    const r = await fetch(`/api/profiles/${del.dataset.id}`, {
      method: "DELETE",
    });
    if (r.ok) {
      toast(tr("Profili u fshi"));
      closeDrawer();
      setTimeout(() => location.reload(), 400);
    }
  });

  // ruajtje forme profili (kriim ose editim)
  document.addEventListener("submit", async (e) => {
    const form = e.target.closest && e.target.closest("form.js-profile-form");
    if (!form) return;
    e.preventDefault();
    const fd = new FormData(form);
    const num = (v) => (v === "" || v === null ? 0 : Number(v) || 0);
    const payload = {
      name: fd.get("name") || "",
      active: fd.get("active") === "on",
      kinds: fd.getAll("kinds"),
      keywords: fd.get("keywords") || "",
      exclude_keywords: fd.get("exclude_keywords") || "",
      location: fd.get("location") || "",
      radius_km: num(fd.get("radius_km")),
      salary_min: num(fd.get("salary_min")),
      hourly_rate_min: num(fd.get("hourly_rate_min")),
      budget_min: num(fd.get("budget_min")),
      min_match: num(fd.get("min_match")),
      remote_ok: fd.get("remote_ok") === "on",
      onsite_ok: fd.get("onsite_ok") === "on",
      hybrid_ok: fd.get("hybrid_ok") === "on",
      sources: fd.getAll("sources"),
    };
    const host = form.closest("[data-profile]");
    const id = host && host.dataset.profile ? host.dataset.profile : "";
    const button = e.submitter;
    if (button) button.disabled = true;
    try {
      const r = await fetch(id ? `/api/profiles/${id}` : "/api/profiles", {
        method: id ? "PATCH" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (r.ok) {
        toast(tr("Profili u ruajt"));
        form.dataset.dirty = "false";
        if (!hasUnsaved(drawerBody)) {
          await closeDrawer();
          setTimeout(() => location.reload(), 400);
        }
      } else {
        const d = await r.json().catch(() => ({}));
        toast(ui(d.error) || tr("Ruajtja dështoi"));
      }
    } catch {
      toast(tr("Ruajtja dështoi. Provo përsëri."));
    } finally {
      if (button) button.disabled = false;
    }
  });

  // Orkestratori (5C): "Gjej mundësi" → profil i zgjedhur ose të gjitha burimet
  const findBtn = $("#btn-find");
  if (findBtn) {
    findBtn.addEventListener("click", async () => {
      const sel = $("#orch-profile");
      const profileId = sel && sel.value ? sel.value : null;
      const fromProfile = profileId === 'ai';
      findBtn.disabled = true;
      const origHTML = findBtn.innerHTML;
      findBtn.textContent = tr("Po kërkon…");
      try {
        const r = await fetch(fromProfile ? '/api/profile/discover?deep=true&expanded=true' : "/api/orchestrator/run", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            profile_id: profileId && !fromProfile ? Number(profileId) : null,
          }),
        });
        const d = await r.json();
        if (!r.ok || !d.ok) {
          toast(ui(d.error || d.reason || "Kërkimi dështoi"));
          findBtn.disabled = false;
          findBtn.innerHTML = origHTML;
          return;
        }
        if (fromProfile) {
          toast(tr('U shtuan {count} oferta të reja.', {count:d.new}));
          location.reload();
          return;
        }
        toast(
          tr("{checked} të kontrolluara · {new} të reja · {sources} burime", {
            checked: d.checked,
            new: d.new,
            sources: d.sources_ok,
          }) +
            (d.sources_failed
              ? tr(" · {p0} burime me gabim", { p0: d.sources_failed })
              : "") +
            (d.new > 0 ? tr(" — po hap") : ""),
          3600,
        );
        if (d.new > 0) setTimeout(() => location.reload(), 900);
        else {
          findBtn.disabled = false;
          findBtn.innerHTML = origHTML;
        }
      } catch {
        toast(tr("Gabim rrjeti"));
        findBtn.disabled = false;
        findBtn.innerHTML = origHTML;
      }
    });
  }

  /* --------------------------------------------- aplikimet (Faza 4A) */
  async function reloadDrawer(jobIdRef) {
    if (!jobIdRef) return;
    if (drawer.hidden) {
      location.reload();
      return;
    }
    await openPanelURL(`/api/jobs/${jobIdRef}/panel`, true);
    const section = $("#apps-section", drawerBody);
    if (section) {
      section.open = true;
      section.scrollIntoView({ block: "start", behavior: "smooth" });
    }
  }

  document.addEventListener("click", async (e) => {
    const b = e.target.closest(
      ".js-app-create, .js-app-save, .js-app-approve, .js-app-regen, .js-app-archive, .js-app-submitted",
    );
    if (!b) return;
    e.preventDefault();
    const jobRef = b.closest("[data-job]")?.dataset.job || jobId;
    const dirtyCard = b.closest(".appcard");
    if (
      !b.classList.contains("js-app-save") &&
      dirtyCard?.dataset.dirty === "true"
    ) {
      toast(
        tr("Ruaj ndryshimet përpara se të ndryshosh gjendjen e aplikimit."),
      );
      return;
    }
    b.disabled = true;
    try {
      let url,
        body = {};
      if (b.classList.contains("js-app-create")) {
        url = `/api/jobs/${b.dataset.job}/applications`;
        body = {
          application_type:
            ($(".js-app-type", b.closest(".panelroot")) || {}).value || "",
          language:
            ($(".js-app-lang", b.closest(".panelroot")) || {}).value || "sq",
        };
      } else if (b.classList.contains("js-app-save")) {
        const card = b.closest(".appcard");
        url = `/api/applications/${b.dataset.app}/edit`;
        body = {
          content: card.querySelector(".app-content").value,
          subject: card.querySelector(".app-subject").value,
        };
      } else if (
        b.classList.contains("js-app-regen") &&
        !confirm(
          tr(
            "Rigjenerimi krijon version të re — editimet e ruajtura mbeten. Vazhdoj?",
          ),
        )
      ) {
        b.disabled = false;
        return;
      } else {
        url = `/api/applications/${b.dataset.app}/${
          b.classList.contains("js-app-approve")
            ? "approve"
            : b.classList.contains("js-app-regen")
              ? "regenerate"
              : b.classList.contains("js-app-archive")
                ? "archive"
                : "submit"
        }`;
      }
      const r = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok || d.error) {
        toast(ui(d.error) || tr("Veprimi dështoi"));
        b.disabled = false;
        return;
      }
      toast(
        b.classList.contains("js-app-create")
          ? tr("Drafti u krijua")
          : b.classList.contains("js-app-save")
            ? tr("U ruajt — version i re")
            : b.classList.contains("js-app-approve")
              ? tr("U aprovua")
              : b.classList.contains("js-app-submitted")
                ? tr("U shënua si e dërguar")
                : tr("U përditësua"),
      );
      if (dirtyCard) dirtyCard.dataset.dirty = "false";
      await reloadDrawer(jobRef);
    } catch {
      toast(tr("Gabim rrjeti"));
      b.disabled = false;
    }
  });

  /* --------------------------------------------- gjenerim me AI (4B) */
  document.addEventListener("click", async (e) => {
    const b = e.target.closest(".js-app-generate");
    if (!b) return;
    e.preventDefault();
    if (hasUnsaved(b.closest(".panelroot"))) {
      toast(tr("Ruaj ndryshimet përpara gjenerimit."));
      return;
    }
    b.disabled = true;
    b.textContent = tr("Po gjeneron…");
    try {
      const drawer = b.closest("#drawer-body");
      const language = ($(".js-app-lang") || {}).value || "sq";
      const tone = ($(".js-app-tone") || {}).value || "professional";
      const type = ($(".js-app-type") || {}).value;
      const body = { tone, language };
      if (b.dataset.app) body.application_id=Number(b.dataset.app);
      else body.opportunity_id=Number(b.dataset.job);
      if (!b.dataset.app && type) body.application_type = type;
      const d=await window.OEWork.run('application_draft',body,job=>{
        b.textContent=tr('Po gjeneron…')+' '+(job.progress_current||0)+'/'+(job.progress_total||0);
      });
      if (d.status!=='succeeded' || !d.result?.application_id) {
        toast(d.message || tr(d.status==='superseded'?'Profili ndryshoi. Gjenero draftin përsëri.':'Gjenerimi dështoi'));
        b.disabled = false;
        b.textContent = tr("Gjenero me AI");
        return;
      }
      toast(tr("U gjenerua me AI{p0}", {p0:""}));
      await reloadDrawer(jobId ?? b.dataset.job);
    } catch (error) {
      toast(error.message || tr("Gabim rrjeti"));
      b.disabled = false;
      b.textContent = tr("Gjenero me AI");
    }
  });

  /* ------------------------------------------------- CV sugjerimet (4D) */
  const parseForm = document.querySelector('form[action="/profile/cv/parse"]');
  const sugList = document.getElementById("cv-suggestions");
  async function renderSuggestions() {
    if (!sugList) return;
    try {
      const r = await fetch("/api/profile/cv");
      const cv = await r.json();
      if (!cv.id) return;
      const cvStatus = $("#cv-status");
      if (cvStatus)
        cvStatus.textContent = `${cv.filename} · ${Math.max(1, Math.ceil(cv.size_bytes / 1024))} KB · ${{ pending: tr("Në pritje"), parsed: tr("Sugjerimet u nxorën"), failed: tr("Nxjerrja dështoi") }[cv.parse_status] || cv.parse_status}`;
      const d = await (
        await fetch(`/api/cv/${cv.id}/suggestions?status=pending`)
      ).json();
      if (!d.count) {
        sugList.innerHTML =
          d.count === 0 && cv.parse_status === "parsed"
            ? `<p class="sub">${esc(tr("Të gjitha sugjerimet janë rishikuar."))} ${esc(tr("Kjo nuk do të thotë se çdo fushë e CV-së është importuar. Kontrollo profilin më poshtë."))}</p>`
            : "";
        return;
      }
      sugList.innerHTML =
        `<h3 class="sub">${esc(tr("Sugjerime në pritje ("))}${d.count})</h3>` +
        d.items
          .map(
            (sug) => `
        <div class="sugg" data-id="${sug.id}">
          <span class="suggestion-label">${esc(tr("Sugjerim · ende i pakonfirmuar"))}</span><div><strong>${esc((sug.section === "contact" ? { name: tr("Emri"), title: tr("Titulli profesional"), location: tr("Vendndodhja"), email: tr("Email"), phone: tr("Telefon") }[sug.field] : null) || { contact: tr("Kontakt"), summary: tr("Përmbledhje (summary)"), education: tr("Edukimi"), experience: tr("Përvoja"), languages: tr("Gjuhët"), skills: tr("Aftësitë"), certifications: tr("Certifikimet"), projects: tr("Projektet"), portfolio: tr("Portfolio") }[sug.section] || sug.section)}</strong> · ${esc(sug.display_value || sug.proposed_value)}</div>
          <div class="sub dëshmi">${esc(tr("Dëshmi:"))} ${esc(sug.evidence)}</div>
          <p class="sub">${esc(tr("Pranimi e shton këtë fakt në profil kur nuk bie ndesh me të dhënat manuale."))}</p><div class="row">
            <button class="btn tiny primary js-accept" data-id="${sug.id}">${esc(tr("Aprovo"))}</button>
            <button class="btn tiny ghost js-reject" data-id="${sug.id}">${esc(tr("Refuzo"))}</button>
          </div>
        </div>`,
          )
          .join("");
    } catch {
      sugList.innerHTML = `<p class="form-error" role="alert">${esc(tr("Sugjerimet nuk u ngarkuan. Rifresko për ta provuar përsëri."))}</p>`;
    }
  }
  if (parseForm || sugList) renderSuggestions();
  if (parseForm)
    parseForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (hasPendingEdits()) {
        toast(tr("Ruaj ndryshimet e profilit përpara përpunimit të CV-së."));
        return;
      }
      const button = e.submitter;
      const autofill = parseForm.elements.profile_mode.value === "autofill";
      const useAi = parseForm.elements.use_ai.checked;
      button.disabled = true;
      const hint = $("#cv-parse-hint");
      if (hint) hint.textContent = tr("Po nxirren sugjerimet…");
      try {
        const cv = await (await fetch("/api/profile/cv")).json();
        const d = await (
          await fetch(`/api/cv/${cv.id}/${autofill ? "autofill" : "parse"}?use_ai=${useAi}`, { method: "POST" })
        ).json();
        if (d.ok && autofill) {
          const result = d.autofill;
          const msg = tr("CV: {merged} të dhëna u plotësuan; {pending} për rishikim.", result) + (d.warning ? " " + tr(d.warning) : "");
          location.assign(`/profile?msg=${encodeURIComponent(msg)}#cv`);
          return;
        }
        toast(
          d.ok
            ? tr("Parse: {p0} sugjerime të reja", { p0: d.created })
            : tr("Parse: {p0}", { p0: ui(d.reason) }),
        );
      } catch {
        toast(tr("Nxjerrja dështoi. Provo përsëri."));
      } finally {
        button.disabled = false;
        if (hint) hint.textContent = tr("Rishiko dëshmitë përpara pranimit.");
      }
      renderSuggestions();
    });
  document.addEventListener("click", async (e) => {
    const acc = e.target.closest(".js-accept");
    const rej = e.target.closest(".js-reject");
    if (!acc && !rej) return;
    e.preventDefault();
    const id = (acc || rej).dataset.id;
    const action = acc ? "accept" : "reject";
    const button = acc || rej;
    button.disabled = true;
    try {
      const r = await fetch(`/api/cv/suggestions/${id}/${action}`, {
        method: "POST",
      });
      const d = await r.json();
      if (d.ok) {
        toast(
          acc
            ? d.merged
              ? tr("U shtua në profil")
              : tr("Pa efekt (ekziston/manual fiton)")
            : tr("Refuzuar"),
        );
        const card = (acc || rej).closest(".sugg");
        if (card) card.remove();
        await renderSuggestions();
        const next = $(".js-accept", sugList);
        if (next) {
          next.focus();
          next.scrollIntoView({ block: "center" });
        }
        document.dispatchEvent(new Event("profile-updated"));
      } else toast(ui(d.error) || tr("Veprimi nuk u krye. Provo përsëri."));
    } catch {
      toast(tr("Gabim rrjeti. Provo përsëri."));
    } finally {
      button.disabled = false;
    }
  });

  /* ------------------------------------------------- njoftime (7A) */
  const bellBtn = $("#btn-bell");
  if (bellBtn) {
    const refreshCount = async () => {
      try {
        const d = await (await fetch("/api/notifications/unread-count")).json();
        const b = $("#bell-count");
        b.hidden = !d.unread;
        b.textContent = d.unread > 99 ? "99+" : d.unread;
      } catch {}
    };
    refreshCount();

    const renderList = async (unreadOnly) => {
      const listHost = $("#notif-list");
      listHost.innerHTML = `<p class="sub" role="status">${esc(tr("Po ngarkohen njoftimet…"))}</p>`;
      let d;
      try {
        const r = await fetch(
          `/api/notifications?limit=30&unread_only=${unreadOnly ? 1 : 0}`,
        );
        if (!r.ok) throw new Error();
        d = await r.json();
      } catch {
        listHost.innerHTML = `<p class="form-error" role="alert">${esc(tr("Njoftimet nuk u ngarkuan. Mbylle dhe provo përsëri."))}</p>`;
        return;
      }
      const list = $("#notif-list");
      if (!d.items.length) {
        list.innerHTML = `<div class="notif-empty">${esc(tr("Nuk ka njoftime"))}</div>`;
        return;
      }
      list.innerHTML = d.items
        .map(
          (n) => `
        <div class="notif-item ${n.read_at ? "" : "unread"}" data-id="${n.id}" data-opp="${n.opportunity_id || ""}">
          <span class="dot"></span>
          <div style="min-width:0">
            <a class="notif-title" href="${n.opportunity_id ? `/jobs/${Number(n.opportunity_id)}` : "#"}">${esc(n.title)}</a>
            ${n.summary ? `<div class="notif-sum">${esc(n.summary)}</div>` : ""}
            <div class="notif-meta">
              <span>${esc(({new_local_opportunity:tr('Mundësi e re lokale'),new_freelance_opportunity:tr('Projekt i ri freelance'),new_product_lead:tr('Kërkesë e re për produktin'),new_high_match:tr('Përputhje e re e fortë'),new_opportunity:tr('Mundësi e re')})[n.type] || tr('Njoftime'))}</span><span>${esc(n.created_at)}</span>
              <button class="linklike dismiss" data-id="${n.id}">${esc(tr("hiq"))}</button>
            </div>
          </div>
        </div>`,
        )
        .join("");
    };

    bellBtn.addEventListener("click", async () => {
      const pop = $("#notif-pop");
      const open = pop.hidden;
      setPopover("#notif-pop", "#btn-bell", open);
      if (open) await renderList(false);
    });
    document.addEventListener("click", async (e) => {
      if (!e.target.closest(".notifwrap") && !e.target.closest("#btn-bell")) {
        const pop = $("#notif-pop");
        if (pop && !pop.hidden) {
          pop.hidden = true;
          bellBtn.setAttribute("aria-expanded", "false");
        }
        return;
      }
      const dismiss = e.target.closest(".notif-meta .dismiss");
      const item = e.target.closest(".notif-item");
      if (!dismiss && !item?.dataset.opp) return;
      e.preventDefault();
      try {
        if (dismiss) {
          e.stopPropagation();
          const r = await fetch(
            `/api/notifications/${dismiss.dataset.id}/dismiss`,
            { method: "POST" },
          );
          if (!r.ok) throw new Error();
          await renderList(false);
          refreshCount();
          return;
        }
        const r = await fetch(`/api/notifications/${item.dataset.id}/read`, {
          method: "POST",
        });
        if (!r.ok) throw new Error();
        item.classList.remove("unread");
        refreshCount();
        openDrawer(item.dataset.opp);
      } catch {
        toast(tr("Njoftimi nuk u përditësua. Provo përsëri."));
      }
    });
    const readAll = $("#notif-readall");
    if (readAll)
      readAll.addEventListener("click", async () => {
        try {
          const r = await fetch("/api/notifications/read-all", {
            method: "POST",
          });
          if (!r.ok) throw new Error();
          await renderList(false);
          refreshCount();
        } catch {
          toast(tr("Njoftimet nuk u përditësuan. Provo përsëri."));
        }
      });
  }

  /* ---------------------------------------------------------- filtrat */
  const moreBtn = $("#more-toggle");
  if (moreBtn) {
    moreBtn.addEventListener("click", () => {
      const mf = $("#morefilters");
      const open = mf.hidden;
      mf.hidden = !open;
      moreBtn.setAttribute("aria-expanded", String(open));
      moreBtn.textContent = open ? tr("Më pak filtra") : tr("Më shumë filtra");
    });
  }

  /* Manual entry: update label text without removing its inputs. */
  const addform = $("#addform");
  if (addform) {
    const syncKind = () => {
      const kind = $("input[name=kind]:checked", addform)?.value;
      const opp = ["freelance", "product_lead", "opportunity"].includes(kind);
      $("#opp-extra").hidden = !opp;
      $("#local-extra").hidden = kind !== "local_job";
      $("#lbl-salary span").textContent = opp
        ? tr("Buxheti / tarifa")
        : tr("Paga (tekst i lirë)");
      $("#lbl-company span").textContent = opp
        ? tr("Klienti")
        : tr("Kompania / Klienti");
    };
    $$("input[name=kind]", addform).forEach((r) =>
      r.addEventListener("change", syncKind),
    );
    syncKind();
  }

  document.addEventListener(
    "toggle",
    async (e) => {
      const revision = e.target;
      if (
        !revision.matches?.(".revision") ||
        !revision.open ||
        revision.dataset.loaded
      )
        return;
      const content = $(".version-content", revision);
      content.textContent = tr("Po ngarkohet versioni…");
      try {
        const response = await fetch(
          `/api/applications/${revision.closest(".appcard").dataset.app}`,
        );
        if (!response.ok) throw new Error();
        const application = await response.json();
        const version = application.versions.find(
          (v) => String(v.version_number) === revision.dataset.version,
        );
        if (!version) throw new Error();
        content.textContent = version.content;
        revision.dataset.loaded = "true";
      } catch {
        content.textContent = tr(
          "Versioni nuk u ngarkua. Mbylle dhe rihape për ta provuar përsëri.",
        );
      }
    },
    true,
  );

  // Facets preserve other choices. Mode switches deliberately start a fresh view.
  const query = new URLSearchParams(location.search);
  const searchForm = $("#searchform"),
    filterForm = $("#filterform");
  if (searchForm)
    for (const [key, value] of query) {
      if (
        ["q", "mode"].includes(key) ||
        ![
          "status",
          "source",
          "loc",
          "remote",
          "mins",
          "days",
          "salary",
          "sort",
        ].includes(key)
      )
        continue;
      const field = document.createElement("input");
      field.type = "hidden";
      field.name = key;
      field.value = value;
      searchForm.append(field);
    }
  $$(".statusstrip a").forEach((link) => {
    const target = new URL(link.href);
    const next = new URLSearchParams(query);
    if (target.searchParams.has("status"))
      next.set("status", target.searchParams.get("status"));
    else next.delete("status");
    link.href = `/?${next}`;
  });
  const strip = $(".statusstrip");
  let mobileStatus;
  function syncMobileStatus() {
    if (!mobileStatus) return;
    const links = $$("a", strip);
    mobileStatus.innerHTML = links
      .map(
        (link) =>
          `<option value="${esc(link.href)}" ${link.classList.contains("on") ? "selected" : ""}>${esc($("span", link).textContent + " · " + $("b", link).textContent)}</option>`,
      )
      .join("");
  }
  if (strip) {
    mobileStatus = document.createElement("select");
    mobileStatus.className = "mobile-status";
    mobileStatus.setAttribute("aria-label", tr("Statusi i mundësive"));
    strip.after(mobileStatus);
    strip.classList.add("mobile-condensed");
    syncMobileStatus();
    mobileStatus.addEventListener("change", () =>
      location.assign(mobileStatus.value),
    );
    new MutationObserver(syncMobileStatus).observe(strip, {
      subtree: true,
      characterData: true,
      childList: true,
    });
  }
  $("#filter-mobile")?.addEventListener("click", (e) => {
    const open = filterForm.classList.toggle("mobile-open");
    e.currentTarget.setAttribute("aria-expanded", String(open));
    if (open) filterForm.querySelector("select")?.focus();
  });
  const allRows = $$("#opportunity-list > .row");
  let visibleRows = Math.min(30, allRows.length);
  function paginate() {
    allRows.forEach((row, i) => (row.hidden = i >= visibleRows));
    if ($("#result-range"))
      $("#result-range").textContent = tr("{p0} nga {p1} rezultate", {
        p0: visibleRows,
        p1: allRows.length,
      });
    if ($("#load-more")) $("#load-more").hidden = visibleRows >= allRows.length;
  }
  if (allRows.length) paginate();
  $("#load-more")?.addEventListener("click", () => {
    const firstNew = allRows[visibleRows];
    visibleRows += 30;
    visibleRows = Math.min(visibleRows, allRows.length);
    paginate();
    firstNew?.querySelector("a")?.focus();
  });
  document.addEventListener("click", async (e) => {
    const save = e.target.closest(".js-quick-save");
    if (save) {
      save.disabled = true;
      const status =
        save.getAttribute("aria-pressed") === "true"
          ? "new"
          : save.dataset.save;
      const result = await saveField(save.dataset.id, { status });
      if (result) {
        syncStatusUI(save.dataset.id, status);
        toast(
          status === "new"
            ? tr("U hoq nga të ruajturat")
            : tr("U ruajt në mundësitë e tua"),
        );
      } else toast(tr("Nuk u ruajt. Provo përsëri."));
      save.disabled = false;
    }
    const go = e.target.closest(".js-go-application");
    if (go) {
      const app = $("#apps-section", go.closest(".panelroot"));
      app.open = true;
      app.scrollIntoView({ behavior: "smooth", block: "start" });
      app.querySelector("select,button")?.focus({ preventScroll: true });
    }
  });
  document.addEventListener("input", (e) => {
    const ordinaryForm = e.target.closest("form.form,form.cv-upload");
    if (
      ordinaryForm &&
      !ordinaryForm.matches("#login-form,.js-profile-form,.js-schedule-form,#resume-options,#discovery-form")
    )
      ordinaryForm.dataset.unsaved = "true";
    const card = e.target.closest(".appcard");
    if (card) {
      card.dataset.dirty = "true";
      const state = $(".edit-state", card);
      if (state) {
        state.textContent = tr(
          "Ndryshime të paruajtura · ruaj përpara aprovimit",
        );
        state.classList.add("dirty");
      }
    }
    const form = e.target.closest(".js-profile-form, .js-schedule-form");
    if (form) form.dataset.dirty = "true";
  });
  function hasPendingEdits() {
    return Boolean(
      hasUnsaved(document) ||
        $("[data-editor-dirty=true]") ||
        $("form[data-unsaved=true]") ||
        $$("textarea[data-autosave]").some(
          (ta) => ta.value !== ta.defaultValue,
        ),
    );
  }
  window.addEventListener("beforeunload", (e) => {
    if (hasPendingEdits()) {
      e.preventDefault();
      e.returnValue = "";
    }
  });

  // Existing HTML POST contracts use fetch so production CSRF stays enforced.
  document.addEventListener("submit", async (e) => {
    const form = e.target;
    if (
      e.defaultPrevented ||
      form.method.toLowerCase() !== "post" ||
      !form.getAttribute("action") ||
      form.getAttribute("action") === "#"
    )
      return;
    e.preventDefault();
    if (form.matches(".cv-upload") && [...document.querySelectorAll('[data-editor-dirty=true],form[data-unsaved=true],.appcard[data-dirty=true]')].some(el => el !== form)) {
      toast(tr("Ruaj ndryshimet e profilit përpara përpunimit të CV-së."));
      return;
    }
    const button = e.submitter || form.querySelector('button[type="submit"]');
    const original = button?.textContent;
    if (button) {
      button.disabled = true;
      button.textContent = tr(form.matches(".cv-upload") ? "Po lexohet CV-ja…" : "Po ruhet…");
    }
    try {
      const response = await fetch(form.action, {
        method: "POST",
        body: new FormData(form),
      });
      if (!response.ok) {
        const d = await response.json().catch(() => ({}));
        throw new Error(
          d.error ||
            tr("Veprimi nuk u krye. Kontrollo fushat dhe provo përsëri."),
        );
      }
      if (form.id === "login-form") {
        location.assign("/");
        return;
      }
      form.dataset.dirty = "false";
      form.dataset.unsaved = "false";
      if (response.redirected) {
        const destination = new URL(response.url);
        if (destination.pathname === "/profile")
          destination.hash = form.closest(".psect")?.id || "profili";
        location.assign(destination.href);
      } else location.reload();
    } catch (error) {
      const errorBox = $("#login-error", form);
      if (errorBox) {
        errorBox.hidden = false;
        errorBox.textContent = tr(error.message);
      } else toast(error.message, 5000);
      if (button) {
        button.disabled = false;
        button.textContent = original;
      }
    }
  });
  $("#btn-logout")?.addEventListener("click", async () => {
    if (
      hasUnsaved(document) &&
      !confirm(tr("Ka ndryshime të paruajtura. Të dalësh?"))
    )
      return;
    try {
      const r = await fetch("/api/auth/logout", { method: "POST" });
      if (r.ok) location.assign("/login");
      else toast(tr("Dalja dështoi."));
    } catch {
      toast(tr("Gabim rrjeti."));
    }
  });
})();
