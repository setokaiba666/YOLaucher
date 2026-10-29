'use strict';
// Yu-Gi-Oh! Online 2 Launcher: la página. Habla con Go por window.go.main.App.<Método>() (promesas) y escucha eventos con
// window.runtime.EventsOn. Los campos que vienen de Go llevan la inicial en mayúscula (Estado, Fila...), salvo Ajustes y el
// formulario del servidor, que llevan etiquetas json en minúscula.
//
// Textos: la interfaz es solo en inglés. Todo lo que ve el usuario sale de i18n.js (t, tm) y de i18n/en.json. Go manda
// mensajes con clave ({k, p}, ver sala.Msg) y la página los escribe en inglés; nunca se compara un texto para decidir qué hacer.
//
// Todo lo que escribe un anfitrión (nombre, descripción, salas...) es texto no fiable: SIEMPRE por textContent (helper el()),
// nunca por innerHTML. Y no se traduce.

// Wails inyecta window.go (los métodos de Go) y window.runtime (eventos) al cargar la página: se miran al usarlos.
const apiGo = () => (window.go && window.go.main && window.go.main.App) || null;
const rt = () => window.runtime || null;
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

function el(tag, props, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === false || v == null) continue;
    if (k === 'class') e.className = v;
    else if (k === 'text') e.textContent = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v === true ? '' : v);
  }
  for (const c of kids.flat()) if (c != null && c !== false) e.append(c.nodeType ? c : document.createTextNode(String(c)));
  return e;
}
// Iconos SVG de index.html (<symbol id="i-...">): sin emojis en ninguna parte
const SVGNS = 'http://www.w3.org/2000/svg';
// Orbes de colores (img/ico/, juego.css): qué orbe lleva cada glifo. Con el tema «juego» el <svg> se pinta con el orbe
// (atributo data-o); sin él (o si falta una imagen) se ve el glifo de siempre. Fuera: los de las ventanas y los pasos.
const ORBE = { 'i-globe': 'globe', 'i-host': 'servidor', 'i-news': 'novedades', 'i-gear': 'ajustes', 'i-key': 'key', 'i-search': 'search',
  'i-star': 'star', 'i-star-o': 'star', 'i-refresh': 'intercambios', 'i-pc': 'duelo_disco', 'i-cup': 'torneos', 'i-cards': 'catalogo',
  'i-users': 'jugadores', 'i-lock': 'sanciones', 'i-shield': 'shield', 'i-spark': 'sparkle', 'i-play': 'play', 'i-stop': 'stop',
  'i-folder': 'folder', 'i-check': 'check', 'i-plug': 'plug', 'i-alert': 'alerta', 'i-copy': 'copy', 'i-eye': 'eye', 'i-eye-off': 'eyeoff' };
function ponOrbe(sv, orbe) {
  if (orbe === false || sv.closest('.wb,.paso,.tip')) return sv;
  const u = sv.querySelector('use'), n = u && (u.getAttribute('href') || '').slice(1);
  const o = orbe || sv.dataset.o || ORBE[n];
  if (o) { sv.dataset.o = o; if (n === 'i-star-o') sv.classList.add('off'); }
  return sv;
}
function icon(nombre, cls, orbe) {
  const sv = document.createElementNS(SVGNS, 'svg');
  sv.setAttribute('class', 'i' + (cls ? ' ' + cls : ''));
  const u = document.createElementNS(SVGNS, 'use');
  u.setAttribute('href', '#' + nombre);
  sv.append(u);
  return ponOrbe(sv, orbe);
}
document.querySelectorAll('svg.i').forEach(sv => ponOrbe(sv));
// El color del avatar de un servidor sale de su peer ID (siempre el mismo para el mismo servidor): 5 colores
function claseAvatar(x) {
  let h = 2166136261;
  for (const ch of String(x.ID || x.Nombre || '')) { h ^= ch.charCodeAt(0); h = Math.imul(h, 16777619) >>> 0; }
  return ['', 'c2', 'c3', 'c4', 'c5'][h % 5];
}
// el avatar es el orbe del disco de duelo en el color de su peer ID (5 colores); el servidor propio lleva el orbe de servidor
const avatar = x => { const c = claseAvatar(x); return el('span', { class: ('av ' + c).trim() }, icon(x.Propio ? 'i-host' : 'i-pc', null, x.Propio ? 'servidor' : c ? 'disco_' + c : 'duelo_disco')); };
// append() convierte null en el texto «null»: esto salta los huecos
function agrega(padre, ...hijos) { padre.append(...hijos.flat().filter(h => h != null && h !== false)); }
function call(nombre, ...args) {
  const a = apiGo();
  if (!a || typeof a[nombre] !== 'function') return Promise.reject(new Error(t('app.no_bridge_call', { name: nombre })));
  return a[nombre](...args);
}
// el texto de un error de Go (con clave -> traducido); errK = su clave ('' si no tiene)
const msg = e => I18N.errInfo(e).text;
const errK = e => I18N.errInfo(e).k;

// Tema «juego» (juego.css, todo bajo html.gfx): los gráficos van dentro del exe, pero si alguno clave faltara se quita la
// clase y la ventana queda con el dibujo CSS de app.css (sin romper nada).
['game/win', 'game/btn_azul', 'game/fila', 'game/cubo_n', 'game/placa', 'game/chip_azul', 'game/osc/win', 'game/osc/fila', 'ico/mundo_64', 'ico/key_64', 'ico/duelo_disco_64'].forEach(n => {
  const im = new Image();
  im.onerror = () => document.documentElement.classList.remove('gfx');
  im.src = 'img/' + n + '.png';
});

// errores de JavaScript -> log del lanzador (para poder verlos sin abrir las herramientas del navegador)
function informarError(x) { try { const a = apiGo(); if (a && a.ErrorJS) a.ErrorJS(String(x)); } catch (_) {} }
window.addEventListener('error', e => informarError('error: ' + e.message + ' (' + e.filename + ':' + e.lineno + ')'));
window.addEventListener('unhandledrejection', e => informarError('promesa sin capturar: ' + ((e.reason && e.reason.message) || e.reason)));
I18N.informarCon(informarError);

const nombreRegla = r => (r === 'vanilla' || r === 'mejorado' ? t('rules.' + r) : r || '?');
const nombreCuentas = c => (c === 'libre' ? t('det.acc_open') : c === 'cerradas' ? t('det.acc_closed') : t('det.acc_unknown'));
// Nombres de sala que escribe el anfitrión; los dos de siempre llevan nombre propio, el resto se enseña tal cual.
const nombreSala = s => (s === 'Mundial' ? t('room.world') : s === 'Por idioma' ? t('room.per_lang') : s);
const nomSrv = n => n || t('server.private'); // un nombre vacío = servidor privado (entrado con código)
const VER_JUEGO = '080912';

const S = {
  lista: { Filas: [], Buscando: false, Vivos: 0, EnCache: 0 },
  estado: null,
  ajustes: { actualizar: true, mostrar_ocultos: false, ocultos: [], servidor: {} },
  sel: null,
  filtros: { texto: '', reglas: '', fav: false, llenos: false, sinClave: false },
  vista: 'jugar',
  ocupado: false,
  cap: null,       // la capa «Entrando…»: su estado
  novedades: null, // novedades.json ya leído
  informe: null,   // último «Comprobar instalación»
  ocup: null,      // la capa «Un momento…»
};

// ------------------------------------------------------------------ aspecto (claro / oscuro / el de Windows)
function pintaTema() {
  const v = TEMA.valor();
  $$('#aTema [data-tema]').forEach(b => { b.textContent = t({ system: 'set.theme_system', light: 'set.theme_light', dark: 'set.theme_dark' }[b.dataset.tema]); });
  $('#aTemaTxt').textContent = t('set.theme_hint');
  $$('#aTema [data-tema]').forEach(b => { const on = b.dataset.tema === v; b.setAttribute('aria-checked', String(on)); b.classList.toggle('on', on); });
}
// ------------------------------------------------------------------ tamano de la ventana del juego (Ajustes > Display)
function pintaVentana() {
  const v = (S.ajustes && S.ajustes.ventana) || '';
  $$('#aVentana [data-vent]').forEach(b => {
    b.textContent = t({ '': 'set.win_orig', '1.5': 'set.win_x15', '2': 'set.win_x2', fit: 'set.win_fit' }[b.dataset.vent]);
    const on = b.dataset.vent === v; b.setAttribute('aria-checked', String(on)); b.classList.toggle('on', on);
  });
  $('#aVentanaTxt').textContent = t(v === '' ? 'set.win_hint' : v === 'fit' ? 'set.win_hint_fit' : 'set.win_hint_zoom');
}
$$('#aVentana [data-vent]').forEach(b => b.addEventListener('click', async () => {
  try { await call('PonerVentana', b.dataset.vent); S.ajustes = await call('Ajustes'); } catch (x) { toast(msg(x), 'mal'); }
  pintaVentana();
}));

$$('#aTema [data-tema]').forEach(b => b.addEventListener('click', async () => {
  TEMA.aplicar(b.dataset.tema); // al instante; después se guarda en los ajustes
  pintaTema();
  try { await call('PonerTema', b.dataset.tema); S.ajustes = await call('Ajustes'); } catch (x) { toast(msg(x), 'mal'); }
}));

// ------------------------------------------------------------------ avisos, confirmaciones, capas
function toast(texto, tipo) {
  const x = el('div', { class: 'toast ' + (tipo || ''), role: 'status', text: texto });
  $('#toasts').append(x);
  setTimeout(() => x.remove(), tipo === 'mal' || tipo === 'warn' ? 10000 : 6000);
}
const capa = (id, on) => $('#' + id).classList.toggle('on', on);

// confirmar({titulo, texto, si, no, rojo}): los textos ya vienen traducidos
function confirmar({ titulo, texto, si, no, rojo }) {
  return new Promise(res => {
    $('#kTit').textContent = titulo;
    $('#kTxt').textContent = texto;
    const bSi = $('#kSi'), bNo = $('#kNo');
    bSi.textContent = si || t('common.ok');
    bNo.textContent = no || t('common.cancel');
    bSi.className = 'b mini ' + (rojo === false ? 'oro' : 'rojo');
    const fin = v => { capa('capaConf', false); bSi.onclick = bNo.onclick = null; res(v); };
    bSi.onclick = () => fin(true);
    bNo.onclick = () => fin(false);
    capa('capaConf', true);
    bNo.focus();
  });
}
// ocupado(): textos por clave de i18n/en.json
function ocupado(titulo, texto, paso) {
  S.ocupado = true;
  S.ocup = { titulo, texto, paso };
  pintaOcupado();
  capa('capaOcup', true);
}
function pintaOcupado() {
  const o = S.ocup;
  if (!o) return;
  $('#oTit').textContent = t(o.titulo || 'busy.title');
  $('#oTxt').textContent = o.texto ? t(o.texto) : '';
  $('#oPaso').textContent = t(o.paso || 'busy.working');
}
function libre() { S.ocupado = false; S.ocup = null; capa('capaOcup', false); }

// ------------------------------------------------------------------ navegación
function mostrar(v) {
  S.vista = v;
  $$('nav a').forEach(a => a.classList.toggle('on', a.dataset.v === v));
  $$('.vista').forEach(s => s.classList.toggle('on', s.id === 'v-' + v));
  if (v === 'crear') { pintaVivo(true); if (G.tab === 'gestion') gCargar(); }
  if (v === 'ajustes') { pintaAjustes(); if (typeof pintaRed === 'function') pintaRed(); }
}
$$('nav a').forEach(a => {
  a.addEventListener('click', () => mostrar(a.dataset.v));
  a.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); mostrar(a.dataset.v); } });
});
$('#eJuego').addEventListener('click', () => { if (S.estado && !S.estado.JuegoOK) mostrar('ajustes'); });
$('#eSrv').addEventListener('click', () => mostrar('crear'));
$('#bJuego').addEventListener('click', () => mostrar('ajustes'));
$('#bJuego').addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); mostrar('ajustes'); } });

// barra de título propia
$('#bMin').onclick = () => call('Minimizar').catch(() => {});
$('#bMax').onclick = () => call('Maximizar').catch(() => {});
$('#bCerrar').onclick = () => call('CerrarVentana').catch(() => {});
$('#barra').addEventListener('dblclick', e => { if (e.target.closest('.btn')) return; call('Maximizar').catch(() => {}); });

// ------------------------------------------------------------------ JUGAR: filtros y lista
function visibles() {
  const f = S.filtros, q = f.texto.trim().toLowerCase();
  const r = S.lista.Filas.filter(x =>
    (S.ajustes.mostrar_ocultos || !x.Oculto) &&
    (!f.reglas || x.Reglas === f.reglas) &&
    (!f.fav || x.Favorito) &&
    (!f.llenos || !x.Lleno || x.Propio) &&
    (!f.sinClave || !x.Contrasena || x.Propio) &&
    (!q || (x.Nombre + ' ' + x.Descripcion + ' ' + x.IDCorto + ' ' + x.Huella).toLowerCase().includes(q)));
  // lo sin confirmar, detrás de lo que ya contestó; luego los que pasaron a privados y al final los que no contestan
  const orden = x => (x.Propio ? 0 : ({ vivo: 1, cache: 2, privado: 3, 'sin-respuesta': 4 })[x.Estado] || 4);
  const llena = x => (x.Lleno && x.Estado === 'vivo' ? 1 : 0); // los llenos, detrás de los que tienen sitio
  r.sort((a, b) =>
    (orden(a) - orden(b)) || (llena(a) - llena(b)) ||
    (a.Estado === 'vivo' && b.Estado === 'vivo' ? b.Jugadores - a.Jugadores : 0) || a.Nombre.localeCompare(b.Nombre)); // más jugadores primero; a igualdad, por nombre
  return r;
}
const pingClase = p => (p <= 0 ? 'na' : p < 60 ? 'ok' : p < 110 ? 'med' : 'mal');
const pingTexto = x => (x.PingMs > 0 ? t('ping.ms', { v: (x.PorRelay ? '~' : '') + x.PingMs }) : '—');

