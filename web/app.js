"use strict";

const $ = (selector) => document.querySelector(selector);
const pages = [...$("#tabs").querySelectorAll("button")].map((button) => button.dataset.page);
const fragment = new URLSearchParams(location.hash.slice(1));
let key = fragment.get("key") || sessionStorage.getItem("life-key") || "";
if (fragment.has("key")) history.replaceState(null, "", location.pathname);
let state;
let observedAt = 0;
let page = "focus";
let chartOffset = 0;
let chartRange = 7;
let structure = "";
let paintedMeter = "";
let updating = false;
let refreshing = false;
let revision = 0;
let dialogAction;

function element(tag, text = "", className = "") {
  const node = document.createElement(tag);
  node.textContent = text;
  if (className) node.className = className;
  return node;
}

function button(text, label, className, action) {
  const node = element("button", text, className);
  node.type = "button";
  node.setAttribute("aria-label", label);
  node.title = label;
  node.addEventListener("click", action);
  return node;
}

function secondsLabel(value, withSeconds = false) {
  const seconds = Math.max(0, Math.floor(value || 0));
  const parts = [Math.floor(seconds / 3600), Math.floor(seconds / 60) % 60];
  if (withSeconds) return [...parts, seconds % 60].map((part) => String(part).padStart(2, "0")).join(":");
  return parts[0] + ":" + String(parts[1]).padStart(2, "0");
}

function category(id) {
  return state.categories.find((item) => item.id === id) || state.categories[0];
}

function currentTask() {
  return state?.tasks.find((task) => task.id === state.current);
}

function tell(message) {
  $("#connection").textContent = message;
  $("#connection").hidden = !message;
}

function showPair(message = "") {
  state = undefined;
  structure = "";
  $("#pair").hidden = false;
  $("#tabs").hidden = true;
  $("#settings").disabled = true;
  $("#options").disabled = true;
  for (const id of pages) $("#" + id).hidden = true;
  tell(message);
}

async function request(action) {
  const response = await fetch(action ? "/api/action" : "/api/state", {
    method: action ? "POST" : "GET",
    headers: { Authorization: "Bearer " + key, ...(action ? { "Content-Type": "application/json" } : {}) },
    body: action ? JSON.stringify(action) : undefined,
    cache: "no-store",
    signal: AbortSignal.timeout(12000)
  });
  const payload = await response.json();
  if (!response.ok) {
    if (response.status === 401) {
      sessionStorage.removeItem("life-key");
      key = "";
      if ($("#dialog").open) $("#dialog").close();
      showPair(payload.error);
    }
    throw new Error(payload.error);
  }
  return payload;
}

function accept(payload) {
  state = payload;
  observedAt = performance.now();
  sessionStorage.setItem("life-key", key);
  document.documentElement.dataset.skin = state.skin;
  $('meta[name="theme-color"]').content = state.skin === "campfire" ? "#211b32" : "#eee9f5";
  $("#pair").hidden = true;
  $("#tabs").hidden = false;
  $("#settings").disabled = false;
  tell("");
  const next = JSON.stringify([
    state.current, state.tasks.map(({elapsed, ...task}) => task),
    state.categories, state.history, state.last_category
  ]);
  if (next !== structure) {
    structure = next;
    renderPage();
  }
  paintClock();
  if (page === "chart") paintChart();
}

async function refresh() {
  if (!key || updating || refreshing) return;
  refreshing = true;
  const before = revision;
  try {
    const payload = await request();
    if (before === revision) accept(payload);
  } catch (error) {
    if (before === revision && key) {
      if (!state) showPair("Nie udało się połączyć z widgetem Windows. " + error.message);
      else tell("Połączenie z widgetem przerwane. " + error.message);
    }
  } finally {
    refreshing = false;
  }
}

async function change(action, fields = {}) {
  if (updating) throw new Error("Trwa zapisywanie. Spróbuj ponownie.");
  updating = true;
  revision++;
  try {
    accept(await request({ action, ...fields }));
  } finally {
    updating = false;
  }
}

