'use strict';

// All model and source text is inserted with textContent / DOM nodes, never innerHTML.
(() => {
  const ALLOWED_HOSTS = ['darglobal.co.uk', 'wasalt.sa', 'web.archive.org'];
  const MAX_SEND_TURNS = 6;
  const state = { history: [], busy: false, seq: 0 };
  const $ = (id) => document.getElementById(id);
  const log = $('log');
  const input = $('input');
  const sendBtn = $('send');
  const emptyTemplate = $('empty').cloneNode(true);
  const TYPE_LABEL = {
    archive_snapshot: 'Archived snapshot',
    live_sitemap_derived: 'From live sitemap',
    live_page: 'Live page',
    manual: 'Saved manually',
  };

  function el(tag, props, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else node.setAttribute(key, value === true ? '' : String(value));
    }
    for (const child of children) {
      if (child === null || child === undefined) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  function safeUrl(raw, hosts = ALLOWED_HOSTS) {
    try {
      const url = new URL(raw);
      if (url.protocol !== 'https:' || url.username || url.password) return null;
      const host = url.hostname.toLowerCase();
      return hosts.some((d) => host === d || host.endsWith(`.${d}`)) ? url.href : null;
    } catch {
      return null;
    }
  }

  const isRtl = (text) => (text.match(/[؀-ۿ]/g) || []).length > (text.match(/[A-Za-z]/g) || []).length;
  const stripCites = (text) => text.replace(/\[\d{1,2}\]/g, '').trim();
  const fmtDate = (iso) => {
    const d = new Date(`${iso}T00:00:00Z`);
    return Number.isNaN(d.getTime()) ? iso : d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric', timeZone: 'UTC' });
  };
  const shortModel = (id) => id.replace(/:free$/, '').split('/').pop();

  function inline(text, cites, msgId) {
    const frag = document.createDocumentFragment();
    const re = /(\*\*[^*\n]+\*\*|\[\d{1,2}\])/g;
    let last = 0;
    let match;
    while ((match = re.exec(text))) {
      if (match.index > last) frag.append(text.slice(last, match.index));
      const token = match[0];
      if (token.startsWith('**')) {
        frag.append(el('strong', { text: token.slice(2, -2) }));
      } else {
        const n = Number(token.slice(1, -1));
        if (cites.has(n)) {
          frag.append(el('sup', null, el('a', { class: 'cite', href: `#src-${msgId}-${n}`, 'aria-label': `Source ${n}`, text: String(n) })));
        }
      }
      last = re.lastIndex;
    }
    if (last < text.length) frag.append(text.slice(last));
    return frag;
  }

  function renderAnswer(text, cites, msgId) {
    const wrap = el('div', { class: 'answer' });
    let list = null;
    let listType = '';
    for (const raw of text.split('\n')) {
      const line = raw.trimEnd();
      const bullet = line.match(/^\s*(?:[-*•]|(\d+)[.)])\s+(.*)$/);
      if (bullet) {
        const type = bullet[1] ? 'ol' : 'ul';
        if (!list || listType !== type) {
          list = el(type);
          listType = type;
          wrap.append(list);
        }
        list.append(el('li', null, inline(bullet[2], cites, msgId)));
        continue;
      }
      list = null;
      if (!line.trim()) continue;
      const heading = line.match(/^#{1,4}\s+(.*)$/);
      wrap.append(el('p', { class: heading ? 'h' : null }, inline(heading ? heading[1] : line, cites, msgId)));
    }
    return wrap;
  }

  function sourceItem(c, idPrefix) {
    const href = safeUrl(c.url);
    const archive = c.archive_url ? safeUrl(c.archive_url) : null;
    const title = href
      ? el('a', { href, target: '_blank', rel: 'noopener noreferrer', text: c.title })
      : el('span', { text: c.title });
    return el('li', { class: 'src', id: `src-${idPrefix}-${c.n}` },
      el('span', { class: 'n', text: String(c.n) }),
      el('div', { class: 'src-body' },
        el('div', { class: 'src-title' }, title),
        el('div', { class: 'src-meta' },
          el('span', { class: `pill ${c.site}`, text: c.site === 'darglobal' ? 'DarGlobal' : 'Wasalt' }),
          el('span', { text: `${TYPE_LABEL[c.source_type] || c.source_type} · ${fmtDate(c.captured_at)}` }),
          archive ? el('a', { class: 'arch', href: archive, target: '_blank', rel: 'noopener noreferrer', text: 'archived copy' }) : null)));
  }

  function addMessage(classes, node, rtl = false) {
    const empty = $('empty');
    if (empty) empty.remove();
    const msg = el('article', { class: `msg ${classes}`, dir: rtl ? 'rtl' : 'auto' }, node);
    log.append(msg);
    msg.scrollIntoView({ behavior: 'smooth', block: 'end' });
    return msg;
  }

  function addAssistant(data) {
    const msgId = ++state.seq;
    const cites = new Set(data.citations.map((c) => c.n));
    const body = el('div', null, renderAnswer(data.answer, cites, msgId));
    if (data.truncated) body.append(el('p', { class: 'note', text: 'The answer was cut short by the model’s length limit.' }));
    if (data.grounding === 'uncited') {
      body.append(el('p', { class: 'note warn', text: 'The model did not cite a source for this answer. Treat it with caution.' }));
    }
    if (data.citations.length) {
      body.append(el('h3', { class: 'src-h', text: 'Sources' }),
        el('ol', { class: 'srcs' }, ...data.citations.map((c) => sourceItem(c, msgId))));
    }
    if (data.related && data.related.length) {
      body.append(el('details', { class: 'related' },
        el('summary', { text: `Other retrieved pages (${data.related.length})` }),
        el('ol', { class: 'srcs' }, ...data.related.map((c) => sourceItem(c, `${msgId}r`)))));
    }
    const secs = (data.latency_ms / 1000).toFixed(1);
    body.append(el('p', { class: 'meta', text: `${data.model}${data.fallback_used ? ' (fallback model)' : ''} · ${secs}s` }));
    addMessage('assistant', body, isRtl(data.answer));
  }

  function addError(err) {
    const text = [err.message || 'Something went wrong.', err.retry_after_s ? `Try again in ${err.retry_after_s}s.` : '']
      .filter(Boolean).join(' ');
    addMessage('assistant error', el('div', null,
      el('p', { text }),
      el('p', { class: 'meta', text: `code: ${err.code || 'unknown'}${err.request_id ? ` · ref ${err.request_id}` : ''}` })));
  }

  function setBusy(busy) {
    state.busy = busy;
    sendBtn.disabled = busy;
    sendBtn.textContent = busy ? 'Thinking…' : 'Send';
  }

  function autosize() {
    input.style.height = 'auto';
    input.style.height = `${Math.min(input.scrollHeight, 180)}px`;
  }

  function updateCount() {
    $('count').textContent = `${input.value.length} / 1000`;
  }

  async function ask(raw) {
    const text = raw.trim();
    if (!text || state.busy) return;
    setBusy(true);
    addMessage('user', el('p', { text }), isRtl(text));
    input.value = '';
    updateCount();
    autosize();
    const pending = addMessage('assistant pending', el('div', { class: 'typing', role: 'status', 'aria-label': 'Assistant is answering' }, el('span'), el('span'), el('span')));
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 95000);
    try {
      const res = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: text, history: state.history.slice(-MAX_SEND_TURNS) }),
        signal: controller.signal,
      });
      let data = null;
      try { data = await res.json(); } catch { data = null; }
      pending.remove();
      if (res.ok && data && data.status === 'ok') {
        addAssistant(data);
        state.history.push({ role: 'user', content: text },
          { role: 'assistant', content: stripCites(data.answer).slice(0, 3900) || '(no answer)' });
        state.history = state.history.slice(-12);
      } else {
        addError((data && data.error) || { message: `Unexpected response (HTTP ${res.status}).`, code: `http_${res.status}` });
      }
    } catch (e) {
      pending.remove();
      const timeout = e && e.name === 'AbortError';
      addError({ message: timeout ? 'The request timed out. Please try again.' : 'Network error. Please check your connection and try again.',
        code: timeout ? 'client_timeout' : 'network' });
    } finally {
      clearTimeout(timer);
      setBusy(false);
      input.focus();
    }
  }

  function renderHow(m) {
    const body = $('how-body');
    body.replaceChildren();
    const docs = m.documents || {};
    const asOf = m.data_as_of || {};
    const label = { 'darglobal:archive_snapshot': 'DarGlobal pages (Internet Archive snapshots)',
      'wasalt:live_sitemap_derived': 'Wasalt coverage and inventory (derived from live sitemaps)',
      'wasalt:archive_snapshot': 'Wasalt pages (Internet Archive snapshots)',
      'darglobal:manual': 'DarGlobal pages saved manually', 'wasalt:manual': 'Wasalt pages saved manually' };
    const list = el('ul');
    for (const [key, range] of Object.entries(asOf)) {
      const span = range.oldest === range.newest ? fmtDate(range.newest) : `${fmtDate(range.oldest)} to ${fmtDate(range.newest)}`;
      list.append(el('li', { text: `${label[key] || key}: ${docs[key] || 0} documents, captured ${span}` }));
    }
    body.append(el('h4', { text: 'Data' }), list);
    body.append(el('h4', { text: 'How it was collected' }), el('p', { text:
      'Both sites show bot challenges (Imperva and Cloudflare) to automated clients. The scraper records that and never bypasses it. '
      + 'Instead it reads the sites’ own sitemaps and dated Internet Archive copies of their pages, applies each site’s robots.txt '
      + '(also to archived copies), paces its requests, and scans everything for hidden instructions aimed at AI systems.' }));
    const traps = m.traps || {};
    const summary = traps.summary || {};
    const trapList = el('ul');
    for (const t of traps.examples || []) {
      trapList.append(el('li', null, `${t.severity} · ${t.pattern} in ${t.location} of ${t.url}: `, el('code', { text: t.excerpt })));
    }
    body.append(el('h4', { text: `Trap scan: ${summary.total || 0} finding(s), none followed` }), trapList);
    const models = (m.models && m.models.available) || [];
    body.append(el('h4', { text: 'Model' }), el('p', { text: models.length
      ? `${models.join(' → ')} (each checked as $0 on OpenRouter${m.models.verified_free ? '' : ', check pending'}). The first available model answers; the rest are fallbacks.`
      : 'No free model is available right now.' }));
    const src = m.source_url ? safeUrl(m.source_url, ['github.com']) : null;
    if (src) body.append(el('p', null, el('a', { href: src, target: '_blank', rel: 'noopener noreferrer', text: 'Source code on GitHub' })));
  }

  async function loadMeta() {
    try {
      const res = await fetch('/api/meta');
      if (!res.ok) throw new Error(String(res.status));
      const m = await res.json();
      const asOf = m.data_as_of || {};
      const bits = [`${(m.documents && m.documents.total) || 0} documents`];
      const dg = asOf['darglobal:archive_snapshot'];
      const ws = asOf['wasalt:live_sitemap_derived'];
      if (dg) bits.push(`DarGlobal snapshots ${fmtDate(dg.oldest)} to ${fmtDate(dg.newest)}`);
      if (ws) bits.push(`Wasalt sitemaps ${fmtDate(ws.newest)}`);
      const models = (m.models && m.models.available) || [];
      bits.push(models.length ? `model: ${shortModel(models[0])}` : 'model: unavailable');
      $('status').textContent = bits.join(' · ');
      renderHow(m);
    } catch {
      $('status').textContent = 'Data summary unavailable.';
    }
  }

  $('composer').addEventListener('submit', (e) => { e.preventDefault(); ask(input.value); });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); ask(input.value); }
  });
  input.addEventListener('input', () => { updateCount(); autosize(); });
  log.addEventListener('click', (e) => {
    const chip = e.target.closest('.chip');
    if (chip) ask(chip.textContent);
  });
  $('new-chat').addEventListener('click', () => {
    if (state.busy) return;
    state.history = [];
    log.replaceChildren(emptyTemplate.cloneNode(true));
    input.focus();
  });
  loadMeta();
})();
