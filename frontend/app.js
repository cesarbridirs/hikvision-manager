let accessProfile = null;
let viewerUsers = [];

function canManage() {
  return accessProfile?.role === "admin";
}

function applyAccessProfile(user) {
  accessProfile = user;
  document.querySelector("#app-shell").classList.toggle("viewer-mode", !!user && !canManage());
  globalThis.cctvWorkspace?.onAccessChange();
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const body = await response.text();
  let data;
  try {
    data = JSON.parse(body);
  } catch {
    if (response.status === 404 && path.startsWith("/api/")) {
      const error = new Error(`Rota ${path} nao encontrada (HTTP 404). Atualize backend/app.py e reinicie o servico.`);
      error.status = response.status;
      throw error;
    }
    const error = new Error(`Resposta invalida de ${path} (HTTP ${response.status}). Verifique o servidor.`);
    error.status = response.status;
    throw error;
  }
  if (response.status === 401) {
    showLogin();
    throw new Error(data.error || "nao autenticado");
  }
  if (!response.ok) {
    const error = new Error(data.error || "Falha na operacao");
    error.status = response.status;
    throw error;
  }
  if (data.error) throw new Error(data.error);
  return data;
}

function showLogin(message = "") {
  applyAccessProfile(null);
  document.querySelector("#login-screen").hidden = false;
  document.querySelector("#change-password-screen").hidden = true;
  document.querySelector("#app-shell").hidden = true;
  document.querySelector("#login-error").textContent = message;
}

function showChangePassword(message = "") {
  document.querySelector("#login-screen").hidden = true;
  document.querySelector("#change-password-screen").hidden = false;
  document.querySelector("#app-shell").hidden = true;
  document.querySelector("#change-password-error").textContent = message;
}

function showApp() {
  document.querySelector("#login-screen").hidden = true;
  document.querySelector("#change-password-screen").hidden = true;
  document.querySelector("#app-shell").hidden = false;
}

