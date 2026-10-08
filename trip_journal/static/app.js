"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = {
  trips: [],
  trip: null,
  photos: [],
  photosById: new Map(),
  day: null,
  activities: [],
  focus: null, // activity id the chat is focused on
  view: "write",
  busy: false,
};

const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const fmtDay = (iso, opts = { weekday: "short", day: "numeric", month: "short", year: "numeric" }) =>
  new Date(iso + "T12:00:00").toLocaleDateString(undefined, opts);
const fmtLong = (iso) => fmtDay(iso, { weekday: "long", day: "numeric", month: "long", year: "numeric" });
const fmtTime = (iso) => (iso ? iso.slice(11, 16) : "");
const dayNumber = (day) => state.trip.days.findIndex((d) => d.day === day) + 1;

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!res.ok) {
    let detail = await res.text();
    try { detail = JSON.parse(detail).detail || detail; } catch { /* plain text */ }
    throw new Error(detail);
  }
  return res.json();
}

// ---------- autosave: one debounced save per field ----------
const pendingSaves = new Map();
function scheduleSave(key, fn) {
  $("#save-state").textContent = "editing…";
  clearTimeout(pendingSaves.get(key)?.timer);
  const entry = { fn, timer: setTimeout(() => flushSave(key), 800) };
  pendingSaves.set(key, entry);
}
async function flushSave(key) {
  const entry = pendingSaves.get(key);
  if (!entry) return;
  pendingSaves.delete(key);
  clearTimeout(entry.timer);
  try {
    await entry.fn();
    if (!pendingSaves.size) $("#save-state").textContent = "saved";
  } catch (e) {
    $("#save-state").textContent = "not saved!";
    console.error(e);
  }
}
const flushAll = () => Promise.all([...pendingSaves.keys()].map(flushSave));
window.addEventListener("beforeunload", () => { flushAll(); });

// ---------- maps ----------
function makeMap(el) {
  const m = L.map(el, { zoomControl: true }).setView([30, 110], 3);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  }).addTo(m);
  return m;
}
const numberIcon = (n, cls = "") =>
  L.divIcon({
    className: "",
    html: `<div style="background:${css("--accent")};color:#fff;border:2px solid #fff;border-radius:50%;width:24px;height:24px;display:flex;align-items:center;justify-content:center;font:600 12px system-ui;box-shadow:0 1px 3px rgba(0,0,0,.4)" class="${cls}">${n}</div>`,
    iconSize: [24, 24],
    iconAnchor: [12, 12],
  });

const writeMap = makeMap("map");
const tripLayer = L.layerGroup().addTo(writeMap);
const dayLayer = L.layerGroup().addTo(writeMap);

function drawTrip() {
  tripLayer.clearLayers();
  const located = state.photos.filter((p) => p.lat != null);
  if (!located.length) return;
  L.polyline(located.map((p) => [p.lat, p.lon]), { color: css("--other"), weight: 2, opacity: 0.5 }).addTo(tripLayer);
  for (const p of located) {
    L.circleMarker([p.lat, p.lon], { radius: 3, color: css("--other"), weight: 1, fillOpacity: 0.5 })
      .on("click", () => selectDay(p.day).then(() => scrollToPhoto(p.id)))
      .addTo(tripLayer);
  }
}

function drawDay() {
  dayLayer.clearLayers();
  const located = state.photos.filter((p) => p.day === state.day && p.lat != null);
  if (!located.length) {
    const all = state.photos.filter((p) => p.lat != null);
    if (all.length) writeMap.fitBounds(L.latLngBounds(all.map((p) => [p.lat, p.lon])), { padding: [20, 20] });
    return;
  }
  L.polyline(located.map((p) => [p.lat, p.lon]), { color: css("--route"), weight: 3 }).addTo(dayLayer);
  for (const p of located) {
    L.circleMarker([p.lat, p.lon], {
      radius: 5, color: "#fff", weight: 1.5, fillColor: css("--route"), fillOpacity: p.location_estimated ? 0.4 : 0.9,
    })
      .bindPopup(`<img src="${p.thumb}" alt=""><b>${fmtTime(p.taken_at)}</b> ${escapeHtml(p.place || "")}`)
      .on("click", () => scrollToPhoto(p.id))
      .addTo(dayLayer);
  }
  state.activities.forEach((a, i) => {
    if (a.lat == null) return;
    L.marker([a.lat, a.lon], { icon: numberIcon(i + 1), title: activityName(a) })
      .on("click", () => scrollToActivity(a.id))
      .addTo(dayLayer);
  });
  writeMap.fitBounds(L.latLngBounds(located.map((p) => [p.lat, p.lon])), { padding: [30, 30], maxZoom: 15 });
}

