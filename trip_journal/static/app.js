"use strict";

const $ = (sel) => document.querySelector(sel);
const state = { trip: null, photos: [], day: null, saveTimer: null, busy: false };

const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const fmtDay = (iso) =>
  new Date(iso + "T12:00:00").toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short", year: "numeric" });
const fmtTime = (iso) => (iso ? iso.slice(11, 16) : "");

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json();
}

// ---------- map ----------
const map = L.map("map", { zoomControl: true }).setView([30, 110], 3);
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
}).addTo(map);
const tripLayer = L.layerGroup().addTo(map);
const dayLayer = L.layerGroup().addTo(map);

function drawTrip() {
  tripLayer.clearLayers();
  const located = state.photos.filter((p) => p.lat != null);
  if (!located.length) return;
  L.polyline(located.map((p) => [p.lat, p.lon]), { color: css("--other"), weight: 2, opacity: 0.6 }).addTo(tripLayer);
  for (const p of located) {
    L.circleMarker([p.lat, p.lon], { radius: 4, color: css("--other"), weight: 1, fillOpacity: 0.6 })
      .on("click", () => selectDay(p.day, { focusPhoto: p }))
      .addTo(tripLayer);
  }
  map.fitBounds(L.latLngBounds(located.map((p) => [p.lat, p.lon])), { padding: [30, 30] });
}

function drawDay(focusPhoto) {
  dayLayer.clearLayers();
  const located = state.photos.filter((p) => p.day === state.day && p.lat != null);
  if (!located.length) return;
  const accent = css("--accent");
  L.polyline(located.map((p) => [p.lat, p.lon]), { color: css("--route"), weight: 3 }).addTo(dayLayer);
  for (const p of located) {
    const marker = L.circleMarker([p.lat, p.lon], {
      radius: 7, color: "#fff", weight: 2, fillColor: accent, fillOpacity: p.location_estimated ? 0.45 : 1,
    })
      .bindPopup(
        `<img src="${p.thumb}" alt=""><b>${fmtTime(p.taken_at)}</b> ${escapeHtml(p.place || "")}` +
          (p.location_estimated ? '<br><span class="muted">location estimated</span>' : "")
      )
      .addTo(dayLayer);
    if (focusPhoto && focusPhoto.id === p.id) setTimeout(() => marker.openPopup(), 300);
  }
  if (focusPhoto && focusPhoto.lat != null) {
    map.setView([focusPhoto.lat, focusPhoto.lon], Math.max(map.getZoom(), 14));
  } else {
    map.fitBounds(L.latLngBounds(located.map((p) => [p.lat, p.lon])), { padding: [40, 40], maxZoom: 15 });
  }
}

// ---------- trips & days ----------
async function loadTrips() {
  const trips = await api("/api/trips");
  const select = $("#trip-select");
  if (!trips.length) {
    $("#empty").hidden = false;
    select.hidden = true;
    return;
  }
  select.innerHTML = trips
    .map((t) => `<option value="${t.id}">${escapeHtml(t.name)}</option>`)
    .join("");
  const saved = localGet("trip");
  if (saved && trips.some((t) => String(t.id) === saved)) select.value = saved;
  select.onchange = () => loadTrip(select.value);
  await loadTrip(select.value);
}

async function loadTrip(id) {
  localSet("trip", id);
  const [trip, photos] = await Promise.all([api(`/api/trips/${id}`), api(`/api/trips/${id}/photos`)]);
  state.trip = trip;
  state.photos = photos;
  const written = trip.days.filter((d) => d.has_journal).length;
  $("#trip-stats").textContent =
    `${photos.length} photos · ${trip.days.length} days · ${written} written` +
    (trip.undated ? ` · ${trip.undated} undated` : "");
  renderDays();
  drawTrip();
  const savedDay = localGet(`day:${id}`);
  const first = trip.days.find((d) => d.day === savedDay) || trip.days[0];
  if (first) selectDay(first.day, { fit: false });
}

function renderDays() {
  $("#days").innerHTML = state.trip.days
    .map(
      (d, i) => `
      <button class="day-item${d.day === state.day ? " active" : ""}" data-day="${d.day}">
        <div class="d">Day ${i + 1}${d.has_journal ? '<span class="dot" title="has journal"></span>' : ""}</div>
        <div class="p">${fmtDay(d.day)}</div>
        <div class="p">${[d.title || d.main_place, `${d.photos} 📷`].filter(Boolean).map(escapeHtml).join(" · ")}</div>
      </button>`
    )
    .join("");
  for (const el of document.querySelectorAll(".day-item")) el.onclick = () => selectDay(el.dataset.day);
}