function botonEntrar(x, grande) {
  const srvOK = S.estado && S.estado.Servidor.EnMarcha && S.estado.Servidor.JuegoOK;
  const conectado = S.estado && (S.estado.Conexion || S.estado.Entrando || S.estado.JugandoLocal);
  let texto = t(grande ? 'play.join_big' : 'play.join'), dis = false, tip = '';
  if (x.Propio) { texto = t(grande ? 'play.here_big' : 'play.here'); dis = !srvOK; } // el propio: nunca «Join», solo «Play here»
  else if (x.Estado === 'cache') { texto = grande ? t('play.checking') : texto; dis = true; tip = t('tag.cache_tip'); }
  else if (x.Estado === 'sin-respuesta') { texto = grande ? t('play.offline') : texto; dis = true; tip = t('tag.noreply_tip'); }
  else if (x.Estado === 'privado') { texto = grande ? t('play.private_now') : texto; dis = true; tip = t('tag.private_tip'); }
  else if (!x.Compatible) { texto = t('play.other_ver'); dis = true; tip = t('play.other_ver_tip', { v: x.Version, mine: VER_JUEGO }); }
  else if (x.Lleno) { texto = t('play.full'); dis = true; }
  if (conectado && !dis) { dis = true; tip = t('play.busy_tip'); }
  return el('button', { class: 'b' + (x.Propio ? ' oro' : '') + (grande ? ' xl' : texto.length > 7 ? ' sm' : ''), text: texto, disabled: dis, title: tip || false,
    onclick: ev => { ev.stopPropagation(); S.sel = x.ID; entrarEn(x); pintaLista(); } });
}
function entrarEn(x) {
  // con contraseña y sin guardarla: primero el diálogo (Go diría lo mismo con msg.err.pide_clave)
  if (!x.Propio && x.Contrasena && !x.ClaveGuard) { abrePw(x.ID, x.Nombre); return; }
  (x.Propio ? call('JugarAqui') : call('Entrar', x.ID)).catch(e => {
    if (errK(e) === 'msg.err.pide_clave') { abrePw(x.ID, x.Nombre); return; }
    toast(msg(e), 'mal');
    if (errK(e) === 'msg.err.sin_juego') mostrar('ajustes');
  });
}

// ------------------------------------------------------------------ contraseña de un servidor de la lista (modo «Password»)
// La contraseña solo vive en el campo hasta pulsar Join: se manda a Go (EntrarConClave) y el campo se vacía. Go la convierte
// en la clave del servidor (Argon2id) y, con «Remember password», guarda solo esa clave cifrada con DPAPI.
function abrePw(id, nombre, error) {
  S.pw = { id, nombre };
  $('#pwTxt').textContent = t('pw.sub', { name: nombre || '?' });
  $('#pwClave').value = '';
  verClave('#pwVer', false);
  const err = $('#pwErr');
  err.textContent = error || '';
  err.hidden = !error;
  capa('capaPw', true);
  $('#pwClave').focus();
}
function cierraPw() { $('#pwClave').value = ''; S.pw = null; capa('capaPw', false); }
async function pwEntrar() {
  const p = S.pw, c = $('#pwClave').value;
  if (!p) return;
  if (!c) { $('#pwErr').textContent = t('pw.empty'); $('#pwErr').hidden = false; return; }
  const recordar = $('#pwRecordar').getAttribute('aria-checked') === 'true';
  try {
    await call('EntrarConClave', p.id, c, recordar);
    cierraPw();
  } catch (e) {
    $('#pwErr').textContent = msg(e);
    $('#pwErr').hidden = false;
    if (errK(e) === 'msg.err.sin_juego') { cierraPw(); mostrar('ajustes'); }
  }
}
$('#pwSi').onclick = pwEntrar;
$('#pwNo').onclick = cierraPw;
$('#pwClave').addEventListener('keydown', e => { if (e.key === 'Enter') pwEntrar(); if (e.key === 'Escape') cierraPw(); });
$('#pwRecordar').onclick = () => ponTog('#pwRecordar', $('#pwRecordar').getAttribute('aria-checked') !== 'true');
// botón «Show»/«Hide» de un campo de contraseña (el del diálogo y el del formulario del anfitrión)
function verClave(boton, ver) {
  const b = $(boton), campo = $('#' + b.getAttribute('aria-controls'));
  campo.type = ver ? 'text' : 'password';
  b.setAttribute('aria-pressed', String(ver));
  b.title = t(ver ? 'pw.hide_tip' : 'pw.show_tip');
  b.replaceChildren(icon(ver ? 'i-eye-off' : 'i-eye'), el('span', { text: t(ver ? 'pw.hide' : 'pw.show') }));
}
$$('.ver-clave').forEach(b => b.addEventListener('click', () => {
  if (b.closest('.bloq')) return;
  verClave('#' + b.id, b.getAttribute('aria-pressed') !== 'true');
  $('#' + b.getAttribute('aria-controls')).focus();
}));

function filaServidor(x) {
  const lleno = x.Lleno && x.Estado === 'vivo';
  const vivo = x.Estado === 'vivo';
  let col3;
  if (vivo) {
    col3 = el('div', { class: 'jugc' },
      el('div', { class: 'jug' }, I18N.fmtN(x.Jugadores) + ' / ' + I18N.fmtN(x.Max), lleno ? el('span', { class: 'full', text: t('tag.full') }) : null),
      el('div', { class: 'track' }, el('i', { style: 'width:' + Math.max(2, Math.min(100, Math.round(100 * x.Jugadores / Math.max(1, x.Max)))) + '%' })),
      x.PingMs > 0 ? el('div', { class: 'ping ' + pingClase(x.PingMs), title: x.PorRelay ? t('ping.relay_tip') : false, text: pingTexto(x) }) : null);
  } else if (x.Estado === 'cache') {
    col3 = el('div', { class: 'jugc' }, el('span', { class: 'est-fila', title: t('tag.cache_tip') }, el('span', { class: 'spin' }), t('play.checking')));
  } else if (x.Estado === 'privado') {
    col3 = el('div', { class: 'jugc' }, el('span', { class: 'est-fila', title: t('tag.private_tip') }, t('play.private_now'),
      el('small', { text: t('play.private_now_sub') })));
  } else {
    col3 = el('div', { class: 'jugc' }, el('span', { class: 'est-fila', title: t('tag.noreply_tip') }, t('play.offline'),
      el('small', { text: t('play.last_seen', { ago: I18N.duracion(x.VistoHace) }) })));
  }
  const row = el('div', { class: 'fila' + (x.ID === S.sel ? ' sel' : '') + (x.Propio ? ' fijo' : '') + (x.Oculto ? ' oculta' : '') +
      (x.Estado === 'cache' ? ' cache' : '') + (x.Estado === 'sin-respuesta' || x.Estado === 'privado' ? ' sin' : ''), role: 'listitem', tabindex: '0' },
    avatar(x),
    el('div', { style: 'min-width:0' },
      el('div', { class: 'nom' }, x.Favorito ? icon('i-star', 'star') : null, x.Nombre),
      el('div', { class: 'tags' },
        el('span', { class: 'tag' + (x.Reglas === 'mejorado' ? ' vi' : ''), text: nombreRegla(x.Reglas).toUpperCase() }),
        x.Contrasena ? el('span', { class: 'tag sin pw', title: t(x.ClaveGuard ? 'tag.password_saved_tip' : 'tag.password_tip') },
          icon('i-lock', null, false), t('tag.password')) : null,
        x.Propio ? el('span', { class: 'tag propio', text: t('tag.yours') }) : null,
        x.Oculto ? el('span', { class: 'tag gr', text: t('tag.hidden') }) : null)),
    col3,
    botonEntrar(x, false));
  row.addEventListener('click', () => { S.sel = x.ID; pintaLista(); });
  row.addEventListener('dblclick', () => { if (vivo && !x.Lleno && x.Compatible) entrarEn(x); });
  row.addEventListener('keydown', e => { if (e.key === 'Enter') { S.sel = x.ID; pintaLista(); } });
  return row;
}

function pintaLista() {
  const L = $('#lista'), top = L.scrollTop;
  // 12-10: todo en la misma lista (ya ordenada): los que no contestan, al final como «Offline · last seen…»
  const principales = visibles(), navegables = principales;
  if (!navegables.some(x => x.ID === S.sel)) S.sel = navegables.length ? navegables[0].ID : null;
  L.replaceChildren();
  for (const x of principales) L.append(filaServidor(x));
  if (!principales.length) {
    const hay = S.lista.Filas.length, f = S.filtros;
    const filtrado = hay > 0 && (f.texto.trim() || f.reglas || f.fav || f.llenos || f.sinClave);
    L.append(el('div', { class: 'pista vacia' },
      filtrado ? t('play.no_match') :
      S.lista.Buscando ? [el('span', { class: 'spin' }), t('play.searching')] :
      t('play.none_found'),
      el('br'), S.lista.Buscando || filtrado ? '' : t('play.none_hint')));
  } else {
    L.append(el('div', { class: 'pista' }, t('play.private_hint')));
  }
  L.scrollTop = top;
  pintaDetalle(navegables.find(x => x.ID === S.sel));
  pintaBarraLista();
}

function fila(k, ...v) { return [el('span', { text: k }), el('b', {}, ...v)]; }
function pintaDetalle(x) {
  const D = $('#det');
  D.replaceChildren();
  if (!x) { D.append(el('div', { class: 'in' }, el('div', { class: 'vacio', text: t('det.pick') }))); return; }
  const vivo = x.Estado === 'vivo';
  const ver = x.Compatible ? el('b', { class: 'ok' }, icon('i-check'), t('det.ver_ok', { v: x.Version })) : el('b', { class: 'mal' }, icon('i-x'), t('det.ver_bad', { v: x.Version, mine: VER_JUEGO }));
  // el ping solo existe si contestó en esta sesión (Go manda -1 en lo demás)
  const ping = vivo && x.PingMs > 0 ? el('b', {}, (x.PorRelay ? '~' : '') + x.PingMs + ' ms', x.PorRelay ? el('small', { text: t('det.ping_relay') }) : null) : el('b', { text: t('det.ping_na') });
  const duelo = x.EnDuelo >= 0 ? I18N.fmtN(x.EnDuelo) : t('det.no_count');
  const visto = vivo ? t('det.seen_now') : x.Estado === 'sin-respuesta' ? t('det.seen_noreply', { ago: I18N.duracion(x.VistoHace) })
    : x.Estado === 'privado' ? t('det.seen_private') : t('det.seen_cache', { ago: I18N.duracion(x.VistoHace) });
  const viejo = () => (vivo ? null : el('small', { text: t('det.stale') }));
  D.append(
    el('div', { class: 'hd' }, el('span', { class: 't', text: x.Nombre }), avatar(x)),
    el('div', { class: 'in' },
      el('div', { class: 'desc', text: x.Descripcion }),
      el('div', { class: 'kv' },
        fila(t('det.host'), el('span', { class: 'mono', style: 'padding:0;border:0;color:inherit', text: x.Huella || '—' }), el('small', { text: t('det.id', { id: x.IDCorto }) })),
        fila(t('det.rules'), nombreRegla(x.Reglas)),
        fila(t('det.accounts'), nombreCuentas(x.Cuentas)),
        fila(t('det.password'), x.Contrasena ? [icon('i-lock', null, false), t('det.pw_yes')] : t('det.pw_no'),
          x.ClaveGuard ? el('small', { text: t('det.pw_saved') }) : null,
          x.ClaveGuard ? el('button', { class: 'b claro mini pw-olvidar', title: t('det.pw_forget_tip'),
            onclick: () => call('OlvidarClave', x.ID).then(() => toast(t('toast.pw_forgotten'), 'ok')).catch(e => toast(msg(e), 'mal')) }, t('det.pw_forget')) : null),
        fila(t('det.players'), t('det.players_val', { n: I18N.fmtN(x.Jugadores), max: I18N.fmtN(x.Max) }), viejo()),
        fila(t('det.in_duel'), duelo, viejo()),
        [el('span', { text: t('det.ping') }), ping],
        [el('span', { text: t('det.version') }), ver],
        fila(t('det.seen'), visto)),
      x.Salas && x.Salas.length ? el('div', {}, el('span', { class: 'lbl', text: t('det.rooms') }),
        el('div', { class: 'salas' }, x.Salas.map(s => el('span', { class: 'tag az', text: nombreSala(s).toUpperCase() })))) : null,
      el('div', { class: 'nota', text: t('det.note') })),
    el('div', { class: 'pie' },
      botonEntrar(x, true),
      el('div', { class: 'dos' },
        el('button', { class: 'b claro' + (x.Favorito ? ' on' : ''), title: x.Favorito ? t('det.fav_remove_tip') : t('det.fav_add_tip'),
          onclick: () => call('Favorito', x.ID, !x.Favorito).catch(e => toast(msg(e), 'mal')) },
          icon(x.Favorito ? 'i-star' : 'i-star-o', 'star'), x.Favorito ? t('det.fav_on') : t('det.fav_off')),
        x.Propio ? null : el('button', { class: 'b claro', title: x.Oculto ? false : t('det.hide_tip'), onclick: () => ocultar(x) },
          icon(x.Oculto ? 'i-eye' : 'i-eye-off'), x.Oculto ? t('det.unhide') : t('det.hide')))));
}
async function ocultar(x) {
  try {
    await call('Ocultar', x.ID, !x.Oculto);
    S.ajustes = await call('Ajustes');
    toast(x.Oculto ? t('toast.visible_again') : t('toast.hidden', { name: x.Nombre }), 'ok');
  } catch (e) { toast(msg(e), 'mal'); }
}

