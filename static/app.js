"use strict";
const $ = (s) => document.querySelector(s),
  $$ = (s) => [...document.querySelectorAll(s)];
const esc = (v) =>
  String(v ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const icon = (name) => `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
const providers = {
  chatgpt: { name: "ChatGPT", mark: "◎", note: "مشاريع · محادثات · ملفات" },
  gemini: { name: "Gemini", mark: "✦", note: "نماذج الحساب · محادثات · ملفات" },
  deepseek: {
    name: "DeepSeek Telegram",
    mark: "ds",
    note: "جلسات الحساب · تفكير · بحث",
  },
  qwen: { name: "Qwen", mark: "Q", note: "محادثات · صور · أدوات" },
};
const pages = [
  ["overview", "نظرة عامة", "grid"],
  ["accounts", "الحسابات والمزوّدون", "cpu"],
  ["models", "النماذج والمسارات", "route"],
  ["sessions", "المحادثات", "chat"],
  ["keys", "مفاتيح الوصول", "key"],
  ["playground", "مختبر المحادثة", "pulse"],
  ["activity", "سجل النشاط", "sliders"],
  ["connect", "الربط والتكامل", "link"],
];
let state = null,
  page = "overview",
  toastTimer,
  chatHistory = [],
  chatBusy = false,
  activePlayModel = "",
  activeDialogAccount = null,
  chatCursor = null,
  chatOffset = 0,
  projectCursor = null,
  requestSeq = 0,
  lastRenderedPage = "";
function logo(provider) {
  return `<span class="provider-logo ${esc(provider)}" aria-hidden="true">${esc(providers[provider]?.mark || "A")}</span>`;
}
function badge(account) {
  const status = !account.enabled ? "disabled" : account.status;
  return `<span class="badge ${status === "connected" ? "" : status === "error" ? "error" : status === "disabled" ? "disabled" : "pending"}">${status === "connected" ? "● متصل" : status === "error" ? "يحتاج مراجعة" : status === "disabled" ? "معطّل" : "بانتظار الربط"}</span>`;
}
function date(value) {
  return new Date(value * 1000).toLocaleString("ar", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}
async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    ...options,
    headers: {
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...options.headers,
    },
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401 && path != "/admin/api/login") showLogin();
    throw new Error(data.error?.message || "تعذر الاتصال بالخادم.");
  }
  return data;
}
const post = (path, data = {}) =>
  api(path, { method: "POST", body: JSON.stringify(data) });
function toast(message, error = false) {
  clearTimeout(toastTimer);
  $("#toast").textContent = message;
  $("#toast").className = error ? "error" : "";
  $("#toast").hidden = false;
  toastTimer = setTimeout(() => ($("#toast").hidden = true), 6500);
}
function showLogin() {
  $("#app").hidden = true;
  $("#login").hidden = false;
}
async function refresh(render = true) {
  state = await api("/admin/api/state");
  $("#login").hidden = true;
  $("#app").hidden = false;
  if (render) renderPage();
}
function empty(title, note = "", action = "") {
  return `<div class="empty">${icon("cpu")}<b>${esc(title)}</b><span>${esc(note)}</span>${action}</div>`;
}
function heading(eyebrow, title, note, action = "") {
  return `<div class="page-head"><div><div class="eyebrow">${eyebrow}</div><h1>${title}</h1><p>${note}</p></div>${action ? `<div class="head-actions">${action}</div>` : ""}</div>`;
}
const button = (label, action, type = "", data = "") =>
  `<button class="button ${type}" data-action="${action}" ${data}>${icon(action.startsWith("add") ? "plus" : action.includes("connect") ? "link" : action === "copy-endpoint" ? "copy" : "arrow")}${label}</button>`;
function accountName(id) {
  return state.accounts.find((a) => a.id === id)?.label || "حساب محذوف";
}
function renderPage() {
  page = location.hash.slice(1) || "overview";
  if (!pages.some((p) => p[0] === page)) page = "overview";
  $("#breadcrumb").textContent = pages.find((p) => p[0] === page)[1];
  $("#nav").innerHTML = pages
    .map(
      ([id, title, ico]) =>
        `<a class="nav-item ${page === id ? "active" : ""}" href="#${id}" aria-label="${title}" ${page === id ? 'aria-current="page"' : ""}>${icon(ico)}<span>${title}</span>${id === "accounts" ? `<b class="count">${String(state.accounts.length).padStart(2, "0")}</b>` : ""}</a>`,
    )
    .join("");
  $("#mobile-nav").innerHTML =
    pages
      .filter(([id]) =>
        ["overview", "accounts", "models", "playground"].includes(id),
      )
      .map(
        ([id, title, ico]) =>
          `<a href="#${id}" aria-label="${title}" class="tab-item ${page === id ? "active" : ""}" ${page === id ? 'aria-current="page"' : ""}>${icon(ico)}<span>${{ overview: "الرئيسية", accounts: "الحسابات", models: "النماذج", playground: "المختبر" }[id]}</span></a>`,
      )
      .join("") +
    `<button class="tab-item ${["sessions", "keys", "activity", "connect"].includes(page) ? "active" : ""}" data-action="open-menu" aria-label="المزيد">${icon("more")}<span>المزيد</span></button>`;
  $("#menu-links").innerHTML = pages
    .filter(([id]) => ["sessions", "keys", "activity", "connect"].includes(id))
    .map(
      ([id, title, ico]) =>
        `<a href="#${id}" class="menu-link">${icon(ico)}<span>${title}</span>${icon("arrow")}</a>`,
    )
    .join("");
  $("#main").innerHTML = {
    overview: overview,
    accounts: accountsPage,
    models: modelsPage,
    sessions: sessionsPage,
    keys: keysPage,
    playground: playgroundPage,
    activity: activityPage,
    connect: connectPage,
  }[page]();
  $("#main")
    .querySelectorAll("table")
    .forEach((table) => {
      const labels = [...table.querySelectorAll("thead th")].map(
        (th) => th.textContent,
      );
      table
        .querySelectorAll("tbody tr")
        .forEach((row) =>
          [...row.cells].forEach(
            (cell, i) => (cell.dataset.label = labels[i] || ""),
          ),
        );
    });
  if (lastRenderedPage !== page)
    window.scrollTo({ top: 0, behavior: "instant" });
  if (
    lastRenderedPage !== page &&
    !matchMedia("(prefers-reduced-motion: reduce)").matches
  )
    $("#main").animate(
      [
        { opacity: 0, transform: "translateY(9px)" },
        { opacity: 1, transform: "translateY(0)" },
      ],
      { duration: 280, easing: "cubic-bezier(.22,.8,.32,1)" },
    );
  lastRenderedPage = page;
}
function overview() {
  const connected = state.accounts.filter(
      (a) => a.enabled && a.status === "connected",
    ).length,
    requests = state.stats.requests || 0,
    success = requests
      ? Math.round((100 * (state.stats.successes || 0)) / requests)
      : null;
  return (
    heading(
      "WORKSPACE / OVERVIEW",
      "مركز التحكم<span>.</span>",
      "حساباتك، نماذجك وتدفق محادثاتك. كل شيء من مكان واحد.",
      button("ربط حساب جديد", "add-account", "gold"),
    ) +
    `<section class="hero"><div class="hero-copy"><div class="hero-tag">INTELLIGENCE, ORCHESTRATED.</div><h2>نماذج متعددة.<br>تجربة واحدة، بطريقتك.</h2><p>اربط حساباتك، حدّد مشاريعك، وافتح احتمالات جديدة مع OpenMinis.</p><button class="button ghost" data-action="go-connect">إعداد اتصال التطبيق ${icon("arrow")}</button></div><div class="gateway-art" aria-hidden="true"><div class="gateway-ring"></div><div class="gateway-ring"></div><div class="gateway-ring"></div><div class="gateway-core">A</div><span class="art-coord mono">THE CONNECTION IS YOURS.</span></div></section>` +
    `<section class="stat-grid" aria-label="إحصائيات المساحة">${[
      [connected, "حساب متصل", "cpu", `${state.accounts.length} حساب مسجّل`],
      [
        state.routes.filter((r) => r.enabled).length,
        "مسار نموذج",
        "route",
        "متحكم به من لوحة الإدارة",
      ],
      [requests, "طلب خلال 24 ساعة", "pulse", "من سجل الطلبات الفعلي"],
      [
        success === null ? "—" : success + "%",
        "نسبة نجاح الطلبات",
        "check",
        requests ? "خلال آخر 24 ساعة" : "تظهر بعد أول طلب",
      ],
    ]
      .map(
        ([value, title, ico, note]) =>
          `<article class="stat"><div class="stat-top"><span>${title}</span><span class="stat-icon">${icon(ico)}</span></div><div class="stat-number">${esc(value)}</div><div class="stat-foot">${esc(note)}</div></article>`,
      )
      .join("")}</section>` +
    `<div class="two-column"><section class="panel"><div class="panel-heading"><h2>${icon("pulse")}تدفق الطلبات</h2><span class="badge neutral">آخر 7 أيام</span></div>${chart()}<div class="metrics-strip"><span>زمن التوليد المتوسط <b>${state.stats.latency ? state.stats.latency.toFixed(1) + "s" : "—"}</b></span><span>المحادثات المحفوظة <b>${state.conversations.length}</b></span></div></section><section class="panel"><div class="panel-heading"><h2>${icon("cpu")}الحسابات المتصلة</h2><a href="#accounts" class="filter-note">عرض الكل ←</a></div>${
      state.accounts.length
        ? state.accounts
            .slice(0, 4)
            .map(
              (a) =>
                `<div class="provider-line">${logo(a.provider)}<div><h3>${esc(a.label)}</h3><small>${esc(providers[a.provider].name)} · ${a.catalog.models?.length || 0} نماذج</small></div>${badge(a)}<button class="icon-button" aria-label="إعداد ${esc(a.label)}" data-action="edit-account" data-id="${a.id}">${icon("sliders")}</button></div>`,
            )
            .join("")
        : empty(
            "بوابتك جاهزة للبدء",
            "أضف أول حساب ليظهر هنا.",
            button("إضافة حساب", "add-account", "ghost"),
          )
    }</section></div>` +
    `<section class="panel"><div class="panel-heading"><h2>${icon("route")}نماذجك الجاهزة للتطبيق</h2><a class="filter-note" href="#models">إدارة المسارات ←</a></div>${routesTable(state.routes.slice(0, 4))}</section>` +
    connection()
  );
}
function chart() {
  const days = [...Array(7)].map((_, i) => {
    const d = new Date();
    d.setDate(d.getDate() - 6 + i);
    return {
      key: d.toISOString().slice(0, 10),
      label: d.toLocaleDateString("ar", { weekday: "short" }),
    };
  });
  const counts = days.map(
    (d) => state.daily.find((x) => x.day === d.key)?.count || 0,
  );
  const max = Math.max(...counts, 1);
  const points = counts
    .map((v, i) => `${i * 100},${140 - (v / max) * 110}`)
    .join(" ");
  return `<div class="chart">${counts.some(Boolean) ? `<svg viewBox="0 0 600 160" preserveAspectRatio="none" aria-label="عدد الطلبات اليومي"><defs><linearGradient id="area" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#c8ad73" stop-opacity=".22"/><stop offset="100%" stop-color="#c8ad73" stop-opacity="0"/></linearGradient></defs><polygon points="0,160 ${points} 600,160" fill="url(#area)"/><polyline points="${points}" fill="none" stroke="#c8ad73" stroke-width="2" vector-effect="non-scaling-stroke"/></svg>` : '<span class="chart-note">ستظهر الحركة هنا عند بدء استخدام البروكسي</span>'}</div><div class="chart-days">${days.map((d) => `<span>${d.label}</span>`).join("")}</div>`;
}
function connection() {
  return `<section class="connection"><div class="connection-title">${icon("link")}<div><h3>بوابتك إلى OpenMinis وأي عميل OpenAI</h3><p>استخدم الرابط التالي مع مفتاح وصول تنشئه من اللوحة. تبقى توكنات حساباتك داخل الخادم.</p></div></div><div class="endpoint"><code>${esc(location.origin)}/v1</code><button data-action="copy-endpoint" aria-label="نسخ رابط البروكسي">${icon("copy")}</button></div></section>`;
}
function accountsPage() {
  return (
    heading(
      "CONTROL / ACCOUNTS",
      "حساباتك<span>، بلا حدود.</span>",
      "الصق التوكن أو الكوكيز؛ يجلب البروكسي النماذج والمشاريع والمحادثات للاختيار.",
      button("ربط حساب جديد", "add-account", "gold"),
    ) +
    `<div class="toolbar"><span class="filter-note">${state.accounts.length} حساب · ${state.accounts.filter((a) => a.source.startsWith("client:")).length} مسجل من التطبيقات</span><span class="badge neutral">ENCRYPTED CREDENTIAL VAULT</span></div><section class="cards">${state.accounts.map((a) => `<article class="account-card"><div class="card-top">${logo(a.provider)}${badge(a)}</div><h2>${esc(a.label)}</h2><p>${esc(providers[a.provider].note)}</p><div class="card-details"><div><small>نماذج الحساب</small><b>${a.catalog.models?.length || "—"}</b></div><div><small>مصدر التسجيل</small><b>${a.source === "dashboard" ? "لوحة الإدارة" : "تطبيق عميل · بانتظار تفعيلك"}</b></div><div><small>سلوك المحادثة</small><b>${modeLabel(a.settings.conversation_mode)}</b></div><div><small>المشروع الافتراضي</small><b>${esc((a.catalog.projects || []).find((p) => p.id === a.settings.project_id)?.name || (a.settings.project_id ? "مشروع محفوظ" : "خارج المشاريع"))}</b></div></div><div class="card-actions">${button("إدارة الحساب", "edit-account", "", `data-id="${a.id}"`)}${button(a.status === "connected" ? "تحديث الربط" : "ربط الحساب", "connect-account", "ghost", `data-id="${a.id}"`)}</div></article>`).join("")}<button class="add-card" data-action="add-account"><span class="add-icon">${icon("plus")}</span><b>أضف حسابًا إلى مساحتك</b><small>ChatGPT · Gemini · DeepSeek · Qwen</small></button></section>`
  );
}
function modeLabel(mode) {
  return (
    {
      automatic: "محادثة مستقلة لكل محادثة تطبيق",
      fixed: "متابعة محادثة مختارة",
      per_request: "محادثة جديدة لكل طلب",
    }[mode] || "تلقائي"
  );
}
function routeModeLabel(r) {
  const a = state.accounts.find((a) => a.id === r.account_id),
    m = (a?.catalog.models || []).find(
      (m) => m.id === (r.config.model || a.settings.model || "auto"),
    );
  return { work: "وضع العمل", research: "بحث معمّق", chat: "دردشة عادية" }[
    r.config.chatgpt_mode || m?.mode || a?.settings.chatgpt_mode || "chat"
  ];
}
function routesTable(routes) {
  return routes.length
    ? `<div class="table-wrap"><table><thead><tr><th>النموذج الظاهر في التطبيق</th><th>الحساب</th><th>المسار</th><th>الحالة</th><th>التحكم</th></tr></thead><tbody>${routes.map((r) => `<tr><td class="model-name mono" dir="ltr">${esc(r.id)}</td><td>${esc(accountName(r.account_id))}</td><td>${esc({ translation: "ترجمة فصول", glossary: "مصطلحات", review: "مراجعة" }[r.config.task] || routeModeLabel(r))}</td><td><span class="badge ${r.enabled ? "" : "disabled"}">${r.enabled ? "نشط" : "معطّل"}</span></td><td><div class="row-actions"><button class="button ghost" data-action="edit-route" data-id="${esc(r.id)}">التفاصيل</button></div></td></tr>`).join("")}</tbody></table></div>`
    : empty(
        "لا توجد مسارات بعد",
        "اربط حسابًا ليظهر نموذجه الافتراضي، ثم خصّص مسارات مستقلة لكل مهمة.",
      );
}
function modelsPage() {
  return (
    heading(
      "CONTROL / ROUTING",
      "وجّه كل نموذج<span> إلى مكانه.</span>",
      "اربط اسمًا في التطبيق بحساب ونموذج ومشروع أو محادثة محددة.",
      button("إضافة مسار نموذج", "add-route", "gold"),
    ) +
    `<div class="toolbar"><input class="search" id="model-search" aria-label="البحث في النماذج" placeholder="ابحث في أسماء النماذج أو الحسابات…"><span class="filter-note">كل مسار له إعدادات مستقلة؛ المحادثات الجديدة تُنشأ داخل مشروعه.</span></div><section class="panel" id="routes-table">${routesTable(state.routes)}</section>`
  );
}
function sessionsPage() {
  return (
    heading(
      "CONTROL / CONVERSATIONS",
      "سياق مستمر<span>، دون اختلاط.</span>",
      "حالة المحادثات تحفظ مشفّرة، وتفصل حسب حساب المزوّد ومفتاح العميل والمشروع.",
    ) +
    `<section class="panel"><div class="panel-heading"><h2>${icon("chat")}المحادثات الأخيرة</h2><small>آخر 100 محادثة · احتفاظ 30 يومًا</small></div>${state.conversations.length ? `<div class="table-wrap"><table><thead><tr><th>معرّف البروكسي</th><th>الحساب</th><th>العميل</th><th>آخر استخدام</th><th>التحكم</th></tr></thead><tbody>${state.conversations.map((c) => `<tr><td class="key-prefix">${esc(c.id.slice(0, 19))}…</td><td>${esc(accountName(c.account_id))}</td><td>${esc(state.keys.find((k) => k.id === c.client_id)?.name || (c.client_id === "admin" ? "مختبر الإدارة" : "مفتاح سابق"))}</td><td>${date(c.updated)}</td><td><button class="button ghost" data-action="reset-session" data-id="${c.id}">نسيان الربط المحلي</button></td></tr>`).join("")}</tbody></table></div>` : empty("لا توجد محادثات نشطة", "ابدأ من OpenMinis أو من مختبر المحادثة. لا ننشئ جلسات تجريبية وهمية.")}</section>`
  );
}
function keysPage() {
  return (
    heading(
      "ACCESS / API KEYS",
      "مفاتيحك<span>، وصلاحياتها.</span>",
      "أنشئ مفتاحًا لكل تطبيق، وحدد النماذج المسموح له باستعمالها.",
      button("إنشاء مفتاح وصول", "add-key", "gold"),
    ) +
    `<div class="help-box">مفتاح البروكسي يوضع في OpenMinis. توكن ChatGPT أو DeepSeek وكوكيز Gemini تُسجّل كحسابات في هذه اللوحة؛ لا تحتاج مشاركتها مع التطبيق.</div><section class="panel">${state.keys.length ? `<div class="table-wrap"><table><thead><tr><th>اسم العميل</th><th>المفتاح</th><th>النماذج المسموح بها</th><th>الحالة</th><th>التحكم</th></tr></thead><tbody>${state.keys.map((k) => `<tr><td>${esc(k.name)}</td><td class="key-prefix">${esc(k.prefix)}</td><td>${k.models.includes("*") ? "جميع النماذج" : esc(k.models.join("، "))}</td><td><span class="badge ${k.enabled ? "" : "disabled"}">${k.enabled ? "نشط" : "ملغى"}</span></td><td>${k.enabled ? `<button class="button danger" data-action="revoke-key" data-id="${k.id}">إلغاء المفتاح</button>` : "—"}</td></tr>`).join("")}</tbody></table></div>` : empty("ابدأ بمفتاح لـOpenMinis", "المفتاح الكامل يظهر مرة واحدة عند الإنشاء.")}</section>` +
    connection()
  );
}
function playgroundPage() {
  const routes = state.routes.filter((r) => r.enabled);
  if (!routes.some((r) => r.id === activePlayModel)) {
    activePlayModel = routes[0]?.id || "";
    chatHistory = [];
  }
  return (
    heading(
      "LAB / PLAYGROUND",
      "اختبر الاتصال<span> قبل الانطلاق.</span>",
      "المحادثة تستخدم الحساب والمسار الفعليين؛ لا توجد إجابات تجريبية مصطنعة.",
    ) +
    `<div class="playground"><section class="panel playground-controls"><label>مسار النموذج<select id="play-model" ${chatBusy ? "disabled" : ""}>${state.routes
      .filter((r) => r.enabled)
      .map(
        (r) =>
          `<option value="${esc(r.id)}" ${r.id === activePlayModel ? "selected" : ""}>${esc(r.id)} · ${esc(accountName(r.account_id))}</option>`,
      )
      .join(
        "",
      )}</select></label><button class="button ghost" data-action="clear-chat" ${chatBusy ? "disabled" : ""}>${icon("plus")}محادثة جديدة</button><p class="field-hint">تغيير النموذج يبدأ محادثة مستقلة. استدعاءات الأدوات تظهر هنا للمراجعة، ولا تُنفَّذ على الخادم.</p></section><section class="panel"><div class="chat-log" id="chat-log">${chatHistory.length ? chatHistory.map(chatBubble).join("") : empty("ماذا تريد أن تختبر؟", "ابدأ برسالة قصيرة للتحقق من حسابك.")}</div><form id="chat-form" class="chat-compose"><textarea id="chat-input" aria-label="رسالة الاختبار" placeholder="اكتب رسالة لاختبار الاتصال…" required></textarea><button class="button gold" type="submit" aria-label="إرسال" ${chatBusy ? "disabled" : ""}>${icon("send")}<span>إرسال</span></button></form></section></div>`
  );
}
function chatBubble(m) {
  return `<div class="chat-bubble ${m.role === "user" ? "user" : ""}"><small class="mono">${m.role === "user" ? "YOU" : "ASSISTANT"}</small>${esc(m.content || (m.tool_calls ? JSON.stringify(m.tool_calls, null, 2) : ""))}</div>`;
}
const eventNames = {
  completion: "توليد محادثة",
  account_registered: "تسجيل حساب",
  account_connect: "ربط حساب",
  chat_created: "إنشاء محادثة",
  route_saved: "تحديث مسار نموذج",
  account_deleted: "حذف حساب",
  key_revoked: "إلغاء مفتاح",
  credential_updated: "تحديث بيانات دخول حساب",
};
function activityPage() {
  return (
    heading(
      "OBSERVABILITY / ACTIVITY",
      "كل عملية<span>، واضحة.</span>",
      "سجل النتائج والأزمنة دون نسخ توكناتك أو نصوص محادثاتك.",
    ) +
    `<section class="panel"><div class="panel-heading"><h2>${icon("pulse")}آخر العمليات</h2><small>سجل محدود حتى 5000 عملية</small></div>${state.events.length ? `<div class="table-wrap"><table><thead><tr><th>العملية</th><th>الحساب</th><th>النموذج</th><th>النتيجة</th><th>الزمن</th><th>التاريخ</th></tr></thead><tbody>${state.events.map((e) => `<tr><td><span class="activity-dot ${e.status >= 400 ? "failed" : ""}"></span>${esc(eventNames[e.kind] || e.kind)}</td><td>${esc(accountName(e.account_id))}</td><td class="mono">${esc(e.model || "—")}</td><td><span class="badge ${e.status >= 400 ? "error" : ""}">${e.status >= 400 ? "تعذر التنفيذ" : "نجح"}</span></td><td class="mono">${e.duration ? e.duration.toFixed(2) + "s" : "—"}</td><td>${date(e.created)}</td></tr>`).join("")}</tbody></table></div>` : empty("الهدوء يسبق أول اتصال", "ستظهر عمليات الربط والمحادثات الحقيقية هنا.")}</section>`
  );
}
function connectPage() {
  return (
    heading(
      "CONNECT / OPENMINIS",
      "اتصال واحد<span> لكل قدراتك.</span>",
      "إعدادات واضحة لـOpenMinis على iPhone وللتطبيقات التي تدعم OpenAI.",
    ) +
    `<div class="docs-grid"><section class="panel"><div class="panel-heading"><h2>${icon("link")}الإعداد في OpenMinis</h2><span class="badge neutral">iOS</span></div><div class="panel-body"><ol class="steps"><li>اربط حسابًا من <b>الحسابات والمزوّدين</b>، واختر مشروع ChatGPT إن أردت إنشاء المحادثات داخله.</li><li>أنشئ مفتاحًا من <b>مفاتيح الوصول</b>، ثم أضف في OpenMinis مزوّدًا مخصصًا من نوع <b>OpenAI Compatible</b>.</li><li>ضع رابط Base URL التالي، والصق مفتاح البروكسي في API Key.</li><li>اجلب النماذج واختر اسم المسار الذي أعددته. استخدم <b>Chat Completions</b>، أو Responses API عند تفعيله صراحة في التطبيق.</li></ol><div class="code">${esc(location.origin)}/v1</div><div class="form-actions">${button("إنشاء مفتاح", "add-key", "gold")}${button("نسخ الرابط", "copy-endpoint", "ghost")}</div></div></section><section class="panel"><div class="panel-heading"><h2>${icon("route")}ما الذي يحدث للمحادثات؟</h2></div><div class="panel-body"><p class="lead">كل محادثة تطبيق جديدة تنشئ محادثة مستقلة لدى المزوّد، داخل مشروع ChatGPT المحدد. الطلبات التالية تستأنف عبر سجل الرسائل؛ لا نعتمد على أول رسالة نظام لربط الجميع معًا.</p><p class="lead">للتطبيقات التي تستطيع إرسال معرّف مستقل للمحادثة، استخدم الترويسة X-Conversation-ID. دونها نطابق التاريخ الكامل؛ إذا كان ملتبسًا نبدأ محادثة آمنة جديدة.</p><h3 class="section-title">الأدوات والمرفقات</h3><p class="lead">أدوات الوكيل تعمل بجسر نصي مع تحقق JSON Schema، وليست API أدوات أصلية لدى الحساب. الصور تُرسل بصيغة data URL. يدعم ChatGPT وGemini رفع الملفات؛ DeepSeek Telegram نصي.</p><h3 class="section-title">حسابات مسجلة من عميل</h3><p class="lead">يمكن لعميل مصرح له تسجيل توكن حساب عبر /v1/accounts/register. يظهر فورًا في لوحة الإدارة معطّلًا حتى تفعّله. لا نجمع ترويسات Bearer العشوائية ولا نكشف التوكنات في الردود.</p></div></section></div>` +
    connection()
  );
}
function modalHead(title, note, tag = "CONTROL / CONFIGURATION") {
  return `<div class="modal-head"><div><div class="eyebrow">${tag}</div><h2>${title}</h2><p>${note}</p></div><button class="icon-button" data-action="close-modal" aria-label="إغلاق النافذة">${icon("x")}</button></div><div class="modal-error" role="alert"></div>`;
}
function showModal(html) {
  $("#modal-content").innerHTML = html;
  $("#modal").showModal();
}
function options(items, value, key = "id", label = "name") {
  return items
    .map(
      (x) =>
        `<option value="${esc(x[key])}" ${x[key] === value ? "selected" : ""}>${esc(x[label] || x.title || "بدون عنوان")}</option>`,
    )
    .join("");
}
function addAccount() {
  showModal(
    modalHead(
      "اربط حسابك.",
      "اختر المزوّد والصق بيانات الدخول. نكتشف التفاصيل بدل أن تكتبها.",
      "VAULT / NEW ACCOUNT",
    ) +
      `<form id="account-add-form"><div class="provider-options">${Object.entries(
        providers,
      )
        .map(
          ([id, p], i) =>
            `<button class="provider-choice" type="button" data-action="choose-provider" data-provider="${id}" aria-pressed="${!i}">${logo(id)}${p.name}</button>`,
        )
        .join(
          "",
        )}</div><input type="hidden" name="provider" value="chatgpt"><label>اسم للحساب (اختياري)<input name="label" placeholder="مثل: حسابي الأساسي" maxlength="80"></label><label class="section-title">توكن الحساب أو الكوكيز<textarea name="credential" dir="ltr" required autocomplete="off" spellcheck="false" placeholder="الصق بيانات حسابك هنا"></textarea></label><p class="field-hint">ChatGPT وDeepSeek: توكن جلسة الحساب. Gemini: ترويسة كوكيز Google الكاملة. لا تستخدم مفتاح OpenAI API مع قالب ChatGPT.</p><div class="form-actions"><button class="button gold" type="submit">${icon("link")}حفظ وربط الحساب</button><button class="button ghost" type="button" data-action="close-modal">إلغاء</button></div></form>`,
  );
}
function accountSettings(a, isRoute = false, config = {}) {
  const s = { ...a.settings, ...config };
  const catalog = a.catalog;
  const selectedModel = (catalog.models || []).find((m) => m.id === s.model);
  const gptMode = s.chatgpt_mode || selectedModel?.mode || "chat";
  return `${a.provider === "chatgpt" ? `<div class="mode-card"><div class="mode-card-heading">${icon("chat")}<div><b>نوع محادثة ChatGPT</b><small>نوع المحادثة مستقل عن المشروع ومكان حفظها.</small></div></div><label>وضع ChatGPT<select name="chatgpt_mode">${(catalog.chatgpt_modes || [{ id: "chat", name: "دردشة عادية" }]).map((m) => `<option value="${esc(m.id)}" ${gptMode === m.id ? "selected" : ""}>${esc(m.name)}</option>`).join("")}</select></label><p class="field-hint">الدردشة العادية تشمل Auto وInstant وThinking. نماذج وضع العمل تظهر فقط عند اختيار وضع العمل.</p></div>` : ""}<div class="form-grid"><label>النموذج الفعلي<select name="model">${a.provider === "chatgpt" ? gptModelOptions(a, s.model, gptMode) : `<option value="">اختيار الحساب الافتراضي</option>${options(catalog.models || [], s.model)}`}</select><span class="field-hint" id="model-explainer">${a.provider === "chatgpt" ? esc(gptModelNote(a, s.model, gptMode)) : ""}</span></label><label>سلوك المحادثة<select name="conversation_mode">${["automatic", "fixed", "per_request"].map((mode) => `<option value="${mode}" ${s.conversation_mode === mode ? "selected" : ""}>${modeLabel(mode)}</option>`).join("")}</select></label>${a.provider === "chatgpt" ? `<label class="full">مشروع ChatGPT<select name="project_id"><option value="">خارج المشاريع</option>${s.project_id && !(catalog.projects || []).some((p) => p.id === s.project_id) ? `<option value="${esc(s.project_id)}" selected>المشروع المحفوظ</option>` : ""}${options(catalog.projects || [], s.project_id)}</select></label><div class="full"><button class="button ghost" type="button" data-action="more-projects">تحديث / مزيد من المشاريع</button></div>` : ""}<label class="full">المحادثة المختارة (عند اختيار متابعة محادثة)<select name="conversation_id"><option value="">اختر محادثة من الحساب</option>${s.conversation_id ? `<option value="${esc(s.conversation_id)}" selected>المحادثة المحفوظة</option>` : ""}${options(catalog.chats || [], s.conversation_id, "id", "title")}</select><span class="field-hint">اختيار مشروع يجلب محادثاته تلقائيًا. إنشاء محادثة تطبيق جديدة يستعمل المشروع في الوضع التلقائي.</span></label><div class="full row-actions"><button class="button ghost" type="button" data-action="load-chats">جلب المحادثات</button><button class="button ghost" type="button" data-action="more-chats">مزيد من المحادثات</button><button class="button ghost" type="button" data-action="new-remote-chat">إنشاء محادثة</button></div></div>${
    a.provider === "chatgpt"
      ? `<label class="section-title">مستوى التفكير<select name="reasoning_effort">${gptEffortOptions(a, s.model, s.reasoning_effort)}</select><span class="field-hint">تظهر المستويات التي يدعمها النموذج المحدد فقط.</span></label>`
      : ""
  }${["deepseek", "qwen"].includes(a.provider) ? `<div class="form-grid section-title"><label class="toggle"><input type="checkbox" name="thinking_enabled" ${s.thinking_enabled ? "checked" : ""}><span>تفعيل التفكير</span></label><label class="toggle"><input type="checkbox" name="search_enabled" ${s.search_enabled ? "checked" : ""}><span>تفعيل البحث</span></label></div>` : ""}${a.provider === "gemini" && !isRoute ? `<label class="toggle section-title"><input type="checkbox" name="gemini_paragraph_mode" ${s.gemini_paragraph_mode ? "checked" : ""}><span>تقسيم الفصل وفحص الفقرات<small>اختياري لـGemini فقط، ويعمل على مسار نوعه «ترجمة فصول». المحادثة العادية والأدوات ترسل النص كاملًا دائمًا.</small></span></label>` : ""}<details><summary>ضبط التوليد المتقدم</summary><div class="form-grid"><label>Temperature<input type="number" name="temperature" min="0" max="2" step="0.1" value="${s.temperature ?? 0.7}" ${a.provider !== "deepseek" ? "disabled" : ""}></label><label>الحد الأقصى للتوكنات<input type="number" name="max_tokens" min="256" max="131072" step="1" value="${s.max_tokens || 16384}" ${a.provider !== "deepseek" ? "disabled" : ""}></label></div><p class="field-hint">هذه القيم تمرر لخدمة DeepSeek Telegram. بروتوكولا ChatGPT وGemini لا يثبتان دعم هذه المعاملات؛ لا نعرضها كقدرات قابلة للتغيير فيهما.</p></details>`;
}
function gptModelOptions(a, selected, mode) {
  const values = (a.catalog.models || []).filter(
    (m) => (m.mode || "chat") === mode,
  );
  return (
    `<option value="">${mode === "chat" ? "Auto · اختيار الحساب التلقائي" : "اختيار النموذج الافتراضي لهذا الوضع"}</option>` +
    values
      .map(
        (m) =>
          `<option value="${esc(m.id)}" ${m.id === selected ? "selected" : ""}>${esc(m.name)}${m.id === "auto" ? "" : ` — ${esc(m.id)}`}</option>`,
      )
      .join("")
  );
}
function gptModelNote(a, id, mode) {
  const m = (a.catalog.models || []).find((m) => m.id === id);
  return mode === "work"
    ? "وضع العمل: يستخدم معرف نموذج العمل كما يعلنه حسابك."
    : mode === "research"
      ? "بحث معمّق: قد يستغرق وقتًا أطول من الدردشة."
      : !id || id === "auto"
        ? `Auto يترك الاختيار لـChatGPT. نموذج الحساب الحالي: ${a.catalog.auto_model || "تلقائي"}.`
        : `دردشة عادية · ${m?.lane === "thinking" ? "تفكير" : m?.lane === "instant" ? "رد سريع" : m?.lane === "auto" ? "اختيار تلقائي" : "نموذج الحساب"}`;
}
function gptEffortOptions(a, id, effort = "auto") {
  const model = (a.catalog.models || []).find((m) => m.id === (id || "auto"));
  return (
    `<option value="auto">تلقائي للنموذج</option>` +
    (model?.reasoning_efforts || [])
      .map(
        (x) =>
          `<option value="${esc(x)}" ${effort === x ? "selected" : ""}>${esc({ standard: "قياسي · Standard", extended: "ممتد · Extended" }[x] || x)}</option>`,
      )
      .join("")
  );
}
function updateGptControls(modeChanged = false) {
  const form = $("#modal form"),
    a = state.accounts.find((a) => a.id === activeDialogAccount);
  if (a?.provider !== "chatgpt") return;
  const mode = form.elements.chatgpt_mode.value;
  if (modeChanged) {
    form.elements.model.innerHTML = gptModelOptions(
      a,
      mode === "chat" ? "auto" : "",
      mode,
    );
    form.elements.model.value =
      mode === "chat"
        ? "auto"
        : (a.catalog.models || []).find((m) => m.mode === mode)?.id || "";
  }
  form.elements.reasoning_effort.innerHTML = gptEffortOptions(
    a,
    form.elements.model.value,
  );
  $("#model-explainer").textContent = gptModelNote(
    a,
    form.elements.model.value,
    mode,
  );
}
function editAccount(id) {
  const a = state.accounts.find((a) => a.id === id);
  if (!a) return;
  activeDialogAccount = id;
  chatCursor = null;
  chatOffset = 0;
  projectCursor = null;
  showModal(
    modalHead(
      esc(a.label),
      "الإعدادات الافتراضية لهذا الحساب. تستطيع تجاوزها لكل مسار نموذج مستقل.",
    ) +
      `<form id="account-settings-form" data-id="${a.id}"><div class="form-grid"><label>اسم الحساب<input name="label" value="${esc(a.label)}" required maxlength="80"></label><label class="toggle"><input type="checkbox" name="enabled" ${a.enabled ? "checked" : ""}><span>تفعيل الحساب<small>الحسابات المسجلة من التطبيقات تحتاج تفعيلك أولًا.</small></span></label></div><h3 class="section-title">النماذج والمشروع والمحادثات</h3>${accountSettings(a)}${(a.catalog.warnings || []).map((w) => `<p class="help-box">${esc(w)}</p>`).join("")}<details><summary>تحديث التوكن أو الكوكيز</summary><label class="section-title">بيانات الدخول الجديدة<textarea name="credential" dir="ltr" autocomplete="off" spellcheck="false" placeholder="الصق بيانات الدخول الجديدة فقط عند تحديثها"></textarea></label><p class="field-hint">يحافظ التحديث على الحساب ومفاتيح العملاء، ويعيد ضبط ربط المشاريع والمحادثات لحماية السياق. اخترها مجددًا بعد الربط.</p><button class="button ghost" type="button" data-action="rotate-credential" data-id="${a.id}">تحديث وربط الحساب</button></details><div class="form-actions"><button class="button gold" type="submit">${icon("check")}حفظ إعدادات الحساب</button><button class="button ghost" type="button" data-action="connect-account" data-id="${a.id}">تحديث بيانات الحساب</button><button class="button danger" type="button" data-action="delete-account" data-id="${a.id}">حذف الحساب</button></div></form>`,
  );
}
function editRoute(id = "") {
  const route = state.routes.find((r) => r.id === id);
  const a =
    state.accounts.find((a) => a.id === route?.account_id) ||
    state.accounts.find((a) => a.enabled && a.status === "connected");
  if (!a) {
    toast("اربط حسابًا أولًا لإنشاء مسار نموذج.", true);
    return;
  }
  activeDialogAccount = a.id;
  chatCursor = null;
  chatOffset = 0;
  showModal(
    modalHead(
      id ? "تفاصيل مسار النموذج" : "مسار جديد. إمكانيات جديدة.",
      "كل اسم هنا يظهر كنموذج مستقل في OpenMinis ويمكن ربطه بمشروع أو محادثة مختلفين.",
    ) +
      `<form id="route-form"><div class="form-grid"><label>اسم النموذج في التطبيق<input name="id" dir="ltr" value="${esc(id)}" placeholder="اختياري — ينشأ تلقائيًا" ${id ? "readonly" : ""}></label><label>الحساب<select id="route-account" name="account_id">${options(
        state.accounts.filter((a) => a.enabled),
        a.id,
        "id",
        "label",
      )}</select></label><label>نوع المسار<select name="task">${[
        ["chat", "محادثة عامة"],
        ["translation", "ترجمة فصول"],
        ["glossary", "استخراج مصطلحات"],
        ["review", "مراجعة"],
      ]
        .map(
          ([value, label]) =>
            `<option value="${value}" ${(route?.config.task || "chat") === value ? "selected" : ""}>${label}</option>`,
        )
        .join(
          "",
        )}</select></label><label class="toggle"><input type="checkbox" name="enabled" ${!route || route.enabled ? "checked" : ""}><span>تفعيل المسار<small>يظهر للعملاء الذين يملكون صلاحية استخدامه.</small></span></label></div><h3 class="section-title">وجهة النموذج والمحادثات</h3><div id="route-settings">${accountSettings(a, true, route?.config || {})}</div><div class="form-actions"><button class="button gold" type="submit">${icon("check")}حفظ مسار النموذج</button>${id ? `<button class="button danger" type="button" data-action="delete-route" data-id="${esc(id)}">حذف المسار</button>` : ""}</div></form>`,
  );
}
function addKey() {
  showModal(
    modalHead(
      "مفتاح اتصال جديد.",
      "استخدم مفتاحًا مستقلًا لكل تطبيق لتتمكن من إلغائه دون التأثير على بقية التطبيقات.",
      "ACCESS / CREATE KEY",
    ) +
      `<form id="key-form"><label>اسم التطبيق<input name="name" value="OpenMinis · iPhone" required maxlength="80"></label><label class="section-title">النماذج المسموح بها<select name="models" multiple size="6"><option value="*" selected>جميع النماذج الحالية والمستقبلية</option>${state.routes.map((r) => `<option value="${esc(r.id)}">${esc(r.id)}</option>`).join("")}</select></label><p class="field-hint">اختر جميع النماذج أو حدد مسارات بعينها. المفتاح الكامل سيظهر مرة واحدة.</p><div class="form-actions"><button class="button gold" type="submit">${icon("key")}إنشاء المفتاح</button></div></form>`,
  );
}
function settingsFrom(form) {
  const data = {};
  for (const key of [
    "model",
    "project_id",
    "conversation_id",
    "conversation_mode",
    "reasoning_effort",
    "chatgpt_mode",
  ])
    if (form.elements[key]) data[key] = form.elements[key].value;
  for (const key of [
    "thinking_enabled",
    "search_enabled",
    "gemini_paragraph_mode",
  ])
    if (form.elements[key]) data[key] = form.elements[key].checked;
  for (const key of ["temperature", "max_tokens"])
    if (form.elements[key] && !form.elements[key].disabled)
      data[key] = Number(form.elements[key].value);
  return data;
}
async function loadChats(more = false) {
  const form = $("#modal form");
  const project = form.elements.project_id?.value || "";
  const a = state.accounts.find((a) => a.id === activeDialogAccount);
  const seq = ++requestSeq;
  const query = new URLSearchParams({
    project,
    offset: String(more ? chatOffset : 0),
    limit: String(
      a.provider === "gemini" && more ? Math.min(200, chatOffset + 50) : 50,
    ),
  });
  if (more && chatCursor) query.set("cursor", chatCursor);
  const result = await api(`/admin/api/accounts/${a.id}/chats?${query}`);
  if (seq !== requestSeq || !form.isConnected) return;
  const select = form.elements.conversation_id;
  const selected = select.value;
  const old = more
    ? [...select.options]
        .filter((o) => o.value)
        .map((o) => ({ id: o.value, title: o.text }))
    : [];
  const items = [
    ...new Map([...old, ...result.items].map((c) => [c.id, c])).values(),
  ];
  select.innerHTML =
    '<option value="">اختر محادثة من الحساب</option>' +
    options(items, selected, "id", "title");
  chatCursor = result.next_cursor || result.cursor;
  chatOffset = items.length;
  toast(
    `جُلبت ${result.items.length} محادثة${a.provider === "gemini" ? " من أحدث محادثات الحساب" : ""}.`,
  );
}
async function moreProjects() {
  const a = state.accounts.find((a) => a.id === activeDialogAccount);
  const result = await api(
    `/admin/api/accounts/${a.id}/projects${projectCursor ? "?cursor=" + encodeURIComponent(projectCursor) : ""}`,
  );
  const select = $("#modal form").elements.project_id;
  const selected = select.value;
  const items = [
    ...new Map(
      [...(a.catalog.projects || []), ...result.items].map((p) => [p.id, p]),
    ).values(),
  ];
  a.catalog.projects = items;
  projectCursor = result.next_cursor || result.cursor;
  select.innerHTML =
    '<option value="">خارج المشاريع</option>' + options(items, selected);
  toast("تم تحديث قائمة المشاريع.");
}
async function act(name, element) {
  const id = element?.dataset.id;
  if (name === "mobile-logout") {
    await post("/admin/api/logout");
    showLogin();
    return;
  }
  if (name === "close-modal") {
    $("#modal").close();
    return;
  }
  if (name === "add-account") return addAccount();
  if (name === "edit-account") return editAccount(id);
  if (name === "add-route") return editRoute();
  if (name === "edit-route") return editRoute(id);
  if (name === "add-key") return addKey();
  if (name === "go-connect") {
    location.hash = "connect";
    return;
  }
  if (name === "choose-provider") {
    $("#account-add-form").elements.provider.value = element.dataset.provider;
    $$(".provider-choice").forEach((e) =>
      e.setAttribute("aria-pressed", String(e === element)),
    );
    return;
  }
  if (name === "open-menu") {
    $("#app-menu").showModal();
    return;
  }
  if (name === "close-menu") {
    $("#app-menu").close();
    return;
  }
  if (name === "install-app") {
    await window.aurumInstall?.();
    return;
  }
  if (name === "rotate-credential") {
    const form = $("#account-settings-form");
    await post(`/admin/api/accounts/${id}/credentials`, {
      credential: form.elements.credential.value,
    });
    form.elements.credential.value = "";
    await post(`/admin/api/accounts/${id}/connect`);
    await refresh();
    editAccount(id);
    toast("تم تحديث بيانات الدخول. اختر المشروع والمحادثة ثم احفظ.");
  }
  if (name === "connect-account") {
    await post(`/admin/api/accounts/${id}/connect`);
    await refresh();
    if ($("#modal").open) editAccount(id);
    toast("تم ربط الحساب وجلب بياناته.");
  }
  if (name === "delete-account") {
    if (
      !confirm(
        "حذف الحساب ومفاتيح ربط محادثاته المحلية؟ لن نحذف المحادثات من حساب المزوّد.",
      )
    )
      return;
    await api(`/admin/api/accounts/${id}`, { method: "DELETE" });
    $("#modal").close();
    await refresh();
    toast("حُذف الحساب من البروكسي.");
  }
  if (name === "delete-route") {
    if (!confirm("حذف مسار النموذج من قائمة التطبيق؟")) return;
    await api("/admin/api/routes/" + encodeURIComponent(id), {
      method: "DELETE",
    });
    $("#modal").close();
    await refresh();
  }
  if (name === "revoke-key") {
    if (
      !confirm(
        "إلغاء هذا المفتاح؟ سيتوقف التطبيق الذي يستعمله حتى تضبط مفتاحًا جديدًا.",
      )
    )
      return;
    await api("/admin/api/keys/" + id, { method: "DELETE" });
    await refresh();
  }
  if (name === "reset-session") {
    if (
      !confirm(
        "نسيان ربط هذه المحادثة في البروكسي؟ الرسالة التالية ستنشئ محادثة جديدة مع إعادة سياق العميل.",
      )
    )
      return;
    await post(`/admin/api/conversations/${id}/reset`);
    await refresh();
  }
  if (name === "copy-endpoint") {
    await navigator.clipboard.writeText(location.origin + "/v1");
    toast("نُسخ رابط البروكسي.");
  }
  if (name === "copy-key") {
    await navigator.clipboard.writeText($("#new-key").textContent);
    toast("نُسخ مفتاح الوصول.");
  }
  if (name === "load-chats") await loadChats();
  if (name === "more-chats") await loadChats(true);
  if (name === "more-projects") await moreProjects();
  if (name === "new-remote-chat") {
    const form = $("#modal form");
    const result = await post(
      `/admin/api/accounts/${activeDialogAccount}/new-chat`,
      {
        project_id: form.elements.project_id?.value || "",
        model: form.elements.model.value,
        chatgpt_mode: form.elements.chatgpt_mode?.value,
        reasoning_effort: form.elements.reasoning_effort?.value,
      },
    );
    if (!result.id) throw new Error("أنشأ المزوّد ردًا دون معرّف محادثة.");
    const option = new Option(result.title, result.id, true, true);
    form.elements.conversation_id.add(option);
    form.elements.conversation_mode.value = "fixed";
    toast("أُنشئت المحادثة. احفظ الإعدادات لتثبيت الاختيار.");
  }
  if (name === "clear-chat") {
    chatHistory = [];
    renderPage();
  }
}
document.addEventListener("click", async (event) => {
  const element = event.target.closest("[data-action]");
  if (!element) return;
  event.preventDefault();
  if (element.disabled) return;
  element.disabled = true;
  try {
    await act(element.dataset.action, element);
  } catch (error) {
    toast(error.message, true);
    const modalError = $("#modal .modal-error");
    if (modalError && $("#modal").open) {
      modalError.className = "modal-error error-message";
      modalError.textContent = error.message;
    }
  } finally {
    if (element.isConnected) element.disabled = false;
  }
});
document.addEventListener("change", async (event) => {
  try {
    if (event.target.name === "chatgpt_mode") updateGptControls(true);
    if (event.target.name === "model") updateGptControls();
    if (event.target.name === "project_id") {
      $("#modal form").elements.conversation_id.value = "";
      chatOffset = 0;
      chatCursor = null;
      await loadChats();
    }
    if (event.target.id === "route-account") {
      requestSeq++;
      activeDialogAccount = event.target.value;
      const a = state.accounts.find((a) => a.id === activeDialogAccount);
      $("#route-settings").innerHTML = accountSettings(a, true);
      chatOffset = 0;
      chatCursor = null;
    }
    if (event.target.id === "play-model") {
      activePlayModel = event.target.value;
      chatHistory = [];
      $("#chat-log").innerHTML = empty(
        "محادثة جديدة",
        "جاهز لاختبار هذا النموذج.",
      );
    }
  } catch (error) {
    toast(error.message, true);
  }
});
document.addEventListener("input", (event) => {
  if (event.target.id === "model-search") {
    const q = event.target.value.toLowerCase();
    $("#routes-table").innerHTML = routesTable(
      state.routes.filter((r) =>
        (r.id + " " + accountName(r.account_id)).toLowerCase().includes(q),
      ),
    );
  }
});
document.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  const submit = form.querySelector("[type=submit]");
  if (submit?.disabled) return;
  if (submit) submit.disabled = true;
  try {
    if (form.getAttribute("id") === "login-form") {
      await post("/admin/api/login", { password: $("#login-password").value });
      $("#login-password").value = "";
      await refresh();
    }
    if (form.getAttribute("id") === "account-add-form") {
      const values = Object.fromEntries(new FormData(form));
      const created = await post("/admin/api/accounts", values);
      form.elements.credential.value = "";
      await refresh();
      try {
        await post(`/admin/api/accounts/${created.id}/connect`);
        await refresh();
        editAccount(created.id);
        toast("تم الربط. اختر إعداداتك ثم احفظ.");
      } catch (error) {
        $("#modal").close();
        await refresh();
        throw error;
      }
    }
    if (form.getAttribute("id") === "account-settings-form") {
      await api("/admin/api/accounts/" + form.dataset.id, {
        method: "PATCH",
        body: JSON.stringify({
          label: form.elements.label.value,
          enabled: form.elements.enabled.checked,
          settings: settingsFrom(form),
        }),
      });
      $("#modal").close();
      await refresh();
      toast("حُفظت إعدادات الحساب.");
    }
    if (form.getAttribute("id") === "route-form") {
      await post("/admin/api/routes", {
        id: form.elements.id.value,
        account_id: form.elements.account_id.value,
        enabled: form.elements.enabled.checked,
        config: { ...settingsFrom(form), task: form.elements.task.value },
      });
      $("#modal").close();
      await refresh();
      toast("حُفظ مسار النموذج وأصبح متاحًا للتطبيق.");
    }
    if (form.getAttribute("id") === "key-form") {
      let models = [...form.elements.models.selectedOptions].map(
        (o) => o.value,
      );
      if (models.includes("*")) models = ["*"];
      const result = await post("/admin/api/keys", {
        name: form.elements.name.value,
        models,
      });
      showModal(
        modalHead(
          "مفتاحك جاهز.",
          "انسخه الآن؛ لن نعرض المفتاح الكامل مرة أخرى.",
          "ACCESS / KEY CREATED",
        ) +
          `<div class="key-reveal"><code id="new-key">${esc(result.key)}</code><p>هذا مفتاح البروكسي، وليس توكن حساب المزوّد.</p></div><div class="form-actions">${button("نسخ المفتاح", "copy-key", "gold")}${button("إغلاق", "close-modal", "ghost")}</div>`,
      );
      await refresh();
    }
    if (form.getAttribute("id") === "chat-form") {
      const text = $("#chat-input").value.trim(),
        model = $("#play-model").value;
      if (!text || !model) throw new Error("اختر نموذجًا متصلًا واكتب رسالة.");
      chatBusy = true;
      $("#play-model").disabled = true;
      chatHistory.push({ role: "user", content: text });
      $("#chat-input").value = "";
      $("#chat-log").innerHTML =
        chatHistory.map(chatBubble).join("") +
        '<div class="field-hint" id="pending-reply"><span class="loading-dot"></span>المزوّد يُعِد الرد…</div>';
      try {
        const result = await post("/admin/api/playground", {
          model,
          messages: chatHistory,
          stream: false,
        });
        chatHistory.push(result.choices[0].message);
      } catch (error) {
        chatHistory.pop();
        if ($("#chat-input")) $("#chat-input").value = text;
        throw error;
      } finally {
        chatBusy = false;
        if ($("#chat-log")) {
          $("#chat-log").innerHTML = chatHistory.map(chatBubble).join("");
          $("#chat-log").scrollTop = $("#chat-log").scrollHeight;
        }
        if ($("#play-model")) $("#play-model").disabled = false;
        await refresh(false);
      }
    }
  } catch (error) {
    if (form.getAttribute("id") === "login-form")
      $("#login-error").textContent = error.message;
    else {
      toast(error.message, true);
      const alert = $("#modal .modal-error");
      if (alert && $("#modal").open) {
        alert.className = "modal-error error-message";
        alert.textContent = error.message;
      }
    }
  } finally {
    if (submit?.isConnected) submit.disabled = false;
  }
});
$("#refresh").addEventListener("click", () =>
  refresh()
    .then(() => toast("تم تحديث بيانات المساحة."))
    .catch((e) => toast(e.message, true)),
);
$("#logout").addEventListener("click", () =>
  post("/admin/api/logout")
    .then(showLogin)
    .catch((e) => toast(e.message, true)),
);
$("#menu-links").addEventListener("click", (event) => {
  if (event.target.closest("a")) $("#app-menu").close();
});
window.addEventListener("hashchange", () => {
  if (state) renderPage();
});
$("#modal").addEventListener("close", () => {
  requestSeq++;
  activeDialogAccount = null;
  $("#modal-content").innerHTML = "";
});
refresh().catch((error) => {
  showLogin();
  if (!error.message.includes("سجّل دخولك"))
    $("#login-error").textContent = error.message;
});