// ---------- trips ----------
async function loadTrips(selectId) {
  state.trips = await api("/api/trips");
  const select = $("#trip-select");
  const has = state.trips.length > 0;
  $("#empty").hidden = has;
  select.hidden = !has;
  $(".tabs").hidden = !has;
  if (!has) {
    $("#write").hidden = $("#read").hidden = true;
    return;
  }
  select.innerHTML = state.trips.map((t) => `<option value="${t.id}">${escapeHtml(t.name)}</option>`).join("");
  const wanted = String(selectId ?? localGet("trip") ?? "");
  if (state.trips.some((t) => String(t.id) === wanted)) select.value = wanted;
  await loadTrip(select.value);
}
$("#trip-select").addEventListener("change", (e) => loadTrip(e.target.value));

async function loadTrip(id) {
  await flushAll();
  localSet("trip", id);
  const [trip, photos] = await Promise.all([api(`/api/trips/${id}`), api(`/api/trips/${id}/photos`)]);
  state.trip = trip;
  state.photos = photos;
  state.photosById = new Map(photos.map((p) => [p.id, p]));
  state.day = null;
  updateStats();
  renderDays();
  drawTrip();
  const savedDay = localGet(`day:${id}`);
  const first = trip.days.find((d) => d.day === savedDay) || trip.days[0];
  if (first) await selectDay(first.day);
  setView(localGet("view") || "write");
}

function updateStats() {
  const t = state.trip;
  const written = t.days.filter((d) => d.has_journal).length;
  $("#trip-stats").textContent =
    `${state.photos.length} photos · ${t.days.length} days · ${written} written` + (t.undated ? ` · ${t.undated} undated` : "");
}

// ---------- views ----------
function setView(view) {
  state.view = view;
  localSet("view", view);
  document.body.dataset.view = view;
  $("#write").hidden = view !== "write";
  $("#read").hidden = view !== "read";
  for (const b of $$(".tabs button")) b.setAttribute("aria-selected", String(b.dataset.view === view));
  if (view === "read") flushAll().then(renderStory);
  else setTimeout(() => writeMap.invalidateSize(), 0);
}
for (const b of $$(".tabs button")) b.addEventListener("click", () => setView(b.dataset.view));

// ---------- Write view: days ----------
function renderDays() {
  $("#days").innerHTML = state.trip.days
    .map(
      (d, i) => `
      <button class="day-item${d.day === state.day ? " active" : ""}" data-day="${d.day}">
        <div class="d">Day ${i + 1}${d.has_journal ? '<span class="dot" title="written"></span>' : ""}</div>
        <div class="p">${fmtDay(d.day)}</div>
        <div class="p">${[d.title || d.main_place, `${d.photos} 📷`].filter(Boolean).map(escapeHtml).join(" · ")}</div>
      </button>`
    )
    .join("");
  for (const el of $$(".day-item")) el.onclick = () => selectDay(el.dataset.day);
}

async function selectDay(day) {
  if (day === state.day) return;
  await flushAll();
  state.day = day;
  state.focus = null;
  localSet(`day:${state.trip.id}`, day);
  const data = await api(`/api/trips/${state.trip.id}/days/${day}`);
  $("#day-empty").hidden = true;
  $("#day-content").hidden = false;
  $("#day-label").textContent = `Day ${dayNumber(day)} · ${fmtLong(day)}`;
  $("#day-title").value = data.title;
  $("#journal").value = data.journal;
  $("#save-state").textContent = data.updated_at ? "saved" : "";
  $("#draft-box").hidden = true;
  state.activities = data.activities;
  renderActivities();
  renderChat(data.chat);
  renderFocus();
  renderDays();
  drawDay();
  $(".day-item.active")?.scrollIntoView({ block: "nearest", inline: "nearest" });
  $("#editor").scrollTop = 0;
}