function pintaBarraLista() {
  const e = S.estado, l = S.lista;
  const red = $('#bRed');
  const estado = e ? e.RedP2P : 'sin arrancar';
  const cls = estado === 'conectada' ? '' : estado === 'conectando' ? 'warn' : 'off';
  // el número de pares de la DHT son nodos de la red, no jugadores: solo en el tooltip, para no confundir
  red.title = estado === 'conectada' && e.Pares ? t('bar.nodes_tip', { n: e.Pares }) : '';
  red.replaceChildren(el('i', { class: 'led ' + cls }),
    el('span', { text: (estado === 'conectada' ? t('bar.p2p_on') : estado === 'conectando' ? t('bar.p2p_connecting') : t('bar.p2p_off')) +
      ' · ' + t('bar.live', { n: l.Vivos }) + (l.EnCache ? ' ' + t('bar.cached', { n: l.EnCache }) : '') }));
  const b = $('#bBusca');
  b.replaceChildren();
  if (l.Buscando) b.append(el('span', { class: 'spin' }), t('bar.searching'));
  $('#bRefrescar').disabled = !!l.Buscando;
}

function pintaConexion() {
  const e = S.estado, c = e && e.Conexion;
  $('#conn').hidden = !c;
  if (!c) return;
  $('#connNom').textContent = nomSrv(c.Nombre);
  $('#connDet').textContent = '· ' + tm(c.Via) + (c.PingMs > 0 ? ' · ' + c.PingMs + ' ms' : '') + (c.JuegoAbierto ? '' : ' · ' + t('play.game_closed'));
  $('#bReabrir').hidden = c.JuegoAbierto;
}

// filtros
$('#fTexto').addEventListener('input', e => { S.filtros.texto = e.target.value; pintaLista(); });
$$('.chip[data-reglas]').forEach(c => c.addEventListener('click', () => {
  S.filtros.reglas = c.dataset.reglas;
  $$('.chip[data-reglas]').forEach(o => o.setAttribute('aria-pressed', String(o === c)));
  pintaLista();
}));
$('#fFav').onclick = e => { S.filtros.fav = !S.filtros.fav; e.currentTarget.setAttribute('aria-pressed', String(S.filtros.fav)); pintaLista(); };
$('#fLlenos').onclick = e => { S.filtros.llenos = !S.filtros.llenos; e.currentTarget.setAttribute('aria-pressed', String(S.filtros.llenos)); pintaLista(); };
$('#fSinClave').onclick = e => { S.filtros.sinClave = !S.filtros.sinClave; e.currentTarget.setAttribute('aria-pressed', String(S.filtros.sinClave)); pintaLista(); };
$('#bRefrescar').onclick = () => { call('Buscar').catch(e => toast(msg(e), 'mal')); };
$('#bDesconectar').onclick = async () => {
  const c = S.estado && S.estado.Conexion;
  if (c && c.JuegoAbierto && !(await confirmar({ titulo: t('conf.disc_title'),
    texto: t(c.Nombre ? 'conf.disc_text' : 'conf.disc_text_private', { name: c.Nombre }), si: t('play.disconnect') }))) return;
  call('Desconectar').then(refrescar).catch(e => toast(msg(e), 'mal'));
};
$('#bReabrir').onclick = () => call('AbrirJuego').catch(e => toast(msg(e), 'mal'));

// entrar con código
$('#bCodigo').onclick = () => { $('#codigo').value = ''; $('#codErr').hidden = true; capa('capaCod', true); $('#codigo').focus(); };
$('#codCancelar').onclick = () => capa('capaCod', false);
async function entrarCodigo() {
  const c = $('#codigo').value.trim();
  if (!c) { $('#codErr').textContent = t('code.paste'); $('#codErr').hidden = false; return; }
  try {
    await call('EntrarConCodigo', c);
    $('#codigo').value = '';
    capa('capaCod', false);
  } catch (e) {
    $('#codErr').textContent = msg(e);
    $('#codErr').hidden = false;
    if (errK(e) === 'msg.err.sin_juego') { capa('capaCod', false); mostrar('ajustes'); }
  }
}
$('#codEntrar').onclick = entrarCodigo;
$('#codigo').addEventListener('keydown', e => { if (e.key === 'Enter') entrarCodigo(); if (e.key === 'Escape') capa('capaCod', false); });

// ------------------------------------------------------------------ capa «Entrando…»
// S.cap = { fase: 'conectando'|'listo'|'error'|'reiniciando', servidor, pasos: [{estado, via, ms}], aviso, error, tecnico, irAjustes, sub }
const PASOS = ['ov.step0', 'ov.step1', 'ov.step2', 'ov.step3'];
function pintaCapa(soloSub) {
  const c = S.cap;
  if (!c) return;
  const sub = $('#cSub');
  $('#cCaja').classList.toggle('error', c.fase === 'error');
  if (c.fase === 'conectando') {
    $('#cTit').textContent = c.servidor ? t('ov.joining', { name: c.servidor }) : t('ov.joining_private');
    sub.textContent = c.sub != null ? c.sub : c.aviso ? tm(c.aviso) : t('ov.takes_a_few');
    sub.classList.remove('listo');
  } else if (c.fase === 'listo') {
    $('#cTit').textContent = t('ov.ready');
    sub.textContent = t('ov.ready_sub');
    sub.classList.add('listo');
  } else if (c.fase === 'error') {
    $('#cTit').textContent = t('ov.fail');
    sub.textContent = c.sub != null ? c.sub : tm(c.error);
    sub.classList.remove('listo');
  } else if (c.fase === 'reiniciando') {
    $('#cTit').textContent = t('ov.restarting');
    sub.textContent = c.servidor ? t('ov.restarting_sub', { name: c.servidor }) : t('ov.restarting_sub_private');
    sub.classList.remove('listo');
  }
  if (soloSub) return;
  const box = $('#pasos');
  box.replaceChildren(...PASOS.map((k, i) => {
    const p = c.pasos[i] || {};
    const det = p.estado === 'ya' && p.via && p.via.k ? tm(p.via) + (p.ms > 0 ? ', ' + p.ms + ' ms' : '') : '';
    return el('div', { class: 'paso' + (p.estado ? ' ' + p.estado : '') },
      el('i', {}, p.estado === 'ya' ? icon('i-check', null, false) : p.estado === 'fallo' ? icon('i-x') : null),
      el('div', {}, el('span', { text: t(k) }), el('small', { text: det })));
  }));
  const tec = c.fase === 'error' && c.tecnico;
  $('#cDet').hidden = !tec;
  $('#cTec').textContent = tec ? c.tecnico : '';
  botonesCapa({ cancelar: c.fase === 'conectando', cerrar: c.fase === 'listo' || c.fase === 'error',
    reintentar: c.fase === 'error' && !c.irAjustes && !c.sinReintento, ajustes: c.fase === 'error' && c.irAjustes });
}
function marcaPaso(i, estado, via, ms) {
  const c = S.cap;
  if (!c || i < 0 || i >= PASOS.length) return;
  for (let k = 0; k < i; k++) if (!c.pasos[k] || c.pasos[k].estado !== 'ya') c.pasos[k] = Object.assign({}, c.pasos[k], { estado: 'ya' });
  c.pasos[i] = { estado, via: via || (c.pasos[i] && c.pasos[i].via), ms: ms || (c.pasos[i] && c.pasos[i].ms) };
}
function botonesCapa({ cancelar, cerrar, reintentar, ajustes }) {
  const b = $('#cBtn');
  b.hidden = !(cancelar || cerrar);
  b.textContent = t(cancelar ? 'common.cancel' : 'common.close');
  b.onclick = cancelar ? () => call('CancelarEntrada').catch(() => {}) : () => { S.cap = null; capa('capa', false); };
  $('#cReintentar').hidden = !reintentar;
  $('#cAjustes').hidden = !ajustes;
}
$('#cReintentar').onclick = () => call('Reintentar').catch(e => { if (S.cap) { S.cap.sub = msg(e); pintaCapa(true); } });
$('#cAjustes').onclick = () => { S.cap = null; capa('capa', false); mostrar('ajustes'); };

function eventoEntrar(e) {
  switch (e.Fase) {
    case 'inicio':
      S.cap = { fase: 'conectando', servidor: e.Servidor, pasos: [], aviso: null };
      pintaCapa();
      capa('capa', true);
      refrescar();
      break;
    case 'paso':
      if (!S.cap) break;
      marcaPaso(e.Paso, e.Estado, e.Via, e.Ms);
      pintaCapa();
      break;
    case 'aviso':
      if (!S.cap) break;
      S.cap.aviso = e.Aviso;
      pintaCapa(true);
      break;
    case 'listo':
      if (!S.cap) S.cap = { fase: 'listo', servidor: e.Servidor, pasos: [] };
      marcaPaso(PASOS.length - 1, 'ya');
      S.cap.fase = 'listo';
      pintaCapa();
      refrescar();
      break;
    case 'error':
      if (e.PideClave && e.Direccion && typeof dirPideClave === 'function') { // red.js: el diálogo «Join by address» otra vez
        S.cap = null;
        capa('capa', false);
        dirPideClave(e);
        refrescar();
        break;
      }
      if (e.PideClave && e.ServidorID) { // contraseña mala o que falta: otra vez el diálogo, con el motivo
        S.cap = null;
        capa('capa', false);
        abrePw(e.ServidorID, e.Servidor, tm(e.Error));
        refrescar();
        break;
      }
      if (!S.cap) S.cap = { fase: 'error', servidor: e.Servidor, pasos: [] };
      Object.assign(S.cap, { fase: 'error', error: e.Error, tecnico: e.Tecnico, irAjustes: e.IrAjustes, sinReintento: e.SinReintento, sub: null });
      pintaCapa();
      capa('capa', true);
      refrescar();
      break;
    case 'cancelado':
      S.cap = null;
      capa('capa', false);
      refrescar();
      break;
    case 'reiniciando':
      S.cap = Object.assign(S.cap || { pasos: [] }, { fase: 'reiniciando', servidor: e.Servidor });
      pintaCapa();
      capa('capa', true);
      break;
  }
}

// ------------------------------------------------------------------ MY SERVERS (varios servidores guardados, cada uno con su mundo)
// Go: MisServidores (tarjetas), CrearMiServidor / EditarMiServidor (formulario), BorrarMiServidor (con el nombre escrito),
// ArrancarMiServidor (solo uno a la vez: si hay otro en marcha, se pregunta y se para antes) y PararServidor.
// Las REGLAS se eligen al crear y no cambian: en «Edit» salen bloqueadas.
const M = { lista: null, modo: 'lista', id: null, cargando: false }; // modo: lista | nuevo | editar
function marcaOpc(grupo, valor) { $$('[role=radio]', $(grupo)).forEach(d => d.setAttribute('aria-checked', String(d.dataset.v === String(valor)))); }
function valorOpc(grupo) { const d = $('[aria-checked=true]', $(grupo)); return d ? d.dataset.v : ''; }
$$('.opc').forEach(g => $$('[role=radio]', g).forEach(d => {
  const pon = () => { if (!g.closest('.bloq')) $$('[role=radio]', g).forEach(x => x.setAttribute('aria-checked', String(x === d))); };
  d.addEventListener('click', pon);
  d.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pon(); } });
}));
function cargaForm(f) {
  $('#sNombre').value = f.nombre || '';
  $('#sDesc').value = f.descripcion || '';
  const sel = $('#sMax');
  if (![...sel.options].some(o => o.value === String(f.max))) sel.append(el('option', { value: String(f.max), text: String(f.max) }));
  sel.value = String(f.max || 30);
  marcaOpc('#sVis', f.modo || (f.publica ? 'publico' : 'oculto')); // «modo» desde el 12-10; antes solo «publica»
  S.tieneClave = !!f.tiene_clave;
  $('#sClave').value = '';
  pintaClaveSrv();
  marcaOpc('#sReglas', f.reglas || 'vanilla');
  $('#sCuentas').value = f.cuentas || 'libre';
  const nv = $('#sNivel'), lv = String(f.nivel_inicial || 1);
  if (![...nv.options].some(o => o.value === lv)) nv.append(el('option', { value: lv, text: lv }));
  nv.value = lv;
  const dp = $('#sDP'), v = String(f.dp_inicio || 10);           // 0 / sin campo (formulario de antes) = 10, el original
  if (![...dp.options].some(o => o.value === v)) dp.append(el('option', { value: v, text: v }));
  dp.value = v;
  ponTog('#sGratis', !!f.duelos_gratis);
  ponTog('#sTorneos', f.torneos !== false);
  ponTog('#sEventos', f.eventos !== false);
  ponTog('#sRelojTorneo', f.reloj_torneo !== false);
  ponTog('#sDuelpassAuth', f.duelpass_auth !== false);
  $('#sRegaloMP').value = String((f.regalos_inicio && f.regalos_inicio.mp) || 0);
  $('#sRegaloDPP').value = String((f.regalos_inicio && f.regalos_inicio.dpp) || 0);
  $('#sBaul').value = (f.regalos_inicio && f.regalos_inicio.baul) || 'empty';
}
function leeForm() {
  return { nombre: $('#sNombre').value.trim(), descripcion: $('#sDesc').value.trim(), max: parseInt($('#sMax').value, 10) || 0,
    modo: valorOpc('#sVis') || 'publico', publica: valorOpc('#sVis') !== 'oculto',
    contrasena: valorOpc('#sVis') === 'clave' ? $('#sClave').value : '', tiene_clave: !!S.tieneClave, reglas: valorOpc('#sReglas') || 'vanilla', cuentas: $('#sCuentas').value,
    nivel_inicial: parseInt($('#sNivel').value, 10) || 1, dp_inicio: parseInt($('#sDP').value, 10) || 10,
    duelos_gratis: $('#sGratis').getAttribute('aria-checked') === 'true',
    torneos: $('#sTorneos').getAttribute('aria-checked') === 'true',
    eventos: $('#sEventos').getAttribute('aria-checked') === 'true',
    reloj_torneo: $('#sRelojTorneo').getAttribute('aria-checked') === 'true',
    duelpass_auth: $('#sDuelpassAuth').getAttribute('aria-checked') === 'true',
    regalos_inicio: { mp: parseInt($('#sRegaloMP').value, 10) || 0, dpp: parseInt($('#sRegaloDPP').value, 10) || 0, baul: $('#sBaul').value } };
}
// el campo de la contraseña solo con «Password»; si ya hay una guardada, vacío = la de antes
function pintaClaveSrv() {
  const on = valorOpc('#sVis') === 'clave';
  $('#sClaveBox').hidden = !on;
  $('#sClave').placeholder = S.tieneClave ? t('host.pw_keep') : t('host.pw_new', { min: 4, max: 64 });
}
$$('#sVis [role=radio]').forEach(d => { d.addEventListener('click', pintaClaveSrv); d.addEventListener('keydown', pintaClaveSrv); });
$('#sGratis').onclick = () => ponTog('#sGratis', $('#sGratis').getAttribute('aria-checked') !== 'true');
$('#sTorneos').onclick = () => ponTog('#sTorneos', $('#sTorneos').getAttribute('aria-checked') !== 'true');
$('#sEventos').onclick = () => ponTog('#sEventos', $('#sEventos').getAttribute('aria-checked') !== 'true');
$('#sRelojTorneo').onclick = () => ponTog('#sRelojTorneo', $('#sRelojTorneo').getAttribute('aria-checked') !== 'true');
$('#sDuelpassAuth').onclick = () => ponTog('#sDuelpassAuth', $('#sDuelpassAuth').getAttribute('aria-checked') !== 'true');