function run(action, fields, nextPage) {
  change(action, fields).then(() => {
    if (nextPage) navigate(nextPage);
  }).catch((error) => tell(error.message));
}

function navigate(id) {
  if (!state) return;
  page = id;
  renderPage();
  paintClock();
  if (page === "chart") paintChart();
}

function renderPage() {
  for (const id of pages) $("#" + id).hidden = page !== id;
  for (const item of $("#tabs").querySelectorAll("button")) {
    if (item.dataset.page === page) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  }
  const task = currentTask();
  $("#options").disabled = page !== "focus" || !task;
  if (page === "tasks") paintTasks();
  if (page === "history") paintHistory();
  if (page === "add") {
    const select = $("#add-form").elements.cat;
    fillCategories(select, select.value || state.last_category);
  }
  if (page !== "focus") return;
  $("#task-name").textContent = task?.title || "Brak zadania";
  $("#task-name").title = task ? "Zmień nazwę" : "";
  $("#task-name").disabled = !task;
  $("#category").hidden = !task;
  $("#complete").disabled = !task;
  const toggle = $("#toggle");
  const label = task ? (task.running ? "Pauza" : "Start") : "Nowe zadanie";
  toggle.setAttribute("aria-label", label);
  toggle.title = label;
  toggle.textContent = task ? (task.running ? "Ⅱ" : "▶") : "+";
  if (task) {
    const cat = category(task.cat);
    $("#category").textContent = cat.name;
    $("#category").title = "Zmień kategorię";
    $("#category").style.setProperty("--category", cat.color);
  }
}

function paintClock() {
  if (!state) return;
  const running = state.tasks.find((task) => task.running);
  const extra = running ? Math.min(5, Math.max(0, (performance.now() - observedAt) / 1000)) : 0;
  const elapsed = (task) => task.elapsed + (task.running ? extra : 0);
  if (page === "tasks") {
    for (const time of $("#task-list").querySelectorAll("[data-time]")) {
      const task = state.tasks.find((item) => item.id === time.dataset.time);
      if (task) time.textContent = secondsLabel(elapsed(task), true) + " / " + secondsLabel(task.planned);
    }
  }
  if (page !== "focus") return;
  const task = currentTask();
  const time = task ? elapsed(task) : 0;
  const over = task && time >= task.planned;
  $("#elapsed").value = secondsLabel(time, true);
  $("#status").classList.toggle("over", Boolean(over));
  $("#status").textContent = task ? (over ? "Ponad plan +" + secondsLabel(time - task.planned, true)
    : (task.running ? "Skupienie" : (time ? "Pauza" : "Gotowe do startu"))) : "";
  $("#plan").textContent = task ? "z " + secondsLabel(task.planned) : "";
  const dial = $(".dial");
  const progress = task ? Math.min(1, time / task.planned) : 0;
  dial.classList.toggle("active", progress > 0 && progress < 1);
  dial.style.setProperty("--progress", (progress * 100) + "%");
  dial.style.setProperty("--angle", (progress * 360) + "deg");
  dial.style.setProperty("--ring", over ? "var(--alert)" : (task ? category(task.cat).color : "var(--accent)"));
  const daily = { ...state.days[state.today] };
  if (running) daily[running.cat] = (daily[running.cat] || 0) + extra;
  const total = Object.values(daily).reduce((a, b) => a + b, 0);
  const goal = Math.max(1, state.goal);
  const percent = Math.min(100, total / goal * 100);
  $("#daily-time").textContent = (state.skin === "campfire" ? "PD dziś " : "Dziś ") + secondsLabel(total) + " h";
  $("#daily-goal").textContent = percent >= 100 ? "Cel osiągnięty ✓" : "Cel " + secondsLabel(goal) + " h";
  const meter = $("#daily-meter");
  meter.setAttribute("aria-valuenow", String(Math.floor(percent)));
  const bars = state.categories.map((cat) => Math.round((daily[cat.id] || 0) / Math.max(goal, total) * 1000) / 10);
  const meterKey = JSON.stringify([bars, state.categories]);
  if (meterKey !== paintedMeter) {
    paintedMeter = meterKey;
    meter.replaceChildren(...state.categories.map((cat, i) => {
      const segment = element("span");
      segment.style.width = bars[i] + "%";
      segment.style.setProperty("--segment", cat.color);
      return segment;
    }));
  }
}

