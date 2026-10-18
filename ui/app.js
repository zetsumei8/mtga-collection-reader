(() => {
  "use strict";

  const byId = (id) => document.getElementById(id);
  const PAGE_NAMES = { sync: "Sync", collection: "Library", history: "Snapshots", settings: "Settings" };
  const state = { backend: null, page: "sync", search: "", reviewOnly: false, offset: 0, pageSize: 80, sort: "name", reverse: false, total: 0, toastTimer: 0, searchTimer: 0, logs: [], busy: false };

  function call(method, ...args) {
    return new Promise((resolve, reject) => {
      if (!state.backend || typeof state.backend[method] !== "function") {
        reject(new Error("The desktop interface is still connecting."));
        return;
      }
      try {
        state.backend[method](...args, resolve);
      } catch (error) {
        reject(error);
      }
    });
  }

  function safely(action) {
    Promise.resolve().then(action).catch((error) => showToast(error?.message || String(error), true));
  }

  function formatNumber(value) {
    return Number(value || 0).toLocaleString();
  }

  function showToast(message, isError = false) {
    const toast = byId("toast");
    toast.textContent = message;
    toast.classList.toggle("is-error", isError);
    toast.classList.add("is-visible");
    window.clearTimeout(state.toastTimer);
    state.toastTimer = window.setTimeout(() => toast.classList.remove("is-visible"), 3600);
  }

  function setStatus(title, detail, kind = "ready") {
    const readableTitle = String(title || "Ready to sync").toLowerCase();
    byId("statusTitle").textContent = readableTitle.charAt(0).toUpperCase() + readableTitle.slice(1);
    byId("statusDetail").textContent = detail || "";
    const led = byId("activityLed");
    led.className = `status-led ${kind === "error" ? "is-error" : kind === "warn" ? "is-offline" : kind === "working" ? "is-checking" : "is-ready"}`;
  }

  function setArena(running, error = "") {
    const sideLed = byId("sideLed");
    const mainLed = byId("mainLed");
    const chip = byId("connectionChip");
    let title, hint, connection, css, heroHint;
    if (running === true) {
      title = "ARENA RUNNING";
      hint = "Stay on the Home screen to sync";
      connection = "Arena is open";
      css = "is-running";
      heroHint = "Arena is open. Sign in and wait for the Home screen before syncing.";
      byId("routeArena").className = "route-step is-done";
      byId("routeHome").className = "route-step is-current";
      byId("routeCapture").className = "route-step";
      byId("routeArenaState").textContent = "DETECTED";
      byId("routeHomeState").textContent = "NEXT";
      byId("routeCaptureState").textContent = "READY";
    } else if (running === false) {
      title = "ARENA NOT OPEN";
      hint = "Open MTG Arena to enable sync";
      connection = "Arena is closed";
      css = "is-offline";
      heroHint = "Open Arena, sign in, and wait for the Home screen.";
      byId("routeArena").className = "route-step is-current";
      byId("routeHome").className = "route-step";
      byId("routeCapture").className = "route-step";
      byId("routeArenaState").textContent = "WAITING";
      byId("routeHomeState").textContent = "NEXT";
      byId("routeCaptureState").textContent = "READY";
    } else {
      title = "ARENA STATUS UNKNOWN";
      hint = "Sync can still try the live connection";
      connection = "Status unavailable";
      css = "is-checking";
      heroHint = "Arena status is unavailable. Sync can still try the live connection.";
      byId("routeArena").className = "route-step is-current";
      byId("routeHome").className = "route-step";
      byId("routeCapture").className = "route-step";
      byId("routeArenaState").textContent = "CHECK";
      byId("routeHomeState").textContent = "NEXT";
      byId("routeCaptureState").textContent = "READY";
    }
    byId("sideArena").textContent = title.charAt(0) + title.slice(1).toLowerCase();
    byId("sideArenaHint").textContent = hint;
    byId("connectionText").textContent = connection;
    sideLed.className = `status-led ${css}`;
    mainLed.className = `status-led ${css}`;
    chip.title = error || hint;
    byId("syncHint").textContent = error ? `${heroHint} ${error}` : heroHint;
    if (error) addLog({ timestamp: new Date().toLocaleTimeString(), level: "WARN", message: `Could not check for Arena: ${error}` });
  }

  function showPage(name) {
    if (!PAGE_NAMES[name]) return;
    state.page = name;
    document.querySelectorAll(".page").forEach((page) => page.classList.toggle("is-visible", page.id === `page-${name}`));
    document.querySelectorAll(".nav-item").forEach((button) => button.classList.toggle("is-active", button.dataset.page === name));
    byId("crumbPage").textContent = PAGE_NAMES[name];
    byId("toolsMenu").hidden = true;
    byId("toolsToggle").setAttribute("aria-expanded", "false");
    if (name === "collection") safely(loadCollection);
    if (name === "history") safely(loadHistory);
  }

  function setMetrics(metrics = {}) {
    byId("ownedCopies").textContent = formatNumber(metrics.owned_copies);
    byId("uniqueCards").textContent = formatNumber(metrics.unique_entries);
    byId("reviewCount").textContent = formatNumber(metrics.needs_review);
    byId("allCardCount").textContent = formatNumber(metrics.unique_entries);
    byId("reviewCardCount").textContent = formatNumber(metrics.review_cards ?? metrics.needs_review);
    byId("collectionCaption").textContent = metrics.unique_entries
      ? `${formatNumber(metrics.unique_entries)} card records kept in your private Arena archive.`
      : "A complete, searchable record of the cards you own in Arena.";
  }

  function showCardDetail(row) {
    byId("cardDetailName").textContent = row.name || `Unknown Arena card ${row.arena_id || ""}`;
    const set = row.set_code || "UNKNOWN SET";
    const printing = row.collector_number ? `No. ${row.collector_number}` : "Printing not identified";
    byId("cardDetailSet").textContent = `${set}  ·  ${printing}`;
    byId("cardDetailQuantity").textContent = formatNumber(row.quantity);
    byId("cardDetailArenaId").textContent = row.arena_id ?? "—";
    const review = Boolean(row.needs_review || row.printing_uncertain);
    byId("cardDetailStatus").textContent = review ? "Needs review" : "Matched";
    byId("cardDetailReview").hidden = !review;
    byId("cardDetailReview").textContent = row.review_reason || (row.printing_uncertain ? "The card identity is known, but the exact printing could not be confirmed." : "This Arena card ID needs a closer look.");
    byId("cardDetailFirstSeen").textContent = row.first_seen_at ? `First seen · ${formatDate(row.first_seen_at)}` : "First seen · —";
    byId("cardDetailLastSeen").textContent = row.last_seen_at ? `Last seen · ${formatDate(row.last_seen_at)}` : "Last seen · —";
    byId("cardDialog").showModal();
  }

  function renderHistory(rows = []) {
    const body = byId("historyBody");
    body.replaceChildren();
    for (const row of rows) {
      const tr = document.createElement("tr");
      const values = [formatDate(row.captured_at), row.source || "—", row.mtga_client_version || "—", formatNumber(row.unique_cards), formatNumber(row.total_copies), row.snapshot_id || "—"];
      values.forEach((value, index) => {
        const cell = document.createElement("td");
        cell.textContent = value;
        if (index >= 3) cell.className = "numeric";
        if (index === 5) cell.title = String(row.snapshot_id || "");
        tr.append(cell);
      });
      body.append(tr);
    }
    byId("historyEmpty").hidden = rows.length > 0;
  }

  function formatDate(value) {
    if (!value) return "—";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
  }

  async function loadHistory() {
    renderHistory(await call("snapshots"));
  }

  function renderCollection(result = {}) {
    const body = byId("collectionBody");
    body.replaceChildren();
    const rows = result.rows || [];
    for (const row of rows) {
      const tr = document.createElement("tr");
      const card = document.createElement("td");
      card.className = "card-name";
      card.textContent = row.name || `Unknown Arena card ${row.arena_id || ""}`;
      const subtitle = document.createElement("span");
      subtitle.className = "card-subtitle";
      subtitle.textContent = `Arena ID ${row.arena_id ?? "—"}`;
      card.append(subtitle);

      const printing = document.createElement("td");
      const set = document.createElement("span");
      set.className = "set-pill";
      set.textContent = row.set_code || "UNKNOWN SET";
      printing.append(set);
      const number = document.createElement("span");
      number.className = "card-subtitle";
      number.textContent = row.collector_number ? `No. ${row.collector_number}` : "Printing not identified";
      printing.append(number);

      const quantity = document.createElement("td");
      quantity.className = "numeric quantity";
      quantity.textContent = formatNumber(row.quantity);

      const status = document.createElement("td");
      const review = Boolean(row.needs_review || row.printing_uncertain);
      const pill = document.createElement("span");
      pill.className = `status-pill${review ? " needs-review" : ""}`;
      const dot = document.createElement("i");
      const label = document.createElement("span");
      label.textContent = review ? "Needs a look" : "Matched";
      pill.append(dot, label);
      status.append(pill);
      tr.append(card, printing, quantity, status);
      tr.tabIndex = 0;
      tr.setAttribute("aria-label", `View archive details for ${row.name || `Arena card ${row.arena_id || ""}`}`);
      tr.addEventListener("click", () => showCardDetail(row));
      tr.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); showCardDetail(row); }
      });
      body.append(tr);
    }

    state.total = Number(result.total || 0);
    const hasRows = rows.length > 0;
    const hasCollection = Number(result.totalCollection || state.total) > 0;
    byId("collectionTable").hidden = !hasRows;
    byId("collectionEmpty").hidden = hasRows;
    byId("emptyTitle").textContent = hasCollection ? "No cards match that search" : "Your collection will appear here";
    byId("emptyDetail").textContent = hasCollection ? "Try another name, set, collector number, or Arena ID." : "Sync Arena or import a snapshot to start your archive.";
    const first = state.total ? state.offset + 1 : 0;
    const last = Math.min(state.offset + state.pageSize, state.total);
    byId("resultCount").textContent = `${formatNumber(state.total)} cards`;
    document.querySelectorAll(".filter-tab").forEach((button) => {
      const active = (button.dataset.filter === "review") === state.reviewOnly;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    document.querySelectorAll("#collectionTable th[data-sort]").forEach((header) => {
      const marker = header.querySelector(".sort-indicator");
      if (marker) marker.textContent = header.dataset.sort === state.sort ? (state.reverse ? "↓" : "↑") : "";
    });
    byId("clearSearch").hidden = !state.search;
    byId("pageSummary").textContent = `Showing ${formatNumber(first)}–${formatNumber(last)} of ${formatNumber(state.total)}`;
    byId("pagePrev").disabled = state.offset <= 0;
    byId("pageNext").disabled = state.offset + state.pageSize >= state.total;
  }

  async function loadCollection() {
    const result = await call("collectionPage", state.search, state.reviewOnly, state.offset, state.pageSize, state.sort, state.reverse);
    renderCollection(result || {});
  }

  async function refreshAll() {
    const data = await call("bootstrap");
    setMetrics(data.metrics);
    byId("rawPath").value = data.settings.arena_raw_path || "";
    byId("exportPath").value = data.settings.output_path || "";
    byId("versionLabel").textContent = data.version || "5.2.5";
    if (data.arena_running !== undefined) setArena(data.arena_running, data.arena_error || "");
    if (data.status) setStatus(data.status.title, data.status.detail, data.status.kind);
    const latest = data.latest_snapshot;
    byId("lastCapture").textContent = latest?.captured_at
      ? `${formatDate(latest.captured_at)} · ${formatNumber(latest.unique_cards)} cards`
      : "No snapshot yet";
    if (state.page === "collection") await loadCollection();
    if (state.page === "history") await loadHistory();
  }

  function addLog(entry = {}) {
    state.logs.unshift({ timestamp: entry.timestamp || new Date().toLocaleTimeString(), level: entry.level || "INFO", message: String(entry.message || "") });
    state.logs = state.logs.slice(0, 100);
    const list = byId("logList");
    list.replaceChildren();
    state.logs.forEach((log) => {
      const line = document.createElement("div");
      line.className = "log-line";
      const timestamp = document.createElement("span"); timestamp.textContent = log.timestamp;
      const level = document.createElement("span"); level.className = `log-level ${log.level}`; level.textContent = log.level;
      const message = document.createElement("span"); message.textContent = log.message;
      line.append(timestamp, level, message);
      list.append(line);
    });
  }

  function setBusy(busy, label = "Working…") {
    state.busy = busy;
    byId("syncButton").disabled = busy;
    byId("importSnapshot").disabled = busy || !byId("snapshotPath").value.trim();
    byId("chooseSnapshot").disabled = busy;
    byId("cancelButton").hidden = !busy;
    byId("taskProgress").hidden = !busy;
    byId("syncButton").querySelector(".button-label").textContent = busy ? "Working…" : "Sync collection";
    if (busy) {
      byId("progressFill").style.width = "0%";
      byId("progressLabel").textContent = label;
      byId("routeCapture").className = "route-step is-current";
      byId("routeCaptureState").textContent = "WORKING";
    } else {
      byId("routeCapture").className = "route-step";
      byId("routeCaptureState").textContent = "READY";
    }
  }

  function onBackendEvent(event) {
    const { type, payload = {} } = event || {};
    if (type === "log") addLog(payload);
    else if (type === "status") setStatus(payload.title, payload.detail, payload.kind);
    else if (type === "stage") {
      setBusy(true, payload.name || "Working…");
      setStatus(payload.name, payload.detail, "working");
      byId("progressLabel").textContent = payload.name || "Working…";
    } else if (type === "progress") {
      setBusy(true, payload.label || "Working…");
      const percent = Math.max(0, Math.min(100, Number(payload.percent || 0)));
      byId("progressFill").style.width = `${percent}%`;
      if (payload.label) byId("progressLabel").textContent = payload.label;
      if (payload.label) byId("statusDetail").textContent = payload.label;
    } else if (type === "arena_presence") setArena(payload.running, payload.error || "");
    else if (type === "confirm_snapshot") {
      const stats = payload.stats || {};
      byId("confirmMessage").textContent = payload.message || "This snapshot is much smaller than the saved collection.";
      byId("savedStats").textContent = `${formatNumber(stats.current_unique)} cards · ${formatNumber(stats.current_total)} copies`;
      byId("incomingStats").textContent = `${formatNumber(stats.new_unique)} cards · ${formatNumber(stats.new_total)} copies`;
      byId("confirmDialog").showModal();
    } else if (type === "complete") {
      const result = payload.result || {};
      setBusy(false);
      byId("taskProgress").hidden = true;
      setStatus("COLLECTION SYNCED", `${formatNumber(result.unique)} Arena IDs · ${formatNumber(result.copies)} copies · ${result.source || "Snapshot saved"}`, "ready");
      setMetrics(payload.metrics || {});
      addLog({ level: "SUCCESS", message: `Saved ${formatNumber(result.unique)} Arena IDs / ${formatNumber(result.copies)} copies.` });
      if (result.paths?.json) showToast(`Collection saved. Export: ${result.paths.json}`);
      safely(refreshAll);
    } else if (type === "cancelled") {
      setBusy(false); byId("taskProgress").hidden = true;
      setStatus("SYNC CANCELLED", "Your last saved collection was preserved.", "warn");
    } else if (type === "error") {
      setBusy(false); byId("taskProgress").hidden = true;
      setStatus("SYNC FAILED", payload.message || "Unknown error", "error");
      addLog({ level: "ERROR", message: payload.message || "Unknown error" });
      showToast(payload.message || "The operation failed.", true);
    } else if (type === "export_complete") {
      setBusy(false); byId("taskProgress").hidden = true;
      setStatus("EXPORT COMPLETE", payload.path || "Your collection exports are ready.", "ready");
      showToast("Collection exports are ready.");
    } else if (type === "export_empty") {
      setBusy(false); byId("taskProgress").hidden = true;
      setStatus("NOTHING TO EXPORT", "Sync Arena or import a snapshot first.", "warn");
    } else if (type === "snapshot_declined") {
      setBusy(false); byId("taskProgress").hidden = true;
      setStatus("SNAPSHOT NOT APPLIED", payload.message || "The last valid collection was preserved.", "warn");
    } else if (type === "api_status") showToast(payload.message || "Local API status updated.");
  }

  function bindUi() {
    document.querySelectorAll(".nav-item").forEach((button) => button.addEventListener("click", () => showPage(button.dataset.page)));
    byId("syncButton").addEventListener("click", () => safely(async () => {
      const saved = await call("saveSettings", byId("rawPath").value, byId("exportPath").value);
      if (!saved) return;
      setBusy(true, "Starting collection capture…");
      state.backend.startSync();
    }));
    byId("cancelButton").addEventListener("click", () => state.backend?.cancel());
    byId("snapshotPath").addEventListener("input", () => { byId("importSnapshot").disabled = !byId("snapshotPath").value.trim(); });
    byId("chooseSnapshot").addEventListener("click", () => safely(async () => {
      const value = await call("chooseSnapshot");
      if (value) { byId("snapshotPath").value = value; byId("importSnapshot").disabled = false; }
    }));
    byId("importSnapshot").addEventListener("click", () => safely(() => {
      setBusy(true, "Reading saved snapshot…");
      state.backend.importSnapshot(byId("snapshotPath").value.trim());
    }));
    byId("exportCurrent").addEventListener("click", () => safely(() => state.backend.exportCurrent()));
    byId("openExports").addEventListener("click", () => safely(() => state.backend.openExports()));
    byId("browseRaw").addEventListener("click", () => safely(async () => { const value = await call("browseRaw", byId("rawPath").value); if (value) byId("rawPath").value = value; }));
    byId("browseExport").addEventListener("click", () => safely(async () => { const value = await call("browseExport", byId("exportPath").value); if (value) byId("exportPath").value = value; }));
    byId("saveSettings").addEventListener("click", () => safely(async () => {
      const saved = await call("saveSettings", byId("rawPath").value, byId("exportPath").value);
      if (!saved) return;
      byId("settingsFeedback").textContent = "Saved on this PC.";
      showToast("Settings saved.");
    }));
    byId("cardSearch").addEventListener("input", () => {
      state.search = byId("cardSearch").value.trim(); state.offset = 0;
      window.clearTimeout(state.searchTimer);
      state.searchTimer = window.setTimeout(() => safely(loadCollection), 140);
    });
    document.querySelectorAll(".filter-tab").forEach((button) => button.addEventListener("click", () => {
      state.reviewOnly = button.dataset.filter === "review"; state.offset = 0; safely(loadCollection);
    }));
    byId("clearSearch").addEventListener("click", () => {
      byId("cardSearch").value = ""; state.search = ""; state.offset = 0;
      byId("clearSearch").hidden = true; byId("cardSearch").focus(); safely(loadCollection);
    });
    byId("pagePrev").addEventListener("click", () => { state.offset = Math.max(0, state.offset - state.pageSize); safely(loadCollection); });
    byId("pageNext").addEventListener("click", () => { if (state.offset + state.pageSize < state.total) { state.offset += state.pageSize; safely(loadCollection); } });
    byId("emptySync").addEventListener("click", () => showPage("sync"));
    byId("historySync").addEventListener("click", () => showPage("sync"));
    document.querySelectorAll("#collectionTable th[data-sort]").forEach((header) => header.addEventListener("click", () => {
      const column = header.dataset.sort;
      if (column === state.sort) state.reverse = !state.reverse; else { state.sort = column; state.reverse = false; }
      state.offset = 0; safely(loadCollection);
    }));
    byId("activityToggle").addEventListener("click", () => {
      const open = byId("activityToggle").getAttribute("aria-expanded") !== "true";
      byId("activityToggle").setAttribute("aria-expanded", String(open));
      byId("activityChevron").textContent = open ? "−" : "＋";
      byId("activityPanel").hidden = !open;
    });
    byId("toolsToggle").addEventListener("click", () => {
      const open = byId("toolsMenu").hidden;
      byId("toolsMenu").hidden = !open;
      byId("toolsToggle").setAttribute("aria-expanded", String(open));
    });
    document.addEventListener("click", (event) => {
      if (!event.target.closest(".tools-menu-wrap")) { byId("toolsMenu").hidden = true; byId("toolsToggle").setAttribute("aria-expanded", "false"); }
    });
    document.querySelectorAll("[data-tool]").forEach((button) => button.addEventListener("click", () => {
      const actions = { openLogs: "openLogs", openData: "openData", startApi: "startApi", copyApi: "copyApi" };
      safely(() => state.backend[actions[button.dataset.tool]]());
      byId("toolsMenu").hidden = true;
    }));
    byId("confirmKeep").addEventListener("click", () => { byId("confirmDialog").close(); state.backend?.resolveConfirmation(false); });
    byId("confirmReplace").addEventListener("click", () => { byId("confirmDialog").close(); state.backend?.resolveConfirmation(true); });
    byId("closeCardDialog").addEventListener("click", () => byId("cardDialog").close());
    byId("closeCardDetails").addEventListener("click", () => byId("cardDialog").close());
    document.addEventListener("keydown", (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        if (state.page !== "collection") showPage("collection");
        window.setTimeout(() => byId("cardSearch").focus(), 40);
      }
    });
  }

  function connect() {
    if (typeof qt === "undefined" || typeof QWebChannel === "undefined") {
      window.setTimeout(connect, 80);
      return;
    }
    new QWebChannel(qt.webChannelTransport, (channel) => {
      state.backend = channel.objects.backend;
      state.backend.event.connect((json) => {
        try { onBackendEvent(JSON.parse(json)); } catch (error) { console.error("Invalid desktop event", error); }
      });
      safely(refreshAll);
      state.backend.checkArena();
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    bindUi();
    connect();
  });
})();