const srvEnMarcha = () => !!(S.estado && S.estado.Servidor.EnMarcha);
const misDe = id => (M.lista || []).find(m => m.ID === id);
async function cargaMis() {
  if (M.cargando) return;
  M.cargando = true;
  try { M.lista = await call('MisServidores'); } catch (x) { informarError('mis servidores: ' + msg(x)); }
  M.cargando = false;
  pintaMis();
  pintaVivo(false);
}
// la lista o el formulario (New / Edit) en la columna de la izquierda
function pintaModo() {
  const form = M.modo !== 'lista';
  $('#misPanel').hidden = form;
  $('#form').hidden = !form;
  if (!form) return;
  const ed = M.modo === 'editar';
  $('#formTit').textContent = t(ed ? 'host.edit_title' : 'host.new_title');
  $('#fGuardarTxt').textContent = t(ed ? 'host.save' : 'host.create');
  $('#sReglas').classList.toggle('bloq', ed);
  $('#sReglas').setAttribute('aria-disabled', String(ed));
  $('#sReglasNota').textContent = t(ed ? 'host.rules_fixed' : 'host.rules_once');
  $('#sReglasNota').classList.toggle('fijas', ed);
}
async function abreForm(modo, m) {
  M.modo = modo; M.id = m ? m.ID : null;
  $('#sErr').hidden = true;
  cargaForm(m ? m.Form : await call('FormNuevo').catch(() => ({})));
  pintaModo();
  $('#form .pc').scrollTop = 0;
  $('#sNombre').focus();
}
function cierraForm() { M.modo = 'lista'; M.id = null; $('#sClave').value = ''; pintaModo(); pintaMis(); }
$('#bNuevo').onclick = () => abreForm('nuevo');
$('#fCancelar').onclick = cierraForm;
$('#fGuardar').onclick = async () => {
  const f = leeForm(), err = $('#sErr'), b = $('#fGuardar');
  err.hidden = true;
  b.disabled = true;
  try {
    if (M.modo === 'editar') { await call('EditarMiServidor', M.id, f); toast(t('toast.srv_saved', { name: f.nombre }), 'ok'); }
    else { await call('CrearMiServidor', f); toast(t('toast.srv_created', { name: f.nombre }), 'ok'); }
    $('#sClave').value = ''; // la contraseña no se queda en la página
    cierraForm();
    await cargaMis();
  } catch (x) { err.textContent = msg(x); err.hidden = false; }
  b.disabled = false;
};

const visTag = f => (f.modo === 'clave' ? el('span', { class: 'tag sin pw' }, icon('i-lock', null, false), t('tag.password'))
  : f.modo === 'oculto' ? etiqueta('gr', t('srv.vis_hidden')) : etiqueta('az', t('srv.vis_public')));
function tarjeta(m) {
  const e = S.estado || { Servidor: {} };
  const parando = !!e.Parando, otro = srvEnMarcha() && !m.EnMarcha;
  const f = m.Form;
  const estado = m.EnMarcha ? t('host.card_running', { n: I18N.fmtN(m.Jugadores), max: I18N.fmtN(m.Max) })
    : m.UsadoHaceS >= 0 ? t('host.card_last', { max: I18N.fmtN(m.Max), ago: I18N.duracion(m.UsadoHaceS) }) : t('host.card_never', { max: I18N.fmtN(m.Max) });
  const acciones = m.EnMarcha
    ? [el('button', { class: 'b rojo mini', disabled: parando, onclick: pararServidor }, icon('i-stop'), parando ? t('srv.stopping') : t('srv.stop'))]
    : [el('button', { class: 'b oro mini', disabled: parando, title: otro ? t('host.start_switch_tip') : false, onclick: () => arrancarMio(m) }, icon('i-play'), t('host.start')),
      el('button', { class: 'b claro mini editar', disabled: parando, title: t('host.edit'), onclick: () => abreForm('editar', m) }, icon('i-gear'), el('span', { class: 'ct', text: t('host.edit') })),
      el('button', { class: 'b claro mini borrar', disabled: parando, title: t('host.delete_tip'), 'aria-label': t('host.delete'), onclick: () => pideBorrar(m) }, icon('i-x'))];
  const fila = el('div', { class: 'fila g mio' + (m.EnMarcha ? ' fijo' : ''), role: 'listitem' },
    el('span', { class: 'av' }, icon('i-host', null, 'servidor')),
    el('div', { style: 'min-width:0' },
      el('div', { class: 'nom', text: f.nombre }),
      el('div', { class: 'tags' },
        el('span', { class: 'tag' + (f.reglas === 'mejorado' ? ' vi' : ''), text: nombreRegla(f.reglas).toUpperCase() }),
        visTag(f),
        m.EnMarcha ? etiqueta('propio', t('host.running')) : null),
      el('small', { class: 'gsub', text: estado + (f.descripcion ? ' · ' + f.descripcion : ''), title: m.Huella ? t('host.fp_tip', { fp: m.Huella }) : null })),
    el('div', { class: 'gact' }, acciones));
  return fila;
}
function pintaMis() {
  const L = $('#misLista');
  if (!L || M.modo !== 'lista') return;
  L.replaceChildren();
  if (M.lista == null) { L.append(vacio(t('mg.loading'))); return; }
  if (!M.lista.length) {
    L.append(el('div', { class: 'pista vacia', text: t('host.none') }));
    return;
  }
  for (const m of M.lista) L.append(tarjeta(m));
}
// «Start»: solo uno a la vez (comparten los puertos del juego). Con otro en marcha se pregunta y se para con orden antes.
async function arrancarMio(m) {
  const s = S.estado && S.estado.Servidor;
  if (s && s.EnMarcha) {
    const ok = await confirmar({ titulo: t('conf.switch_title'), si: t('conf.switch_btn'), rojo: false,
      texto: t('conf.switch_text', { actual: s.Nombre || t('srv.title'), nuevo: m.Form.nombre }) + (s.Jugadores > 0 ? ' ' + t('conf.switch_players', { n: s.Jugadores }) : '') });
    if (!ok) return;
    if (!(await pararServidor(true))) return;
  }
  ocupado('busy.starting', null, 'busy.starting_step');
  try {
    await call('ArrancarMiServidor', m.ID);
    toast(t('toast.server_starting'), 'ok');
  } catch (x) { toast(msg(x), 'mal'); }
  libre();
  await refrescar();
  await cargaMis();
  pintaVivo(true);
}
// «Delete»: hay que escribir el nombre; nunca el que está en marcha (Go lo comprueba también)
function pideBorrar(m) {
  M.borrar = m;
  $('#bTxt').textContent = t(m.BorraDatos ? 'host.delete_text' : 'host.delete_text_keep', { name: m.Form.nombre });
  $('#bNomTxt').textContent = m.Form.nombre;
  const inp = $('#bNombre');
  inp.value = '';
  $('#bErr').hidden = true;
  $('#bSi').disabled = true;
  capa('capaBorrar', true);
  inp.focus();
}
$('#bNombre').addEventListener('input', () => { $('#bSi').disabled = !M.borrar || $('#bNombre').value.trim() !== M.borrar.Form.nombre.trim(); });
$('#bNombre').addEventListener('keydown', e => { if (e.key === 'Enter' && !$('#bSi').disabled) $('#bSi').click(); });
$('#bNo').onclick = () => { M.borrar = null; capa('capaBorrar', false); };
$('#bSi').onclick = async () => {
  const m = M.borrar;
  if (!m) return;
  $('#bSi').disabled = true;
  try {
    await call('BorrarMiServidor', m.ID, $('#bNombre').value);
    capa('capaBorrar', false);
    M.borrar = null;
    toast(t(m.BorraDatos ? 'toast.srv_deleted' : 'toast.srv_removed', { name: m.Form.nombre }), 'ok');
    await cargaMis();
  } catch (x) { $('#bErr').textContent = msg(x); $('#bErr').hidden = false; $('#bSi').disabled = false; }
};

let firmaVivo = '';
// estado del túnel P2P: en la fila, el mensaje y como mucho una pista corta («Tor fallback» si no entran directos);
// lo que se sabe de la red de este equipo (lista de mensajes) va entero en el title (al pasar el ratón)
function textoP2P(m) {
  const base = tm(m);
  const red = (m && m.p && m.p.red) || [];
  const tor = red.some(x => x && (x.k === 'msg.red.hp_fallo' || x.k === 'msg.red.alcance_privada'));
  return [tor ? base + ' · ' + t('msg.red.corto_tor') : base, red.length ? red.map(tm).join('; ') : ''];
}

// «Your server» compacto (12-10, petición del owner: sin scroll a 1160x680 ni a 900x600): 4 chips con LED en 2x2 (texto corto;
// el detalle largo en el title), jugadores en una fila baja, código de sala en una fila y los botones fijos al pie del panel.
// Con NET (red.js): un 5.º chip «Internet» a lo ancho y, solo si hay dirección pública, una fila más con ella y Copy.
function chip(ok, etiqueta, corto, detalle) {
  return el('div', { class: 'ck', title: detalle || null }, el('i', { class: 'led ' + (ok === true ? '' : ok === 'warn' ? 'warn' : 'off') }),
    el('b', { text: etiqueta }), el('span', { text: corto }));
}
const kDe = m => (m && m.k) || '';
const esFallo = k => k.includes('no_disponible') || k.includes('error');
function chipsServidor(s) {
  if (!s.EnMarcha) {
    return [chip(false, t('srv.ck_server'), t('srv.ck_stopped'), t('srv.server') + ': ' + t('srv.stopped')),
      chip(false, t('srv.ck_p2p'), '—', t('srv.p2p')), chip(false, t('srv.ck_list'), '—', t('srv.listed')), chip(false, t('srv.ck_tor'), '—', t('srv.tor')),
      typeof chipInternet === 'function' ? chipInternet(null) : null];
  }
  const [p2pTxt, p2pRed] = textoP2P(s.P2P);
  const kp = kDe(s.P2P), kt = kDe(s.Tor), kl = kDe(s.Listado);
  const p2pCorto = s.P2POK ? 'srv.ck_announced' : kp === 'msg.p2p.desactivado' ? 'srv.ck_off' : esFallo(kp) ? 'srv.ck_error' : 'srv.ck_connecting';
  const listaCorto = !s.Publica ? 'srv.ck_hidden' : s.ListadoOK ? (s.ConClave ? 'srv.ck_listed_pw' : 'srv.ck_listed')
    : esFallo(kl) ? 'srv.ck_error' : 'srv.ck_announcing';
  const torCorto = s.TorOK ? 'srv.ck_ready' : kt === 'msg.tor.desactivado' ? 'srv.ck_off' : esFallo(kt) ? 'srv.ck_error' : 'srv.ck_connecting';
  return [
    chip(s.JuegoOK ? true : 'warn', t('srv.ck_server'), t(s.JuegoOK ? 'srv.ck_listening' : 'srv.ck_not_listening'),
      t('srv.server') + ': ' + (s.JuegoOK ? t('srv.runpy_on', { state: tm(s.Juego), ago: I18N.hace(s.DesdeS) }) : tm(s.Juego))),
    chip(s.P2POK ? true : 'warn', t('srv.ck_p2p'), t(p2pCorto), t('srv.p2p') + ': ' + p2pTxt + (p2pRed ? ' (' + p2pRed + ')' : '')),
    chip(s.Publica ? (s.ListadoOK ? true : 'warn') : false, t('srv.ck_list'), t(listaCorto),
      t('srv.listed') + ': ' + tm(s.Listado) + (s.ConClave ? ' · ' + t('srv.pw_on') : '')),
    chip(s.TorOK ? true : kt === 'msg.tor.desactivado' ? false : 'warn', t('srv.ck_tor'), t(torCorto), t('srv.tor') + ': ' + tm(s.Tor)),
    typeof chipInternet === 'function' ? chipInternet(s.Acceso) : null];   // NET (red.js): 5.º chip, a lo ancho
}