function paintTasks() {
  const list = $("#task-list");
  list.replaceChildren();
  for (const task of state.tasks) {
    const row = element("li", "", "entry");
    const details = element("div");
    const select = button(task.title, "Otwórz: " + task.title, "entry-title", () => run("select", { id: task.id }, "focus"));
    const time = element("small");
    time.dataset.time = task.id;
    details.append(select, time);
    const control = button(task.running ? "Ⅱ" : "▶", task.running ? "Pauza" : "Start", "soft entry-run",
      () => run(task.running ? "pause" : "start", { id: task.id }));
    row.append(details, control);
    list.append(row);
  }
  if (!state.tasks.length) list.append(element("li", "Brak otwartych zadań."));
}

function paintHistory() {
  const list = $("#history-list");
  list.replaceChildren();
  for (const entry of state.history) {
    const row = element("li", "", "entry");
    const details = element("div");
    details.append(element("span", entry.title), element("small", entry.date + " · " + category(entry.cat).name));
    const time = element("small", secondsLabel(entry.planned_original_s) + " → " + secondsLabel(entry.actual_s));
    row.append(details, time);
    list.append(row);
  }
  if (!state.history.length) list.append(element("li", "Brak zaliczonych zadań."));
}

function paintChart() {
  const end = new Date(state.today + "T12:00:00Z");
  end.setUTCDate(end.getUTCDate() + (7 - (end.getUTCDay() || 7)) + chartOffset * chartRange);
  const columns = Array.from({ length: chartRange }, (_, i) => {
    const day = new Date(end);
    day.setUTCDate(end.getUTCDate() - chartRange + i + 1);
    const values = state.days[day.toISOString().slice(0, 10)] || {};
    return { day, values, total: Object.values(values).reduce((sum, n) => sum + n, 0) };
  });
  const max = Math.max(1, state.goal, ...columns.map((col) => col.total));
  const totals = {};
  const container = $("#chart-body");
  container.replaceChildren();
  const label = (day) => day.toLocaleDateString("pl-PL", { day: "numeric", month: "short", timeZone: "UTC" });
  container.append(element("p", label(columns[0].day) + " – " + label(end)));
  for (let start = 0; start < chartRange; start += 7) {
    const week = element("div", "", "chart-week");
    for (const {day, values, total} of columns.slice(start, start + 7)) {
      const cell = element("div", "", "chart-day");
      const stack = element("div");
      cell.title = label(day) + " · " + secondsLabel(total) + " h";
      stack.setAttribute("role", "img");
      stack.setAttribute("aria-label", cell.title);
      for (const cat of state.categories) {
        const time = values[cat.id] || 0;
        if (!time) continue;
        totals[cat.id] = (totals[cat.id] || 0) + time;
        const segment = element("span");
        segment.style.height = (100 * time / max) + "%";
        segment.style.setProperty("--segment", cat.color);
        stack.append(segment);
      }
      cell.append(stack, element("time", day.getUTCDate()));
      week.append(cell);
    }
    container.append(week);
  }
  const legend = element("div", "", "chart-legend");
  for (const cat of state.categories) {
    if (!totals[cat.id]) continue;
    const item = element("span", cat.name + " " + secondsLabel(totals[cat.id]) + " h");
    item.style.setProperty("--segment", cat.color);
    legend.append(item);
  }
  if (legend.childElementCount) container.append(legend);
  $("#next").disabled = chartOffset === 0;
}

function fillCategories(select, selected) {
  select.replaceChildren(...state.categories.map((cat) => new Option(cat.name, cat.id)));
  select.value = category(selected).id;
}