function saveDay() {
  const body = { title: $("#day-title").value, journal: $("#journal").value };
  const trip = state.trip;
  const day = state.day;
  scheduleSave("day", async () => {
    await api(`/api/trips/${trip.id}/days/${day}`, { method: "PUT", body: JSON.stringify(body) });
    const d = trip.days.find((x) => x.day === day);
    if (d) {
      d.title = body.title;
      d.has_journal = body.journal.trim().length > 0;
      renderDays();
      updateStats();
    }
  });
}
$("#journal").addEventListener("input", saveDay);
$("#day-title").addEventListener("input", saveDay);

// ---------- Write view: activities ----------
const COLLAPSED_PHOTOS = 15;
const activityName = (a) => a.title || a.place || "Untitled activity";
const partOfDay = (iso) => {
  const h = Number(iso.slice(11, 13));
  return h < 5 ? "Night" : h < 12 ? "Morning" : h < 17 ? "Afternoon" : h < 21 ? "Evening" : "Night";
};
const timeRange = (a) => (fmtTime(a.start) === fmtTime(a.end) ? fmtTime(a.start) : `${fmtTime(a.start)}–${fmtTime(a.end)}`);

function renderActivities() {
  const box = $("#activities");
  box.innerHTML = "";
  state.activities.forEach((a, i) => {
    const card = document.createElement("div");
    card.className = "activity" + (a.id === state.focus ? " focused" : "");
    card.dataset.id = a.id;
    card.innerHTML = `
      <div class="act-head">
        <span class="act-time">${i + 1} · ${timeRange(a)}</span>
        <input class="act-title" placeholder="${escapeHtml(a.place || "Name this activity…")}" value="${escapeHtml(a.title)}">
        <span class="act-actions">
          <button type="button" class="ghost small ask" title="Talk to Claude about this activity (it sees more of its photos)">💬 Ask</button>
          ${i > 0 ? '<button type="button" class="ghost small merge" title="Merge into the activity above">⤒ Merge</button>' : ""}
        </span>
      </div>
      <div class="act-meta">${[a.place, `${a.photos} photo${a.photos === 1 ? "" : "s"}`].filter(Boolean).map(escapeHtml).join(" · ")}</div>
      <div class="grid"></div>
      <textarea class="act-notes" placeholder="What did you do here? Who were you with, what did you eat, what did it feel like?">${escapeHtml(a.notes)}</textarea>`;
    const grid = $(".grid", card);
    const visible = a.expanded ? a.photo_ids : a.photo_ids.slice(0, COLLAPSED_PHOTOS);
    visible.forEach((pid, j) => grid.appendChild(photoTile(state.photosById.get(pid), j > 0, a.photo_ids)));
    if (visible.length < a.photo_ids.length) {
      const more = document.createElement("button");
      more.type = "button";
      more.className = "ghost show-all";
      more.textContent = `Show all ${a.photo_ids.length} photos`;
      more.onclick = () => {
        a.expanded = true;
        more.remove();
        a.photo_ids.slice(COLLAPSED_PHOTOS).forEach((pid) => grid.appendChild(photoTile(state.photosById.get(pid), true, a.photo_ids)));
      };
      grid.after(more);
    }

    const title = $(".act-title", card);
    const notes = $(".act-notes", card);
    const save = () => {
      a.title = title.value;
      a.notes = notes.value;
      const body = JSON.stringify({ title: a.title, notes: a.notes });
      scheduleSave(`act:${a.id}`, () => api(`/api/activities/${a.id}`, { method: "PUT", body }));
    };
    title.addEventListener("input", save);
    notes.addEventListener("input", save);
    $(".ask", card).onclick = () => setFocus(a.id);
    $(".merge", card)?.addEventListener("click", async () => {
      await flushAll();
      const res = await api(`/api/activities/${a.id}/merge-previous`, { method: "POST" });
      updateActivities(res.activities);
    });
    box.appendChild(card);
  });
}