function pintaVivo(forzar) {
  const e = S.estado;
  if (!e) return;
  const s = e.Servidor;
  const firma = JSON.stringify([s.EnMarcha, s.ID, s.Nombre, Math.floor(s.DesdeS / 60), s.JuegoOK, s.Juego, s.P2POK, s.P2P, s.ListadoOK, s.Listado, s.TorOK, s.Tor,
    s.Jugadores, s.EnDuelo, s.Max, s.Publica, s.ConClave, s.RunPyPropio, s.Aviso, e.Parando, e.OpenServer, e.JuegoOK, !!e.Conexion, e.JugandoLocal, e.Entrando,
    M.lista ? M.lista.length : -1, s.Acceso]);
  // las tarjetas de «My servers» también cambian con el estado (en marcha, jugadores, parando): se piden de nuevo
  const claveMis = JSON.stringify([s.EnMarcha, s.ID, s.Jugadores, s.Max, e.Parando]);
  if (forzar === true || claveMis !== M.clave) { M.clave = claveMis; cargaMis(); }
  if (!forzar && firma === firmaVivo) return;
  firmaVivo = firma;
  const V = $('#vivo'), P = $('#vivoPie');
  V.replaceChildren();
  P.replaceChildren();
  $('#vivoTit').textContent = t(s.EnMarcha ? 'srv.title_on' : 'srv.title');
  if (!s.EnMarcha) {
    const sin = M.lista && !M.lista.length;
    agrega(V,
      el('div', { class: 'cks' }, chipsServidor(s)),
      el('p', { class: 'intro', text: t('srv.intro') }),
      el('p', { class: 'intro', text: t(sin ? 'host.hint_new' : 'host.hint_start') }),
      e.OpenServer ? el('div', { class: 'aviso-srv', text: t('srv.openserver') }) : null);
    if (sin) P.append(el('button', { class: 'b oro xl', onclick: () => abreForm('nuevo') }, icon('i-host'), t('host.new_big')));
    P.hidden = !sin;
    return;
  }
  P.hidden = false;
  const jug = I18N.fmtN(s.Jugadores) + (s.Max ? ' / ' + I18N.fmtN(s.Max) : '');
  const porTuneles = s.FuenteJug === 'túneles';
  const nomFp = s.Nombre ? t('srv.name_fp', { name: s.Nombre, fp: s.Huella }) : '';
  agrega(V,
    s.Nombre ? el('div', { class: 'sub', text: nomFp, title: nomFp }) : null,
    el('div', { class: 'cks' }, chipsServidor(s)),
    el('div', { class: 'nums bajo' },
      el('div', { title: porTuneles ? t('srv.tunnel_count') : null }, el('small', { text: t('srv.players') + (porTuneles ? ' *' : '') }), el('b', { text: jug })),
      el('div', {}, el('small', { text: t('det.in_duel') }), el('b', { text: s.EnDuelo >= 0 ? I18N.fmtN(s.EnDuelo) : '—' }))),
    s.Publica && !s.ConClave ? null : el('div', { class: 'cod', title: t('srv.code_label') + (s.ConClave ? ' ' + t('srv.code_skips_pw') : '') },
      el('span', { class: 'cl', text: t('srv.code_short') }), el('b', { text: '••••••' }), el('span', { class: 'sp' }),
      el('button', { class: 'b claro mini', onclick: () => call('CopiarCodigo').then(() => toast(t('toast.code_copied'), 'ok')).catch(x => toast(msg(x), 'mal')) },
        icon('i-copy'), t('srv.copy'))),
    typeof filaDireccion === 'function' ? filaDireccion(s.Acceso) : null,   // NET (red.js): una fila, solo con dirección pública
    s.Aviso && s.Aviso.k ? el('div', { class: 'aviso-srv', text: tm(s.Aviso) }) : null);
  agrega(P,
    el('button', { class: 'b oro', disabled: !s.JuegoOK || !e.JuegoOK || !!e.Conexion || e.JugandoLocal || e.Entrando,
      title: !e.JuegoOK ? t('srv.no_game_tip') : false, onclick: () => call('JugarAqui').catch(x => toast(msg(x), 'mal')) }, icon('i-play'), t('play.here')),
    el('button', { class: 'b rojo', disabled: e.Parando, onclick: () => pararServidor() }, icon('i-stop'), e.Parando ? t('srv.stopping') : t('srv.stop')));
}

// para el que está en marcha; sinPreguntar = ya se preguntó (cambiar de servidor). Devuelve true si se paró.
async function pararServidor(sinPreguntar) {
  const s = S.estado.Servidor;
  if (s.Jugadores > 0 && sinPreguntar !== true) {
    const ok = await confirmar({ titulo: t('conf.stop_title'), texto: t('conf.stop_text', { n: s.Jugadores }), si: t('srv.stop_btn') });
    if (!ok) return false;
  }
  ocupado('busy.stopping', 'busy.stopping_text', 'busy.stopping_step');
  let ok = true;
  try {
    const r = await call('PararServidor');
    toast((r.Partes || []).map(tm).join(' '), r.Forzada ? 'warn' : 'ok');
  } catch (x) { toast(msg(x), 'mal'); ok = false; }
  libre();
  await refrescar();
  pintaVivo(true);
  return ok;
}

// ------------------------------------------------------------------ MANAGE (la API de gestión de run.py, solo con el servidor propio en marcha)
// Jugadores (expulsar, vetar), Bans, Accounts, Announcement y Backup. Todo lo que viene del servidor (nombres de jugadores,
// usuarios, motivos) es texto no fiable: SIEMPRE textContent. Las contraseñas solo pasan por los campos y se vacían al enviar.
const G = { tab: 'servidor', sec: 'jugadores', jugadores: null, vetos: null, cuentas: null, copias: null, duelpass: null, regalos: null, cartas: null, mazos: null, eventos: null, torneo: null, cargando: false };
const gActivo = () => !!(S.estado && S.estado.Servidor.EnMarcha);
const MAX_AVISO = 200;

function pintaGestionTabs() {
  const on = gActivo();
  $('#crearTabs').hidden = !on;
  if (!on && G.tab === 'gestion') { G.tab = 'servidor'; G.jugadores = G.vetos = G.cuentas = G.copias = G.duelpass = G.regalos = G.cartas = G.mazos = G.eventos = G.torneo = null; }
  $('#tServidor').setAttribute('aria-pressed', String(G.tab === 'servidor'));
  $('#tGestion').setAttribute('aria-pressed', String(G.tab === 'gestion'));
  $('#crearCols').hidden = G.tab !== 'servidor';
  $('#gestion').hidden = G.tab !== 'gestion';
}
function gTab(t) { G.tab = t; pintaGestionTabs(); if (t === 'gestion') gSec(G.sec); else pintaVivo(true); }
$('#tServidor').onclick = () => gTab('servidor');
$('#tGestion').onclick = () => gTab('gestion');

function gSec(s) {
  G.sec = s;
  $$('#gNav .chip').forEach(c => c.setAttribute('aria-pressed', String(c.dataset.s === s)));
  $$('.gsec').forEach(x => x.classList.toggle('on', x.id === 'g-' + s));
  pintaGestion();
  gCargar();
}
$$('#gNav .chip').forEach(c => c.addEventListener('click', () => gSec(c.dataset.s)));

function gError(x) {
  const e = $('#gErr');
  e.hidden = !x;
  e.textContent = x ? (typeof x === 'string' ? x : msg(x)) : '';
}
// una acción: ejecuta, avisa y recarga; los errores salen como aviso rojo (con su texto en inglés)
async function gHacer(fn, okTexto, tipo) {
  try {
    const r = await fn();
    if (okTexto) toast(typeof okTexto === 'function' ? okTexto(r) : okTexto, tipo || 'ok');
    await gCargar(true);
    return true;
  } catch (x) {
    toast(msg(x), 'mal');
    gCargar(true);
    return false;
  }
}
async function gCargar(forzar) {
  if (G.tab !== 'gestion' || (G.cargando && !forzar)) return;
  if (!S.estado.Servidor.JuegoOK) { gError(t('mg.wait_server')); return; }
  G.cargando = true;
  try {
    G.jugadores = await call('GestionJugadores');
    if (G.sec === 'vetos' || G.vetos == null) G.vetos = await call('GestionVetados');
    if (G.sec === 'cuentas') G.cuentas = await call('GestionCuentas');
    if (G.sec === 'copias') G.copias = await call('GestionCopias');
    if (G.sec === 'duelpass') {
      G.duelpass = await call('GestionDuelpass'); G.regalos = await call('GestionRegalosInicio');
      G.cartas = await call('GestionDuelpassCartas'); G.mazos = await call('GestionDuelpassMazos');
    }
    if (G.sec === 'torneos') {
      G.torneo = await call('GestionTorneo'); G.eventos = await call('GestionTorneoOpciones');
      G.cartas = await call('GestionDuelpassCartas');
    }
    gError(null);
  } catch (x) { gError(x); }
  G.cargando = false;
  pintaGestion();
}
function pintaGestion() {
  const nJ = G.jugadores ? G.jugadores.length : 0, nV = G.vetos ? G.vetos.length : 0;
  $('#gnJug').textContent = G.jugadores ? String(nJ) : '';
  $('#gnVet').textContent = nV ? String(nV) : '';
  if (G.sec === 'jugadores') pintaJugadores();
  else if (G.sec === 'vetos') pintaVetos();
  else if (G.sec === 'cuentas') pintaCuentas();
  else if (G.sec === 'copias') pintaCopias();
  else if (G.sec === 'duelpass') pintaDuelpass();
  else if (G.sec === 'torneos') pintaTorneo();
  else pintaAviso();
}
setInterval(() => {
  const abierta = $$('.capa.on').length > 0;
  if (S.vista === 'crear' && G.tab === 'gestion' && !abierta && !document.hidden) gCargar();
}, 3000);

const etiqueta = (cls, texto) => el('span', { class: 'tag' + (cls ? ' ' + cls : ''), text: texto.toUpperCase() });
const vacio = texto => el('div', { class: 'pista vacia', text: texto });
const lugar = j => {
  if (j.estado === 'duelo') return etiqueta('vi', t('mg.st_duel'));
  if (j.estado === 'sala') return etiqueta('az', t('mg.st_room'));
  if (j.estado === 'mapa') {
    const mundo = j.mundo === 'Academia' ? t('mg.world_academia') : t('mg.world_domino');
    return etiqueta('az', t('mg.st_map', { place: j.zona && j.zona !== mundo ? mundo + ' · ' + j.zona : mundo }));
  }
  return etiqueta('gr', t('mg.st_menu'));
};
function gFila(av, nombre, tags, sub, acciones) {
  return el('div', { class: 'fila g', role: 'listitem' }, av,
    el('div', { style: 'min-width:0' }, el('div', { class: 'nom', text: nombre }),
      tags.length ? el('div', { class: 'tags' }, tags) : null, sub ? el('small', { class: 'gsub', text: sub }) : null),
    el('div', { class: 'gact' }, acciones));
}

