(() => {
  const S = window.SITE || {};
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const fmt = n => Number(n || 0).toLocaleString('en-US');
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const API = (S.api || '').replace(/\/$/, '');
  const B58 = /^[1-9A-HJ-NP-Za-km-z]{32,44}$/;
  const j = async u => { const r = await fetch(u, { cache: 'no-store' }); if (!r.ok) throw new Error(r.status); return r.json(); };
  if (S.ca && $('ghlink')) $('ghlink').hidden = false; // repo goes public with the CA

  /* ---------------------------------------------------------- CA chip + X */
  document.querySelectorAll('[data-ca]').forEach(b => {
    const span = b.querySelector('span'), short = S.ca ? S.ca.slice(0, 4) + '…' + S.ca.slice(-4) + ' · copy' : '';
    if (!S.ca) { b.disabled = true; return; }
    span.textContent = short;
    b.addEventListener('click', async () => {
      try { await navigator.clipboard.writeText(S.ca); span.textContent = 'copied'; } catch { span.textContent = S.ca; }
      setTimeout(() => (span.textContent = short), 1600);
    });
  });
  document.querySelectorAll('[data-x]').forEach(a => { if (S.x) a.href = S.x; else a.style.display = 'none'; });

  /* ---------------------------------------------------------- reasons + rows */
  const RANK = r => /dev already sold|Factory|Mayhem|lookalike|Paid dev check/.test(r) ? 0 : /Paused/.test(r) ? 1 : 2;
  const order = rs => [...(rs || [])].sort((a, b) => RANK(a) - RANK(b));
  const hm = t => new Date(t * 1000).toTimeString().slice(0, 5);
  function parts(e) {
    if (e.kind === 'buy') return e.allowed ? ['ALLOWED', 'go', `${e.sol} SOL · ${e.why || ''}`] : ['BLOCKED', '', order(e.reasons).join(' ')];
    if (e.kind === 'pay') return e.allowed ? ['PAID', 'pay', `$${e.usd ?? '?'} ${e.network && e.network !== 'paper' ? 'USDC · x402' : 'x402'} · dev check`] : ['BLOCKED', '', order(e.reasons).join(' ')];
    if (e.kind === 'exit') return ['SOLD', 'ex', `${e.x}x · ${e.why}`];
    return null;
  }
  function evRow(e) {
    const p = parts(e); if (!p) return null;
    const div = document.createElement('div'); div.className = 'ev';
    const tx = e.kind === 'pay' && e.tx && B58.test(String(e.tx).slice(0, 44)) ? ` <a href="${esc(e.explorer || 'https://solscan.io/tx/' + e.tx)}" target="_blank" rel="noopener">receipt</a>` : '';
    div.innerHTML = `<span class="t">${hm(e.t)}</span><span class="k ${p[1]}">${p[0]}</span><span class="w"><b>$${esc(e.symbol)}</b> ${esc(p[2])}${tx}</span>`;
    return div;
  }
  const feed = $('feed');
  const pushFeed = e => { const r = evRow(e); if (!r) return; feed.prepend(r); while (feed.children.length > 40) feed.lastChild.remove(); };
  function setCrawl(events) {
    const html = events.slice(0, 30).map(e => {
      const p = parts(e); if (!p) return '';
      return `<span class="ci"><span class="k ${p[1]}">${p[0]}</span><b>$${esc(e.symbol)}</b><span class="r">${esc(p[2].slice(0, 70))}</span></span>`;
    }).join('');
    const el = $('crawl'); el.innerHTML = html + html;
    el.style.setProperty('--dur', Math.max(40, events.length * 4) + 's');
  }

  /* ---------------------------------------------------------- the replay broadcast */
  const cv = $('pitch'), ctx = cv.getContext('2d');
  const review = $('review'), rvState = $('rvState'), rvCoin = $('rvCoin'), rvDec = $('rvDecision'), rvWhy = $('rvWhy'), clock = $('sbClock');
  let W = 0, H = 0, DPR = 1;
  function size() { DPR = Math.min(2, devicePixelRatio || 1); W = cv.clientWidth; H = cv.clientHeight; cv.width = W * DPR; cv.height = H * DPR; ctx.setTransform(DPR, 0, 0, DPR, 0, 0); }
  addEventListener('resize', size); size();

  const COND = '"Saira Condensed", sans-serif', MONO = '"Geist Mono", monospace';
  const T_END = 20, T_DECIDE = 10, PLAY_MS = 3400, TELE_MS = 520, HOLD_MS = 2700, OUT_MS = 450;
  let coins = [], ci = 0, phase = 'idle', t0 = 0, cur = null, visible = true;

  function interleave(list) {
    const by = k => list.filter(c => c.kind === k);
    const q = { dev: by('dev'), factory: by('factory'), mayhem: by('mayhem'), allowed: by('allowed') };
    const pattern = ['dev', 'factory', 'dev', 'mayhem', 'dev', 'allowed', 'dev', 'factory', 'mayhem', 'dev'];
    const out = []; let i = 0;
    while (Object.values(q).some(a => a.length) && i < 400) { const k = pattern[i++ % pattern.length]; if (q[k].length) out.push(q[k].shift()); }
    return out;
  }
  function prep(c) {
    const pts = c.pts.filter(p => p[0] >= 0 && p[0] <= T_END && p[1] > 0);
    if (pts.length < 3) return null;
    const ys = pts.map(p => Math.log(p[1]));
    let lo = Math.min(...ys), hi = Math.max(...ys); if (hi - lo < 0.15) { lo -= 0.1; hi += 0.1; }
    const pad = (hi - lo) * 0.18;
    return { ...c, pts, lo: lo - pad, hi: hi + pad, sell: pts.find(p => p[2]) };
  }
  const box = () => W < 820 ? { x0: W * 0.06, x1: W * 0.94, y0: 96, y1: 222 } : { x0: Math.max(W * 0.47, 600), x1: W * 0.955, y0: H * 0.46, y1: H * 0.8 };
  const X = (b, t) => b.x0 + (b.x1 - b.x0) * (t / T_END);
  const Y = (b, c, mc) => b.y1 - (b.y1 - b.y0) * ((Math.log(mc) - c.lo) / (c.hi - c.lo));
  const valueAt = (c, t) => { let v = c.pts[0][1]; for (const p of c.pts) { if (p[0] > t) break; v = p[1]; } return v; };

  function grid(b, alpha) {
    ctx.save(); ctx.globalAlpha = alpha;
    ctx.strokeStyle = 'rgba(136,147,165,.12)'; ctx.lineWidth = 1;
    for (let s = 0; s <= T_END; s += 2) { const x = X(b, s); ctx.beginPath(); ctx.moveTo(x, b.y0 - 20); ctx.lineTo(x, b.y1 + 14); ctx.stroke(); }
    for (let k = 0; k <= 4; k++) { const y = b.y0 + (b.y1 - b.y0) * k / 4; ctx.beginPath(); ctx.moveTo(b.x0, y); ctx.lineTo(b.x1, y); ctx.stroke(); }
    ctx.fillStyle = 'rgba(136,147,165,.55)'; ctx.font = `500 11px ${MONO}`; ctx.textAlign = 'center';
    for (let s = 0; s < T_END; s += 5) ctx.fillText(`+${s}s`, X(b, s), b.y1 + 30);
    const xd = X(b, T_DECIDE);
    ctx.strokeStyle = 'rgba(244,171,60,.55)'; ctx.setLineDash([6, 6]); ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.moveTo(xd, b.y0 - 34); ctx.lineTo(xd, b.y1 + 14); ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle = 'rgba(244,171,60,.9)'; ctx.font = `800 13px ${COND}`; ctx.textAlign = 'left';
    ctx.fillText('AGENT DECIDES · +10s', xd + 8, b.y0 - 24);
    ctx.restore();
  }
  function line(b, c, tEnd, alpha) {
    const pts = c.pts.filter(p => p[0] <= tEnd);
    if (!pts.length) return;
    const path = new Path2D(); let ly = 0;
    pts.forEach((p, i) => { const x = X(b, p[0]), y = Y(b, c, p[1]); if (i) { path.lineTo(x, ly); path.lineTo(x, y); } else path.moveTo(x, y); ly = y; });
    const xe = X(b, tEnd); path.lineTo(xe, ly);
    ctx.save(); ctx.globalAlpha = alpha;
    const fill = new Path2D(path); fill.lineTo(xe, b.y1); fill.lineTo(X(b, pts[0][0]), b.y1); fill.closePath();
    const g = ctx.createLinearGradient(0, b.y0, 0, b.y1); g.addColorStop(0, 'rgba(244,246,249,.16)'); g.addColorStop(1, 'rgba(244,246,249,0)');
    ctx.fillStyle = g; ctx.fill(fill);
    ctx.shadowColor = 'rgba(244,246,249,.5)'; ctx.shadowBlur = 14;
    ctx.strokeStyle = '#f4f6f9'; ctx.lineWidth = 2.6; ctx.lineJoin = 'round'; ctx.stroke(path);
    ctx.shadowBlur = 0;
    for (const p of pts) if (p[2]) { const x = X(b, p[0]), y = Y(b, c, p[1]); ctx.fillStyle = '#ff4d3f'; ctx.beginPath(); ctx.arc(x, y, 5, 0, 7); ctx.fill(); }
    ctx.fillStyle = '#f4f6f9'; ctx.beginPath(); ctx.arc(xe, ly, 5.5, 0, 7); ctx.fill();
    ctx.strokeStyle = 'rgba(244,246,249,.25)'; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(xe, b.y0 - 10); ctx.lineTo(xe, b.y1); ctx.stroke();
    ctx.restore();
  }
  function after(b, c, k, alpha) {
    // what really happened after the agent's decision (+10s .. +20s), real trades
    const tEnd = T_DECIDE + (T_END - T_DECIDE) * k;
    const pts = c.pts.filter(p => p[0] > T_DECIDE && p[0] <= tEnd);
    let y = Y(b, c, valueAt(c, T_DECIDE));
    const path = new Path2D(); path.moveTo(X(b, T_DECIDE), y);
    for (const p of pts) { const x = X(b, p[0]); path.lineTo(x, y); y = Y(b, c, p[1]); path.lineTo(x, y); }
    path.lineTo(X(b, tEnd), y);
    const col = c.kind === 'allowed' ? '47,209,122' : '255,77,63';
    ctx.save(); ctx.globalAlpha = alpha;
    ctx.strokeStyle = `rgba(${col},.85)`; ctx.lineWidth = 2.2; ctx.setLineDash([2, 5]); ctx.lineCap = 'round'; ctx.stroke(path); ctx.setLineDash([]);
    ctx.fillStyle = `rgba(${col},.9)`; ctx.beginPath(); ctx.arc(X(b, tEnd), y, 4, 0, 7); ctx.fill();
    ctx.font = `800 12px ${COND}`; ctx.textAlign = 'right'; ctx.fillText('WHAT HAPPENED NEXT · REAL TRADES', b.x1, b.y1 + 30);
    ctx.restore();
  }
  function telestrator(b, c, k, alpha) {
    let tx, ty, label, col;
    if (c.kind === 'dev' && c.sell) { tx = X(b, c.sell[0]); ty = Y(b, c, c.sell[1]); label = `DEV SOLD +${c.sell[0].toFixed(1)}s`; col = '#ff4d3f'; }
    else {
      const p0 = c.pts[0], al = c.kind === 'allowed';
      tx = X(b, al ? T_DECIDE : p0[0]); ty = Y(b, c, al ? valueAt(c, T_DECIDE) : p0[1]);
      label = c.kind === 'factory' ? `DEV: ${fmt(c.prior)} PRIOR LAUNCHES` : c.kind === 'mayhem' ? 'MAYHEM-MODE CURVE' : `CLEAR · ${c.buyers || 0} BUYERS`;
      col = al ? '#2fd17a' : '#f4ab3c';
    }
    const r = W < 820 ? 26 : 38;
    ctx.save(); ctx.globalAlpha = alpha; ctx.strokeStyle = col; ctx.lineWidth = 3.4; ctx.lineCap = 'round';
    const end = -0.6 + k * Math.PI * 2.15;
    ctx.beginPath();
    for (let a = -0.6, i = 0; a <= end; a += 0.06, i++) {
      const wob = 1 + 0.06 * Math.sin(a * 3 + c.sym.length) + (a > Math.PI * 1.5 ? 0.08 : 0);
      const x = tx + Math.cos(a) * r * 1.25 * wob, y = ty + Math.sin(a) * r * wob;
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    }
    ctx.stroke();
    if (k > 0.85) {
      const right = tx < b.x0 + (b.x1 - b.x0) * 0.55, lx = right ? tx + r * 1.5 + 40 : tx - r * 1.5 - 40, ly = Math.max(b.y0 - 40, ty - r - 34);
      ctx.beginPath(); ctx.moveTo(tx + (right ? r * 1.2 : -r * 1.2), ty - r * 0.6); ctx.lineTo(lx, ly + 10); ctx.stroke();
      ctx.font = `800 ${W < 820 ? 16 : 22}px ${COND}`;
      const w = ctx.measureText(label).width + 22, h = W < 820 ? 28 : 36, bx = right ? lx : lx - w;
      ctx.fillStyle = col; ctx.fillRect(bx, ly - h / 2 - 4, w, h);
      ctx.fillStyle = col === '#ff4d3f' ? '#fff' : '#07090d'; ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
      ctx.fillText(label, bx + 11, ly - 3);
    }
    ctx.restore();
  }
  function decide(c) {
    const allowed = c.kind === 'allowed';
    rvCoin.textContent = '$' + c.sym;
    rvDec.textContent = allowed ? 'Buy allowed' : 'No buy';
    rvDec.className = 'rv-decision' + (allowed ? ' go' : '');
    rvWhy.textContent = allowed ? `0.02 SOL, inside every limit. ${c.reason || ''}`.trim() : c.reason;
  }
  const setReview = cls => { review.className = 'review ' + cls; };

  /* ---------------------------------------------------------- hero: check any coin */
  let manual = false, resume = 0;
  const VERDICT = { AVOID: ['No buy', ''], CAUTION: ['Review', ' warn'], CLEAR: ['Buy allowed', ' go'], UNKNOWN: ['No data', ' na'], OFF: ['Off air', ' na'] };
  function showCheck(sym, verdict, why) {
    manual = true; clearTimeout(resume);
    const [label, cls] = VERDICT[verdict] || VERDICT.UNKNOWN;
    rvState.textContent = 'YOUR COIN'; rvCoin.textContent = sym;
    rvDec.textContent = label; rvDec.className = 'rv-decision' + cls; rvWhy.textContent = why;
    setReview(''); void review.offsetWidth; setReview('s1 s2 s3');
    resume = setTimeout(() => { manual = false; if (coins.length && !reduce) next(performance.now()); }, 15000);
  }
  $('ck').addEventListener('submit', async e => {
    e.preventDefault();
    const m = $('ckIn').value.trim();
    if (!B58.test(m)) return showCheck('$—', 'UNKNOWN', "That isn't a Solana contract address. Paste the CA from pump.fun.");
    if (!API) return showCheck(m.slice(0, 4) + '…' + m.slice(-4), 'OFF', 'Live checks run while the agent is on air. The feed below replays real decisions.');
    showCheck(m.slice(0, 4) + '…' + m.slice(-4), 'UNKNOWN', 'Checking the dev record…'); rvDec.textContent = '…';
    try {
      const r = await j(API + '/api/check/' + m);
      showCheck('$' + (r.symbol || m.slice(0, 4) + '…'), r.verdict, (r.why || []).join(' '));
    } catch (err) {
      showCheck('$—', 'UNKNOWN', String(err.message) === '429' ? 'Too many checks from you. Try again in a minute.' : "Couldn't reach the agent. Try again in a moment.");
    }
  });

  function frame(now) {
    requestAnimationFrame(frame);
    if (!cur || !visible || manual) return;
    const b = box(), el = now - t0;
    ctx.clearRect(0, 0, W, H);
    if (phase === 'play') {
      const t = Math.min(T_DECIDE, (el / PLAY_MS) * T_DECIDE);
      grid(b, Math.min(1, el / 300)); line(b, cur, t, 1);
      clock.textContent = '+' + t.toFixed(1).padStart(4, '0') + 's';
      if (el > 250 && !review.classList.contains('s2')) { rvCoin.textContent = '$' + cur.sym; setReview('s1 s2'); }
      if (t >= T_DECIDE) { phase = 'tele'; t0 = now; rvState.textContent = 'REVIEW'; }
    } else if (phase === 'tele' || phase === 'hold') {
      grid(b, 1); line(b, cur, T_DECIDE, 1);
      const k = phase === 'tele' ? Math.min(1, el / TELE_MS) : 1;
      if (phase === 'hold') after(b, cur, Math.min(1, el / (HOLD_MS * 0.7)), 1);
      telestrator(b, cur, k, 1);
      if (phase === 'tele' && k >= 1) { phase = 'hold'; t0 = now; decide(cur); rvState.textContent = 'DECISION'; setReview('s1 s2 s3'); }
      if (phase === 'hold' && el > HOLD_MS) { phase = 'out'; t0 = now; setReview('s1 s2 s3 out'); }
    } else if (phase === 'out') {
      const a = 1 - Math.min(1, el / OUT_MS);
      grid(b, a); line(b, cur, T_DECIDE, a); after(b, cur, 1, a); telestrator(b, cur, 1, a);
      if (el > OUT_MS) next(now);
    }
  }
  function next(now) {
    cur = coins[ci++ % coins.length];
    phase = 'play'; t0 = now; rvState.textContent = 'CHECKING';
    setReview(''); void review.offsetWidth; setReview('s1');
  }
  function staticCoin() {
    cur = coins[ci++ % coins.length]; const b = box();
    ctx.clearRect(0, 0, W, H); grid(b, 1); line(b, cur, T_DECIDE, 1); after(b, cur, 1, 1); telestrator(b, cur, 1, 1);
    decide(cur); rvState.textContent = 'DECISION'; setReview('s1 s2 s3'); clock.textContent = '+10.0s';
  }
  new IntersectionObserver(es => { visible = es[0].isIntersecting; }).observe($('top'));

  async function startBroadcast() {
    const raw = await j('data/replays.json');
    coins = interleave(raw.map(prep).filter(Boolean));
    if (!coins.length) return;
    if (document.fonts) await document.fonts.ready;
    if (reduce) { staticCoin(); setInterval(staticCoin, 7000); return; }
    next(performance.now()); requestAnimationFrame(frame);
  }

  /* ---------------------------------------------------------- challenge the ref */
  let trusted = { 'launch intel': $('trustedAddr').textContent.trim() };
  function setTrusted(t) {
    trusted = t; const a = Object.values(t)[0] || '';
    $('trustedAddr').textContent = a;
    $('tPay').textContent = a.slice(0, 4) + '…' + a.slice(-3);
    $('tryAddr').value = a.slice(0, 3) + 'kq7Vx2LmPzR9nWb4TcYs8JhD1fGu6AeNo'.repeat(2).slice(0, Math.max(0, a.length - 6)) + a.slice(-3);
  }
  setTrusted(trusted);
  $('tryBtn').addEventListener('click', () => {
    const a = $('tryAddr').value.trim(), out = $('tryOut');
    let ok = false, msg;
    const look = Object.entries(trusted).find(([, t]) => a !== t && a.length > 30 && a.slice(0, 3) === t.slice(0, 3) && a.slice(-3) === t.slice(-3));
    if (!B58.test(a)) msg = "That isn't a Solana address. Nothing to pay.";
    else if (Object.values(trusted).includes(a)) { ok = true; msg = "That's the real seller's address. The agent pays it, up to $0.01 a call and $1 a day."; }
    else if (look) {
      const t = look[1];
      const mid = a.slice(3, -3).split('').map((ch, i) => ch === t[i + 3] ? esc(ch) : `<mark>${esc(ch)}</mark>`).join('');
      msg = `Same start and end as ${esc(look[0])}'s address, different middle. That's address poisoning.<br>${esc(a.slice(0, 3))}${mid}${esc(a.slice(-3))}`;
    } else msg = 'Not on the trusted list. The agent only pays addresses it was given.';
    out.innerHTML = `<span class="slab ${ok ? 'go' : ''}">${ok ? 'Paid' : 'Blocked'}</span><br>${msg}`;
  });

  /* ---------------------------------------------------------- feed: live API or replay snapshot */
  async function live() {
    const seen = new Set();
    const st = await j(API + '/api/state');
    $('liveDot').classList.add('on'); $('sbMode').textContent = 'LIVE';
    $('feedMode').textContent = 'LIVE'; $('feedMode').classList.add('live');
    $('logSrc').textContent = 'live paper agent · real pump.fun launches as they happen';
    if (st.config) {
      const p = st.config.trusted_payees; if (p && Object.keys(p).length) setTrusted(p);
      document.querySelectorAll('[data-cfg]').forEach(i => { const v = st.config[i.dataset.cfg]; if (v != null) i.textContent = v; });
    }
    async function poll(first) {
      try {
        const [s, f] = await Promise.all([j(API + '/api/state'), j(API + '/api/feed?n=60')]);
        if (s.stats && s.stats.wanted != null) { $('sbWanted').textContent = fmt(s.stats.wanted); $('sbAllowed').textContent = fmt((s.stats.trades || 0) + (s.open || []).length); }
        const fresh = f.filter(e => !seen.has(e.t + e.kind + e.mint)).reverse();
        fresh.forEach(e => { seen.add(e.t + e.kind + e.mint); pushFeed(e); });
        if (first || fresh.length) setCrawl(f);
      } catch { $('liveDot').classList.remove('on'); }
    }
    await poll(true); setInterval(poll, 5000);
    try {
      const pays = await j(API + '/api/payments?n=6');
      if (pays.length) $('receipts').innerHTML = pays.map(p => `<div><span style="color:var(--green)">PAID</span> $${p.usd} USDC · <a href="${esc(p.explorer)}" target="_blank" rel="noopener">${esc(p.tx.slice(0, 8))}…${esc(p.tx.slice(-6))}</a></div>`).join('');
      const pr = await j(API + '/x402/price');
      $('tNet').textContent = pr.network.startsWith('solana:5eyk') ? 'SOLANA MAINNET' : 'SOLANA DEVNET';
      $('tPrice').textContent = '$' + Object.values(pr.endpoints)[0] + ' USDC';
    } catch {}
  }
  async function replay() {
    const d = await j('data/demo_feed.json');
    $('logSrc').textContent = 'replay of real launches, Nov 7 · budget-only blocks hidden';
    const ev = d.events.filter(e => (e.kind !== 'pay' || e.allowed) && !(e.kind === 'buy' && !e.allowed && (e.reasons || []).every(r => RANK(r) === 2)));
    ev.slice(0, 30).reverse().forEach(pushFeed);
    setCrawl(ev.slice(0, 40));
    let i = 30;
    const step = () => { const e = ev[i++ % ev.length]; pushFeed({ ...e, t: Date.now() / 1000 }); setTimeout(step, 1800 + Math.random() * 2200); };
    if (!reduce) setTimeout(step, 1500);
  }

  startBroadcast().catch(() => {});
  (API ? live().catch(replay) : replay()).catch(() => { $('logSrc').textContent = 'feed unavailable'; });

  /* ---------------------------------------------------------- reveals */
  const io = new IntersectionObserver(es => es.forEach(e => { if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target); } }), { threshold: .15 });
  document.querySelectorAll('main h2, main .lead, .laws li, .feedPanel, .matchReport, .liveFeed, .monitor, .versus, .reversal').forEach(el => { el.classList.add('reveal'); io.observe(el); });
  io.observe($('h2h'));
})();