function showDialog(title, content, action, submitLabel = "Zapisz") {
  if ($("#dialog").open) $("#dialog").close();
  $("#dialog-title").textContent = title;
  $("#dialog-fields").replaceChildren(...content);
  $("#dialog-error").hidden = true;
  $("#save").hidden = !action;
  $("#save").textContent = submitLabel;
  $("#cancel").textContent = action ? "Anuluj" : "Zamknij";
  dialogAction = action;
  $("#dialog").showModal();
}

function field(label, control) {
  const wrapper = element("label", label);
  wrapper.append(control);
  return wrapper;
}

function textDialog(title, label, value, action, task, fieldName, required, limit) {
  const input = element("input");
  input.name = fieldName;
  input.value = value;
  input.required = required;
  input.maxLength = limit;
  showDialog(title, [field(label, input)], (data) => change(action, { id: task.id, [fieldName]: data.get(fieldName) }),
    action === "complete" ? "Zalicz" : "Zapisz");
  input.select();
}

function rename(task) {
  textDialog("Zmień nazwę", "Nazwa zadania", task.title, "rename", task, "title", true, 200);
}

function menu(title, items) {
  showDialog(title, items.map(([label, action]) => button(label, label, "soft", action)));
}

for (const control of document.querySelectorAll("[data-page]")) {
  control.addEventListener("click", () => navigate(control.dataset.page));
}
$("#pair").addEventListener("submit", (event) => {
  event.preventDefault();
  key = new FormData(event.currentTarget).get("key").trim();
  refresh();
});
$("#toggle").addEventListener("click", () => {
  const task = currentTask();
  if (task) run(task.running ? "pause" : "start", { id: task.id });
  else navigate("add");
});
$("#task-name").addEventListener("click", () => rename(currentTask()));
$("#category").addEventListener("click", () => {
  const task = currentTask();
  const select = element("select");
  select.name = "cat";
  fillCategories(select, task.cat);
  showDialog("Zmień kategorię", [field("Kategoria", select)], (data) => change("category", { id: task.id, cat: data.get("cat") }));
});
$("#complete").addEventListener("click", () => {
  textDialog("Zalicz zadanie", "Następny krok (opcjonalnie)", "", "complete", currentTask(), "note", false, 1000);
});
$("#options").addEventListener("click", () => {
  const task = currentTask();
  menu(task.title, [
    ["Przedłuż", () => textDialog("Przedłuż", "Dodatkowy czas, np. 15", "", "extend", task, "duration", true, 24)],
    ["Usuń zadanie", () => showDialog("Usuń zadanie", [element("p", task.title)], () => change("delete", { id: task.id }), "Usuń")]
  ]);
});
$("#settings").addEventListener("click", () => {
  const select = element("select");
  select.name = "skin";
  select.append(new Option("Lawenda", "lavender"), new Option("Ognisko RPG", "campfire"));
  select.value = state.skin;
  showDialog("Wygląd", [field("Motyw", select)], (data) => change("skin", { skin: data.get("skin") }));
});
$("#cancel").addEventListener("click", () => $("#dialog").close());
$("#dialog-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!dialogAction || updating) return;
  $("#dialog-error").hidden = true;
  $("#save").disabled = true;
  try {
    await dialogAction(new FormData(event.currentTarget));
    $("#dialog").close();
  } catch (error) {
    $("#dialog-error").textContent = error.message;
    $("#dialog-error").hidden = false;
  } finally {
    $("#save").disabled = false;
  }
});
$("#add-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (updating) return;
  const form = event.currentTarget;
  try {
    await change("add", { ...Object.fromEntries(new FormData(form)), start: event.submitter?.value === "start" });
    form.reset();
    navigate("focus");
  } catch (error) {
    tell(error.message);
  }
});
$("#previous").addEventListener("click", () => { chartOffset--; paintChart(); });
$("#next").addEventListener("click", () => { chartOffset++; paintChart(); });
$("#chart-range").addEventListener("change", (event) => {
  chartRange = Number(event.target.value);
  chartOffset = 0;
  paintChart();
});

if (key) refresh();
else showPair();
setInterval(refresh, 2000);
setInterval(paintClock, 250);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