// ---- Players
function pintaJugadores() {
  const L = $('#gjLista'), js = G.jugadores || [];
  $('#gjTit').textContent = G.jugadores ? t('mg.players_online', { n: js.length }) : t('mg.loading');
  L.replaceChildren();
  if (G.jugadores && !js.length) { L.append(vacio(t('mg.players_none'))); return; }
  for (const j of js) {
    const nombre = j.nombre || t('mg.joining');
    const tags = [lugar(j), j.nivel >= 0 ? etiqueta('gr', t('mg.lv', { n: j.nivel })) : null,
      j.dp >= 0 ? etiqueta('gr', t('mg.dp_tag', { n: I18N.fmtN(j.dp) })) : null,
      j.dueno ? etiqueta('', t('mg.owner')) : null, j.vetado ? etiqueta('sin', t('mg.banned')) : null].filter(Boolean);
    const sinPj = !(j.hueco >= 0);
    const bNivel = el('button', { class: 'b claro mini', disabled: sinPj, title: sinPj ? t('mg.level_tip_nochar') : false,
      onclick: () => gNivel({ corto: j.corto, nombre, fichas: [{ hueco: j.hueco, nombre, nivel: j.nivel }] }) }, t('mg.level'));
    const bDP = el('button', { class: 'b claro mini', disabled: sinPj, title: sinPj ? t('mg.level_tip_nochar') : false,
      onclick: () => gPuntos({ corto: j.corto, nombre, fichas: [{ hueco: j.hueco, nombre, dp: j.dp, mp: j.mp }] }) }, t('mg.dp'));
    L.append(gFila(avatar({ ID: j.corto, Nombre: j.nombre, Propio: j.dueno }), nombre, tags,
      j.desde_s >= 0 ? t('mg.online_for', { t: I18N.duracion(j.desde_s) }) : '',
      j.dueno ? [bNivel, bDP] : [bNivel, bDP,
        el('button', { class: 'b claro mini', onclick: () => gExpulsar(j) }, t('mg.kick')),
        el('button', { class: 'b rojo mini', onclick: () => gVetar(j) }, t('mg.ban'))]));
  }
}
// ---- Level (Players y Accounts): LV 1-99 de un personaje. q = { corto, nombre, fichas: [{hueco, nombre, nivel}] }
function pideNivel(q) {
  return new Promise(res => {
    const fs = q.fichas || [];
    $('#nTit').textContent = t('mg.level_title', { name: q.nombre });
    $('#nTxt').textContent = t('mg.level_text');
    $('#nPjBox').hidden = fs.length < 2;
    $('#nPj').replaceChildren(...fs.map(f => el('option', { value: String(f.hueco),
      text: f.nivel >= 0 ? t('mg.level_char', { name: f.nombre, n: f.nivel }) : f.nombre })));
    const actual = () => fs.find(f => String(f.hueco) === $('#nPj').value) || fs[0] || {};
    const ponActual = () => { const f = actual(); $('#nNivel').value = f.nivel >= 1 ? String(f.nivel) : '2'; };
    ponActual();
    $('#nPj').onchange = ponActual;
    $('#nErr').hidden = true;
    const fin = v => { capa('capaNivel', false); $('#nSi').onclick = $('#nNo').onclick = $('#nNivel').onkeydown = $('#nPj').onchange = null; res(v); };
    const acepta = () => {
      const txt = $('#nNivel').value.trim(), n = Number(txt);
      if (!/^\d+$/.test(txt) || n < 1 || n > 99) { $('#nErr').textContent = t('mg.level_range'); $('#nErr').hidden = false; return; }
      fin({ hueco: actual().hueco, nivel: n, nombre: actual().nombre || q.nombre });
    };
    $('#nSi').onclick = acepta;
    $('#nNo').onclick = () => fin(null);
    $('#nNivel').onkeydown = e => { if (e.key === 'Enter') acepta(); else if (e.key === 'Escape') fin(null); };
    capa('capaNivel', true);
    $('#nNivel').focus(); $('#nNivel').select();
  });
}
async function gNivel(q) {
  const r = await pideNivel(q);
  if (!r) return;
  gHacer(() => call('GestionNivel', q.corto, r.hueco, r.nivel),
    x => t(x && x.al_momento ? 'mg.t_level' : 'mg.t_level_later', { name: r.nombre, n: r.nivel }));
}
// ---- Duelpass (Players y Accounts): puntos DUELPASS y MP de un personaje. q = { corto, nombre, fichas: [{hueco, nombre, dp, mp}] }
// «Set to» pone el número (0-9.999.999); «Add» suma (negativo = quitar; nunca baja de 0). Casilla vacía = no tocar.
const PUNTOS_MAX = 9999999;
function pideDP(q) {
  return new Promise(res => {
    const fs = q.fichas || [];
    let modo = 'poner';
    $('#dTit').textContent = t('mg.dp_title', { name: q.nombre });
    $('#dTxt').textContent = t('mg.dp_text');
    $('#dPjBox').hidden = fs.length < 2;
    $('#dPj').replaceChildren(...fs.map(f => el('option', { value: String(f.hueco),
      text: f.dp >= 0 ? t('mg.dp_char', { name: f.nombre, dp: I18N.fmtN(f.dp), mp: I18N.fmtN(f.mp >= 0 ? f.mp : 0) }) : f.nombre })));
    const actual = () => fs.find(f => String(f.hueco) === $('#dPj').value) || fs[0] || {};
    const pinta = () => {
      const f = actual(), sabe = f.dp >= 0;
      $('#dAhora').textContent = sabe ? t('mg.dp_now', { dp: I18N.fmtN(f.dp), mp: I18N.fmtN(f.mp >= 0 ? f.mp : 0) }) : t('mg.dp_now_unknown');
      $$('#dModo [data-modo]').forEach(b => {
        const on = b.dataset.modo === modo;
        b.textContent = t(b.dataset.modo === 'poner' ? 'mg.dp_set' : 'mg.dp_add');
        b.setAttribute('aria-checked', String(on)); b.classList.toggle('on', on);
      });
      for (const [id, v] of [['#dDP', f.dp], ['#dMP', f.mp]]) {
        $(id).value = modo === 'poner' && v >= 0 ? String(v) : '';
        $(id).placeholder = id === '#dDP'
          ? (modo === 'poner' ? 'DP' : 'DP (+/-)')
          : (modo === 'poner' ? 'MPP' : 'MPP (+/-)');
        $(id).min = modo === 'poner' ? '0' : String(-PUNTOS_MAX);
        $(id).max = String(PUNTOS_MAX);
      }
      $('#dErr').hidden = true;
    };
    pinta();
    $('#dPj').onchange = pinta;
    $$('#dModo [data-modo]').forEach(b => { b.onclick = () => { modo = b.dataset.modo; pinta(); }; });
    const fin = v => {
      capa('capaDP', false);
      $('#dSi').onclick = $('#dNo').onclick = $('#dDP').onkeydown = $('#dMP').onkeydown = $('#dPj').onchange = null;
      $$('#dModo [data-modo]').forEach(b => { b.onclick = null; });
      res(v);
    };
    const err = k => { $('#dErr').textContent = t(k); $('#dErr').hidden = false; };
    const acepta = () => {
      const lee = id => {
        const txt = $(id).value.trim();
        if (txt === '') return null;
        if (!/^-?\d+$/.test(txt)) return NaN;
        return Number(txt);
      };
      const dp = lee('#dDP'), mp = lee('#dMP');
      const mal = n => n !== null && (Number.isNaN(n) || Math.abs(n) > PUNTOS_MAX || (modo === 'poner' && n < 0));
      if (mal(dp) || mal(mp)) { err(modo === 'poner' ? 'mg.dp_range_set' : 'mg.dp_range_add'); return; }
      if (dp === null && mp === null) { err('mg.dp_empty'); return; }
      fin({ hueco: actual().hueco, dp, mp, modo, nombre: actual().nombre || q.nombre });
    };
    $('#dSi').onclick = acepta;
    $('#dNo').onclick = () => fin(null);
    $('#dDP').onkeydown = $('#dMP').onkeydown = e => { if (e.key === 'Enter') acepta(); else if (e.key === 'Escape') fin(null); };
    capa('capaDP', true);
    $('#dDP').focus(); $('#dDP').select();
  });
}
async function gPuntos(q) {
  const r = await pideDP(q);
  if (!r) return;
  gHacer(() => call('GestionPuntos', q.corto, r.hueco, r.dp, r.mp, r.modo),
    x => t(x && x.al_momento ? 'mg.t_dp' : 'mg.t_dp_later', { name: r.nombre,
      dp: I18N.fmtN(x && x.ahora ? x.ahora.dp : 0), mp: I18N.fmtN(x && x.ahora ? x.ahora.mp : 0) }));
}
async function gExpulsar(j) {
  const name = j.nombre || t('mg.joining');
  if (!(await confirmar({ titulo: t('mg.kick_title', { name }), texto: t('mg.kick_text'), si: t('mg.kick') }))) return;
  gHacer(() => call('GestionExpulsar', j.corto), t('mg.t_kicked', { name }));
}
function pideMotivo(titulo, texto) {
  return new Promise(res => {
    $('#vTit').textContent = titulo; $('#vTxt').textContent = texto;
    $('#vMotivo').value = ''; $('#vErr').hidden = true;
    const fin = v => { capa('capaVeto', false); $('#vSi').onclick = $('#vNo').onclick = null; $('#vMotivo').onkeydown = null; res(v); };
    $('#vSi').onclick = () => fin($('#vMotivo').value.trim());
    $('#vNo').onclick = () => fin(null);
    $('#vMotivo').onkeydown = e => { if (e.key === 'Enter') fin($('#vMotivo').value.trim()); else if (e.key === 'Escape') fin(null); };
    capa('capaVeto', true);
    $('#vMotivo').focus();
  });
}
async function gVetar(j) {
  const name = j.nombre || t('mg.joining');
  const motivo = await pideMotivo(t('mg.ban_title', { name }), t('mg.ban_text'));
  if (motivo === null) return;
  gHacer(() => call('GestionVetar', j.corto, motivo), t('mg.t_banned', { name }));
}

// ---- Bans
function pintaVetos() {
  const L = $('#gvLista'), vs = G.vetos || [];
  $('#gvTit').textContent = G.vetos ? t('mg.bans_count', { n: vs.length }) : t('mg.loading');
  L.replaceChildren();
  if (G.vetos && !vs.length) { L.append(vacio(t('mg.bans_none'))); return; }
  for (const v of vs) {
    const name = v.nombres.length ? v.nombres.join(', ') : t('mg.acct_short', { id: v.corto });
    L.append(gFila(el('span', { class: 'av' }, icon('i-lock')), name, [etiqueta('gr', t('mg.banned_on', { date: v.fecha }))],
      v.motivo ? t('mg.reason_is', { reason: v.motivo }) : t('mg.no_reason'),
      [el('button', { class: 'b claro mini', onclick: () => gQuitarVeto(v, name) }, t('mg.unban'))]));
  }
}
async function gQuitarVeto(v, name) {
  if (!(await confirmar({ titulo: t('mg.unban_title', { name }), texto: t('mg.unban_text'), si: t('mg.unban'), rojo: false }))) return;
  gHacer(() => call('GestionQuitarVeto', v.corto), t('mg.t_unbanned'));
}

// ---- Accounts
function pintaCuentas() {
  const c = G.cuentas, L = $('#gcLista');
  const hint = $('#gcHint');
  hint.hidden = !c || c.estricto;
  hint.textContent = c && !c.estricto ? t('mg.acc_open_hint') : '';
  $('#gcNota').textContent = c ? t(c.estricto ? 'mg.acc_note_closed' : 'mg.acc_note_open', { max: c.max }) : '';
  $('#gcTit').textContent = c ? t('mg.acc_count', { n: c.cuentas.length }) : t('mg.loading');
  L.replaceChildren();
  if (!c) return;
  if (!c.cuentas.length) { L.append(vacio(t('mg.acc_none'))); return; }
  for (const a of c.cuentas) {
    const name = a.usuario || t('mg.acct_short', { id: a.corto });
    const fichas = a.fichas && a.fichas.length ? a.fichas : null; // run.py anterior al 12-10: solo nombres
    const pj = fichas ? fichas.map(f => (f.nivel >= 0 ? t('mg.level_char', { name: f.nombre, n: f.nivel }) : f.nombre)) : a.personajes;
    const tags = [a.dueno ? etiqueta('', t('mg.owner')) : null, a.en_linea ? etiqueta('propio', t('mg.online')) : null,
      a.vetado ? etiqueta('sin', t('mg.banned')) : null, ...pj.slice(0, 3).map(p => etiqueta('az', p)),
      pj.length > 3 ? etiqueta('gr', '+' + (pj.length - 3)) : null].filter(Boolean);
    const noBorrar = a.dueno ? t('mg.del_tip_owner') : a.en_linea ? t('mg.del_tip_online') : a.personajes.length ? t('mg.del_tip_chars') : '';
    L.append(gFila(el('span', { class: 'av' }, icon('i-key')), name, tags, a.personajes.length ? '' : t('mg.no_chars'), [
      fichas ? el('button', { class: 'b claro mini', onclick: () => gNivel({ corto: a.corto, nombre: fichas.length === 1 ? fichas[0].nombre : name, fichas }) }, t('mg.level')) : null,
      fichas ? el('button', { class: 'b claro mini', onclick: () => gPuntos({ corto: a.corto, nombre: fichas.length === 1 ? fichas[0].nombre : name, fichas }) }, t('mg.dp')) : null,
      el('button', { class: 'b claro mini', onclick: () => gClave(a, name) }, t('mg.change_pw')),
      a.dueno ? null : el('button', { class: 'b rojo mini', disabled: !!noBorrar, title: noBorrar || false, onclick: () => gBorrar(a, name) }, t('mg.delete'))]));
  }
}
async function gCrear() {
  const u = $('#gcUsuario').value, c = $('#gcClave').value;
  if (!u.trim() || !c) { toast(t('mg.acc_fill'), 'warn'); return; }
  $('#gcCrear').disabled = true;
  $('#gcClave').value = ''; // la contraseña no se queda en el campo
  try {
    await call('GestionCrearCuenta', u.trim(), c);
    $('#gcUsuario').value = '';
    toast(t('mg.t_created', { user: u.trim() }), 'ok');
    await gCargar(true);
  } catch (x) { toast(msg(x), 'mal'); }
  $('#gcCrear').disabled = false;
}
$('#gcCrear').onclick = gCrear;
$('#gcClave').addEventListener('keydown', e => { if (e.key === 'Enter') gCrear(); });
function pideClave(nombre, pedirUsuario) {
  return new Promise(res => {
    $('#pTit').textContent = t('mg.pw_title', { name: nombre });
    $('#pTxt').textContent = t('mg.pw_text');
    $('#pUsuarioBox').hidden = !pedirUsuario;
    $('#pUsuario').value = ''; $('#pClave').value = ''; $('#pErr').hidden = true;
    const limpia = () => { $('#pClave').value = ''; capa('capaClave', false); $('#pSi').onclick = $('#pNo').onclick = null; $('#pClave').onkeydown = null; };
    const acepta = () => {
      const c = $('#pClave').value, u = $('#pUsuario').value.trim();
      if (!c || (pedirUsuario && !u)) { $('#pErr').textContent = t('mg.acc_fill'); $('#pErr').hidden = false; return; }
      limpia(); res({ clave: c, usuario: u });
    };
    $('#pSi').onclick = acepta;
    $('#pNo').onclick = () => { limpia(); res(null); };
    $('#pClave').onkeydown = e => { if (e.key === 'Enter') acepta(); else if (e.key === 'Escape') { limpia(); res(null); } };
    capa('capaClave', true);
    $(pedirUsuario ? '#pUsuario' : '#pClave').focus();
  });
}
async function gClave(a, name) {
  const r = await pideClave(name, !a.usuario);
  if (!r) return;
  gHacer(() => call('GestionCambiarClave', a.corto, r.clave, r.usuario), t('mg.t_pw_changed'));
}
async function gBorrar(a, name) {
  if (!(await confirmar({ titulo: t('mg.del_title', { name }), texto: t('mg.del_text'), si: t('mg.delete') }))) return;
  gHacer(() => call('GestionBorrarCuenta', a.corto), t('mg.t_deleted'));
}