function photoTile(p, canSplit, list) {
  const fig = document.createElement("figure");
  fig.className = "tile";
  fig.dataset.photo = p.id;
  fig.innerHTML = `
    <img loading="lazy" src="${p.thumb}" alt="">
    <span class="t">${fmtTime(p.taken_at)}</span>
    <button type="button" class="star${p.starred ? " on" : ""}" title="Star: feature this photo">★</button>
    ${canSplit ? '<button type="button" class="split" title="Start a new activity from this photo">✂ new</button>' : ""}`;
  fig.addEventListener("click", () => openLightbox(list.map((id) => state.photosById.get(id)), list.indexOf(p.id)));
  $(".star", fig).addEventListener("click", async (e) => {
    e.stopPropagation();
    p.starred = p.starred ? 0 : 1;
    e.target.classList.toggle("on", !!p.starred);
    await api(`/api/photos/${p.id}/star`, { method: "PUT", body: JSON.stringify({ starred: !!p.starred }) });
  });
  $(".split", fig)?.addEventListener("click", async (e) => {
    e.stopPropagation();
    await flushAll();
    const res = await api(`/api/photos/${p.id}/split`, { method: "POST" });
    updateActivities(res.activities);
  });
  return fig;
}

function updateActivities(activities) {
  const expanded = new Set(state.activities.filter((a) => a.expanded).map((a) => a.id));
  for (const a of activities) a.expanded = expanded.has(a.id);
  state.activities = activities;
  for (const a of activities) for (const pid of a.photo_ids) state.photosById.get(pid).activity_id = a.id;
  if (state.focus && !activities.some((a) => a.id === state.focus)) state.focus = null;
  const scroll = $("#editor").scrollTop;
  renderActivities();
  renderFocus();
  drawDay();
  $("#editor").scrollTop = scroll;
}

function scrollToActivity(id) {
  $(`.activity[data-id="${id}"]`)?.scrollIntoView({ behavior: "smooth", block: "start" });
}
function scrollToPhoto(id) {
  const el = $(`.tile[data-photo="${id}"]`);
  if (!el) return;
  el.scrollIntoView({ behavior: "smooth", block: "center" });
  el.animate([{ outline: `3px solid ${css("--accent")}` }, { outline: "3px solid transparent" }], { duration: 1600 });
}

$("#suggest-titles").addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  await flushAll();
  btn.disabled = true;
  btn.textContent = "✨ Looking at photos…";
  try {
    const res = await api(`/api/trips/${state.trip.id}/days/${state.day}/suggest-titles`, { method: "POST" });
    updateActivities(res.activities);
  } catch (err) {
    alert(err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "✨ Suggest names";
  }
});

// ---------- Write view: chat ----------
function setFocus(id) {
  state.focus = id;
  renderFocus();
  for (const c of $$(".activity")) c.classList.toggle("focused", Number(c.dataset.id) === id);
  if (id) $("#chat-input").focus();
}
function renderFocus() {
  const a = state.activities.find((x) => x.id === state.focus);
  $("#focus-chip").hidden = !a;
  if (a) $("#focus-chip span").textContent = `About: ${activityName(a)} (${timeRange(a)})`;
}
$("#focus-chip button").addEventListener("click", () => setFocus(null));

function renderChat(messages) {
  const chat = $("#chat");
  chat.innerHTML = "";
  if (!messages.length) {
    chat.innerHTML =
      '<p class="muted">Claude looks at this day\'s activities, photos, times and places and asks you questions to help you remember. Press Send to start, or 💬 Ask on an activity to talk about just that one.</p>';
  }
  for (const m of messages) addBubble(m.role, m.content);
  chat.scrollTop = chat.scrollHeight;
}

function addBubble(role, text) {
  const el = document.createElement("div");
  el.className = `msg ${role}`;
  el.textContent = text;
  const chat = $("#chat");
  $("p.muted", chat)?.remove();
  chat.appendChild(el);
  chat.scrollTop = chat.scrollHeight;
  return el;
}

async function streamInto(url, body, el, scroller) {
  const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    el.textContent += decoder.decode(value, { stream: true });
    scroller.scrollTop = scroller.scrollHeight;
  }
}

function setBusy(busy) {
  state.busy = busy;
  for (const b of $$("#chat-form button[type=submit], #draft-btn")) b.disabled = busy;
}

