(() => {
  'use strict';

  // Estado
  const $ = (selector) => document.querySelector(selector);
  const all = (selector) => [...document.querySelectorAll(selector)];
  const saved = {
    get(key, fallback) {
      try {
        return localStorage.getItem(key) || fallback;
      } catch {
        return fallback;
      }
    },
    set(key, value) {
      try {
        localStorage.setItem(key, value);
      } catch {}
    }
  };
  const quality = $('#quality');
  const dataMode = $('#data');
  const image = $('#video');
  const hlsVideo = $('#hls');
  const toastBox = $('#toast');
  const viewer = $('#viewer');
  const modes = { fluido: [0, 5], medio: [6, 6], minimo: [3, 8] };
  let fps = 0;
  let jpegQuality = 5;
  let activeMove = false;
  let heartbeat = 0;
  let sweeping = false;
  let audioAbort = null;
  let audioContext = null;
  let gainNode = null;
  let nextAudioTime = 0;
  let recording = false;
  let statusFailures = 0;
  let lastReload = 0;
  let hiddenImageSource = '';
  let dragOrigin = null;
  let dragDirection = '';
  const pressedKeys = new Set();
  let toastTimer = 0;

  quality.value = saved.get('calidad', 'baja');
  dataMode.value = saved.get('datos', 'fluido');
  $('#volume').value = saved.get('volumen', '0.7');
  $('#drag').checked = saved.get('arrastrar', 'false') === 'true';
  $('#axis').value = saved.get('eje', 'x');
  $('#sweepSec').value = saved.get('sweep', '5');
  if (!document.querySelector('.screen').requestFullscreen) $('#full').hidden = true;

  function toast(message) {
    toastBox.textContent = message;
    toastBox.style.display = 'block';
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toastBox.style.display = 'none'; }, 4000);
  }

  async function api(path, options = {}) {
    const response = await fetch(path, { cache: 'no-store', ...options });
    if (!response.ok) {
      let detail = {};
      try { detail = await response.json(); } catch {}
      throw new Error(detail.error || `Error ${response.status}`);
    }
    return response.status === 204 ? null : response.json();
  }

  function post(path, value) {
    return api(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(value)
    });
  }

  // Video
  function imageStream() {
    [fps, jpegQuality] = modes[dataMode.value] || modes.fluido;
    image.src = `/stream?calidad=${quality.value}&fps=${fps}&q=${jpegQuality}&r=${Date.now()}`;
  }

  async function waitForHls() {
    const url = `/hls/${quality.value}/index.m3u8`;
    const deadline = Date.now() + 15000;
    $('#live').textContent = 'Preparando…';
    while (Date.now() < deadline) {
      try {
        const response = await fetch(url, { cache: 'no-store' });
        if (response.status === 200) return url;
      } catch {}
      await new Promise((resolve) => setTimeout(resolve, 1000));
    }
    throw new Error('No se pudo preparar Ahorro');
  }

  async function stopAudio() {
    if (audioAbort) audioAbort.abort();
    audioAbort = null;
    if (audioContext) await audioContext.close().catch(() => {});
    audioContext = null;
    gainNode = null;
    $('#sound').textContent = '♫ Sonido';
  }

  async function setStream() {
    await stopAudio();
    if (dataMode.value === 'ahorro') {
      if (!hlsVideo.canPlayType('application/vnd.apple.mpegurl')) {
        dataMode.value = 'fluido';
        saved.set('datos', 'fluido');
        toast('Ahorro solo está disponible en Safari');
        image.hidden = false;
        hlsVideo.hidden = true;
        imageStream();
        return;
      }
      image.hidden = true;
      hlsVideo.hidden = false;
      hlsVideo.muted = true;
      try {
        hlsVideo.src = await waitForHls();
        hlsVideo.load();
        await hlsVideo.play().catch(() => {});
      } catch (error) {
        dataMode.value = 'fluido';
        saved.set('datos', 'fluido');
        toast(`${error.message}; se cambió a Fluido`);
        hlsVideo.removeAttribute('src');
        hlsVideo.hidden = true;
        image.hidden = false;
        imageStream();
      }
      return;
    }
    hlsVideo.pause();
    hlsVideo.removeAttribute('src');
    hlsVideo.hidden = true;
    image.hidden = false;
    imageStream();
  }

  image.onerror = () => {
    if (!document.hidden && dataMode.value !== 'ahorro') setTimeout(imageStream, 1200);
  };

  $('#full').onclick = () => {
    const screen = $('.screen');
    if (screen.requestFullscreen) screen.requestFullscreen();
  };

  // Movimiento
  function sendStop() {
    fetch('/ptz', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{"stop":true}',
      keepalive: true
    }).catch(() => {});
  }

  function stopMovement() {
    if (!activeMove) return;
    activeMove = false;
    clearInterval(heartbeat);
    heartbeat = 0;
    sendStop();
  }

  function directionFromKeys() {
    const x = (pressedKeys.has('ArrowRight') ? 1 : 0) - (pressedKeys.has('ArrowLeft') ? 1 : 0);
    const y = (pressedKeys.has('ArrowUp') ? 1 : 0) - (pressedKeys.has('ArrowDown') ? 1 : 0);
    return x || y ? [Math.sign(x) * 0.5, Math.sign(y) * 0.5] : null;
  }

  function move(x, y) {
    if (!x && !y) return;
    if (sweeping) {
      sweeping = false;
      post('/ptz', { barrido: false }).catch(() => {});
      $('#sweep').textContent = 'Barrido: apagado';
    }
    if (!activeMove) {
      activeMove = true;
      heartbeat = setInterval(() => post('/ptz', { latido: true }).catch(() => {}), 700);
    }
    post('/ptz', { x, y }).catch(() => {});
  }

  function bindHold(button, x, y) {
    button.addEventListener('pointerdown', (event) => {
      event.preventDefault();
      if (activeMove) return;
      button.setPointerCapture(event.pointerId);
      move(x, y);
    });
    ['pointerup', 'pointercancel', 'lostpointercapture'].forEach((name) => button.addEventListener(name, stopMovement));
  }

  all('.pad [data-x]').forEach((button) => bindHold(button, +button.dataset.x, +button.dataset.y));
  $('#halt').onclick = () => {
    sweeping = false;
    stopMovement();
    sendStop();
  };

  $('#sweep').onclick = async () => {
    sweeping = !sweeping;
    $('#sweep').textContent = `Barrido: ${sweeping ? 'activo' : 'apagado'}`;
    try {
      await post('/ptz', {
        barrido: sweeping,
        eje: $('#axis').value,
        segundos: +$('#sweepSec').value
      });
    } catch (error) {
      sweeping = false;
      toast(error.message);
    }
  };

  const screen = $('.screen');
  screen.addEventListener('pointerdown', (event) => {
    if (!$('#drag').checked || event.target.closest('button')) return;
    dragOrigin = [event.clientX, event.clientY];
    dragDirection = '';
    screen.setPointerCapture(event.pointerId);
  });
  screen.addEventListener('pointermove', (event) => {
    if (!dragOrigin || !$('#drag').checked) return;
    const dx = event.clientX - dragOrigin[0];
    const dy = dragOrigin[1] - event.clientY;
    const distance = Math.hypot(dx, dy);
    if (distance < 24) return;
    const angle = Math.atan2(dy, dx);
    const sector = Math.round(angle / (Math.PI / 4));
    const x = [1, 1, 0, -1, -1, -1, 0, 1][(sector + 8) % 8] * 0.5;
    const y = [0, 1, 1, 1, 0, -1, -1, -1][(sector + 8) % 8] * 0.5;
    const direction = `${x},${y}`;
    if (direction !== dragDirection) {
      dragDirection = direction;
      move(x, y);
    }
  });
  function finishDrag() {
    if (!dragOrigin) return;
    dragOrigin = null;
    dragDirection = '';
    stopMovement();
  }
  ['pointerup', 'pointercancel', 'lostpointercapture'].forEach((name) => screen.addEventListener(name, finishDrag));

  window.addEventListener('keydown', (event) => {
    if (event.target.matches('input, select, textarea') || event.target.isContentEditable) return;
    if (event.key === ' ') {
      event.preventDefault();
      $('#halt').click();
      return;
    }
    if (!event.key.startsWith('Arrow')) return;
    event.preventDefault();
    if (pressedKeys.has(event.key)) return;
    pressedKeys.add(event.key);
    const direction = directionFromKeys();
    if (direction) move(...direction);
  });
  window.addEventListener('keyup', (event) => {
    if (!event.key.startsWith('Arrow')) return;
    pressedKeys.delete(event.key);
    const direction = directionFromKeys();
    if (direction) move(...direction);
    else stopMovement();
  });
  window.addEventListener('blur', stopMovement);
  window.addEventListener('pagehide', stopMovement);

  // Sonido, fotos y grabación
  $('#sound').onclick = async () => {
    if (dataMode.value === 'ahorro') {
      hlsVideo.muted = !hlsVideo.muted;
      $('#sound').textContent = hlsVideo.muted ? '♫ Sonido' : '♫ Silenciar';
      return;
    }
    if (audioAbort) {
      await stopAudio();
      return;
    }
    try {
      audioContext = new (window.AudioContext || window.webkitAudioContext)();
      await audioContext.resume();
      gainNode = audioContext.createGain();
      gainNode.gain.value = +$('#volume').value;
      gainNode.connect(audioContext.destination);
      audioAbort = new AbortController();
      const activeAbort = audioAbort;
      $('#sound').textContent = '♫ Silenciar';
      const response = await fetch('/audio', { signal: activeAbort.signal });
      const reader = response.body.getReader();
      let carry = new Uint8Array(0);
      nextAudioTime = audioContext.currentTime + 0.25;
      while (audioAbort === activeAbort) {
        const { value, done } = await reader.read();
        if (done) break;
        const bytes = new Uint8Array(carry.length + value.length);
        bytes.set(carry);
        bytes.set(value, carry.length);
        const evenLength = bytes.length & ~1;
        carry = bytes.slice(evenLength);
        if (!evenLength) continue;
        const view = new DataView(bytes.buffer, bytes.byteOffset, evenLength);
        const buffer = audioContext.createBuffer(1, evenLength / 2, 8000);
        const channel = buffer.getChannelData(0);
        for (let index = 0; index < channel.length; index += 1) {
          channel[index] = view.getInt16(index * 2, true) / 32768;
        }
        if (nextAudioTime < audioContext.currentTime) nextAudioTime = audioContext.currentTime + 0.05;
        if (nextAudioTime - audioContext.currentTime > 1) continue;
        const source = audioContext.createBufferSource();
        source.buffer = buffer;
        source.connect(gainNode);
        source.start(nextAudioTime);
        nextAudioTime += buffer.duration;
      }
    } catch (error) {
      if (error.name !== 'AbortError') toast('No se pudo iniciar el sonido');
    } finally {
      if (audioAbort) await stopAudio();
    }
  };

  $('#volume').oninput = (event) => {
    saved.set('volumen', event.target.value);
    if (gainNode) gainNode.gain.value = +event.target.value;
  };

  $('#photo').onclick = async () => {
    try {
      const result = await post('/foto', { calidad: 'alta' });
      toast('Foto guardada');
      await refreshFiles();
      toastLink(result.archivo);
    } catch (error) {
      toast(error.message);
    }
  };

  function toastLink(name) {
    toastBox.replaceChildren();
    const link = document.createElement('a');
    link.href = `/archivos/${encodeURIComponent(name)}`;
    link.textContent = 'Descargar';
    toastBox.append(link);
    toastBox.style.display = 'block';
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toastBox.style.display = 'none'; }, 4000);
  }

  $('#record').onchange = async (event) => {
    if (!event.target.value || recording) return;
    try {
      await post('/grabar', { segundos: +event.target.value || 600, calidad: quality.value });
      recording = true;
      $('#stopRecord').hidden = false;
      toast('Grabación iniciada');
    } catch (error) {
      toast(error.message);
    }
    event.target.value = '';
  };

  $('#stopRecord').onclick = async () => {
    try {
      await post('/grabar', { detener: true });
      toast('Deteniendo grabación…');
    } catch (error) {
      toast(error.message);
    }
  };

  // Archivos
  function readableName(file) {
    const match = file.nombre.match(/^(clip|foto)_(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})/);
    if (!match) return file.nombre;
    return `${match[1] === 'clip' ? 'Clip' : 'Foto'} ${match[4]}/${match[3]}/${match[2]} ${match[5]}:${match[6]}`;
  }

  function openViewer(source, type) {
    const content = $('#viewerContent');
    content.replaceChildren();
    const element = document.createElement(type === 'foto' ? 'img' : 'video');
    element.src = source;
    if (type === 'clip') {
      element.controls = true;
      element.playsInline = true;
    }
    content.append(element);
    viewer.showModal();
  }

  async function refreshFiles() {
    try {
      const files = await api('/archivos');
      const root = $('#filesList');
      root.replaceChildren();
      files.forEach((file) => {
        const url = `/archivos/${encodeURIComponent(file.nombre)}`;
        const card = document.createElement('article');
        card.className = 'card file-card';
        const preview = document.createElement('button');
        preview.className = 'file-preview';
        preview.setAttribute('aria-label', `Ver ${readableName(file)}`);
        if (file.tipo === 'foto') {
          const thumbnail = document.createElement('img');
          thumbnail.className = 'thumb';
          thumbnail.src = `${url}?miniatura=1`;
          thumbnail.alt = readableName(file);
          preview.append(thumbnail);
        } else {
          preview.textContent = '▶';
          preview.classList.add('clip-preview');
        }
        preview.onclick = () => openViewer(url, file.tipo);
        const title = document.createElement('p');
        title.textContent = readableName(file);
        const size = document.createElement('p');
        size.textContent = `${(file.bytes / 1048576).toFixed(2)} MB`;
        const actions = document.createElement('div');
        actions.className = 'file-actions';
        const viewButton = document.createElement('button');
        viewButton.textContent = 'Ver';
        viewButton.onclick = () => openViewer(url, file.tipo);
        const download = document.createElement('a');
        download.href = url;
        download.download = file.nombre;
        download.textContent = 'Descargar';
        const remove = document.createElement('button');
        remove.textContent = 'Borrar';
        remove.onclick = async () => {
          if (!confirm('¿Borrar este archivo?')) return;
          try {
            await fetch(url, { method: 'DELETE' });
            await refreshFiles();
          } catch (error) {
            toast(error.message);
          }
        };
        actions.append(viewButton, download, remove);
        card.append(preview, title, size, actions);
        root.append(card);
      });
    } catch (error) {
      toast(error.message);
    }
  }

  $('#closeViewer').onclick = () => viewer.close();

  // Cámara
  function addInfoRow(root, label, value, className = '') {
    const row = document.createElement('p');
    row.className = className;
    const strong = document.createElement('strong');
    strong.textContent = `${label}: `;
    row.append(strong, document.createTextNode(value || '—'));
    root.append(row);
  }

  async function loadCamera() {
    try {
      const info = await api('/camara/info');
      const root = $('#cameraInfo');
      root.replaceChildren();
      const card = document.createElement('div');
      card.className = 'card';
      addInfoRow(card, 'Fabricante', info.fabricante);
      addInfoRow(card, 'Modelo', info.modelo);
      addInfoRow(card, 'Firmware', info.firmware);
      addInfoRow(card, 'Serie', info.serie);
      const profiles = info.perfiles || [];
      profiles.forEach((profile) => addInfoRow(card, 'Resolución', `${profile.nombre}: ${profile.codec} ${profile.ancho}x${profile.alto}`));
      (info.red || []).forEach((network) => {
        addInfoRow(card, 'Red', `${network.nombre || 'Interfaz'} · IP ${network.ip || '—'} · MAC ${network.mac || '—'}`);
      });
      const difference = Number(info.desfase_segundos || 0);
      addInfoRow(card, 'Hora de la cámara', `${info.hora_camara || '—'} (desfase respecto a la PC: ${difference} s)`, Math.abs(difference) > 60 ? 'warning-text' : '');
      root.append(card);
      $('#ip').value = info.ip_camara || info.ip || '';
    } catch (error) {
      $('#cameraInfo').textContent = error.message;
    }
  }

  async function action(url, value, message) {
    try {
      await post(url, value);
      toast(message);
      if (url.includes('camara')) loadCamera();
    } catch (error) {
      toast(error.message);
    }
  }

  $('#syncTime').onclick = () => action('/camara/hora', {}, 'Hora sincronizada');
  $('#findCamera').onclick = () => action('/camara/buscar', {}, 'Búsqueda terminada');
  $('#saveIp').onclick = () => action('/camara/ip', { ip: $('#ip').value }, 'IP guardada');
  $('#showSecret').onclick = async () => {
    const secrets = $('#secrets');
    if (secrets.textContent) {
      secrets.textContent = '';
      $('#showSecret').textContent = 'Mostrar credenciales';
      return;
    }
    try {
      const result = await api('/camara/clave');
      secrets.replaceChildren();
      (result.usuarios || []).forEach((user) => addInfoRow(secrets, user.usuario, user.clave));
      $('#showSecret').textContent = 'Ocultar credenciales';
    } catch (error) {
      toast(error.message);
    }
  };
  $('#rebootCamera').onclick = () => {
    if (confirm('La cámara tardará alrededor de un minuto en reiniciar. ¿Continuar?')) {
      action('/camara/reiniciar', {}, 'Reinicio solicitado');
    }
  };

  // Servidor
  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(() => toast('Dirección copiada')).catch(() => fallbackCopy(text));
    } else {
      fallbackCopy(text);
    }
  }

  function fallbackCopy(text) {
    const input = document.createElement('textarea');
    input.value = text;
    document.body.append(input);
    input.select();
    toast('Selecciona y copia la dirección');
    input.remove();
  }

  function renderServer(state) {
    const server = state.servidor || {};
    const root = $('#serverInfo');
    root.replaceChildren();
    addInfoRow(root, 'Activo desde hace', `${Math.floor((server.segundos_activo || 0) / 3600)}h ${Math.floor(((server.segundos_activo || 0) % 3600) / 60)}m`);
    addInfoRow(root, 'Versión', server.version || '—');
    addInfoRow(root, 'Clientes', `vídeo ${state.clientes_video || 0}, audio ${state.clientes_audio || 0}`);
    const addresses = [
      ['Local', `http://${server.ip_local || location.hostname}:${server.puerto || 8770}`],
      ['Tailscale', server.ip_tailscale ? `http://${server.ip_tailscale}:${server.puerto || 8770}` : 'No disponible']
    ];
    addresses.forEach(([label, address]) => {
      const row = document.createElement('p');
      const name = document.createElement('strong');
      name.textContent = `${label}: `;
      row.append(name);
      if (address !== 'No disponible') {
        const link = document.createElement('a');
        link.href = address;
        link.textContent = address;
        row.append(link);
        const copy = document.createElement('button');
        copy.textContent = 'Copiar';
        copy.onclick = () => copyText(address);
        row.append(' ', copy);
      } else {
        row.append(address);
      }
      root.append(row);
    });
  }

  $('#refreshLog').onclick = async () => {
    try {
      $('#log').textContent = (await api('/servidor/registro?lineas=200')).lineas.join('\n');
    } catch (error) {
      toast(error.message);
    }
  };
  $('#restartServer').onclick = () => {
    if (confirm('¿Reiniciar el servidor?')) action('/servidor/reiniciar', {}, 'Servidor reiniciándose');
  };

  // Pestañas
  all('nav [data-tab]').forEach((button) => {
    button.onclick = () => {
      all('.panel').forEach((panel) => { panel.hidden = panel.id !== button.dataset.tab; });
      saved.set('pestana', button.dataset.tab);
      if (button.dataset.tab === 'files') refreshFiles();
      if (button.dataset.tab === 'camera') loadCamera();
      if (button.dataset.tab === 'server') $('#refreshLog').click();
    };
  });

  quality.onchange = () => {
    saved.set('calidad', quality.value);
    setStream();
  };
  dataMode.onchange = () => {
    saved.set('datos', dataMode.value);
    setStream();
  };
  $('#axis').onchange = () => saved.set('eje', $('#axis').value);
  $('#sweepSec').onchange = () => saved.set('sweep', $('#sweepSec').value);
  $('#drag').onchange = () => saved.set('arrastrar', $('#drag').checked);

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      stopMovement();
      if (dataMode.value !== 'ahorro' && image.src) {
        hiddenImageSource = image.src;
        image.removeAttribute('src');
      }
      return;
    }
    if (dataMode.value === 'ahorro') setStream();
    else {
      if (hiddenImageSource) image.src = hiddenImageSource;
      else imageStream();
      hiddenImageSource = '';
    }
  });

  setInterval(async () => {
    if (document.hidden) return;
    try {
      const state = await api(`/estado?calidad=${quality.value}`);
      statusFailures = 0;
      $('#status').textContent = state.conectado ? 'En vivo' : 'Conectando…';
      $('#live').textContent = state.segundos_desde_ultimo_fotograma == null
        ? 'Conectando…'
        : state.segundos_desde_ultimo_fotograma >= 3
          ? `Imagen detenida hace ${Math.floor(state.segundos_desde_ultimo_fotograma)} s`
          : 'En vivo';
      if (recording && !state.grabando) {
        recording = false;
        $('#stopRecord').hidden = true;
        toast('Clip guardado');
        refreshFiles();
      }
      if (state.grabando) {
        recording = true;
        $('#stopRecord').hidden = false;
        $('#stopRecord').textContent = `Detener grabación (${state.grabando.segundos || 0} s)`;
      }
      const kbps = dataMode.value === 'ahorro' ? state.kbps : state.kbps_enviados;
      $('#metrics').textContent = `${state.fps || 0} fps · ${kbps || 0} kbps`;
      $('#estimate').textContent = kbps ? `≈ ${((kbps * 3600 / 8) / 1024).toFixed(0)} MB por hora` : '—';
      if (dataMode.value !== 'ahorro' && state.segundos_desde_ultimo_fotograma >= 3 && Date.now() - lastReload > 2500) {
        lastReload = Date.now();
        imageStream();
      }
      renderServer(state);
    } catch {
      statusFailures += 1;
      $('#status').textContent = 'Sin conexión con la PC';
      if (statusFailures >= 3) $('#live').textContent = 'Sin conexión con la PC';
    }
  }, 2000);

  const tab = saved.get('pestana', 'files');
  const tabButton = $(`nav [data-tab="${tab}"]`) || $('nav [data-tab="files"]');
  tabButton.click();
  setStream();
  refreshFiles();
})();