async function selectDay(day, { focusPhoto = null } = {}) {
  await flushSave();
  if (day !== state.day) {
    state.day = day;
    localSet(`day:${state.trip.id}`, day);
    const data = await api(`/api/trips/${state.trip.id}/days/${day}`);
    $("#day-panel").hidden = false;
    const idx = state.trip.days.findIndex((d) => d.day === day);
    $("#day-label").textContent = `Day ${idx + 1} · ${fmtDay(day)}`;
    $("#day-title").value = data.title;
    $("#journal").value = data.journal;
    $("#save-state").textContent = data.updated_at ? "saved" : "";
    $("#draft-box").hidden = true;
    renderPhotos();
    renderChat(data.chat);
    renderDays();
    document.querySelector(".day-item.active")?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }
  drawDay(focusPhoto);
}

function renderPhotos() {
  const photos = state.photos.filter((p) => p.day === state.day);
  const strip = $("#photo-strip");
  strip.innerHTML = photos
    .map((p) => `<figure data-id="${p.id}"><img loading="lazy" src="${p.thumb}" alt=""><figcaption>${fmtTime(p.taken_at)}</figcaption></figure>`)
    .join("");
  for (const fig of strip.querySelectorAll("figure")) {
    const p = photos.find((x) => String(x.id) === fig.dataset.id);
    fig.onclick = () => {
      openLightbox(p);
      if (p.lat != null) drawDay(p);
    };
  }
}

// ---------- journal autosave ----------
function scheduleSave() {
  $("#save-state").textContent = "editing…";
  clearTimeout(state.saveTimer);
  state.saveTimer = setTimeout(flushSave, 800);
}

async function flushSave() {
  if (!state.saveTimer) return;
  clearTimeout(state.saveTimer);
  state.saveTimer = null;
  const body = { title: $("#day-title").value, journal: $("#journal").value };
  try {
    await api(`/api/trips/${state.trip.id}/days/${state.day}`, { method: "PUT", body: JSON.stringify(body) });
    $("#save-state").textContent = "saved";
    const d = state.trip.days.find((x) => x.day === state.day);
    if (d) {
      d.title = body.title;
      d.has_journal = body.journal.trim().length > 0;
      renderDays();
    }
  } catch (e) {
    $("#save-state").textContent = "not saved!";
    console.error(e);
  }
}
$("#journal").addEventListener("input", scheduleSave);
$("#day-title").addEventListener("input", scheduleSave);
window.addEventListener("beforeunload", () => flushSave());

// ---------- chat ----------
function renderChat(messages) {
  const chat = $("#chat");
  chat.innerHTML = "";
  if (!messages.length) {
    chat.innerHTML = '<p class="muted">Claude will look at this day\'s photos, times and places and ask you questions to help you remember. Press Send to start.</p>';
  }
  for (const m of messages) addBubble(m.role, m.content);
}

function addBubble(role, text) {
  const el = document.createElement("div");
  el.className = `msg ${role}`;
  el.textContent = text;
  const chat = $("#chat");
  chat.querySelector("p.muted")?.remove();
  chat.appendChild(el);
  return el;
}

async function streamInto(url, body, el) {
  const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    el.textContent += decoder.decode(value, { stream: true });
    el.scrollIntoView({ block: "nearest" });
  }
}

function setBusy(busy) {
  state.busy = busy;
  for (const b of document.querySelectorAll("#chat-form button, #draft-btn")) b.disabled = busy;
}

$("#chat-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (state.busy) return;
  await flushSave(); // so Claude sees the latest journal text
  const input = $("#chat-input");
  const message = input.value.trim();
  input.value = "";
  if (message) addBubble("user", message);
  const reply = addBubble("assistant", "");
  reply.classList.add("pending");
  setBusy(true);
  try {
    await streamInto(`/api/trips/${state.trip.id}/days/${state.day}/chat`, { message }, reply);
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
  await flushSave();
  const box = $("#draft-box");
  const text = $("#draft-text");
  box.hidden = false;
  text.textContent = "";
  text.classList.add("pending");
  setBusy(true);
  try {
    await streamInto(`/api/trips/${state.trip.id}/days/${state.day}/draft`, {}, text);
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
  scheduleSave();
});
$("#draft-discard").addEventListener("click", () => ($("#draft-box").hidden = true));

$("#clear-chat").addEventListener("click", async () => {
  if (!confirm("Clear the conversation for this day? Your journal is kept.")) return;
  await api(`/api/trips/${state.trip.id}/days/${state.day}/chat`, { method: "DELETE" });
  renderChat([]);
});

// ---------- lightbox ----------
function openLightbox(p) {
  const lb = $("#lightbox");
  lb.querySelector("img").src = p.thumb;
  lb.querySelector(".caption").textContent = [fmtTime(p.taken_at), p.place, p.camera].filter(Boolean).join(" · ");
  lb.hidden = false;
}
$("#lightbox").addEventListener("click", () => ($("#lightbox").hidden = true));
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") $("#lightbox").hidden = true;
});

// ---------- utils ----------
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
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