$("#chat-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (state.busy) return;
  await flushAll(); // so Claude sees the latest journal and notes
  const input = $("#chat-input");
  const message = input.value.trim();
  const focus = state.focus;
  const a = state.activities.find((x) => x.id === focus);
  input.value = "";
  if (message || a) addBubble("user", (a ? `(About ${activityName(a)}) ` : "") + (message || "Let's talk about this one."));
  const reply = addBubble("assistant", "");
  reply.classList.add("pending");
  setBusy(true);
  try {
    await streamInto(`/api/trips/${state.trip.id}/days/${state.day}/chat`, { message, activity_id: focus }, reply, $("#chat"));
  } catch (err) {
    reply.textContent += `\n[error: ${err.message}]`;
  } finally {
    reply.classList.remove("pending");
    setBusy(false);
  }
});
$("#chat-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $("#chat-form").requestSubmit();
  }
});

$("#draft-btn").addEventListener("click", async () => {
  if (state.busy) return;
  await flushAll();
  const box = $("#draft-box");
  const text = $("#draft-text");
  box.hidden = false;
  text.textContent = "";
  text.classList.add("pending");
  setBusy(true);
  try {
    await streamInto(`/api/trips/${state.trip.id}/days/${state.day}/draft`, {}, text, box);
  } catch (err) {
    text.textContent += `\n[error: ${err.message}]`;
  } finally {
    text.classList.remove("pending");
    setBusy(false);
  }
});
$("#draft-append").addEventListener("click", () => {
  const journal = $("#journal");
  const draft = $("#draft-text").textContent.trim();
  journal.value = journal.value.trim() ? `${journal.value.trim()}\n\n${draft}` : draft;
  $("#draft-box").hidden = true;
  saveDay();
  journal.scrollIntoView({ behavior: "smooth", block: "center" });
});
$("#draft-discard").addEventListener("click", () => ($("#draft-box").hidden = true));

$("#clear-chat").addEventListener("click", async () => {
  if (!confirm("Clear the conversation for this day? Your journal and notes are kept.")) return;
  await api(`/api/trips/${state.trip.id}/days/${state.day}/chat`, { method: "DELETE" });
  renderChat([]);
});

// ---------- Read view ----------
let storyMap = null;
const GALLERY_LIMIT = 8;

function paragraphs(text) {
  return text
    .split(/\n\s*\n/)
    .map((p) => p.trim())
    .filter(Boolean)
    .map((p) => `<p>${escapeHtml(p).replace(/\n/g, "<br>")}</p>`)
    .join("");
}

/** Up to `limit` photos for a gallery: starred first, then spread across the rest, in time order. */
function galleryPick(ids, limit) {
  if (ids.length <= limit) return ids;
  const starred = ids.filter((id) => state.photosById.get(id).starred).slice(0, limit);
  const rest = ids.filter((id) => !starred.includes(id));
  const k = limit - starred.length;
  const spread = k <= 0 ? [] : k === 1 ? [rest[0]] : Array.from({ length: k }, (_, i) => rest[Math.round((i * (rest.length - 1)) / (k - 1))]);
  return ids.filter((id) => starred.includes(id) || spread.includes(id));
}

async function renderStory() {
  const days = await api(`/api/trips/${state.trip.id}/story`);
  const t = state.trip;
  const first = days[0]?.day;
  const last = days[days.length - 1]?.day;
  const story = $("#story");
  story.innerHTML = `
    <h1>${escapeHtml(t.name)}</h1>
    <div class="sub">${first ? `${fmtDay(first)} – ${fmtDay(last)} · ` : ""}${days.length} days · ${state.photos.length} photos</div>
    <div id="story-map"></div>
    <ol class="toc">${days
      .map((d, i) => `<li><a href="#day-${d.day}"><span class="n">Day ${i + 1}</span>${escapeHtml(d.title || t.days[i]?.main_place || fmtDay(d.day))}</a></li>`)
      .join("")}</ol>
    ${days.map((d, i) => chapterHtml(d, i)).join("")}`;

  drawStoryMap(days);
}