// ---- Announcement
function pintaAviso() {
  const n = $('#gaTexto').value.length;
  $('#gaNota').textContent = t('mg.notice_hint');
  $('#gaCont').textContent = n + '/' + MAX_AVISO;
  $('#gaEnviar').disabled = !$('#gaTexto').value.trim();
}
$('#gaTexto').addEventListener('input', pintaAviso);
$('#gaEnviar').onclick = async () => {
  const texto = $('#gaTexto').value.trim();
  if (!texto) return;
  $('#gaEnviar').disabled = true;
  try {
    const n = await call('GestionAviso', texto);
    $('#gaTexto').value = '';
    if (n > 0) toast(t('mg.t_notice_sent', { n }), 'ok'); else toast(t('mg.t_notice_nobody'), 'warn');
  } catch (x) { toast(msg(x), 'mal'); }
  pintaAviso();
};

// ---- Backup
function pintaCopias() {
  const L = $('#gbLista'), cs = G.copias || [];
  $('#gbNota').textContent = t('mg.backup_hint');
  $('#gbTit').textContent = G.copias ? t('mg.backups_title') : t('mg.loading');
  L.replaceChildren();
  if (G.copias && !cs.length) { L.append(vacio(t('mg.backups_none'))); return; }
  for (const c of cs) {
    L.append(gFila(el('span', { class: 'av' }, icon('i-folder')), c.fichero, [], t('mg.backup_row', { kb: I18N.fmtN(c.kb), date: c.fecha }), []));
  }
}
$('#gbCopia').onclick = async () => {
  $('#gbCopia').disabled = true;
  await gHacer(() => call('GestionCopia'), f => t('mg.t_backup', { file: f }));
  $('#gbCopia').disabled = false;
};

// ---- DUELPASS codes
function pintaDuelpass() {
  const L = $('#gdLista'), xs = G.duelpass || [];
  $('#gdTit').textContent = G.duelpass ? t('mg.duelpass_count', { n: xs.length }) : t('mg.loading');
  L.replaceChildren();
  if (G.cartas) {
    const value = $('#gdCarta').value;
    $('#gdCarta').replaceChildren(el('option', { value: '0', text: 'No card reward' }),
      ...G.cartas.map(x => el('option', { value: String(x.id), text: x.nombre ? `${x.id} · ${x.nombre}` : `Card ${x.id}` })));
    if ([...$('#gdCarta').options].some(x => x.value === value)) $('#gdCarta').value = value;
  }
  if (G.mazos) {
    const value = $('#gdMazo').value;
    $('#gdMazo').replaceChildren(el('option', { value: '0', text: 'No starter deck reward' }),
      ...G.mazos.map(x => el('option', { value: String(x.id), text: `${x.id} · ${x.nombre}` })));
    if ([...$('#gdMazo').options].some(x => x.value === value)) $('#gdMazo').value = value;
  }
  if (G.regalos) {
    if (document.activeElement !== $('#gdInicioMP')) $('#gdInicioMP').value = String(G.regalos.mp || 0);
    if (document.activeElement !== $('#gdInicioDPP')) $('#gdInicioDPP').value = String(G.regalos.dpp || 0);
    if (document.activeElement !== $('#gdInicioBaul')) $('#gdInicioBaul').value = G.regalos.baul || 'empty';
  }
  for (const x of xs) {
    const tags = [etiqueta(x.estado === 'libre' ? 'gr' : 'sin', x.estado),
      etiqueta('az', 'DPP ' + x.dpp), x.carta ? etiqueta('az', 'Card ' + x.carta) : null,
      x.mazo ? etiqueta('az', 'Deck ' + x.mazo) : null].filter(Boolean);
    L.append(gFila(el('span', { class: 'av' }, icon('i-cards')), x.codigo, tags,
      x.nota || x.creado, x.estado === 'libre' ? [el('button', { class: 'b rojo mini',
        onclick: () => gHacer(() => call('GestionAnularDuelpass', x.codigo), t('mg.duelpass_voided')) }, t('mg.duelpass_void'))] : []));
  }
  if (G.duelpass && !xs.length) L.append(vacio(t('mg.duelpass_none')));
}
$('#gdGuardarInicio').onclick = async () => {
  const r = { mp: Number($('#gdInicioMP').value) || 0, dpp: Number($('#gdInicioDPP').value) || 0, baul: $('#gdInicioBaul').value };
  try {
    await call('GestionGuardarRegalosInicio', r);
    toast(t('mg.duelpass_start_saved'), 'ok');
  } catch (x) { toast(msg(x), 'mal'); }
};
$('#gdCrear').onclick = async () => {
  const n = Number($('#gdN').value), dpp = Number($('#gdDPP').value), carta = Number($('#gdCarta').value), mazo = Number($('#gdMazo').value);
  if (!Number.isInteger(n) || n < 1 || n > 10000 || ![dpp, carta, mazo].every(Number.isInteger)) return toast(t('mg.duelpass_bad'), 'warn');
  $('#gdCrear').disabled = true;
  try {
    const codes = await call('GestionCrearDuelpass', n, dpp, carta, mazo, $('#gdNota').value.trim());
    await navigator.clipboard?.writeText(codes.join('\n')).catch(() => {});
    toast(t('mg.duelpass_created', { n: codes.length }), 'ok');
    await gCargar(true);
  } catch (x) { toast(msg(x), 'mal'); }
  $('#gdCrear').disabled = false;
};

// ---- CHAMPIONSHIP
function pintaTorneo() {
  const t0 = G.torneo;
  if (G.eventos) {
    const old = $('#gtEvento').value;
    $('#gtEvento').replaceChildren(...G.eventos.map(x => el('option', { value: String(x.evento), text: x.nombre + (x.cuadro ? ' · bracket' : ' · event') })));
    if ([...$('#gtEvento').options].some(x => x.value === old)) $('#gtEvento').value = old;
  }
  actualizaTorneoForm();
  $('#gtTit').textContent = t0 ? (t0.activo ? t('mg.tournament_status') : t('mg.tournament_none')) : t('mg.loading');
  const progress = t0 && t0.sesion && t0.sesion.progreso ? Object.entries(t0.sesion.progreso) : [];
  const family = t0 && t0.sesion ? t0.sesion.familia : '';
  $('#gtLista').replaceChildren(
    t0 && t0.activo ? el('div', { class: 'gnota', text:
      `${t0.nombre} · phase ${t0.fase} · round ${t0.ronda} · ${t0.vivos}/${t0.inscritos} alive · ${t0.segundos}s` }) : null,
    ...progress.map(([name, p]) => el('div', { class: 'gnota torneo-progreso' },
      el('b', { text: name }),
      el('span', { text: `${resumenProgreso(family, p)} · ${p.pendientes || 0} pending` }))));
  const sessionLive = !!(t0 && t0.sesion && ['activo', 'pausado'].includes(t0.sesion.estado));
  $('#gtEventoEstado').textContent = t0 && (t0.evento || sessionLive)
    ? `Active event: ${t0.evento ? t0.evento.nombre : t0.sesion.familia} · edition ${t0.sesion ? t0.sesion.serial : t0.evento.serial}${t0.pausado ? ' · PAUSED' : ''}`
    : 'No active event.';
}
function resumenProgreso(family, p) {
  if (family === 'gunslinger') return `streak ${p.racha || 0}`;
  if (family === 'evolution' || family === 'ultimate') return `${p.estrellas || 0} stars`;
  if (family === 'championship') return 'classification';
  return `${p.victorias || 0} wins`;
}
function cartaPremioNombre(id) {
  if (!Number(id)) return 'No card';
  const c = (G.cartas || []).find(x => String(x.id) === String(id));
  return c ? `${c.id} · ${c.nombre || 'Card'}` : `Card ${id}`;
}
function etiquetaCondicion(selected, milestone) {
  const m = String(milestone);
  if (selected.contador === 'racha') return `${m}-win streak`;
  if (selected.contador === 'estrellas') return `${m} stars`;
  if (m === 'campeon') return 'Champion';
  if (m === 'subcampeon') return 'Runner-up';
  return `${m} wins`;
}
function aseguraCatalogoPremio() {
  const sel = $('#gtCartaPremio');
  if (!sel || sel.dataset.ready === '1' || !(G.cartas || []).length) return;
  const current = document.querySelector('#gtPremios .torneo-condicion.on');
  sel.replaceChildren(el('option', { value: '0', text: 'No card reward' }),
    ...G.cartas.map(c => el('option', { value: String(c.id), text: `${c.id} · ${c.nombre || 'Card'}` })));
  sel.dataset.ready = '1';
  for (const b of document.querySelectorAll('#gtPremios .torneo-condicion')) {
    b.querySelector('small').textContent = cartaPremioNombre(b.dataset.card);
  }
  if (current && [...sel.options].some(o => o.value === current.dataset.card)) sel.value = current.dataset.card;
}
function seleccionaCondicion(b) {
  for (const x of document.querySelectorAll('#gtPremios .torneo-condicion')) x.classList.toggle('on', x === b);
  const sel = $('#gtCartaPremio');
  if ([...sel.options].some(o => o.value === b.dataset.card)) sel.value = b.dataset.card;
}
function pintaPremios(selected) {
  aseguraCatalogoPremio();
  const box = $('#gtPremios');
  if (!selected) { box.replaceChildren(); box.dataset.familia = ''; return; }
  if (box.dataset.familia === selected.familia && box.childElementCount) return;
  box.dataset.familia = selected.familia;
  box.replaceChildren(...(selected.milestones || []).map((milestone, i) => {
    const card = String((selected.recompensas || {})[String(milestone)] || 0);
    const b = el('button', { type: 'button', class: 'torneo-condicion' + (i ? '' : ' on') },
      el('span', { text: etiquetaCondicion(selected, milestone) }),
      el('small', { text: cartaPremioNombre(card) }));
    b.dataset.milestone = String(milestone);
    b.dataset.card = card;
    b.onclick = () => seleccionaCondicion(b);
    return b;
  }));
  const first = box.querySelector('.torneo-condicion');
  if (first) seleccionaCondicion(first);
}
function actualizaTorneoForm() {
  const selected = G.eventos && G.eventos.find(x => String(x.evento) === $('#gtEvento').value);
  const bracket = !!(selected && selected.cuadro);
  pintaPremios(selected);
  for (const id of ['#gtNombre', '#gtDia', '#gtMax']) $(id).disabled = !bracket;
  for (const id of ['#gtNombreBox', '#gtDiaBox', '#gtMaxBox']) $(id).hidden = !bracket;
  const liveSession = !!(G.torneo && G.torneo.sesion && ['activo', 'pausado'].includes(G.torneo.sesion.estado));
  const active = !!(G.torneo && (G.torneo.activo || G.torneo.evento || liveSession));
  $('#gtTick').disabled = !bracket || !active || !!G.torneo.pausado;
  $('#gtPausar').disabled = !active;
  $('#gtFinalizar').disabled = !active;
  $('#gtAnunciar').disabled = !(G.torneo && G.torneo.evento);
  $('#gtGuardarPremios').disabled = !(selected && selected.confirmado);
  $('#gtCartaPremio').disabled = !(selected && selected.confirmado);
  $('#gtIniciar').disabled = !(selected && selected.confirmado && selected.activo);
  $('#gtPremioAyuda').textContent = !selected ? 'Select a condition, then choose its card.'
    : selected.confirmado ? 'Select a condition, choose one valid card, then save the set.'
    : 'This YO2 rule is not confirmed, so its rewards stay locked.';
  $('#gtPausar').textContent = G.torneo && G.torneo.pausado ? 'Resume event progress' : 'Pause event progress';
  $('#gtEventoTipo').textContent = selected
    ? `${selected.modalidad} · ${selected.descripcion}${selected.confirmado ? '' : ' · BLOCKED: unconfirmed YO2 rule.'}`
    : 'Choose an event.';
  $('#gtIniciar').textContent = bracket ? 'Start Championship registration' : 'Start selected event';
}
$('#gtEvento').addEventListener('change', () => { $('#gtPremios').dataset.familia = ''; actualizaTorneoForm(); });
$('#gtCartaPremio').addEventListener('change', () => {
  const b = document.querySelector('#gtPremios .torneo-condicion.on');
  if (!b) return;
  b.dataset.card = $('#gtCartaPremio').value;
  b.querySelector('small').textContent = cartaPremioNombre(b.dataset.card);
});
async function accionTorneo(accion) {
  const r = await call('GestionAccionTorneo', accion, $('#gtNombre').value.trim(),
    Number($('#gtEvento').value) || 11, Number($('#gtSerial').value) || 1,
    Number($('#gtDia').value) || 1, Number($('#gtMax').value) || 8);
  G.torneo = r; pintaGestion();
}
$('#gtGuardarPremios').onclick = () => gHacer(async () => {
  const selected = G.eventos && G.eventos.find(x => String(x.evento) === $('#gtEvento').value);
  if (!selected) throw new Error('Choose an event.');
  const rewards = {};
  for (const x of document.querySelectorAll('#gtPremios .torneo-condicion')) rewards[x.dataset.milestone] = Number(x.dataset.card) || 0;
  const saved = await call('GestionGuardarReglaEvento', selected.familia, true, rewards);
  Object.assign(selected, saved); $('#gtPremios').dataset.familia = '';
  actualizaTorneoForm();
}, 'Event rewards saved.');
$('#gtIniciar').onclick = () => gHacer(() => accionTorneo('iniciar'), t('mg.tournament_started'));
$('#gtAnunciar').onclick = () => gHacer(() => accionTorneo('evento_on'), t('mg.tournament_event'));
$('#gtTick').onclick = () => gHacer(() => accionTorneo('tick'), t('mg.tournament_advanced'));
$('#gtPausar').onclick = () => gHacer(() => accionTorneo(G.torneo && G.torneo.pausado ? 'reanudar' : 'pausar'),
  G.torneo && G.torneo.pausado ? 'Event progress resumed.' : 'Event progress paused.');
