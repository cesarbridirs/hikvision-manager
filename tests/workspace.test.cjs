const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '..');
const nvrs = [
  {id:1,name:'NVR_CIDADE_A',base_url:'http://192.0.2.39',username:'admin',city:'Cidade A',block:'Bloco A',location_id:1,channel_count:16,ok_channels:14,offline_channels:1,no_recording_channels:1,pending_offline:1,pending_no_recording:1,pending_hdd_error:1,pending_alerts:2,lookback_minutes:0},
  {id:2,name:'NVR_CIDADE_B',base_url:'http://192.0.2.74',username:'admin',city:'Cidade B',block:'Bloco B',location_id:2,channel_count:8,ok_channels:8},
  {id:3,name:'NVR_SEM_LOCAL',base_url:'http://192.0.2.99',username:'admin',channel_count:0},
];
const locations = [{id:1,city:'Cidade A',block:'Bloco A',nvrs:1},{id:2,city:'Cidade B',block:'Bloco B',nvrs:1}];
const users = [];
let server, browser;
(async () => {
  server = http.createServer(async (req,res) => {
    const url = new URL(req.url, 'http://localhost');
    if (url.pathname.startsWith('/api/')) {
      let data = {items:[]};
      if (url.pathname === '/api/me') data = {user:{username:process.env.VIEWER === '1'?'viewer':'admin',role:process.env.VIEWER === '1'?'viewer':'admin',must_change_password:false}};
      if (url.pathname === '/api/nvrs') data = {items:nvrs};
      if (url.pathname === '/api/channels/tree' && process.env.MISSING_TREE_ROUTE === '1') {
        res.writeHead(404,{'Content-Type':'text/plain'}); res.end('arquivo nao encontrado'); return;
      }
      if (url.pathname === '/api/channels/tree') data = {items:Array.from({length:16},(_,i)=>({id:i+1,nvr_id:1,channel:i+1,track_id:(i+1)*100+1,camera_ip:`192.0.2.${i+1}`,last_status:i===14?'offline':i===15?'no_recording':'ok'}))};
      if (url.pathname === '/api/locations') {
        if (req.method === 'POST') {let body=''; for await(const chunk of req) body+=chunk; const value=JSON.parse(body); locations.push({...value,id:3,nvrs:0});}
        data = {items:locations};
      }
      if (url.pathname === '/api/users') {
        if (req.method === 'POST') {let body=''; for await(const chunk of req) body+=chunk; const value=JSON.parse(body); users.push({id:users.length+1,username:value.username,role:'viewer',cities:value.cities,must_change_password:1,created_at:'2026-10-23T14:30:00-03:00'});}
        data = {items:users};
      }
      if (/^\/api\/users\/\d+\/cities$/.test(url.pathname) && req.method === 'POST') {
        let body=''; for await(const chunk of req) body+=chunk;
        const user=users.find(u=>u.id===Number(url.pathname.split('/')[3]));
        user.cities=JSON.parse(body).cities; data={ok:true};
      }
      if (/^\/api\/users\/\d+\/password$/.test(url.pathname) && req.method === 'POST') data = {ok:true};
      if (/^\/api\/users\/\d+$/.test(url.pathname) && req.method === 'DELETE') {users.splice(users.findIndex(u=>u.id===Number(url.pathname.split('/')[3])),1);data={deleted:true};}
      if (url.pathname === '/api/dashboard') data = {summary:{nvrs:3,channels:24,alerts:3,offline_channels:1,no_recording_channels:1,hdd_errors:1}};
      if (url.pathname === '/api/settings') data = {scheduler_enabled:false,scheduler_interval_minutes:30,global_lookback_minutes:720,state:{}};
      if (url.pathname === '/api/alerts') data = {items:[
        {id:1,nvr_id:1,nvr_name:'NVR_CIDADE_A',channel:16,track_id:1601,camera_ip:'192.0.2.16',last_status:'no_recording',last_message:'camera online sem gravacao',alert_type:'channel_status',ref_id:1},
        {nvr_id:1,nvr_name:'NVR_CIDADE_A',channel:'HDD 2',last_status:'hdd_error',last_message:'HDD 2: estado error',alert_type:'hdd_status',ref_id:2}
      ]};
      if (/\/channels$/.test(url.pathname)) data = {items:url.pathname.includes('/nvrs/1/') ? Array.from({length:16},(_,i)=>({id:i+1,channel:i+1,track_id:(i+1)*100+1,camera_ip:`192.0.2.${i+1}`,last_status:i===14?'offline':i===15?'no_recording':'ok',last_message:i===14?'camera offline':i===15?'camera online sem gravacao recente':'gravacao recente encontrada',last_recording_end:i<14?'2026-10-23T14:30:00-03:00':null})) : []};
      if (/\/hdds$/.test(url.pathname)) data = {items:[
        {hdd_id:1,name:'HDD 1',status:'Idle',capacity:4000000,free_space:240000,type:'SATA',last_checked_at:'2026-10-23T14:30:00-03:00'},
        {hdd_id:2,name:'Empty bay',status:'notExist',last_checked_at:'2026-10-23T14:30:00-03:00'}
      ]};
      res.writeHead(200,{'Content-Type':'application/json'}); res.end(JSON.stringify(data)); return;
    }
    const name = url.pathname === '/' ? 'index.html' : path.basename(url.pathname);
    const file = path.join(root,'frontend',name);
    if (!fs.existsSync(file)) {res.writeHead(404);res.end();return;}
    res.setHeader('Content-Type', name.endsWith('.js')?'application/javascript':name.endsWith('.css')?'text/css':'text/html');
    res.end(fs.readFileSync(file));
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  if (process.argv.includes('--preview')) {console.log(`Preview com dados ficticios: http://127.0.0.1:${server.address().port}`);return;}
  browser=await chromium.launch({headless:true,channel:process.env.BROWSER_CHANNEL || 'msedge'});
  const page=await browser.newPage({viewport:{width:1440,height:900}});
  const errors=[]; page.on('pageerror',e=>{errors.push(e.message);console.error(e);});
  page.on('console',m=>{if(m.type()==='error') console.error(m.text());});
  await page.goto(`http://127.0.0.1:${server.address().port}`);
  await page.getByRole('tab',{name:'Alertas atuais',exact:true}).waitFor();
  if (process.env.VIEWER === '1') {
    assert.equal(await page.locator('#app-shell').getAttribute('class'),'viewer-mode');
    for (const selector of ['#open-nvr-modal','#run-verify-now','#menu-hdds','#delete-all-ack-alerts','#alert-list [data-action="ack-alert"]']) {
      assert.equal(await page.locator(selector).isVisible(),false,selector);
    }
    assert.equal(await page.getByText('Cadastro',{exact:true}).isVisible(),false);
    await page.locator('#location-search').fill('NVR_CIDADE_A');
    await page.locator('.tree-nvr').filter({hasText:'NVR_CIDADE_A'}).click();
    await page.locator('.nvr-detail').getByRole('heading',{name:'HDDs',exact:true}).waitFor();
    for (const selector of ['.nvr-detail [data-action="edit"]','.nvr-detail [data-action="verify-now"]','.nvr-detail [data-action="snapshot"]','.nvr-detail .nvr-web-link','.nvr-detail .camera-http-link']) {
      assert.equal(await page.locator(selector).first().isVisible(),false,selector);
    }
    await page.locator('summary').filter({hasText:'Ferramentas'}).click();
    await page.locator('#menu-ack').click();
    await page.getByRole('tab',{name:'Alertas reconhecidos',exact:true}).waitFor();
    assert.equal(await page.locator('#delete-all-ack-alerts').isVisible(),false);
    console.log('Workspace viewer: somente leitura, sem consultas nem reconhecimento OK.');
    return;
  }
  assert.equal(await page.getByRole('tab',{name:'Alertas atuais',exact:true}).getAttribute('aria-selected'),'true');
  await page.locator('#location-tree').getByText('Cidade A',{exact:true}).waitFor();
  const city = page.locator('#location-tree > details').filter({has:page.locator('summary .tree-branch-label').getByText('Cidade A',{exact:true})});
  assert.equal(await city.getAttribute('open'),null);
  assert.equal(await city.locator(':scope > summary .tree-alert-count').textContent(),'2 alertas');
  await city.locator(':scope > summary').click();
  const block = city.locator(':scope > details').filter({has:page.locator('summary .tree-branch-label').getByText('Bloco A',{exact:true})});
  assert.equal(await block.getAttribute('open'),null);
  assert.equal(await block.locator(':scope > summary .tree-alert-count').textContent(),'2 alertas');
  await city.locator(':scope > summary').click();
  await page.locator('#location-search').fill('192.0.2.15');
  await page.getByRole('button',{name:'Expandir arvore'}).click();
  assert.equal(await page.locator('#location-search').inputValue(),'');
  assert.equal(await page.locator('#location-tree details[open]').count(),6);
  assert.equal(await page.locator('#location-tree .tree-nvr-toggle[aria-expanded="true"]').count(),3);
  assert.equal(await page.locator('#location-tree .tree-camera:visible').count(),16);
  await page.getByRole('button',{name:'Recolher arvore'}).click();
  assert.equal(await page.locator('#location-tree details[open]').count(),0);
  assert.equal(await page.locator('#location-tree .tree-nvr-toggle[aria-expanded="true"]').count(),0);
  assert.equal(await page.locator('#location-tree .tree-camera:visible').count(),0);
  await page.locator('#location-search').fill('192.0.2.15');
  await page.locator('#location-tree .tree-nvr').filter({hasText:'NVR_CIDADE_A'}).waitFor();
  await page.locator('#location-tree .tree-camera:visible').first().waitFor();
  if (process.env.MISSING_TREE_ROUTE === '1') {
    assert.equal(await page.locator('#operation-status').isVisible(),false);
  }
  assert.equal(await page.locator('#location-tree .tree-nvr').count(),1);
  assert.equal(await page.locator('#location-tree .tree-camera').count(),1);
  await page.locator('#location-tree .tree-camera').click();
  await page.locator('.nvr-detail tr.selected-camera-row').waitFor();
  assert.equal(await page.locator('.nvr-detail tr.selected-camera-row').getAttribute('data-channel-id'),'15');
  const cameraLink = page.locator('.nvr-detail tr.selected-camera-row .camera-http-link');
  assert.equal(await cameraLink.getAttribute('href'),'http://192.0.2.15/');
  assert.equal(await cameraLink.getAttribute('target'),'_blank');
  assert.equal(await cameraLink.getAttribute('rel'),'noopener noreferrer');
  await page.locator('#location-search').fill('');
  await page.locator('#location-tree').getByText('Cidade A',{exact:true}).click();
  await page.locator('#location-tree').getByText('Bloco A',{exact:true}).click();
  await page.locator('#location-tree .tree-nvr-toggle').first().click();
  assert.equal(await page.locator('#location-tree .tree-camera:visible').count(),16);
  await page.locator('.tree-nvr').filter({hasText:'NVR_CIDADE_A'}).click();
  await page.locator('.nvr-detail').getByRole('heading',{name:'HDDs',exact:true}).waitFor();
  assert.equal(await page.locator('.nvr-detail .badge.ok').filter({hasText:'Idle'}).count(),1);
  assert.equal(await page.locator('.nvr-detail .badge.ok').filter({hasText:'notExist'}).count(),1);
  const nvrLink = page.locator('.nvr-detail .nvr-web-link');
  assert.equal(await nvrLink.getAttribute('href'),'http://192.0.2.39/');
  assert.equal(await nvrLink.getAttribute('target'),'_blank');
  assert.equal(await page.locator('.nvr-detail tbody tr').count(),18);
  await page.locator('.tree-nvr').filter({hasText:'NVR_CIDADE_A'}).click();
  assert.equal(await page.getByRole('tab',{name:'NVR_CIDADE_A',exact:true}).count(),1);
  const out=fs.mkdtempSync(path.join(os.tmpdir(),'hikvision-workspace-'));
  await page.screenshot({path:path.join(out,'workspace-desktop.png'),fullPage:true});
  await page.locator('.nvr-detail').getByRole('button',{name:'Editar',exact:true}).click();
  assert.equal(await page.locator('#nvr-city').inputValue(),'Cidade A');
  assert.equal(await page.locator('#nvr-location').inputValue(),'1');
  await page.locator('[data-close-modal="nvr-modal"]').click();
  await page.locator('summary').filter({hasText:'Cadastro'}).click();
  await page.locator('[data-view="users"]').click();
  await page.getByRole('heading',{name:'Usuarios de visualizacao',exact:true}).waitFor();
  await page.locator('#user-form [name="username"]').fill('operador');
  await page.locator('#user-form [name="password"]').fill('Password123');
  await page.locator('#user-create-city-options input[value="Cidade A"]').check();
  await page.locator('#user-submit').click();
  await page.locator('#users-list').getByText('operador',{exact:true}).waitFor();
  assert.equal(await page.locator('#users-list tr').count(),1);
  assert.match(await page.locator('#users-list tr').innerText(),/Cidade A/);
  await page.locator('#users-list [data-user-action="cities"]').click();
  await page.locator('#user-edit-city-options input[value="Cidade B"]').check();
  await page.locator('#user-edit-city-options input[value="Cidade A"]').uncheck();
  await page.locator('#user-cities-form').getByRole('button',{name:'Salvar cidades'}).click();
  await page.getByText('Cidades atualizadas. O acesso foi alterado imediatamente.',{exact:true}).waitFor();
  assert.match(await page.locator('#users-list tr').innerText(),/Cidade B/);
  assert.doesNotMatch(await page.locator('#users-list tr').innerText(),/Cidade A/);
  await page.screenshot({path:path.join(out,'users-desktop.png'),fullPage:true});
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth <= innerWidth),true);
  await page.screenshot({path:path.join(out,'users-mobile.png'),fullPage:true});
  await page.setViewportSize({width:1440,height:900});
  await page.locator('#users-list [data-user-action="password"]').click();
  await page.locator('#user-form [name="password"]').fill('ChangedPassword123');
  await page.locator('#user-submit').click();
  await page.getByText('Senha redefinida. As sessoes anteriores foram encerradas.',{exact:true}).waitFor();
  page.on('dialog',dialog=>dialog.accept());
  await page.locator('#users-list [data-user-action="delete"]').click();
  await page.getByText('Nenhum usuario de visualizacao cadastrado.',{exact:true}).waitFor();
  await page.locator('summary').filter({hasText:'Ferramentas'}).click();
  await page.locator('[data-view="alerts"]').click();
  await page.getByRole('heading',{name:'Alertas Atuais',exact:true}).waitFor();
  assert.equal(await page.locator('#filter-offline, #filter-status-change').count(),0);
  assert.equal(await page.locator('#alert-list .badge.offline, #alert-list .badge.status_change').count(),0);
  await page.screenshot({path:path.join(out,'alertas-desktop.png'),fullPage:true});
  assert.equal(await page.locator('#alert-list .badge.hdd_error').count(),1);
  await page.locator('#filter-hdd').uncheck();
  await page.waitForFunction(()=>document.querySelector('#alert-count').textContent==='1 item(ns)');
  await page.locator('#filter-no-recording').uncheck();
  await page.waitForFunction(()=>document.querySelector('#alert-count').textContent==='0 item(ns)');
  await page.locator('#filter-no-recording').check();
  await page.locator('#filter-hdd').check();
  await page.locator('#open-ack-alerts').click();
  await page.getByRole('tab',{name:'Alertas reconhecidos',exact:true}).waitFor();
  await page.getByRole('button',{name:'Fechar Alertas reconhecidos',exact:true}).click();
  await page.locator('summary').filter({hasText:'Cadastro'}).click();
  await page.locator('[data-view="locations"]').click();
  await page.locator('#location-form [name="city"]').fill('Cidade C');
  await page.locator('#location-form [name="block"]').fill('Bloco B');
  await page.locator('#location-form').getByRole('button',{name:'Salvar',exact:true}).click();
  await page.getByText('Localizacao salva.',{exact:true}).waitFor();
  await page.getByRole('tab',{name:'NVR_CIDADE_A',exact:true}).click();
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth <= innerWidth),true);
  await page.screenshot({path:path.join(out,'workspace-mobile.png'),fullPage:true});
  await page.locator('#toggle-sidebar').click();
  await page.locator('#location-search').fill('NVR_SEM_LOCAL');
  await page.locator('.tree-nvr').filter({hasText:'NVR_SEM_LOCAL'}).click();
  await page.getByRole('tab',{name:'NVR_SEM_LOCAL',exact:true}).waitFor();
  while (await page.locator('#workspace-tabs .workspace-tab').count() > 1) {
    await page.locator('#workspace-tabs .workspace-tab').last().locator('.tab-close').click();
  }
  await page.locator('#workspace-tabs .workspace-tab .tab-close').click();
  assert.equal(await page.getByRole('tab',{name:'Alertas atuais',exact:true}).getAttribute('aria-selected'),'true');
  assert.deepEqual(errors,[]);
  console.log('Workspace: arvore, abas unicas, canais/HDDs, edicao, alertas, cadastro e mobile OK.');
})().catch(e=>{console.error(e);process.exitCode=1;}).finally(async()=>{await browser?.close();if(!process.argv.includes('--preview')) server?.close();});