$("#story").addEventListener("click", (e) => {
  const img = e.target.closest("img[data-photo]");
  if (img) {
    const list = img.dataset.list.split(",").map(Number);
    openLightbox(list.map((id) => state.photosById.get(id)), list.indexOf(Number(img.dataset.photo)));
    return;
  }
  const more = e.target.closest(".more");
  if (more) {
    const ids = more.dataset.list.split(",").map(Number);
    more.parentElement.innerHTML = ids.map((id) => galleryImg(id, ids)).join("");
    return;
  }
  const write = e.target.closest(".unwritten a");
  if (write) selectDay(write.dataset.day).then(() => setView("write"));
});

function galleryImg(id, list) {
  const p = state.photosById.get(id);
  return `<img loading="lazy" src="${p.thumb}" alt="" data-photo="${id}" data-list="${list.join(",")}">`;
}

function chapterHtml(d, i) {
  const allIds = d.activities.flatMap((a) => a.photo_ids);
  const heroId = allIds.find((id) => state.photosById.get(id).starred);
  const written = d.journal.trim() || d.activities.some((a) => a.notes.trim());
  const title = d.title || state.trip.days[i]?.main_place || `Day ${i + 1}`;
  return `
    <section class="chapter" id="day-${d.day}">
      <div class="kicker">Day ${i + 1} · ${fmtLong(d.day)}</div>
      <h2>${escapeHtml(title)}</h2>
      ${heroId ? `<img class="hero" src="${state.photosById.get(heroId).thumb}" alt="" data-photo="${heroId}" data-list="${allIds.join(",")}">` : ""}
      <div class="prose">${paragraphs(d.journal)}</div>
      ${written ? "" : `<p class="unwritten">Nothing written for this day yet. <a data-day="${d.day}">Write it →</a></p>`}
      <ol class="timeline">${d.activities
        .map((a) => {
          const shown = galleryPick(a.photo_ids, GALLERY_LIMIT);
          const hidden = a.photo_ids.length - shown.length;
          return `
          <li>
            <div class="when">${fmtTime(a.start)}</div>
            <div class="what">
              <h3>${escapeHtml(a.title || a.place || partOfDay(a.start))}</h3>
              <div class="where">${[a.title && a.place ? a.place : "", timeRange(a)].filter(Boolean).map(escapeHtml).join(" · ")}</div>
              <div class="notes">${paragraphs(a.notes)}</div>
              <div class="gallery">${shown.map((id) => galleryImg(id, a.photo_ids)).join("")}${
                hidden > 0 ? `<button type="button" class="more" data-list="${a.photo_ids.join(",")}">+${hidden} more</button>` : ""
              }</div>
            </div>
          </li>`;
        })
        .join("")}</ol>
    </section>`;
}

function drawStoryMap(days) {
  if (storyMap) storyMap.remove();
  storyMap = makeMap("story-map");
  const located = state.photos.filter((p) => p.lat != null);
  if (!located.length) return;
  L.polyline(located.map((p) => [p.lat, p.lon]), { color: css("--route"), weight: 2.5, opacity: 0.8 }).addTo(storyMap);
  days.forEach((d, i) => {
    const pts = state.photos.filter((p) => p.day === d.day && p.lat != null);
    if (!pts.length) return;
    const lat = pts.reduce((s, p) => s + p.lat, 0) / pts.length;
    const lon = pts.reduce((s, p) => s + p.lon, 0) / pts.length;
    L.marker([lat, lon], { icon: numberIcon(i + 1), title: `Day ${i + 1}` })
      .on("click", () => document.getElementById(`day-${d.day}`).scrollIntoView({ behavior: "smooth" }))
      .addTo(storyMap);
  });
  storyMap.fitBounds(L.latLngBounds(located.map((p) => [p.lat, p.lon])), { padding: [30, 30] });
}