$('#gtFinalizar').onclick = () => gHacer(() => accionTorneo('finalizar'), 'Event ended.');

// ------------------------------------------------------------------ NOVEDADES
// novedades.json: [{fecha: 'AAAA-MM-DD', titulo, puntos: [...]}]
function pintaNovedades() {
  const box = $('#novedades');
  if (!S.novedades) { box.replaceChildren(el('div', { class: 'pista vacia', text: t('news.error') })); return; }
  const ICOS = ['i-globe', 'i-cup', 'i-cards', 'i-news', 'i-spark'], COLS = ['', 'c3', 'c4', 'c2', 'c5'];
  box.replaceChildren(...S.novedades.map((n, i) => {
    const pts = n.puntos || [];
    return el('div', { class: 'nov' }, el('span', { class: ('av ' + COLS[i % 5]).trim() }, icon(ICOS[i % 5])),
      el('div', {}, el('h4', {}, n.titulo, el('small', { text: I18N.fmtFecha(n.fecha) })),
        pts.length === 1 ? el('p', { text: pts[0] }) : el('ul', {}, pts.map(p => el('li', { text: p })))));
  }));
}
async function cargaNovedades() {
  try {
    const r = await fetch('novedades.json', { cache: 'no-cache' });
    S.novedades = await r.json();
  } catch (x) {
    S.novedades = null;
    informarError('novedades: ' + msg(x));
  }
  pintaNovedades();
}

// ------------------------------------------------------------------ AJUSTES
function ponTog(id, on) { $(id).setAttribute('aria-checked', String(!!on)); }
function pintaAjustes() {
  const e = S.estado, a = S.ajustes;
  if (document.activeElement !== $('#aCarpeta')) $('#aCarpeta').value = (e && e.CarpetaJuego) || a.carpeta_juego || '';
  const st = $('#aCarpetaEst');
  const pon = (cls, ic, texto) => { st.className = 'est-aj ' + cls; st.replaceChildren(icon(ic), texto); };
  if (!e) { st.className = 'est-aj'; st.replaceChildren(); }
  else if (!e.JuegoOK) pon('aviso', 'i-alert', t('set.game_missing'));
  else if (e.VersionJuego && e.VersionJuego !== VER_JUEGO) pon('aviso', 'i-alert', t('set.game_other_ver', { v: e.VersionJuego, esperada: VER_JUEGO }));
  else pon('ok', 'i-check', t('set.game_found') + (e.VersionJuego ? ' · Ver.' + e.VersionJuego : ''));
  pintaTema();
  pintaVentana();
  ponTog('#aAct', a.actualizar);
  ponTog('#aOcultos', a.mostrar_ocultos);
  const n = (a.ocultos || []).length;
  $('#aRestTxt').textContent = t('set.restore') + (n ? ' (' + n + ')' : '');
  $('#aRestaurar').disabled = !n;
  $('#aVersion').textContent = t('set.version', { v: e ? e.Version : '' });
}
async function resultadoCarpeta(r) {
  if (r.Cancelado) return;
  S.ajustes = await call('Ajustes');
  await refrescar();
  if (r.Msg && r.Msg.k) toast(tm(r.Msg), r.OK ? 'warn' : 'mal');
  else if (r.OK) toast(t('toast.folder_saved'), 'ok');
  pintaAjustes();
}
$('#aCambiar').onclick = () => call('ExaminarCarpetaJuego').then(resultadoCarpeta).catch(x => toast(msg(x), 'mal'));
$('#aDetectar').onclick = () => call('DetectarCarpetaJuego').then(resultadoCarpeta).catch(x => toast(msg(x), 'mal'));
$('#aCarpeta').addEventListener('change', e => { const v = e.target.value.trim(); if (v) call('ElegirCarpetaJuego', v).then(resultadoCarpeta).catch(x => toast(msg(x), 'mal')); });
$('#aAct').onclick = async () => { const v = $('#aAct').getAttribute('aria-checked') !== 'true'; ponTog('#aAct', v); try { await call('PonerActualizar', v); S.ajustes = await call('Ajustes'); refrescar(); } catch (x) { toast(msg(x), 'mal'); } };
$('#aOcultos').onclick = async () => { const v = $('#aOcultos').getAttribute('aria-checked') !== 'true'; ponTog('#aOcultos', v); try { await call('PonerMostrarOcultos', v); S.ajustes = await call('Ajustes'); pintaLista(); } catch (x) { toast(msg(x), 'mal'); } };
$('#aRestaurar').onclick = async () => { try { await call('RestaurarOcultos'); S.ajustes = await call('Ajustes'); pintaAjustes(); pintaLista(); toast(t('toast.all_visible'), 'ok'); } catch (x) { toast(msg(x), 'mal'); } };
$('#aLogs').onclick = () => call('AbrirLogs').catch(x => toast(msg(x), 'mal'));
function pintaInforme() {
  const inf = $('#informe');
  if (!S.informe) { inf.hidden = true; return; }
  $('#informeLista').replaceChildren(...S.informe.map(c => el('div', { class: 'chk' }, el('i', { class: 'led ' + (c.Estado === 'ok' ? '' : c.Estado === 'aviso' ? 'warn' : 'bad') }), el('b', { text: tm(c.Nombre) }), el('small', { text: tm(c.Detalle) }))));
  inf.hidden = false;
}
$('#aComprobar').onclick = async () => {
  const b = $('#aComprobar');
  b.disabled = true;
  try {
    S.informe = await call('Comprobar');
    pintaInforme();
  } catch (x) { toast(msg(x), 'mal'); }
  b.disabled = false;
};
function conmutador(id) { $(id).addEventListener('keydown', e => { if (e.key === ' ' || e.key === 'Enter') { e.preventDefault(); e.currentTarget.click(); } }); }
conmutador('#aAct'); conmutador('#aOcultos'); conmutador('#sGratis');
conmutador('#sTorneos'); conmutador('#sEventos'); conmutador('#sRelojTorneo'); conmutador('#sDuelpassAuth');

// ------------------------------------------------------------------ estado general
function pintaEstado() {
  const e = S.estado;
  if (!e) return;
  $('#ver').textContent = 'v' + e.Version;
  // cada luz de la barra de iconos: color, título y detalle (el detalle sale en el globo al pasar el ratón)
  const luz = (id, cls, titulo, detalle) => { const sp = $(id); $('i', sp).className = 'led' + (cls ? ' ' + cls : ''); $('b', sp).textContent = titulo; $('small', sp).textContent = detalle || ''; };
  const bj = $('#bJuego');
  if (!e.JuegoOK) {
    luz('#eJuego', 'bad', t('st.game_missing'), t('st.game_missing_tip'));
    bj.hidden = false; bj.className = 'av-juego'; $('span', bj).textContent = t('st.game_missing'); bj.title = t('st.game_missing_tip');
  } else if (e.VersionJuego && e.VersionJuego !== VER_JUEGO) {
    const tip = t('st.game_ver_tip', { folder: e.CarpetaJuego, esperada: VER_JUEGO });
    luz('#eJuego', 'warn', t('st.game_ver', { v: e.VersionJuego }), tip);
    bj.hidden = false; bj.className = 'av-juego warn'; $('span', bj).textContent = t('st.game_ver', { v: e.VersionJuego }); bj.title = tip;
  } else {
    luz('#eJuego', '', t('st.game_ok'), e.CarpetaJuego + (e.VersionJuego ? ' · Ver.' + e.VersionJuego : ''));
    bj.hidden = true;
  }
  luz('#eLanz', e.Actualizar ? '' : 'warn', e.Actualizar ? t('st.launcher', { v: e.Version }) : t('st.no_autoupdate'), t(e.Actualizar ? 'st.launcher_tip_on' : 'st.launcher_tip_off'));
  const c = $('#eConn');
  c.hidden = !e.Conexion;
  if (e.Conexion) luz('#eConn', '', t('st.connected', { name: nomSrv(e.Conexion.Nombre) }), tm(e.Conexion.Via));
  if (e.Servidor.EnMarcha) luz('#eSrv', e.Servidor.JuegoOK ? '' : 'warn', t('st.srv_playing', { n: e.Servidor.Jugadores }), e.Servidor.Nombre || '');
  else luz('#eSrv', 'off', t('st.no_server'), '');
  pintaConexion();
  pintaBarraLista();
  pintaGestionTabs();
  if (S.vista === 'crear' || S.vista === 'jugar') pintaVivo(false);
  if (S.vista === 'ajustes') pintaAjustes();
  // 12-10: la fila de NUESTRO servidor en la lista lleva los jugadores de ahora (Go los pone en Servidores); se pide de
  // nuevo cuando cambian, sin esperar a otra búsqueda
  const claveProp = JSON.stringify([e.Servidor.EnMarcha, e.Servidor.Jugadores, e.Servidor.EnDuelo, e.Servidor.Max]);
  if (claveProp !== S.claveProp) {
    S.claveProp = claveProp;
    if ((S.lista.Filas || []).some(f => f.Propio)) call('Servidores').then(l => { S.lista = l; pintaLista(); }).catch(() => {});
  }
  // los botones «Entrar» dependen del estado (conectado, entrando...): se repintan solo si cambió
  const clave = JSON.stringify([!!e.Conexion, e.Entrando, e.JugandoLocal, e.Servidor.EnMarcha, e.Servidor.JuegoOK]);
  if (clave !== S.claveBotones) { S.claveBotones = clave; pintaLista(); }
}
async function refrescar() {
  try { S.estado = await call('Estado'); pintaEstado(); } catch (x) { informarError('estado: ' + msg(x)); }
}

// ------------------------------------------------------------------ eventos de Go
function registrarEventos() {
  const RT = rt();
  RT.EventsOn('lista', l => { S.lista = l; pintaLista(); });
  RT.EventsOn('lista_error', m => toast(tm(m), 'warn'));
  RT.EventsOn('entrar', eventoEntrar);
  RT.EventsOn('aviso', m => toast(tm(m), 'warn'));
  RT.EventsOn('juego', m => { toast(tm(m), 'ok'); refrescar(); });
  RT.EventsOn('cerrar', async p => {
    call('CierreVisto').catch(() => {});
    const partes = [];
    if (p.Conectado) partes.push(t(p.Juego ? 'conf.close_conn_game' : 'conf.close_conn'));
    if (p.Servidor) partes.push(p.Jugadores > 0 ? t('conf.close_srv_players', { n: p.Jugadores }) : t('conf.close_srv'));
    const ok = await confirmar({ titulo: t('conf.close_title'), texto: partes.join(' '), si: t('conf.close_anyway') });
    if (ok) { ocupado('busy.closing', p.Servidor ? 'busy.closing_text_srv' : null, 'busy.closing_step'); call('ConfirmarCierre').catch(() => {}); }
    else call('CancelarCierre').catch(() => {});
  });
  RT.EventsOn('cerrando', () => {});
}

// ------------------------------------------------------------------ arranque
async function iniciar() {
  await I18N.cargar();
  cargaNovedades();
  for (let i = 0; i < 100 && !(apiGo() && rt()); i++) await new Promise(r => setTimeout(r, 50)); // hasta 5 s
  if (!apiGo() || !rt()) { toast(t('app.no_bridge'), 'mal'); return; }
  registrarEventos();
  try {
    const ini = await call('Inicio');
    S.ajustes = ini.Ajustes;
    TEMA.aplicar(S.ajustes.tema || 'light');
    S.estado = ini.Estado;
    $('#ver').textContent = 'v' + ini.Version;
    if (ini.Reanudando) toast(t('app.updated'), 'ok');
  } catch (x) { informarError('inicio: ' + msg(x)); toast(t('app.state_error', { error: msg(x) }), 'mal'); }
  pintaNovedades();
  pintaEstado();
  pintaLista();
  pintaAjustes();
  await call('Listo').catch(x => informarError('listo: ' + msg(x)));
  setInterval(refrescar, 1000);
}
iniciar();