async function checkLogin() {
  try {
    const data = await api("/api/me");
    if (data.user) {
      applyAccessProfile(data.user);
      if (data.user.must_change_password) {
        showChangePassword();
        return;
      }
      showApp();
      await refreshAll();
    } else {
      showLogin();
    }
  } catch {
    showLogin();
  }
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function formatSaoPauloTime(value) {
  if (!value || value === "-") return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const parts = new Intl.DateTimeFormat("sv-SE", {
    timeZone: "America/Sao_Paulo",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date);
  return `${parts.replace(" ", "T")}-03:00`;
}

function setOperationStatus(title, message, state = "running") {
  const box = document.querySelector("#operation-status");
  box.hidden = false;
  box.classList.toggle("done", state === "done");
  box.classList.toggle("error", state === "error");
  document.querySelector("#operation-title").textContent = title;
  document.querySelector("#operation-message").textContent = message;
}

function clearOperationStatusSoon() {
  setTimeout(() => {
    const box = document.querySelector("#operation-status");
    if (box.classList.contains("done")) {
      box.hidden = true;
    }
  }, 12000);
}

async function loadDashboard() {
  const data = await api("/api/dashboard");
  document.querySelector("#total-nvrs").textContent = data.summary.nvrs || 0;
  document.querySelector("#total-channels").textContent = data.summary.channels || 0;
  document.querySelector("#total-alerts").textContent = data.summary.alerts || 0;
  document.querySelector("#total-offline").textContent = data.summary.offline_channels || 0;
  document.querySelector("#total-no-recording").textContent = data.summary.no_recording_channels || 0;
  document.querySelector("#total-errors").textContent = (data.summary.error_channels || 0) + (data.summary.hdd_errors || 0);
}

async function loadNvrs() {
  const data = await api("/api/nvrs");
  const tbody = document.querySelector("#nvr-list");
  document.querySelector("#list-count").textContent = `${data.items.length} item(ns)`;
  tbody.innerHTML = data.items.map((nvr) => {
    const tags = [];
    if ((nvr.pending_no_recording || 0) > 0) tags.push(`<span class="badge no_recording">SEM GRAVACAO</span>`);
    if ((nvr.pending_error || 0) > 0) tags.push(`<span class="badge error">ERRO</span>`);
    if ((nvr.pending_hdd_error || 0) > 0) tags.push(`<span class="badge hdd_error">HDD</span>`);
    if ((nvr.change_alerts || 0) > 0) tags.push(`<span class="badge change">ALTERACAO</span>`);
    return `
    <tr class="nvr-row" data-id="${nvr.id}">
      <td>
        <div class="nvr-name">
          <button class="secondary tree-toggle" data-action="toggle" data-id="${nvr.id}" data-name="${escapeHtml(nvr.name)}">+</button>
          <span>${escapeHtml(nvr.name)}</span>
        </div>
      </td>
      <td>${escapeHtml(nvr.base_url)}</td>
      <td>
        ${nvr.channel_count || 0}
        <span class="muted">
          OK ${nvr.ok_channels || 0} |
          Off ${nvr.offline_channels || 0} |
          Sem ${nvr.no_recording_channels || 0}
        </span>
        <span class="nvr-tags">${tags.join("")}</span>
      </td>
      <td>
        <div class="actions">
          <button class="secondary" data-action="channels" data-id="${nvr.id}" data-name="${escapeHtml(nvr.name)}">Canais</button>
          <button class="secondary admin-only" data-action="edit"
            data-id="${nvr.id}"
            data-name="${escapeHtml(nvr.name)}"
            data-base-url="${escapeHtml(nvr.base_url)}"
            data-username="${escapeHtml(nvr.username)}"
            data-lookback-minutes="${escapeHtml(nvr.lookback_minutes ?? 0)}"
            data-nvr-time-offset-hours="${escapeHtml(nvr.nvr_time_offset_hours ?? -3)}">Editar</button>
          <button class="secondary admin-only" data-action="verify-now" data-id="${nvr.id}" data-name="${escapeHtml(nvr.name)}">Verificar Agora</button>
          <button class="secondary admin-only" data-action="hdds" data-id="${nvr.id}" data-name="${escapeHtml(nvr.name)}">HDDs</button>
          <button class="danger admin-only" data-action="delete" data-id="${nvr.id}">Excluir</button>
        </div>
      </td>
    </tr>
    <tr class="channel-row" id="channels-${nvr.id}" hidden>
      <td colspan="4"><div class="channel-tree" data-loaded="false"></div></td>
    </tr>
  `;
  }).join("");
  await globalThis.cctvWorkspace?.sync(data.items);
}

function statusText(status) {
  return {
    ok: "OK",
    offline: "OFFLINE",
    no_recording: "SEM GRAVACAO",
    change: "ALTERACAO",
    status_change: "MUDANCA STATUS",
    error: "ERRO",
    hdd_error: "FALHA HDD",
  }[status] || status;
}

async function loadAlerts() {
  const data = await api("/api/alerts");
  const showNoRecording = document.querySelector("#filter-no-recording").checked;
  const showChange = document.querySelector("#filter-change").checked;
  const showHdd = document.querySelector("#filter-hdd").checked;
  document.querySelector("#total-alerts").textContent = data.items.length;
  const items = data.items.filter((item) => {
    if (item.last_status === "no_recording") return showNoRecording;
    if (item.last_status === "change") return showChange;
    if (item.last_status === "hdd_error") return showHdd;
    return true;
  });
  document.querySelector("#alert-count").textContent = `${items.length} item(ns)`;
  const rows = items.map((item) => {
    const cameraIp = item.last_status === "change"
      ? `${item.old_ip || "-"} -> ${item.new_ip || "-"}`
      : (item.camera_ip || "-");
    return `
      <tr>
        <td>${escapeHtml(item.nvr_name)}</td>
        <td>${escapeHtml(item.channel)}</td>
        <td>${escapeHtml(item.track_id)}</td>
        <td>${escapeHtml(cameraIp)}</td>
        <td><span class="badge ${escapeHtml(item.last_status)}">${statusText(item.last_status)}</span></td>
        <td>${escapeHtml(item.last_message)}</td>
        <td>${escapeHtml(formatSaoPauloTime(item.last_checked_at))}</td>
        <td class="admin-only">
          <button class="secondary" data-action="ack-alert"
            data-alert-type="${escapeHtml(item.alert_type)}"
            data-ref-id="${escapeHtml(item.ref_id)}"
            data-status="${escapeHtml(item.alert_status || item.last_status)}"
            data-status-changed-at="${escapeHtml(item.last_status_changed_at || "")}">
            Reconhecer
          </button>
        </td>
      </tr>
    `;
  });
  document.querySelector("#alert-list").innerHTML = rows.join("");
}

async function loadAcknowledgedAlerts() {
  const data = await api("/api/alerts/acknowledged");
  document.querySelector("#ack-alerts-list").innerHTML = data.items.map((item) => {
    const type = item.alert_type === "ip_change" ? "ALTERACAO" : item.alert_type === "hdd_status" ? "HDD" : "STATUS";
    const changeText = item.alert_type === "hdd_status"
      ? (item.status || "-")
      : item.alert_type === "ip_change"
      ? `${item.old_ip || "-"} -> ${item.new_ip || "-"}`
      : (item.camera_ip || "-");
    return `
      <tr>
        <td>${escapeHtml(formatSaoPauloTime(item.acknowledged_at))}</td>
        <td>${escapeHtml(item.nvr_name || "-")}</td>
        <td>${escapeHtml(item.channel || "-")}</td>
        <td>${escapeHtml(item.track_id || "-")}</td>
        <td>${escapeHtml(type)}</td>
        <td>${escapeHtml(statusText(item.status || item.change_type || "-"))}</td>
        <td>${escapeHtml(changeText)}</td>
        <td class="admin-only">
          <button class="danger" data-action="delete-ack" data-id="${escapeHtml(item.id)}">
            Excluir
          </button>
        </td>
      </tr>
    `;
  }).join("");
}

async function loadUsers() {
  const [data, locations] = await Promise.all([api("/api/users"), api("/api/locations")]);
  viewerUsers = data.items;
  const cities = [...new Set(locations.items.map((item) => item.city))].sort((a, b) => a.localeCompare(b, "pt-BR"));
  const options = cities.map((city) => `<label><input type="checkbox" name="city" value="${escapeHtml(city)}">${escapeHtml(city)}</label>`).join("");
  document.querySelector("#user-create-city-options").innerHTML = options || "Cadastre uma cidade em Cadastro > Localizacoes.";
  document.querySelector("#user-edit-city-options").innerHTML = options;
  document.querySelector("#users-list").innerHTML = viewerUsers.map((user) => `
    <tr>
      <td>${escapeHtml(user.username)}</td>
      <td>Visualizacao</td>
      <td>${escapeHtml((user.cities || []).join(", ") || "Nenhuma cidade")}</td>
      <td>${user.must_change_password ? "Pendente" : "Concluida"}</td>
      <td>${escapeHtml(formatSaoPauloTime(user.created_at))}</td>
      <td><div class="actions">
        <button class="secondary" data-user-action="cities" data-id="${escapeHtml(user.id)}">Editar cidades</button>
        <button class="secondary" data-user-action="password" data-id="${escapeHtml(user.id)}" data-username="${escapeHtml(user.username)}">Redefinir senha</button>
        <button class="danger" data-user-action="delete" data-id="${escapeHtml(user.id)}" data-username="${escapeHtml(user.username)}">Excluir</button>
      </div></td>
    </tr>
  `).join("") || '<tr><td colspan="6">Nenhum usuario de visualizacao cadastrado.</td></tr>';
}

function selectedUserCities(form) {
  return [...form.querySelectorAll('input[name="city"]:checked')].map((input) => input.value);
}

function resetUserForm() {
  const form = document.querySelector("#user-form");
  form.reset();
  form.elements.id.value = "";
  form.elements.username.readOnly = false;
  document.querySelector("#user-submit").textContent = "Cadastrar";
  document.querySelector("#user-cancel").hidden = true;
}

function renderScheduler(settings) {
  document.querySelector("#scheduler-enabled").checked = settings.scheduler_enabled;
  document.querySelector("#scheduler-interval").value = settings.scheduler_interval_minutes;
  document.querySelector("#global-lookback").value = settings.global_lookback_minutes;
  const state = settings.state || {};
  const running = state.running ? "em execucao" : "parado";
  const last = state.last_finished_at ? ` | ultima: ${formatSaoPauloTime(state.last_finished_at)}` : "";
  document.querySelector("#scheduler-state").textContent = `${running} | horario America/Sao_Paulo | ${state.last_message || ""}${last}`;
  const mode = settings.scheduler_enabled ? "Ativa" : "Desativada";
  const discover = "descoberta";
  const monitor = "verificacao";
  const savedAt = settings.scheduler_updated_at ? formatSaoPauloTime(settings.scheduler_updated_at) : "padrao inicial";
  document.querySelector("#scheduler-saved").innerHTML =
    `<strong>Agenda unica do sistema:</strong> ${mode}, a cada ${settings.scheduler_interval_minutes} min, ${discover} + ${monitor}. Salva em: ${escapeHtml(savedAt)}.`;
}

async function loadScheduler() {
  if (!canManage()) return;
  renderScheduler(await api("/api/settings"));
}

function summarizeSchedulerPart(part, type) {
  if (!part) return "-";
  if (type === "discover") {
    return `${part.discovered || 0} canal(is), ${part.ok || 0} OK, ${part.error || 0} erro(s)`;
  }
  return `${part.checked || 0} canal(is), ${part.ok || 0} OK, ${part.offline || 0} offline, ${part.no_recording || 0} sem gravacao, ${part.error || 0} erro(s)`;
}

async function loadSchedulerHistory() {
  const data = await api("/api/scheduler/history");
  document.querySelector("#scheduler-history-list").innerHTML = data.items.map((item) => `
    <tr>
      <td>${escapeHtml(formatSaoPauloTime(item.started_at))}</td>
      <td>${escapeHtml(formatSaoPauloTime(item.finished_at))}</td>
      <td><span class="badge ${escapeHtml(item.status === "ok" ? "ok" : item.status === "running" ? "change" : "error")}">${escapeHtml(item.status)}</span></td>
      <td>${escapeHtml(summarizeSchedulerPart(item.discover_summary, "discover"))}</td>
      <td>${escapeHtml(summarizeSchedulerPart(item.monitor_summary, "monitor"))}</td>
      <td>${escapeHtml(item.message || "-")}</td>
    </tr>
  `).join("");
}

async function loadIpChanges() {
  const data = await api("/api/camera-ip-changes");
  document.querySelector("#ip-change-count").textContent = `${data.items.length} item(ns)`;
  document.querySelector("#ip-change-list").innerHTML = data.items.map((item) => `
    <tr>
      <td>${escapeHtml(formatSaoPauloTime(item.detected_at))}</td>
      <td>${escapeHtml(item.nvr_name)}</td>
      <td>${escapeHtml(item.channel)}</td>
      <td>${escapeHtml(item.track_id)}</td>
      <td>${escapeHtml(item.old_ip || "-")}</td>
      <td>${escapeHtml(item.new_ip || "-")}</td>
      <td>${escapeHtml(item.change_type === "new_ip" ? "Novo IP" : item.change_type === "ip_removed" ? "IP removido" : "IP substituido")}</td>
    </tr>
  `).join("");
}

function hddStatusClass(status) {
  const normalized = String(status || "").trim().toLowerCase();
  if (["ok", "normal", "healthy", "rw", "readwrite", "working", "idle", "notexist"].includes(normalized)) return "ok";
  return "error";
}

let currentSnapshot = null;

function loadSnapshotImage() {
  if (!currentSnapshot) return;
  const image = document.querySelector("#snapshot-image");
  const status = document.querySelector("#snapshot-status");
  status.textContent = "Carregando imagem...";
  image.removeAttribute("src");
  image.onload = () => {
    status.textContent = "Imagem carregada.";
  };
  image.onerror = () => {
    status.textContent = "Nao foi possivel carregar a imagem deste canal.";
  };
  image.src = `${currentSnapshot.url}?t=${Date.now()}`;
}

function openSnapshotModal({ nvrId, channelId, nvrName, channel, trackId, cameraIp }) {
  currentSnapshot = {
    url: `/api/nvrs/${nvrId}/channels/${channelId}/snapshot`,
  };
  document.querySelector("#snapshot-title").textContent =
    `${nvrName || "NVR"} - Canal ${channel || "-"} / TrackID ${trackId || "-"} / ${cameraIp || "sem IP"}`;
  openModal("snapshot-modal");
  loadSnapshotImage();
}

async function refreshAll() {
  await loadDashboard();
  await loadNvrs();
  await loadAlerts();
  await loadIpChanges();
  await loadScheduler();
}

function cameraHttpUrl(value) {
  const parts = String(value || "").trim().split(".");
  if (parts.length !== 4 || parts.some((part) => !/^(0|[1-9]\d{0,2})$/.test(part) || Number(part) > 255)) return null;
  if (parts.every((part) => part === "0")) return null;
  return `http://${parts.join(".")}/`;
}

async function showChannels(id, name, destination = null) {
  const [channelsData, hddsData] = await Promise.all([
    api(`/api/nvrs/${id}/channels`),
    api(`/api/nvrs/${id}/hdds`),
  ]);
  const row = document.querySelector(`#channels-${id}`);
  const target = destination || row.querySelector(".channel-tree");
  target.dataset.loaded = "true";
  const channelsHtml = channelsData.items.length === 0
    ? `<div class="channel-tree-empty">Nenhum canal descoberto ainda.</div>`
    : `
      <div class="table-wrap">
        <table>
          <thead>
            <tr><th>Canal</th><th>TrackID</th><th>IP Camera</th><th>Status</th><th>Mensagem</th><th>Ultima Gravacao</th><th class="admin-only">Acoes</th></tr>
          </thead>
          <tbody>
            ${channelsData.items.map((item) => {
              const cameraUrl = cameraHttpUrl(item.camera_ip);
              return `
              <tr data-channel-id="${escapeHtml(item.id)}" tabindex="-1">
                <td>${escapeHtml(item.channel)}</td>
                <td>${escapeHtml(item.track_id)}</td>
                <td><div class="camera-ip-cell"><span>${escapeHtml(item.camera_ip || "-")}</span>${cameraUrl ? `<a class="camera-http-link admin-only" href="${cameraUrl}" target="_blank" rel="noopener noreferrer" title="Abrir interface HTTP da camera" aria-label="Abrir camera do canal ${escapeHtml(item.channel)} no navegador"><i data-lucide="external-link"></i></a>` : ""}</div></td>
                <td><span class="badge ${escapeHtml(item.last_status || "")}">${statusText(item.last_status || "-")}</span></td>
                <td>${escapeHtml(item.last_message || "-")}</td>
                <td>${escapeHtml(formatSaoPauloTime(item.last_recording_end))}</td>
                <td class="admin-only">
                  <button class="secondary" data-action="snapshot" title="Visualizar camera" aria-label="Visualizar camera ${escapeHtml(item.channel)}"
                    data-nvr-id="${escapeHtml(id)}"
                    data-channel-id="${escapeHtml(item.id)}"
                    data-nvr-name="${escapeHtml(name)}"
                    data-channel="${escapeHtml(item.channel)}"
                    data-track-id="${escapeHtml(item.track_id)}"
                    data-camera-ip="${escapeHtml(item.camera_ip || "-")}">
                    <i data-lucide="camera"></i>
                  </button>
                </td>
              </tr>
            `;
            }).join("")}
          </tbody>
        </table>
      </div>
  `;
  const hddsHtml = `
    <div class="subsection-head">
      <h3>HDDs</h3>
      <span>${hddsData.items.length} item(ns)</span>
    </div>
    ${hddsData.items.length === 0 ? `<div class="channel-tree-empty">Nenhum HDD consultado ainda.</div>` : `
      <div class="table-wrap">
        <table>
          <thead>
            <tr><th>HDD</th><th>Nome</th><th>Status</th><th>Capacidade</th><th>Livre</th><th>Tipo</th><th>Ultima Consulta</th></tr>
          </thead>
          <tbody>
            ${hddsData.items.map((item) => `
              <tr>
                <td>${escapeHtml(item.hdd_id)}</td>
                <td>${escapeHtml(item.name || "-")}</td>
                <td><span class="badge ${hddStatusClass(item.status)}">${escapeHtml(item.status || "-")}</span></td>
                <td>${escapeHtml(item.capacity || "-")}</td>
                <td>${escapeHtml(item.free_space || "-")}</td>
                <td>${escapeHtml(item.type || item.property || "-")}</td>
                <td>${escapeHtml(formatSaoPauloTime(item.last_checked_at))}</td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      </div>
    `}
  `;
  target.innerHTML = `${channelsHtml}${hddsHtml}`;
  globalThis.lucide?.createIcons();
}

document.querySelector("#refresh").addEventListener("click", refreshAll);
document.querySelector("#filter-no-recording").addEventListener("change", loadAlerts);
document.querySelector("#filter-change").addEventListener("change", loadAlerts);
document.querySelector("#filter-hdd").addEventListener("change", loadAlerts);
document.querySelector("#refresh-snapshot").addEventListener("click", loadSnapshotImage);

document.querySelector("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.target).entries());
  try {
    const result = await api("/api/login", {
      method: "POST",
      body: JSON.stringify(data),
    });
    event.target.reset();
    if (result.user?.must_change_password) {
      applyAccessProfile(result.user);
      showChangePassword();
      return;
    }
    applyAccessProfile(result.user);
    showApp();
    await refreshAll();
  } catch (error) {
    showLogin(error.message);
  }
});

document.querySelector("#change-password-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.target).entries());
  if (data.new_password !== data.confirm_password) {
    showChangePassword("confirmacao diferente da nova senha");
    return;
  }
  try {
    await api("/api/change-password", {
      method: "POST",
      body: JSON.stringify(data),
    });
    event.target.reset();
    showApp();
    await refreshAll();
  } catch (error) {
    showChangePassword(error.message);
  }
});

document.querySelector("#logout").addEventListener("click", async () => {
  try {
    await fetch("/api/logout", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
  } finally {
    showLogin();
  }
});

document.querySelector("#user-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!canManage()) return;
  const form = event.currentTarget;
  const id = form.elements.id.value;
  const button = document.querySelector("#user-submit");
  const message = document.querySelector("#users-message");
  button.disabled = true;
  message.textContent = "";
  try {
    const cities = id ? undefined : selectedUserCities(form);
    if (!id && !cities.length) throw new Error("Selecione ao menos uma cidade.");
    await api(id ? `/api/users/${id}/password` : "/api/users", {
      method: "POST",
      body: JSON.stringify(id
        ? {password: form.elements.password.value}
        : {username: form.elements.username.value.trim(), password: form.elements.password.value, role: "viewer", cities}),
    });
    message.textContent = id ? "Senha redefinida. As sessoes anteriores foram encerradas." : "Usuario cadastrado.";
    resetUserForm();
    await loadUsers();
  } catch (error) {
    message.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

document.querySelector("#user-cancel").addEventListener("click", resetUserForm);
document.querySelector("#user-cities-cancel").addEventListener("click", () => {
  document.querySelector("#user-cities-form").hidden = true;
});

document.querySelector("#user-cities-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!canManage()) return;
  const form = event.currentTarget;
  const cities = selectedUserCities(form);
  const message = document.querySelector("#users-message");
  if (!cities.length) { message.textContent = "Selecione ao menos uma cidade."; return; }
  try {
    await api(`/api/users/${form.elements.id.value}/cities`, {method: "POST", body: JSON.stringify({cities})});
    form.hidden = true;
    message.textContent = "Cidades atualizadas. O acesso foi alterado imediatamente.";
    await loadUsers();
  } catch (error) { message.textContent = error.message; }
});

document.querySelector("#users-list").addEventListener("click", async (event) => {
  if (!canManage()) return;
  const button = event.target.closest("[data-user-action]");
  if (!button) return;
  const form = document.querySelector("#user-form");
  if (button.dataset.userAction === "cities") {
    const user = viewerUsers.find((item) => String(item.id) === button.dataset.id);
    if (!user) return;
    const cityForm = document.querySelector("#user-cities-form");
    cityForm.elements.id.value = button.dataset.id;
    document.querySelector("#user-cities-title").textContent = `Cidades de ${user.username}`;
    for (const checkbox of cityForm.querySelectorAll('input[name="city"]')) {
      checkbox.checked = (user.cities || []).some((city) => city.toLocaleLowerCase("pt-BR") === checkbox.value.toLocaleLowerCase("pt-BR"));
    }
    cityForm.hidden = false;
    cityForm.scrollIntoView({block: "nearest"});
    return;
  }
  if (button.dataset.userAction === "password") {
    form.elements.id.value = button.dataset.id;
    form.elements.username.value = button.dataset.username;
    form.elements.username.readOnly = true;
    form.elements.password.value = "";
    document.querySelector("#user-submit").textContent = "Salvar senha";
    document.querySelector("#user-cancel").hidden = false;
    form.elements.password.focus();
    return;
  }
  if (!confirm(`Excluir o usuario ${button.dataset.username}?`)) return;
  button.disabled = true;
  try {
    await api(`/api/users/${button.dataset.id}`, {method: "DELETE"});
    if (form.elements.id.value === button.dataset.id) resetUserForm();
    if (document.querySelector("#user-cities-form").elements.id.value === button.dataset.id) document.querySelector("#user-cities-form").hidden = true;
    document.querySelector("#users-message").textContent = "Usuario excluido.";
    await loadUsers();
  } catch (error) {
    document.querySelector("#users-message").textContent = error.message;
    button.disabled = false;
  }
});

document.querySelector("#alert-list").addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-action='ack-alert']");
  if (!button) return;
  button.disabled = true;
  button.textContent = "Reconhecendo...";
  try {
    await api("/api/alerts/ack", {
      method: "POST",
      body: JSON.stringify({
        alert_type: button.dataset.alertType,
        ref_id: button.dataset.refId,
        status: button.dataset.status,
        status_changed_at: button.dataset.statusChangedAt,
      }),
    });
    await refreshAll();
  } finally {
    button.disabled = false;
    button.textContent = "Reconhecer";
  }
});

document.querySelector("#save-scheduler").addEventListener("click", async () => {
  const button = document.querySelector("#save-scheduler");
  button.disabled = true;
  button.textContent = "Salvando...";
  try {
    const settings = await api("/api/settings", {
      method: "POST",
      body: JSON.stringify({
        scheduler_enabled: document.querySelector("#scheduler-enabled").checked,
        scheduler_run_discover: true,
        scheduler_run_monitor: true,
        scheduler_interval_minutes: document.querySelector("#scheduler-interval").value,
        global_lookback_minutes: document.querySelector("#global-lookback").value,
      }),
    });
    renderScheduler(settings);
    button.textContent = "Salvo";
  } catch (error) {
    setOperationStatus("Falha ao salvar agenda", error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = "Salvar";
  }
});

document.querySelector("#run-scheduler-now").addEventListener("click", async (event) => {
  const button = event.target;
  button.disabled = true;
  button.textContent = "Executando...";
  try {
    const result = await api("/api/scheduler/run", { method: "POST", body: "{}" });
    const discover = result.discover ? `Descobertos: ${result.discover.discovered}` : "Descoberta ignorada";
    const monitor = result.monitor ? `Verificados: ${result.monitor.checked}` : "Verificacao ignorada";
    alert(`${discover}\n${monitor}`);
    await refreshAll();
  } finally {
    button.disabled = false;
    button.textContent = "Executar agora";
  }
});

document.querySelector("#ack-alerts-list").addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-action='delete-ack']");
  if (!button) return;
  if (!confirm("Excluir este item da lista de reconhecidos? Ele continuara descartado e nao voltara para Alertas Atuais.")) return;
  button.disabled = true;
  button.textContent = "Excluindo...";
  try {
    await api(`/api/alerts/acknowledged/${button.dataset.id}`, { method: "DELETE" });
    await loadAcknowledgedAlerts();
    await loadAlerts();
    await loadNvrs();
    await loadDashboard();
  } finally {
    button.disabled = false;
    button.textContent = "Excluir";
  }
});

document.querySelector("#delete-all-ack-alerts").addEventListener("click", async (event) => {
  if (!confirm("Excluir todos da lista de reconhecidos? Eles continuarao descartados e nao voltarao para Alertas Atuais.")) return;
  const button = event.target;
  button.disabled = true;
  button.textContent = "Excluindo...";
  try {
    await api("/api/alerts/acknowledged", { method: "DELETE" });
    await loadAcknowledgedAlerts();
    await loadAlerts();
    await loadNvrs();
    await loadDashboard();
  } finally {
    button.disabled = false;
    button.textContent = "Excluir todos";
  }
});

function openModal(id) {
  if (id === "nvr-modal") document.querySelector("#nvr-form-error").textContent = "";
  if (globalThis.cctvWorkspace?.openTool(id)) return;
  document.querySelector(`#${id}`).hidden = false;
}

function closeModal(id) {
  document.querySelector(`#${id}`).hidden = true;
  if (id === "snapshot-modal") {
    currentSnapshot = null;
    document.querySelector("#snapshot-image").removeAttribute("src");
    document.querySelector("#snapshot-status").textContent = "";
  }
}

function updateLookbackInput() {
  document.querySelector("#nvr-form").elements.lookback_minutes.disabled =
    document.querySelector("#nvr-use-global").checked;
}

document.querySelector("#nvr-use-global").addEventListener("change", updateLookbackInput);

document.querySelector("#open-nvr-modal").addEventListener("click", () => {
  const form = document.querySelector("#nvr-form");
  form.reset();
  form.elements.id.value = "";
  form.elements.lookback_minutes.value = document.querySelector("#global-lookback").value || "720";
  form.elements.nvr_time_offset_hours.value = "-3";
  updateLookbackInput();
  document.querySelector("#nvr-modal-title").textContent = "Novo NVR";
  document.querySelector("#nvr-submit").textContent = "Adicionar";
  openModal("nvr-modal");
});
document.querySelector("#open-import-modal").addEventListener("click", () => openModal("import-modal"));
document.querySelector("#open-scheduler-history").addEventListener("click", async () => {
  await loadSchedulerHistory();
  openModal("scheduler-history-modal");
});
document.querySelector("#open-ack-alerts").addEventListener("click", async () => {
  await loadAcknowledgedAlerts();
  openModal("ack-alerts-modal");
});

document.querySelectorAll("[data-close-modal]").forEach((button) => {
  button.addEventListener("click", () => closeModal(button.dataset.closeModal));
});

document.querySelectorAll(".modal").forEach((modal) => {
  modal.addEventListener("click", (event) => {
    if (event.target === modal) closeModal(modal.id);
  });
});

function verificationFailed(result) {
  return (result.discover?.error || 0) + (result.monitor?.error || 0) +
    (result.monitor?.nvr_errors || 0) + (result.hdds?.error || 0) > 0;
}

function verifyNowMessage(result) {
  const discover = result.discover || {};
  const monitor = result.monitor || {};
  return `NVRs consultados: ${discover.nvrs || 0}` +
    `\nCanais descobertos: ${discover.discovered || 0}` +
    `\nVerificados: ${monitor.checked || 0}` +
    `\nOK: ${monitor.ok || 0}` +
    `\nOffline: ${monitor.offline || 0}` +
    `\nSem gravacao: ${monitor.no_recording || 0}` +
    `\nErros de descoberta: ${discover.error || 0}` +
    `\nErros de canais: ${monitor.error || 0}` +
    `\nNVRs indisponiveis: ${monitor.nvr_errors || 0}` +
    `\nHDDs consultados: ${result.hdds?.hdds || 0}` +
    `\nErros de consulta HDD: ${result.hdds?.error || 0}`;
}

document.querySelector("#run-verify-now").addEventListener("click", async (event) => {
  const button = event.target;
  button.disabled = true;
  button.textContent = "Verificando...";
  setOperationStatus(
    "Verificacao em andamento",
    "Atualizando canais e IPs de todos os NVRs. Em seguida serao verificadas as gravacoes e os HDDs."
  );
  try {
    const result = await api("/api/verify-now/run", { method: "POST", body: "{}" });
    setOperationStatus(verificationFailed(result) ? "Verificacao concluida com falhas" : "Verificacao concluida", verifyNowMessage(result).replaceAll("\n", " | "), verificationFailed(result) ? "error" : "done");
    await refreshAll();
    clearOperationStatusSoon();
  } catch (error) {
    setOperationStatus("Erro na verificacao", error.message || "Falha ao verificar os NVRs.", "error");
  } finally {
    button.disabled = false;
    button.textContent = "Verificar agora todos";
  }
});

document.querySelector("#run-hdds").addEventListener("click", async (event) => {
  const button = event.target;
  button.disabled = true;
  button.textContent = "Consultando...";
  try {
    const result = await api("/api/hdds/run", { method: "POST", body: "{}" });
    alert(`NVRs consultados: ${result.summary.nvrs}\nCom sucesso: ${result.summary.ok}\nCom erro: ${result.summary.error}\nHDDs encontrados: ${result.summary.hdds}`);
    await refreshAll();
  } finally {
    button.disabled = false;
    button.textContent = "Consultar HDDs";
  }
});

document.querySelector("#nvr-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.target);
  const data = Object.fromEntries(form.entries());
  if (document.querySelector("#nvr-use-global").checked) data.lookback_minutes = 0;
  const id = data.id;
  delete data.id;
  const button = document.querySelector("#nvr-submit");
  button.disabled = true;
  try {
    await api(id ? `/api/nvrs/${id}/update` : "/api/nvrs", {
      method: "POST",
      body: JSON.stringify(data),
    });
    event.target.reset();
    closeModal("nvr-modal");
    await refreshAll();
  } catch (error) {
    document.querySelector("#nvr-form-error").textContent = error.message;
  } finally { button.disabled = false; }
});

document.querySelector("#import-btn").addEventListener("click", async () => {
  const text = document.querySelector("#import-text").value;
  const result = await api("/api/import/nvrs-txt", {
    method: "POST",
    body: JSON.stringify({ text }),
  });
  document.querySelector("#import-result").textContent =
    `Importados: ${result.imported}; ignorados: ${result.skipped}`;
  closeModal("import-modal");
  await refreshAll();
});

document.querySelector("#app-shell").addEventListener("click", async (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  const id = button.dataset.id;
  const action = button.dataset.action;
  if (!action) return;
  if ((action === "channels" || action === "toggle") && globalThis.cctvWorkspace) {
    await globalThis.cctvWorkspace.openNvr(id);
    return;
  }

  if (action === "snapshot") {
    openSnapshotModal({
      nvrId: button.dataset.nvrId,
      channelId: button.dataset.channelId,
      nvrName: button.dataset.nvrName,
      channel: button.dataset.channel,
      trackId: button.dataset.trackId,
      cameraIp: button.dataset.cameraIp,
    });
    return;
  }

  if (action === "channels") {
    const row = document.querySelector(`#channels-${id}`);
    const toggle = document.querySelector(`button[data-action="toggle"][data-id="${id}"]`);
    row.hidden = !row.hidden;
    if (toggle) toggle.textContent = row.hidden ? "+" : "-";
    if (!row.hidden && row.querySelector(".channel-tree").dataset.loaded !== "true") {
      await showChannels(id, button.dataset.name);
    }
  }

  if (action === "toggle") {
    const row = document.querySelector(`#channels-${id}`);
    row.hidden = !row.hidden;
    button.textContent = row.hidden ? "+" : "-";
    if (!row.hidden && row.querySelector(".channel-tree").dataset.loaded !== "true") {
      await showChannels(id, button.dataset.name);
    }
  }

  if (action === "edit") {
    const form = document.querySelector("#nvr-form");
    form.reset();
    form.elements.id.value = id;
    form.elements.name.value = button.dataset.name || "";
    form.elements.ip.value = button.dataset.baseUrl || "";
    form.elements.username.value = button.dataset.username || "";
    form.elements.password.value = "";
    globalThis.cctvWorkspace?.selectLocation(id);
    const lookback = Number(button.dataset.lookbackMinutes || 0);
    document.querySelector("#nvr-use-global").checked = lookback === 0;
    form.elements.lookback_minutes.value = lookback || document.querySelector("#global-lookback").value || "720";
    updateLookbackInput();
    form.elements.nvr_time_offset_hours.value = button.dataset.nvrTimeOffsetHours || "-3";
    document.querySelector("#nvr-modal-title").textContent = "Editar NVR";
    document.querySelector("#nvr-submit").textContent = "Salvar";
    openModal("nvr-modal");
  }

  if (action === "verify-now") {
    button.disabled = true;
    button.textContent = "Verificando...";
    const nvrName = button.dataset.name || "NVR selecionado";
    setOperationStatus(
      "Verificacao em andamento",
      `Atualizando canais e IPs de ${nvrName}. Em seguida serao verificadas as gravacoes e os HDDs.`
    );
    try {
      const result = await api(`/api/nvrs/${id}/verify-now`, { method: "POST", body: "{}" });
      setOperationStatus(`Verificacao ${verificationFailed(result) ? "com falhas" : "concluida"}: ${nvrName}`, verifyNowMessage(result).replaceAll("\n", " | "), verificationFailed(result) ? "error" : "done");
      await refreshAll();
      clearOperationStatusSoon();
    } catch (error) {
      setOperationStatus(`Erro na verificacao: ${nvrName}`, error.message || "Falha ao verificar o NVR.", "error");
    } finally {
      button.disabled = false;
      button.textContent = "Verificar Agora";
    }
  }

  if (action === "hdds") {
    button.disabled = true;
    button.textContent = "Consultando...";
    try {
      const result = await api(`/api/nvrs/${id}/hdds/run`, { method: "POST", body: "{}" });
      alert(`HDDs encontrados: ${result.summary.hdds}\nErros: ${result.summary.error}`);
      const row = document.querySelector(`#channels-${id}`);
      row.hidden = false;
      const toggle = document.querySelector(`button[data-action="toggle"][data-id="${id}"]`);
      if (toggle) toggle.textContent = "-";
      row.querySelector(".channel-tree").dataset.loaded = "false";
      await showChannels(id, button.dataset.name);
      await refreshAll();
    } finally {
      button.disabled = false;
      button.textContent = "HDDs";
    }
  }

  if (action === "delete") {
    if (!confirm("Excluir este NVR e seus canais?")) return;
    await api(`/api/nvrs/${id}`, { method: "DELETE" });
    await refreshAll();
  }
});