// ---------- lightbox ----------
const lightbox = { list: [], index: 0 };
function openLightbox(list, index) {
  lightbox.list = list;
  lightbox.index = Math.max(0, index);
  showLightbox();
  $("#lightbox").hidden = false;
}
function showLightbox() {
  const p = lightbox.list[lightbox.index];
  $("#lightbox img").src = p.thumb;
  $("#lightbox figcaption").textContent = [
    `${lightbox.index + 1} / ${lightbox.list.length}`,
    fmtTime(p.taken_at),
    p.place,
    p.camera,
  ].filter(Boolean).join(" · ");
  $("#lightbox .prev").hidden = lightbox.index === 0;
  $("#lightbox .next").hidden = lightbox.index === lightbox.list.length - 1;
}
function stepLightbox(d) {
  const i = lightbox.index + d;
  if (i < 0 || i >= lightbox.list.length) return;
  lightbox.index = i;
  showLightbox();
}
$("#lightbox figure").addEventListener("click", () => ($("#lightbox").hidden = true));
$("#lightbox").addEventListener("click", (e) => { if (e.target.id === "lightbox") $("#lightbox").hidden = true; });
$("#lightbox .prev").addEventListener("click", () => stepLightbox(-1));
$("#lightbox .next").addEventListener("click", () => stepLightbox(1));
document.addEventListener("keydown", (e) => {
  if ($("#lightbox").hidden) return;
  if (e.key === "Escape") $("#lightbox").hidden = true;
  if (e.key === "ArrowLeft") stepLightbox(-1);
  if (e.key === "ArrowRight") stepLightbox(1);
});

// ---------- import dialog ----------
let browsePath = "";
async function browse(path) {
  const list = $("#folder-list");
  try {
    const res = await api(`/api/folders?path=${encodeURIComponent(path)}`);
    browsePath = res.path;
    const parts = res.path ? res.path.split("/") : [];
    const crumbs = [`<a data-path="">${escapeHtml(res.root)}</a>`].concat(
      parts.map((name, i) => `<a data-path="${escapeHtml(parts.slice(0, i + 1).join("/"))}">${escapeHtml(name)}</a>`)
    );
    $("#folder-crumbs").innerHTML = crumbs.join(" / ");
    list.innerHTML = res.folders.length
      ? res.folders.map((f) => `<li data-name="${escapeHtml(f)}">📁 ${escapeHtml(f)}</li>`).join("")
      : '<li class="none">No sub-folders. Importing this folder takes every photo in it.</li>';
    for (const a of $$("#folder-crumbs a")) a.onclick = () => browse(a.dataset.path);
    for (const li of $$("#folder-list li[data-name]")) {
      li.onclick = () => browse(browsePath ? `${browsePath}/${li.dataset.name}` : li.dataset.name);
    }
    $("#import-start").textContent = `Import ${res.path ? `“${parts[parts.length - 1]}”` : "everything here"}`;
  } catch (err) {
    list.innerHTML = `<li class="none">${escapeHtml(err.message)}</li>`;
  }
}

$("#add-photos").addEventListener("click", () => {
  $("#trip-names").innerHTML = state.trips.map((t) => `<option value="${escapeHtml(t.name)}">`).join("");
  $("#import-log").hidden = true;
  $("#import-start").disabled = false;
  $("#import-dialog").showModal();
  browse(browsePath);
});
$("#import-close").addEventListener("click", () => $("#import-dialog").close());
$("#import-start").addEventListener("click", async () => {
  const trip = $("#import-trip").value.trim();
  if (!trip) return $("#import-trip").reportValidity();
  const log = $("#import-log");
  log.hidden = false;
  log.textContent = "Starting…";
  $("#import-start").disabled = true;
  try {
    await api("/api/imports", { method: "POST", body: JSON.stringify({ trip, folder: browsePath, geocode: $("#import-geocode").checked }) });
    for (;;) {
      await new Promise((r) => setTimeout(r, 1000));
      const s = await api("/api/imports/current");
      log.textContent = s.log.join("\n") + (s.error ? `\nError: ${s.error}` : "");
      log.scrollTop = log.scrollHeight;
      if (!s.running) {
        if (s.trip_id) await loadTrips(s.trip_id);
        break;
      }
    }
  } catch (err) {
    log.textContent += `\nError: ${err.message}`;
  } finally {
    $("#import-start").disabled = false;
  }
});

// ---------- utils ----------
function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}
function localGet(k) {
  try { return localStorage.getItem(k); } catch { return null; }
}
function localSet(k, v) {
  try { localStorage.setItem(k, v); } catch { /* private mode */ }
}

loadTrips().catch((e) => {
  console.error(e);
  alert("Couldn't load trips: " + e.message);
});
