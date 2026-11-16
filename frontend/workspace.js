/* Workspace owns navigation; app.js keeps monitoring, authentication and actions. */
(() => {
  const shell = document.querySelector('#app-shell');
  const main = shell.querySelector('main');
  const panels = new Map([
    ['nvrs', document.querySelector('#nvr-list').closest('.panel')],
    ['alerts', document.querySelector('#alert-list').closest('.panel')],
    ['changes', document.querySelector('#ip-change-list').closest('.panel')],
    ['schedule', document.querySelector('.scheduler-panel')],
    ['locations', document.querySelector('#locations-panel')],
    ['users', document.querySelector('#users-panel')],
  ]);
  const labels = {nvrs:'NVRs', alerts:'Alertas atuais', changes:'Alteracoes de IP', schedule:'Agendamento', locations:'Cidades e blocos', users:'Usuarios'};
  const tabs = new Map();
  let active = null, nvrs = [], locations = [], channelsByNvr = new Map(), generation = 0;
  let cameraLoading = false, channelSyncVersion = 0;
  const cameraLoadErrors = new Set();
  const expanded = new Set();
  const layout = document.createElement('div');
  layout.className = 'workspace-layout';
  layout.innerHTML = `<aside class="location-sidebar" aria-label="Localizacoes"><div class="sidebar-head"><strong>Localizacoes</strong><div class="sidebar-tree-actions"><button id="expand-tree" class="secondary icon-button" title="Expandir arvore" aria-label="Expandir arvore"><i data-lucide="chevrons-up-down"></i></button><button id="collapse-tree" class="secondary icon-button" title="Recolher arvore" aria-label="Recolher arvore"><i data-lucide="chevrons-down-up"></i></button></div></div><input id="location-search" type="search" placeholder="Cidade, bloco, NVR ou IP de camera" aria-label="Buscar localizacao, NVR ou camera"><nav id="location-tree" aria-label="NVRs por localizacao"></nav></aside><div class="workspace-right"><div id="workspace-tabs" role="tablist" aria-label="Abas abertas"></div><div id="workspace-content"></div></div>`;
  shell.querySelector('header').after(layout);
  layout.querySelector('.workspace-right').prepend(document.querySelector('#operation-status'));
  const content = layout.querySelector('#workspace-content');
  const tabbar = layout.querySelector('#workspace-tabs');
  const search = layout.querySelector('#location-search');
  const overview = document.createElement('section');
  overview.append(main.querySelector('.summary'), panels.get('nvrs'));
  panels.set('nvrs', overview);
  for (const panel of panels.values()) { panel.hidden = true; content.append(panel); }
  main.remove();
  for (const id of ['open-import-modal','run-hdds','refresh']) document.getElementById(id).hidden = true;
  const icons = () => globalThis.lucide?.createIcons();
  const report = error => setOperationStatus('Nao foi possivel concluir', error.message, 'error');
  const safe = fn => (...args) => Promise.resolve().then(() => fn(...args)).catch(report);

  function activate(key) {
    const item = tabs.get(key);
    if (!item) return;
    active = key;
    for (const [id, tab] of tabs) {
      tab.panel.hidden = id !== key;
      tab.button.setAttribute('aria-selected', String(id === key));
      tab.button.tabIndex = id === key ? 0 : -1;
      tab.element.classList.toggle('active', id === key);
    }
    renderTree();
    if (item.dirty && key.startsWith('nvr:')) { item.dirty = false; safe(() => renderNvr(key.slice(4)))(); }
  }
  function close(key) {
    const item = tabs.get(key);
    if (!item) return;
    item.panel.hidden = true;
    item.element.remove();
    if (key.startsWith('nvr:')) item.panel.remove();
    tabs.delete(key);
    if (active === key) {
      if (tabs.size) activate([...tabs.keys()].at(-1));
      else open('alerts');
    }
  }
  function open(key, label = labels[key], panel = panels.get(key)) {
    if (!tabs.has(key)) {
      const element = document.createElement('div'); element.className = 'workspace-tab';
      const button = document.createElement('button'); button.textContent = label;
      button.setAttribute('role', 'tab'); button.id = `tab-${key}`;
      panel.id ||= `panel-${key}`;
      button.setAttribute('aria-controls', panel.id);
      panel.setAttribute('role', 'tabpanel'); panel.setAttribute('aria-labelledby', button.id);
      button.addEventListener('click', () => activate(key));
      button.addEventListener('keydown', event => {
        if (event.key === 'Delete') { event.preventDefault(); close(key); tabs.get(active)?.button.focus(); }
        if (!['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
        event.preventDefault();
        const keys = [...tabs.keys()], index = keys.indexOf(key);
        const next = event.key === 'Home' ? keys[0] : event.key === 'End' ? keys.at(-1) : keys[(index + (event.key === 'ArrowRight' ? 1 : keys.length - 1)) % keys.length];
        activate(next); tabs.get(next).button.focus();
      });
      const closer = document.createElement('button'); closer.className = 'tab-close';
      closer.innerHTML = '<i data-lucide="x"></i>'; closer.title = `Fechar ${label}`;
      closer.setAttribute('aria-label', closer.title); closer.addEventListener('click', () => close(key));
      element.append(button, closer); tabbar.append(element);
      tabs.set(key, {element, button, panel});
    }
    activate(key); icons();
  }
  function tags(nvr) {
    return [['pending_no_recording','no_recording','SEM GRAVACAO'],['pending_error','error','ERRO'],['pending_hdd_error','hdd_error','HDD'],['change_alerts','change','ALTERACAO']]
      .filter(([field]) => nvr[field] > 0).map(([,css,label]) => `<span class="badge ${css}">${label}</span>`).join('');
  }
  function pendingAlerts(nvr) {
    if (nvr.pending_alerts != null) return Number(nvr.pending_alerts) || 0;
    return ['pending_no_recording','pending_error','pending_hdd_error','change_alerts']
      .reduce((total, field) => total + (Number(nvr[field]) || 0), 0);
  }
  function renderTree() {
    const query = search.value.trim().toLocaleLowerCase('pt-BR');
    const exactIp = /^\d{1,3}(?:\.\d{1,3}){3}$/.test(query);
    const groups = new Map();
    for (const nvr of nvrs) {
      const cameras = channelsByNvr.get(nvr.id) || [];
      const parentMatches = exactIp
        ? (() => { try { return new URL(nvr.base_url).hostname === query; } catch { return false; } })()
        : [nvr.city,nvr.block,nvr.name,nvr.base_url].join(' ').toLocaleLowerCase('pt-BR').includes(query);
      const matchingCameras = cameras.filter(camera => exactIp
        ? camera.camera_ip === query
        : String(camera.camera_ip || '').toLocaleLowerCase('pt-BR').includes(query));
      if (!parentMatches && !matchingCameras.length) continue;
      const visibleCameras = parentMatches ? cameras : matchingCameras;
      const city = nvr.city || 'Sem localizacao', block = nvr.block || 'Sem bloco';
      if (!groups.has(city)) groups.set(city, new Map());
      if (!groups.get(city).has(block)) groups.get(city).set(block, []);
      groups.get(city).get(block).push({nvr, visibleCameras});
    }
    const tree = layout.querySelector('#location-tree'); tree.replaceChildren();
    function branch(parent, label, key, count, alerts) {
      const node = document.createElement('details'); node.open = !!query || expanded.has(key);
      const summary = document.createElement('summary');
      summary.innerHTML = `<span class="tree-branch-label">${escapeHtml(label)}</span><span class="tree-branch-meta"><small>${count}</small>${alerts ? `<strong class="tree-alert-count" title="${alerts} alerta(s) pendente(s)">${alerts} ${alerts === 1 ? 'alerta' : 'alertas'}</strong>` : ''}</span>`;
      node.append(summary); parent.append(node);
      node.addEventListener('toggle', () => { if (node.isConnected && !query) node.open ? expanded.add(key) : expanded.delete(key); });
      return node;
    }
    for (const city of [...groups.keys()].sort((a,b) => a.localeCompare(b))) {
      const blocks = groups.get(city);
      const cityItems = [...blocks.values()].flat();
      const cityNode = branch(tree, city, JSON.stringify([city]), cityItems.length,
        cityItems.reduce((total, {nvr}) => total + pendingAlerts(nvr), 0));
      for (const block of [...blocks.keys()].sort((a,b) => a.localeCompare(b))) {
        const items = blocks.get(block);
        const blockNode = branch(cityNode, block, JSON.stringify([city,block]), items.length,
          items.reduce((total, {nvr}) => total + pendingAlerts(nvr), 0));
        for (const {nvr, visibleCameras} of items) {
          const node = document.createElement('div'); node.className = 'tree-nvr-node';
          const row = document.createElement('div'); row.className = 'tree-nvr-row';
          const children = document.createElement('div'); children.className = 'tree-cameras';
          const nvrKey = `nvr:${nvr.id}`;
          const opened = !!query || expanded.has(nvrKey);
          const toggle = document.createElement('button'); toggle.type = 'button';
          toggle.className = 'tree-nvr-toggle'; toggle.title = `Expandir cameras de ${nvr.name}`;
          toggle.setAttribute('aria-label', toggle.title);
          toggle.setAttribute('aria-expanded', String(opened));
          toggle.innerHTML = '<i data-lucide="chevron-right"></i>';
          toggle.addEventListener('click', () => {
            const isOpen = toggle.getAttribute('aria-expanded') !== 'true';
            toggle.setAttribute('aria-expanded', String(isOpen));
            children.hidden = !isOpen;
            isOpen ? expanded.add(nvrKey) : expanded.delete(nvrKey);
          });
          const button = document.createElement('button'); button.className = 'tree-nvr';
          button.classList.toggle('selected', active === `nvr:${nvr.id}`);
          button.innerHTML = `<i data-lucide="server"></i><span><strong>${escapeHtml(nvr.name)}</strong><small>${escapeHtml(nvr.base_url)}</small><span class="tree-tags">${tags(nvr)}</span></span>`;
          button.addEventListener('click', safe(() => openNvr(nvr.id)));
          children.hidden = !opened;
          for (const camera of visibleCameras) {
            const cameraButton = document.createElement('button'); cameraButton.className = 'tree-camera';
            cameraButton.innerHTML = `<span>Canal ${escapeHtml(camera.channel ?? '-')}</span><small>${escapeHtml(camera.camera_ip || 'Sem IP')}</small>${camera.last_status && camera.last_status !== 'ok' ? `<span class="badge ${escapeHtml(camera.last_status)}">${escapeHtml(statusText(camera.last_status))}</span>` : ''}`;
            cameraButton.title = `Abrir canal ${camera.channel ?? '-'} de ${nvr.name}`;
            cameraButton.addEventListener('click', safe(() => openNvr(nvr.id, camera.id)));
            children.append(cameraButton);
          }
          if (!visibleCameras.length) children.textContent = cameraLoading
            ? 'Carregando cameras...'
            : cameraLoadErrors.has(nvr.id) ? 'Falha ao carregar cameras.' : 'Nenhuma camera descoberta.';
          row.append(toggle, button); node.append(row, children); blockNode.append(node);
        }
      }
    }
    if (!groups.size) tree.textContent = cameraLoading && query
      ? 'Buscando IPs das cameras...'
      : nvrs.length ? 'Nenhum NVR encontrado.' : 'Nenhum NVR cadastrado.';
    icons();
  }
  async function renderNvr(id) {
    const key = `nvr:${id}`, tab = tabs.get(key), nvr = nvrs.find(n => String(n.id) === String(id));
    if (!tab || !nvr) return;
    const token = ++generation; tab.generation = token;
    tab.button.textContent = nvr.name;
    const actions = document.querySelector(`#nvr-list .nvr-row[data-id="${id}"] .actions`);
    let webUrl = null;
    try {
      const parsed = new URL(nvr.base_url);
      if (['http:', 'https:'].includes(parsed.protocol)) webUrl = parsed.href;
    } catch { /* An old invalid address should not become a clickable link. */ }
    const webLink = webUrl ? `<a class="nvr-web-link admin-only" href="${escapeHtml(webUrl)}" target="_blank" rel="noopener noreferrer" title="Abrir interface web do NVR"><i data-lucide="external-link"></i>Acessar NVR</a>` : '';
    tab.panel.innerHTML = `<div class="nvr-detail-head"><div><div class="muted">${escapeHtml(nvr.city || 'Sem localizacao')} / ${escapeHtml(nvr.block || 'Sem bloco')}</div><h2>${escapeHtml(nvr.name)}</h2><span>${escapeHtml(nvr.base_url)}</span></div><div class="actions">${webLink}${actions?.innerHTML || ''}</div></div><div class="detail-status">${nvr.channel_count || 0} canais <span>OK ${nvr.ok_channels || 0}</span><span>Offline ${nvr.offline_channels || 0}</span><span>Sem gravacao ${nvr.no_recording_channels || 0}</span>${tags(nvr)}</div><div class="channel-tree" aria-live="polite">Carregando canais e discos...</div>`;
    tab.panel.querySelector('[data-action="channels"]')?.remove();
    icons();
    const target = tab.panel.querySelector('.channel-tree');
    try { await showChannels(id, nvr.name, target); }
    catch (error) { if (tab.generation === token) { target.textContent = error.message; report(error); } }
  }
  async function openNvr(id, channelId = null) {
    const nvr = nvrs.find(n => String(n.id) === String(id)); if (!nvr) return;
    const key = `nvr:${id}`;
    if (!tabs.has(key)) { const panel = document.createElement('section'); panel.className = 'nvr-detail'; content.append(panel); open(key, nvr.name, panel); }
    else {tabs.get(key).dirty = false; activate(key);}
    layout.classList.remove('sidebar-open');
    await renderNvr(id);
    if (channelId != null) {
      const row = tabs.get(key)?.panel.querySelector(`tr[data-channel-id="${channelId}"]`);
      if (row) { row.classList.add('selected-camera-row'); row.scrollIntoView({block:'center'}); row.focus({preventScroll:true}); }
    }
  }
  function openTool(id) {
    const names = {'ack-alerts-modal':'Alertas reconhecidos','scheduler-history-modal':'Historico de execucoes'};
    if (!(id in names)) return false;
    const panel = document.getElementById(id);
    panel.classList.add('workspace-tool'); content.append(panel);
    open(id, names[id], panel); return true;
  }
  function fillBlocks(selected = '') {
    const city = document.querySelector('#nvr-city').value;
    document.querySelector('#nvr-location').innerHTML = `<option value="">Sem localizacao</option>` + locations.filter(l => l.city === city).map(l => `<option value="${l.id}">${escapeHtml(l.block)}</option>`).join('');
    document.querySelector('#nvr-location').value = String(selected || '');
    document.querySelector('#nvr-location').required = !!city;
  }
  function selectLocation(id) {
    const nvr = nvrs.find(n => String(n.id) === String(id));
    document.querySelector('#nvr-city').value = nvr?.city || '';
    fillBlocks(nvr?.location_id);
  }
  async function loadLocations() {
    locations = (await api('/api/locations')).items;
    const citySelect = document.querySelector('#nvr-city'), city = citySelect.value;
    const cities = [...new Set(locations.map(l => l.city))];
    citySelect.innerHTML = '<option value="">Sem localizacao</option>' + cities.map(c => `<option>${escapeHtml(c)}</option>`).join('');
    citySelect.value = city;
    document.querySelector('#city-options').innerHTML = cities.map(c => `<option value="${escapeHtml(c)}"></option>`).join('');
    document.querySelector('#location-list').innerHTML = locations.map(l => `<tr><td>${escapeHtml(l.city)}</td><td>${escapeHtml(l.block)}</td><td>${l.nvrs}</td><td><button class="secondary" data-location-edit="${l.id}">Editar</button> <button class="danger" data-location-delete="${l.id}" ${l.nvrs ? 'disabled title="Transfira os NVRs antes de excluir"' : ''}>Excluir</button></td></tr>`).join('') || '<tr><td colspan="4">Nenhuma cidade ou bloco cadastrado.</td></tr>';
  }
  async function loadChannelsFallback(items, version) {
    const pending = items.filter(nvr => (nvr.channel_count || 0) > 0);
    let completed = 0;
    const worker = async () => {
      while (pending.length && version === channelSyncVersion) {
        const nvr = pending.shift();
        try {
          const data = await api(`/api/nvrs/${nvr.id}/channels`);
          if (version !== channelSyncVersion) return;
          channelsByNvr.set(nvr.id, data.items || []);
        } catch {
          if (version !== channelSyncVersion) return;
          cameraLoadErrors.add(nvr.id);
        }
        completed++;
        if (completed % 8 === 0) renderTree();
      }
    };
    await Promise.all(Array.from({length:Math.min(4, pending.length)}, () => worker()));
    if (version !== channelSyncVersion) return;
    cameraLoading = false;
    renderTree();
    if (cameraLoadErrors.size) report(new Error(`Falha ao carregar cameras de ${cameraLoadErrors.size} NVR(s).`));
  }
  globalThis.cctvWorkspace = {openNvr, openTool, selectLocation, onAccessChange() {
    if (!canManage()) for (const key of ['schedule', 'locations', 'users']) close(key);
  }, async sync(items) {
    nvrs = items;
    const version = ++channelSyncVersion;
    cameraLoading = false;
    cameraLoadErrors.clear();
    const nextChannels = new Map();
    try {
      const data = await api('/api/channels/tree');
      for (const camera of data.items) {
        if (!nextChannels.has(camera.nvr_id)) nextChannels.set(camera.nvr_id, []);
        nextChannels.get(camera.nvr_id).push(camera);
      }
      channelsByNvr = nextChannels;
    } catch (error) {
      if (error.status === 404) {
        channelsByNvr = new Map();
        cameraLoading = true;
        loadChannelsFallback(items, version).catch(report);
      } else {
        channelsByNvr = new Map();
        report(error);
      }
    }
    renderTree();
    for (const key of [...tabs.keys()]) if (key.startsWith('nvr:') && !nvrs.some(n => key === `nvr:${n.id}`)) close(key);
    await loadLocations();
    for (const [key, tab] of tabs) if (key.startsWith('nvr:')) tab.dirty = true;
    if (active?.startsWith('nvr:')) { tabs.get(active).dirty = false; await renderNvr(active.slice(4)); }
  }};
  shell.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', () => {
    open(button.dataset.view);
    if (button.dataset.view === 'users') safe(loadUsers)();
    button.closest('details').open = false;
  }));
  for (const [menu, target] of [['menu-ack','open-ack-alerts'],['menu-history','open-scheduler-history'],['menu-import','open-import-modal'],['menu-hdds','run-hdds'],['menu-refresh','refresh']]) {
    document.getElementById(menu).addEventListener('click', event => {document.getElementById(target).click(); event.target.closest('details').open = false;});
  }
  document.addEventListener('click', event => {
    shell.querySelectorAll('.workspace-menu details[open]').forEach(menu => {if (!menu.contains(event.target)) menu.open = false;});
    const closeButton = event.target.closest('[data-close-modal]');
    if (closeButton && tabs.has(closeButton.dataset.closeModal)) close(closeButton.dataset.closeModal);
  });
  document.addEventListener('keydown', event => { if (event.key === 'Escape') { layout.classList.remove('sidebar-open'); shell.querySelectorAll('.workspace-menu details').forEach(m => m.open = false); } });
  search.addEventListener('input', renderTree);
  document.querySelector('#expand-tree').addEventListener('click', () => {
    search.value = '';
    for (const nvr of nvrs) {
      const city = nvr.city || 'Sem localizacao', block = nvr.block || 'Sem bloco';
      expanded.add(JSON.stringify([city]));
      expanded.add(JSON.stringify([city, block]));
      expanded.add(`nvr:${nvr.id}`);
    }
    renderTree();
  });
  document.querySelector('#collapse-tree').addEventListener('click', () => { expanded.clear(); search.value = ''; renderTree(); });
  document.querySelector('#toggle-sidebar').addEventListener('click', () => layout.classList.toggle('sidebar-open'));
  document.querySelector('#nvr-city').addEventListener('change', () => fillBlocks());
  document.querySelector('#open-nvr-modal').addEventListener('click', () => selectLocation(null));
  const form = document.querySelector('#location-form');
  document.querySelector('#cancel-location').addEventListener('click', () => form.reset());
  form.addEventListener('submit', async event => {
    event.preventDefault(); const button = form.querySelector('button'); button.disabled = true;
    try {
      await api('/api/locations', {method:'POST', body:JSON.stringify(Object.fromEntries(new FormData(form)))});
      form.reset(); await refreshAll(); document.querySelector('#location-message').textContent = 'Localizacao salva.';
    } catch (error) { document.querySelector('#location-message').textContent = error.message; }
    finally {button.disabled = false;}
  });
  document.querySelector('#location-list').addEventListener('click', safe(async event => {
    const edit = event.target.closest('[data-location-edit]'), remove = event.target.closest('[data-location-delete]');
    if (edit) {const item = locations.find(l => String(l.id) === edit.dataset.locationEdit); for (const field of ['id','city','block']) form.elements[field].value = item[field]; form.elements.city.focus();}
    if (remove && confirm('Excluir esta localizacao?')) {await api(`/api/locations/${remove.dataset.locationDelete}`, {method:'DELETE'}); await refreshAll();}
  }));
  open('alerts');
  checkLogin();
})();
