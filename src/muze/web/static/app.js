/* muze webui */
"use strict";

const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);

/* ---- storage that cannot kill the app ----
 * localStorage THROWS in some browsers (block-all-cookies, some private
 * modes, extension interference). The old code read it at line ~7, so one
 * SecurityError killed every event handler on the page: nothing clickable,
 * no requests, nothing stored. Probe first and fall back to memory. */
let storageOK = true;
const memStore = {};
try {
  localStorage.setItem("muze_probe", "1");
  localStorage.removeItem("muze_probe");
} catch (e) {
  storageOK = false;
}
function store(k, v) {
  memStore[k] = v;
  if (storageOK) { try { localStorage.setItem(k, v); } catch (e) { storageOK = false; } }
}
function recall(k) {
  if (k in memStore) return memStore[k];
  if (!storageOK) return null;
  try { return localStorage.getItem(k); } catch (e) { storageOK = false; return null; }
}

/* ---- surface JS errors on screen instead of dying silently ----
 * "clicked, nothing happened, no request" is undebuggable for the user;
 * a red bar with the error message is not. */
function fatal(msg) {
  let bar = document.getElementById("jserr");
  if (!bar) {
    bar = document.createElement("div");
    bar.id = "jserr";
    bar.style.cssText =
      "position:fixed;top:0;left:0;right:0;z-index:9999;background:#b00;" +
      "color:#fff;padding:6px 10px;font:12px/1.5 monospace;white-space:pre-wrap";
    (document.body || document.documentElement).appendChild(bar);
  }
  bar.textContent = "JS 错误（页面可能部分失效）: " + msg;
}
window.addEventListener("error", (e) => {
  const at = e.filename ? " @" + String(e.filename).split("/").pop() + ":" + e.lineno : "";
  fatal((e.message || "unknown error") + at);
});
window.addEventListener("unhandledrejection", (e) => {
  const r = e.reason;
  fatal("未处理的异常: " + ((r && (r.stack || r.message)) || String(r)));
});

let TOKEN = recall("muze_token") || "";
$("#token").value = TOKEN;
if (!storageOK) {
  fatal("localStorage 不可用（浏览器阻止了站点数据？）。本次可用，但 token 不会被记住。");
}
let authorized = !!TOKEN;
let ACCT = ""; // selected account name; "" -> server default (first)

/* inject the account selector param into every api call */
function withAccount(path) {
  if (!ACCT) return path;
  return path + (path.includes("?") ? "&" : "?") + "account=" + encodeURIComponent(ACCT);
}

async function api(path, opts = {}) {
  const o = Object.assign({}, opts);
  // headers must be latin1; a token pasted with CJK chars (IME on, or a
  // copied line like "...并点保存") would make fetch() throw synchronously
  const bearer = String(TOKEN).replace(/[^\x20-\xFF]/g, "");
  o.headers = Object.assign({ Authorization: "Bearer " + bearer }, opts.headers || {});
  if (o.body && typeof o.body !== "string") {
    o.body = JSON.stringify(o.body);
    o.headers["Content-Type"] = "application/json";
  }
  const r = await fetch(withAccount(path), o);
  if (r.status === 401) {
    $("#statusdot").className = "dot off";
    authorized = false;
    const e = new Error("unauthorized");
    e.unauthorized = true;
    throw e;
  }
  $("#statusdot").className = "dot on";
  authorized = true;
  return r.json();
}

function authHint() {
  return authorized
    ? ""
    : '<div class="dim pad">未授权（401）：token 不对。常见原因：复制了 muze.log 里<b>旧一次启动</b>的 WEBUI 行（每次重启随机 token 都会变）、或把结尾的右括号“)”一起复制了。建议在 config.yaml 设固定 web.token 后重启，再填到这里并点“保存”。</div>';
}

function esc(s) {
  return (s ?? "").toString()
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function fmtTime(t) {
  if (!t) return "";
  const d = new Date(t * 1000);
  return d.toLocaleString("zh-CN", { hour12: false });
}

/* ---------------- tiny markdown ----------------
 * Renders model output in the AI tab and session details. Safe: everything
 * is escaped first, then a small subset is transformed (headings, lists,
 * blockquote, fenced/inline code, bold/italic/strike, http(s) links). */
function mdInline(s) {
  return s
    .replace(/`([^`\n]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>")
    .replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, "$1<i>$2</i>")
    .replace(/~~([^~\n]+)~~/g, "<s>$1</s>")
    .replace(/\[([^\]\n]+)\]\((https?:\/\/[^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener">$1</a>');
}
function mdRender(text) {
  const src = esc((text ?? "").toString()).replace(/\r\n/g, "\n");
  const codes = [];
  let body = src.replace(/```([\w-]*)\n?([\s\S]*?)```/g, (_, lang, code) => {
    codes.push(`<pre><code>${code.replace(/\n$/, "")}</code></pre>`);
    return "\x00CODE" + (codes.length - 1) + "\x00";
  });
  const out = [];
  let list = null; // "ul" | "ol"
  const flushList = () => {
    if (list) { out.push(`</${list}>`); list = null; }
  };
  for (const block of body.split(/\n{2,}/)) {
    const lines = block.split("\n");
    const first = lines[0] || "";
    if (/^\x00CODE\d+\x00$/.test(first) && lines.length === 1) {
      flushList(); out.push(first); continue;
    }
    const h = first.match(/^(#{1,4})\s+(.*)$/);
    if (h) { flushList(); out.push(`<h${h[1].length + 1}>${mdInline(h[2])}</h${h[1].length + 1}>`); continue; }
    if (/^(-{3,}|\*{3,})$/.test(first)) { flushList(); out.push("<hr>"); continue; }
    if (lines.every((l) => l.startsWith(">"))) {
      flushList();
      out.push("<blockquote>" + mdInline(lines.map((l) => l.replace(/^>\s?/, "")).join("\n"))
        .replace(/\n/g, "<br>") + "</blockquote>");
      continue;
    }
    const li = lines.map((l) => l.match(/^\s*[-*]\s+(.*)$/));
    if (li.length && li.every(Boolean)) {
      if (list !== "ul") { flushList(); out.push("<ul>"); list = "ul"; }
      li.forEach((m) => out.push(`<li>${mdInline(m[1])}</li>`));
      continue;
    }
    const oi = lines.map((l) => l.match(/^\s*\d+[.)]\s+(.*)$/));
    if (oi.length && oi.every(Boolean)) {
      if (list !== "ol") { flushList(); out.push("<ol>"); list = "ol"; }
      oi.forEach((m) => out.push(`<li>${mdInline(m[1])}</li>`));
      continue;
    }
    flushList();
    out.push("<p>" + mdInline(lines.join("\n")).replace(/\n/g, "<br>") + "</p>");
  }
  flushList();
  return out.join("\n").replace(/\x00CODE(\d+)\x00/g, (_, i) => codes[+i]);
}

/* ---------------- tabs ---------------- */
$$("nav button").forEach((b) => b.addEventListener("click", () => {
  $$("nav button").forEach((x) => x.classList.remove("active"));
  b.classList.add("active");
  $$(".tab").forEach((x) => x.classList.remove("active"));
  $("#tab-" + b.dataset.tab).classList.add("active");
  if (b.dataset.tab === "pending") loadPending();
  if (b.dataset.tab === "logs") loadLogs();
  if (b.dataset.tab === "status") loadStatus();
  if (b.dataset.tab === "chats") loadConversations();
  if (b.dataset.tab === "sess") loadSess();
  if (b.dataset.tab === "settings") loadSettings();
  if (b.dataset.tab === "memories") loadMemories();
  if (b.dataset.tab === "rag") loadRag();
}));

/* tolerate sloppy pastes: "(token: xxx)" / trailing ')' / quotes / whitespace */
function cleanToken(raw) {
  let t = (raw || "").trim();
  t = t.replace(/^[-(]?\s*token:?\s*/i, "");
  return t.replace(/[)\]}"'“”‘’,，、;；\s]+$/g, "");
}

$("#saveToken").addEventListener("click", () => {
  TOKEN = cleanToken($("#token").value);
  if (/[^\x20-\x7E]/.test(TOKEN)) {
    // CJK in a token is always a paste mistake -- say so, loudly
    fatal("token 里混入了中文/全角字符（可能把提示文字一起复制了，或输入法在中文状态）。已自动剔除，若仍 401 请重新纯文本粘贴。");
    TOKEN = TOKEN.replace(/[^\x20-\x7E]/g, "");
  }
  $("#token").value = TOKEN;
  store("muze_token", TOKEN);
  refreshAll();
});
/* Enter in the token box also saves */
$("#token").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#saveToken").click(); });

/* refresh everything after (re)authorization */
function refreshAll() {
  loadAccounts();
  loadInfo();
  loadConversations();
  pendingBadgeReload();
}

/* ---------------- accounts + info ---------------- */
let acctNames = null;
$("#acctSel").addEventListener("change", () => {
  ACCT = $("#acctSel").value;
  curConv = null;
  $("#msglist").innerHTML = "";
  $("#msghead").textContent = "选择左侧会话";
  refreshAll();
});

async function loadAccounts() {
  try {
    const d = await api("/api/accounts");
    acctNames = d.accounts.map((a) => a.name);
    const sel = $("#acctSel");
    sel.innerHTML = d.accounts
      .map((a) => `<option value="${esc(a.name)}">${esc(a.name)} (${a.self_id})</option>`)
      .join("");
    sel.classList.toggle("hidden", d.accounts.length < 2);
    if (!ACCT) ACCT = d.default || (acctNames[0] || "");
    sel.value = ACCT;
  } catch { /* dot handled */ }
}

async function loadInfo() {
  try {
    const info = await api("/api/info");
    $("#version").textContent = info.version;
    const pending = (info.accounts || []).reduce(
      (n, a) => n + ((a.stats && a.stats.pending_actions) || 0), 0);
    pendingBadge(pending);
  } catch { /* dot handled */ }
}
function pendingBadge(n) {
  const b = $("#pendingBadge");
  b.textContent = n;
  b.classList.toggle("hidden", !n);
}

/* ---------------- conversations ----------------
 * convMeta cached per "scene/peer" for media links
 */
const convMeta = {};

async function loadConversations() {
  const box = $("#convlist");
  box.innerHTML = '<div class="dim pad">加载中…</div>';
  try {
    const d = await api("/api/conversations?limit=200");
    box.innerHTML = "";
    if (!d.conversations.length) {
      box.innerHTML = '<div class="dim pad">暂无会话（等待消息入库或运行 /sync）</div>';
      return;
    }
    for (const c of d.conversations) {
      convMeta[c.scene + "/" + c.peer_id] = c;
      const div = document.createElement("div");
      div.className = "conv";
      div.dataset.key = c.scene + "/" + c.peer_id;
      div.innerHTML =
        `<div class="t"><span class="name">${esc(c.name || c.scene + "/" + c.peer_id)}</span>` +
        `<span class="cnt">${c.messages || c.message_count || 0}条</span></div>` +
        `<div class="prev dim">${esc(c.scene)} · ${esc(fmtTime(c.last_active_at))}</div>`;
      div.addEventListener("click", () => openConversation(c.scene, c.peer_id, div));
      box.appendChild(div);
    }
  } catch (e) {
    box.innerHTML = e.unauthorized
      ? authHint()
      : '<div class="dim pad">加载失败：' + esc(e.message) + "</div>";
  }
}

let curConv = null;
async function openConversation(scene, peer, div) {
  $$(".conv").forEach((x) => x.classList.remove("sel"));
  if (div) div.classList.add("sel");
  curConv = { scene, peer };
  const label = scene + "/" + peer;
  const name = convMeta[label]?.name || label;
  $("#msghead").innerHTML = `<b>${esc(name)}</b> <span class="dim">${esc(label)}</span>`;
  $("#msglist").innerHTML = "";
  await loadMessages(null);
}

async function loadMessages(beforeSeq) {
  if (!curConv) return;
  const { scene, peer } = curConv;
  let url = `/api/messages/${scene}/${peer}?limit=50`;
  if (beforeSeq) url += `&before_seq=${beforeSeq}`;
  const d = await api(url);
  const list = $("#msglist");
  const atTop = list.scrollTop;
  const frag = document.createDocumentFragment();
  for (const m of d.messages) frag.appendChild(renderMessage(m));
  if (beforeSeq) list.prepend(frag);
  else { list.innerHTML = ""; list.appendChild(frag); }
  $("#older").onclick = () => {
    const first = list.querySelector(".m");
    if (first) loadMessages(parseInt(first.dataset.seq));
  };
  if (beforeSeq) list.scrollTop = atTop;
  else list.scrollTop = list.scrollHeight;
}

function renderMessage(m) {
  const div = document.createElement("div");
  div.className = "m" + (m.recalled ? " recalled" : "");
  div.dataset.seq = m.message_seq;
  let raw;
  try { raw = JSON.parse(m.raw_json); } catch { raw = { segments: [] }; }
  const segs = raw.segments || [];
  let body = esc(m.text_content || "(无文本)");
  const thumbs = [];
  const files = [];
  for (const s of segs) {
    if (s.type === "file") files.push(s.data || {});
  }
  div.innerHTML =
    `<div class="meta"><span>#${m.message_seq}</span><span>${esc(m.sender_id)}</span>` +
    `<span>${esc(fmtTime(m.time))}</span>${m.recalled ? "<span>[已撤回]</span>" : ""}` +
    `${m.source === "backfill" ? '<span title="漫游补拉">⤺</span>' : ""}</div>` +
    `<div class="body">${body}</div><div class="thumbs"></div>`;
  mediaFor(m).then((medias) => {
    const t = div.querySelector(".thumbs");
    for (const md of medias) {
      if (md.status !== "done" || !md.file_path) continue;
      if (md.kind === "image" || md.kind === "face") {
        const img = document.createElement("img");
        img.src = withAccount(`/api/mediafile/${md.id}?token=${encodeURIComponent(TOKEN)}`);
        img.loading = "lazy";
        img.addEventListener("click", () => window.open(img.src));
        t.appendChild(img);
      } else {
        const a = document.createElement("a");
        a.className = "fl";
        a.href = withAccount(`/api/mediafile/${md.id}?token=${encodeURIComponent(TOKEN)}`);
        a.textContent = `[${md.kind}] ${md.file_name || md.file_path}`;
        a.target = "_blank";
        t.appendChild(a);
      }
    }
  });
  return div;
}

async function mediaFor(m) {
  try {
    const d = await api(`/api/message/${m.scene}/${m.peer_id}/${m.message_seq}`);
    return d.media || [];
  } catch { return []; }
}

/* ---------------- search ---------------- */
$("#searchBtn").addEventListener("click", runSearch);
$("#searchq").addEventListener("keydown", (e) => { if (e.key === "Enter") runSearch(); });
async function runSearch() {
  const q = $("#searchq").value.trim();
  if (!q) return;
  const box = $("#searchresults");
  box.innerHTML = '<div class="dim pad">搜索中…</div>';
  const d = await api("/api/search?q=" + encodeURIComponent(q) + "&limit=50");
  box.innerHTML = d.matches && d.matches.length
    ? d.matches.map((r) =>
        `<div class="card"><div class="head"><b>${esc(r.scene)}/${r.peer_id}</b>` +
        `<span class="dim">#${r.message_seq} · ${esc(fmtTime(r.time))} · 发送者 ${r.sender_id}</span></div>` +
        `<div>${esc(r.text_content)}</div></div>`).join("")
    : '<div class="dim pad">无结果</div>';
}

/* ---------------- ai ---------------- */
const SESS_ICON = { running: "⏳", done: "✔", error: "✖", suspend: "⏸" };
const SESS_CN = { running: "运行中", done: "完成", error: "出错", suspend: "已暂停" };

function aiBubble(who, text, err) {
  const div = document.createElement("div");
  div.className = "msgrow " + who + (err ? " err" : "");
  const body = who === "user" ? esc(text) : mdRender(text);
  div.innerHTML = `<div class="who">${who === "user" ? "你" : "muze"}</div>` +
    `<div class="bubble md">${body}</div>`;
  $("#aichat").appendChild(div);
  $("#aichat").scrollTop = 1e9;
  return div;
}
async function aiSend() {
  const input = $("#aiinput");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  aiBubble("user", text);
  const holder = aiBubble("bot", "…");
  try {
    const d = await api("/api/agent", { method: "POST", body: { text } });
    holder.querySelector(".bubble").innerHTML = mdRender(d.text || "(空)");
    pendingBadgeReload();
  } catch (e) {
    holder.querySelector(".bubble").textContent = "调用失败: " + e.message;
  }
}
$("#aiSend").addEventListener("click", aiSend);
$("#aiinput").addEventListener("keydown", (e) => { if (e.key === "Enter") aiSend(); });
$("#aiReset").addEventListener("click", async () => {
  await api("/api/agent/reset", { method: "POST", body: {} });
  $("#aichat").innerHTML = "";
});

/* ---------------- memories ---------------- */
async function loadMemories() {
  const box = $("#memlist");
  const q = $("#memq").value.trim();
  box.innerHTML = '<div class="dim pad">加载中…</div>';
  try {
    const d = await api(q ? "/api/memories?q=" + encodeURIComponent(q) : "/api/memories");
    if (!d.memories || !d.memories.length) {
      box.innerHTML = '<div class="dim pad">暂无记忆（做梦提炼或 :: 让 muze remember 后出现）</div>';
      return;
    }
    box.innerHTML = "";
    for (const m of d.memories) {
      const card = document.createElement("div");
      card.className = "card";
      card.innerHTML =
        `<div class="head"><b>[${esc(m.kind || "fact")}] ${esc(m.subject || "")}</b>` +
        `<span class="dim">#${m.id}${m.active ? "" : " · 已停用"}</span></div>` +
        `<div>${esc(m.content || "")}</div>` +
        (m.active ? '<div class="ops"><button class="no">停用</button></div>' : "");
      const btn = card.querySelector("button");
      if (btn) btn.addEventListener("click", async () => {
        try {
          await api("/api/command", { method: "POST", body: { text: "forget " + m.id } });
          btn.disabled = true;
          btn.textContent = "已停用";
        } catch (e) { btn.textContent = "失败: " + e.message; }
      });
      box.appendChild(card);
    }
  } catch (e) {
    box.innerHTML = e.unauthorized ? authHint()
      : '<div class="dim pad">加载失败：' + esc(e.message) + "</div>";
  }
}
$("#memBtn").addEventListener("click", loadMemories);
$("#memq").addEventListener("keydown", (e) => { if (e.key === "Enter") loadMemories(); });

/* ---------------- rag ---------------- */
async function loadRag() {
  const bar = $("#ragstate");
  try {
    const s = await api("/api/rag");
    if (!s.enabled) {
      bar.innerHTML = '<span class="dim">RAG 未启用（config.yaml 开 rag.enabled 且 embedding 可用后自动生效；检索工具同时停用）</span>';
      return;
    }
    const kinds = Object.entries(s.docs_by_kind || {})
      .map(([k, v]) => `${k}:${v}`).join(" · ") || "空";
    bar.innerHTML =
      `<b>RAG 开启</b> · 文档 <b>${s.docs}</b> 篇（${esc(kinds)}）` +
      ` · 模型 ${esc(s.vec_model || "-")}` +
      `${s.chat_enabled ? " · 含聊天窗口" : " · 仅摘要"}` +
      ` · 上次索引 ${s.last_index_at ? esc(fmtTime(s.last_index_at)) : "-"}` +
      (s.embedding_healthy ? "" : ' · <b style="color:#b00">embedding 异常</b>');
  } catch (e) {
    bar.innerHTML = e.unauthorized ? authHint()
      : '<span class="dim">状态加载失败：' + esc(e.message) + "</span>";
  }
}
async function runRagSearch() {
  const q = $("#ragq").value.trim();
  const box = $("#ragresults");
  if (!q) return;
  box.innerHTML = '<div class="dim pad">检索中…</div>';
  try {
    let url = "/api/rag/search?q=" + encodeURIComponent(q) + "&k=8";
    const kind = $("#ragKind").value;
    if (kind) url += "&kind=" + encodeURIComponent(kind);
    const d = await api(url);
    if (d.enabled === false) {
      box.innerHTML = '<div class="dim pad">RAG 未启用</div>';
      return;
    }
    box.innerHTML = (d.results && d.results.length) ? "" : '<div class="dim pad">无结果（索引可能还在后台闲时构建中）</div>';
    for (const r of d.results || []) {
      const card = document.createElement("div");
      card.className = "card";
      const when = r.time_start ? esc(fmtTime(r.time_start)) : "";
      card.innerHTML =
        `<div class="head"><b>[${esc(r.kind)}] ${esc(r.ref || "")}</b>` +
        `<span class="dim">score ${r.score}${when ? " · " + when : ""}</span></div>` +
        `<div class="dim pad" style="white-space:pre-wrap">${esc((r.text || "").slice(0, 500))}</div>`;
      box.appendChild(card);
    }
  } catch (e) {
    box.innerHTML = e.unauthorized ? authHint()
      : '<div class="dim pad">检索失败：' + esc(e.message) + "</div>";
  }
}
$("#ragBtn").addEventListener("click", runRagSearch);
$("#ragq").addEventListener("keydown", (e) => { if (e.key === "Enter") runRagSearch(); });

/* ---------------- sess (agent sessions) ---------------- */
async function loadSess() {
  const box = $("#sesslist");
  box.innerHTML = '<div class="dim pad">加载中…</div>';
  try {
    const d = await api("/api/sess");
    if (!d.sessions || !d.sessions.length) {
      box.innerHTML = '<div class="dim pad">暂无 AI 会话（在 AI 标签页或 QQ 里 :: 提问后出现）</div>';
      return;
    }
    box.innerHTML = "";
    for (const s of d.sessions) {
      const div = document.createElement("div");
      div.className = "conv";
      div.innerHTML =
        `<div class="t"><span class="name">${SESS_ICON[s.status] || "?"} #${esc(s.id)} ${esc(s.title || "")}</span>` +
        `<span class="cnt">${esc(SESS_CN[s.status] || s.status)}</span></div>` +
        `<div class="prev dim">${esc(s.chat_key)} · ${esc(fmtTime(s.updated))}</div>`;
      div.addEventListener("click", () => loadSessDetail(s.id, div));
      box.appendChild(div);
    }
  } catch (e) {
    box.innerHTML = e.unauthorized ? authHint()
      : '<div class="dim pad">加载失败：' + esc(e.message) + "</div>";
  }
}

async function loadSessDetail(sid, div) {
  $$("#sesslist .conv").forEach((x) => x.classList.remove("sel"));
  if (div) div.classList.add("sel");
  const pane = $("#sessdetail");
  pane.innerHTML = '<div class="dim pad">加载中…</div>';
  try {
    const s = await api("/api/sess/" + encodeURIComponent(sid));
    let html =
      `<div class="panehead"><b>${SESS_ICON[s.status] || "?"} #${esc(s.id)} ${esc(s.title || "")}</b>` +
      ` <span class="dim">${esc(s.chat_key)} · ${esc(SESS_CN[s.status] || s.status)}` +
      ` · ${esc(fmtTime(s.created))} · ${s.step}轮 · ${s.total_chars}字` +
      (s.status === "running" ? ` · 空闲${s.idle}s` : "") + "</span></div>";
    /* prompt */
    html += `<div class="card"><div class="head"><b>问</b></div>` +
      `<div class="md">${mdRender(s.prompt)}</div></div>`;
    /* tool-loop messages, replies rendered as markdown */
    const msgs = s.messages || [];
    if (msgs.length) {
      html += '<div class="sessmsgs">';
      for (const m of msgs) {
        if (m.role === "user") {
          html += `<div class="msgrow user"><div class="who">你</div>` +
            `<div class="bubble">${esc(m.content || "")}</div></div>`;
        } else if (m.role === "assistant") {
          const calls = (m.tool_calls || []).map((c) =>
            `<details class="tool"><summary>🔧 ${esc(c.function?.name || "?")}(` +
            `<code>${esc((c.function?.arguments || "").slice(0, 300))}</code>)</summary></details>`).join("");
          html += `<div class="msgrow bot"><div class="who">muze</div>` +
            `<div class="bubble md">${m.content ? mdRender(m.content) : ""}${calls}</div></div>`;
        } else if (m.role === "tool") {
          const txt = (m.content || "").slice(0, 1500);
          html += `<details class="tool"><summary>📤 工具结果 ${esc(m.name || m.tool_call_id || "")}</summary>` +
            `<pre>${esc(txt)}</pre></details>`;
        }
      }
      html += "</div>";
    }
    /* event timeline (collapsed) */
    const ev = (s.events || []).filter((e) => e.kind !== "prompt");
    if (ev.length) {
      html += `<details class="tool tl"><summary>⏱ 过程时间线（${ev.length} 项）</summary>` +
        ev.map((e) =>
          `<div><span class="dim">${e.t}s</span> ${esc(e.kind)}: ${esc(e.detail || "")}</div>`).join("") +
        "</details>";
    }
    pane.innerHTML = html;
  } catch (e) {
    pane.innerHTML = e.unauthorized ? authHint()
      : '<div class="dim pad">加载失败：' + esc(e.message) + "</div>";
  }
}

/* ---------------- pending ---------------- */
async function loadPending() {
  const box = $("#pendinglist");
  try {
    const d = await api("/api/pending");
    pendingBadge(d.pending.length);
    box.innerHTML = d.pending.length ? "" : '<div class="dim pad">没有待确认的操作</div>';
    for (const p of d.pending) {
      const card = document.createElement("div");
      card.className = "card";
      const texts = (p.segments || [])
        .filter((s) => s.type === "text").map((s) => s.data?.text || "").join(" ");
      card.innerHTML =
        `<div class="head"><b>#${p.id} → ${esc(p.scene)}/${p.peer_id}</b>` +
        `<span class="dim">来源:${esc(p.created_by)} · 控制会话:${esc(p.control)}</span></div>` +
        `<pre>${esc(texts || JSON.stringify(p.segments, null, 2))}</pre>` +
        `<div class="ops"><button class="ok">确认发送</button><button class="no">取消</button></div>`;
      const [okBtn, noBtn] = card.querySelectorAll("button");
      okBtn.addEventListener("click", async () => {
        const r = await api(`/api/pending/${p.id}/confirm`, { method: "POST", body: {} });
        card.querySelector("pre").textContent += "\n→ " + r.text;
        okBtn.disabled = noBtn.disabled = true;
        pendingBadgeReload();
      });
      noBtn.addEventListener("click", async () => {
        const r = await api(`/api/pending/${p.id}/cancel`, { method: "POST", body: {} });
        card.querySelector("pre").textContent += "\n→ " + r.text;
        okBtn.disabled = noBtn.disabled = true;
        pendingBadgeReload();
      });
      box.appendChild(card);
    }
  } catch (e) {
    box.innerHTML = e.unauthorized
      ? authHint()
      : '<div class="dim pad">加载失败：' + esc(e.message) + "</div>";
  }
}
async function pendingBadgeReload() {
  try { const d = await api("/api/pending"); pendingBadge(d.pending.length); } catch {}
}

/* ---------------- settings ---------------- */
const CFG_FIELDS = [
  { group: "协议端 (milky)", items: [
    { path: "milky.base_url", label: "base_url（保存后需重启生效）", type: "text" },
    { path: "milky.access_token", label: "access_token（留空保持）", type: "password", secret: true },
  ]},
  { group: "授权 (authority)", items: [
    { path: "authority", label: "控制者QQ（逗号分隔）", type: "text" },
  ]},
  { group: "控制 (control)", items: [
    { path: "control.control_group", label: "控制群号（0=禁用）", type: "number" },
  ]},
  { group: "LLM · chat", items: [
    { path: "llm.chat.base_url", label: "base_url", type: "text" },
    { path: "llm.chat.model", label: "model", type: "text" },
    { path: "llm.chat.api_key", label: "api_key（留空保持）", type: "password", secret: true },
  ]},
  { group: "LLM · vision", items: [
    { path: "llm.vision.model", label: "model（空=禁用视觉）", type: "text" },
    { path: "llm.vision.api_key", label: "api_key（留空保持）", type: "password", secret: true },
  ]},
  { group: "做梦 (digest)", items: [
    { path: "digest.enabled", label: "启用", type: "checkbox" },
    { path: "digest.at", label: "每日时间 HH:MM", type: "text" },
    { path: "digest.inject_context", label: "注入处理上下文", type: "checkbox" },
  ]},
];

function cfgGet(obj, path) {
  return path.split(".").reduce((o, k) => (o == null ? o : o[k]), obj);
}

function cfgLookup(d, path) {
  if (path.startsWith("milky.")) return d.endpoint ? d.endpoint[path.slice(6)] : undefined;
  return cfgGet(d.masked, path);
}

async function loadSettings() {
  const box = $("#settingsForm");
  try {
    const d = await api("/api/config");
    box.innerHTML = "";
    for (const g of CFG_FIELDS) {
      const div = document.createElement("div");
      div.className = "group";
      div.innerHTML = `<h3>${esc(g.group)} · 账号 ${esc(d.instance || "")}</h3>`;
      for (const it of g.items) {
        const val = cfgLookup(d, it.path);
        const f = document.createElement("div");
        f.className = "field";
        const shown = it.type === "checkbox" ? (val ? "checked" : "")
          : (val === "***set***" ? "" : (val ?? ""));
        f.innerHTML = `<label>${esc(it.label)}</label>` +
          `<input data-path="${esc(it.path)}" type="${it.type}" value="${esc(String(shown))}" ${shown}>`;
        div.appendChild(f);
      }
      box.appendChild(div);
    }
  } catch (e) {
    box.innerHTML = '<div class="dim pad">加载失败：' + esc(e.message) + "</div>";
  }
}

$("#cfgSave").addEventListener("click", async () => {
  const updates = {};
  $$("#settingsForm input[data-path]").forEach((inp) => {
    const path = inp.dataset.path;
    let v = inp.type === "checkbox" ? inp.checked : inp.value.trim();
    if (inp.type === "password" && v === "") v = "";
    updates[path] = v;
  });
  try {
    const d = await api("/api/config", { method: "POST", body: { updates } });
    alert("已生效: " + (d.applied || []).join(", ") + "\n" + (d.hint || ""));
  } catch (e) {
    alert("保存失败: " + e.message);
  }
});

/* ---------------- logs / status ---------------- */
async function loadLogs() {
  const d = await api("/api/logs/tail?lines=400");
  $("#logview").textContent = (d.lines || []).join("\n");
  $("#logpath").textContent = "";
}
$("#logRefresh").addEventListener("click", loadLogs);

async function loadStatus() {
  const info = await api("/api/info");
  const parts = (info.accounts || []).map((a) => {
    const s = a.stats || {};
    return (
      `== 账号 ${a.name} (self ${a.self_id}) ==\n` +
      `agent: ${a.agent ? "on" : "off"}\n` +
      `conversations: ${s.conversations}   messages: ${s.messages}\n` +
      `memories: ${a.memories ?? "-"}   profiles: ${a.profiles ?? "-"}\n` +
      `rag: ${a.rag && a.rag.enabled
        ? `on docs=${a.rag.docs}(${Object.entries(a.rag.docs_by_kind || {}).map(([k, v]) => k + ":" + v).join(" ")}) model=${a.rag.vec_model || "-"}${a.rag.chat_enabled ? " +chat" : ""}`
        : "off"}\n` +
      `archive: ${(s.oldest_ts ? new Date(s.oldest_ts * 1000).toLocaleString("zh-CN", { hour12: false }) : "-")} ~ ${(s.newest_ts ? new Date(s.newest_ts * 1000).toLocaleString("zh-CN", { hour12: false }) : "-")}\n` +
      `by scene: ${JSON.stringify(s.messages_by_scene || {})}\n` +
      `media: ${JSON.stringify(s.media || {})}\n` +
      `backfill: ${JSON.stringify(a.backfill || {})}\n` +
      (a.capabilities || []).map((c) => "  " + c).join("\n")
    );
  });
  $("#statusview").textContent =
    `muze ${info.version}   uptime: ${info.uptime}\n\n` + parts.join("\n\n");
}

/* ---------------- boot ---------------- */
refreshAll();
setInterval(loadInfo, 30000);
