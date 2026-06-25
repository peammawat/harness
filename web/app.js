const $ = (id) => document.getElementById(id);
const chat = $("chat");
const providerSel = $("provider");
const backendSel = $("backend");
const enableSearch = $("enableSearch");
const deepResearch = $("deepResearch");
const convList = $("convList");
let convQuery = "";   // current sidebar search filter

// ===================== Attachments =====================
const IMAGE_TYPES = ["image/png", "image/jpeg", "image/webp", "image/gif"];
const MAX_FILE_BYTES = 10 * 1024 * 1024;   // per-file cap (raw bytes)
const MAX_ATTACHMENTS = 10;

// Cleared after each successful send.
let pendingAttachments = { images: [], documents: [] };

// Read a File as raw base64 (strips the "data:...;base64," prefix).
function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",")[1] || "");
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

async function addFiles(fileList) {
  for (const file of fileList) {
    const total = pendingAttachments.images.length + pendingAttachments.documents.length;
    if (total >= MAX_ATTACHMENTS) {
      addMsg("error", `แนบได้สูงสุด ${MAX_ATTACHMENTS} ไฟล์ต่อข้อความ`);
      break;
    }
    if (file.size > MAX_FILE_BYTES) {
      addMsg("error", `ไฟล์ ${file.name} ใหญ่เกินไป (จำกัด ${MAX_FILE_BYTES / 1024 / 1024} MB)`);
      continue;
    }
    let data;
    try { data = await fileToBase64(file); } catch { continue; }
    if (IMAGE_TYPES.includes(file.type)) {
      pendingAttachments.images.push({ media_type: file.type, data, name: file.name });
    } else {
      pendingAttachments.documents.push({ filename: file.name, media_type: file.type, data });
    }
  }
  renderAttachPreview();
}

function removeAttachment(kind, idx) {
  pendingAttachments[kind].splice(idx, 1);
  renderAttachPreview();
}

function renderAttachPreview() {
  const box = $("attachPreview");
  box.innerHTML = "";
  const chip = (label, kind, idx, thumbSrc) => {
    const el = document.createElement("span");
    el.className = "attach-chip";
    if (thumbSrc) {
      const img = document.createElement("img");
      img.src = thumbSrc;
      el.appendChild(img);
    }
    const name = document.createElement("span");
    name.className = "attach-name";
    name.textContent = label;
    el.appendChild(name);
    const x = document.createElement("button");
    x.type = "button";
    x.className = "attach-x";
    x.textContent = "×";
    x.onclick = () => removeAttachment(kind, idx);
    el.appendChild(x);
    box.appendChild(el);
  };
  pendingAttachments.images.forEach((im, i) =>
    chip(im.name || "image", "images", i, `data:${im.media_type};base64,${im.data}`));
  pendingAttachments.documents.forEach((d, i) =>
    chip("📄 " + (d.filename || "document"), "documents", i, null));
}

// ===================== Auth (session token) =====================
const TOKEN_KEY = "harness_token";
const USER_KEY = "harness_user";

const getToken = () => localStorage.getItem(TOKEN_KEY) || "";
const authHeader = () => {
  const t = getToken();
  return t ? { Authorization: "Bearer " + t } : {};
};

function showLogin(message) {
  $("app").hidden = true;
  $("login").hidden = false;
  const err = $("loginError");
  if (message) { err.textContent = message; err.hidden = false; }
  else { err.hidden = true; }
  $("loginUser").focus();
}

function showApp() {
  $("login").hidden = true;
  $("app").hidden = false;
  $("userName").textContent = localStorage.getItem(USER_KEY) || "";
}

function logout(message) {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(USER_KEY);
  showLogin(message);
}

async function login(username, password) {
  const res = await fetch("/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!res.ok) {
    let detail = "เข้าสู่ระบบไม่สำเร็จ";
    try { detail = (await res.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  const data = await res.json();
  localStorage.setItem(TOKEN_KEY, data.token);
  localStorage.setItem(USER_KEY, data.username);
}

// ===================== Chat history store (server-backed) =====================
// History lives in the server's central DB (SQLite), reached via /v1/conversations
// and /v1/chat. Every machine that logs in as the same user sees the same chats.
let convSummaries = [];   // sidebar list from the server (newest first)
let activeId = null;      // id of the open chat (null = a fresh, unsent chat)
let activeMessages = [];  // messages of the open chat, kept in memory for render + context

// Pull the conversation list from the server and refresh the sidebar.
async function loadConversations() {
  try {
    const res = await fetch("/v1/conversations", { headers: authHeader() });
    if (res.status === 401) { logout("เซสชันหมดอายุ กรุณาเข้าสู่ระบบใหม่"); return; }
    if (!res.ok) { convSummaries = []; renderSidebar(); return; }
    convSummaries = await res.json();
  } catch {
    convSummaries = [];
  }
  renderSidebar();
}

// Show the "share" button only when there's a saved chat to share.
function updateShareBtn() {
  $("shareBtn").hidden = !activeId;
}

function newConversation() {
  activeId = null;
  activeMessages = [];
  renderSidebar();
  renderMessages();
  updateShareBtn();
  $("input").focus();
}

async function selectConversation(id) {
  try {
    const res = await fetch("/v1/conversations/" + encodeURIComponent(id), {
      headers: authHeader(),
    });
    if (res.status === 401) { logout("เซสชันหมดอายุ กรุณาเข้าสู่ระบบใหม่"); return; }
    if (!res.ok) { addMsg("error", "โหลดแชทไม่สำเร็จ"); return; }
    const conv = await res.json();
    activeId = conv.id;
    activeMessages = conv.messages || [];
  } catch (e) {
    addMsg("error", "โหลดแชทไม่สำเร็จ: " + e);
    return;
  }
  renderSidebar();
  renderMessages();
  updateShareBtn();
}

// Delete confirmation modal: the × button opens it; deletion runs only on confirm.
let pendingDeleteId = null;
function openDeleteModal(id) { pendingDeleteId = id; $("confirmModal").hidden = false; }
function closeDeleteModal() { pendingDeleteId = null; $("confirmModal").hidden = true; }

async function deleteConversation(id) {
  try {
    const res = await fetch("/v1/conversations/" + encodeURIComponent(id), {
      method: "DELETE",
      headers: authHeader(),
    });
    if (res.status === 401) { logout("เซสชันหมดอายุ กรุณาเข้าสู่ระบบใหม่"); return; }
  } catch (e) {
    addMsg("error", "ลบแชทไม่สำเร็จ: " + e);
    return;
  }
  await loadConversations();
  if (activeId === id) {
    if (convSummaries.length) { await selectConversation(convSummaries[0].id); }
    else { newConversation(); }
  }
}

// Match a conversation summary against the search query (title only — server
// summaries don't carry message text).
function matchesQuery(c, q) {
  if (!q) return true;
  return (c.title || "").toLowerCase().includes(q);
}

function renderSidebar() {
  convList.innerHTML = "";
  const q = convQuery.trim().toLowerCase();
  let shown = 0;
  for (const c of convSummaries) {
    if (!matchesQuery(c, q)) continue;
    shown++;
    const li = document.createElement("li");
    if (c.id === activeId) li.className = "active";

    const title = document.createElement("span");
    title.className = "title";
    title.textContent = c.title || "แชทใหม่";
    title.onclick = () => selectConversation(c.id);

    const del = document.createElement("button");
    del.className = "del";
    del.textContent = "×";
    del.title = "ลบแชทนี้";
    del.onclick = (e) => { e.stopPropagation(); openDeleteModal(c.id); };

    li.appendChild(title);
    li.appendChild(del);
    convList.appendChild(li);
  }
  if (q && shown === 0) {
    const empty = document.createElement("li");
    empty.className = "conv-empty";
    empty.textContent = "ไม่พบแชทที่ตรงกัน";
    convList.appendChild(empty);
  }
}

function renderMessages() {
  chat.innerHTML = "";
  for (const m of activeMessages) addMsg(m.role, m.content, m.images);
  chat.scrollTop = chat.scrollHeight;
}

// ===================== Rendering helpers =====================
function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// Only allow safe link targets (http/https or relative); everything else is dropped.
function safeHref(url) {
  const u = String(url).trim();
  if (/^https?:\/\//i.test(u) || /^\//.test(u)) return u;
  return null;
}

// A small self-contained markdown -> HTML renderer (no external deps, offline-safe).
// Input is HTML-escaped FIRST, so all transforms below operate on safe text.
// Sentinels use a private-use char (\uE000) that won't appear in real content.
function renderMarkdown(src) {
  const SENT = "\uE000";
  const codeBlocks = [];
  // 1. Pull out fenced code blocks so inline rules never touch their contents.
  // Matches ``` or ~~~ fences (3+). The body runs to the matching closing fence,
  // or to end-of-string when it's still unclosed (mid-stream or truncated output) —
  // so streaming code shows in a box immediately instead of as plain text.
  let text = String(src).replace(/(`{3,}|~{3,})([^\n`~]*)\n?([\s\S]*?)(?:\1|$)/g, (_, fence, lang, body) => {
    const i = codeBlocks.length;
    const label = lang.trim() || "text";
    const code = escapeHtml(body.replace(/\n$/, ""));
    codeBlocks.push(
      `<div class="codeblock">` +
        `<div class="codeblock-header">` +
          `<span class="codeblock-lang">${escapeHtml(label)}</span>` +
          `<button class="codeblock-copy" type="button" aria-label="Copy code">Copy</button>` +
        `</div>` +
        `<pre><code>${code}</code></pre>` +
      `</div>`
    );
    return `\n${SENT}CB${i}${SENT}\n`;
  });

  text = escapeHtml(text);

  // 2. Inline code (its contents stay literal — already escaped above).
  const inlineCodes = [];
  text = text.replace(/`([^`\n]+)`/g, (_, body) => {
    const i = inlineCodes.length;
    inlineCodes.push(`<code>${body}</code>`);
    return `${SENT}IC${i}${SENT}`;
  });

  // 3. Block-level: process line by line, grouping consecutive list/quote lines.
  const lines = text.split(/\n/);
  const out = [];
  let list = null;  // { tag: "ul"|"ol", items: [] }
  let quote = null; // string[] of blockquote content lines
  const flushList = () => {
    if (list) {
      out.push(`<${list.tag}>${list.items.map((i) => `<li>${i}</li>`).join("")}</${list.tag}>`);
      list = null;
    }
  };
  const flushQuote = () => {
    if (quote) {
      out.push(`<blockquote>${quote.map((l) => `<p>${inline(l)}</p>`).join("")}</blockquote>`);
      quote = null;
    }
  };
  const flush = () => { flushList(); flushQuote(); };

  // A table is a header row + a separator row (|---|---|) + zero or more body rows.
  const isTableSep = (l) => /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*$/.test(l);
  const splitRow = (l) => {
    let s = l.trim();
    if (s.startsWith("|")) s = s.slice(1);
    if (s.endsWith("|")) s = s.slice(0, -1);
    return s.split("|").map((c) => c.trim());
  };

  const cbLine = new RegExp(`^${SENT}CB(\\d+)${SENT}$`);
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    // Headings: allow up to 3 leading spaces and a missing space after the
    // hashes (e.g. Thai "###หัวข้อ"), but not "#1"-style ordinals (digit guard).
    const heading = line.match(/^\s{0,3}(#{1,6})(?!\d)\s*(.*\S)?\s*$/);
    const hr = /^\s*([-*_])\1{2,}\s*$/.test(line);
    // Text was HTML-escaped above, so a quote marker is "&gt;", not ">".
    const bq = line.match(/^\s*&gt;\s?(.*)$/);
    const ul = line.match(/^\s*[-*]\s+(.*)$/);
    const ol = line.match(/^\s*\d+\.\s+(.*)$/);
    const isTableRow = line.includes("|") && !cbLine.test(line);

    if (cbLine.test(line)) {
      // A standalone fenced-code placeholder — emit raw, no <p> wrapper.
      flush();
      out.push(line);
    } else if (isTableRow && i + 1 < lines.length && isTableSep(lines[i + 1])) {
      // Table: consume the header, separator, and the following pipe rows.
      flush();
      const header = splitRow(line);
      const rows = [];
      i += 2;
      while (i < lines.length && lines[i].includes("|") && !cbLine.test(lines[i])) {
        rows.push(splitRow(lines[i]));
        i++;
      }
      i--; // step back so the outer loop's increment lands on the next line
      const th = header.map((c) => `<th>${inline(c)}</th>`).join("");
      const trs = rows
        .map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`)
        .join("");
      out.push(`<table><thead><tr>${th}</tr></thead><tbody>${trs}</tbody></table>`);
    } else if (hr) {
      flush();
      out.push("<hr>");
    } else if (heading) {
      flush();
      const level = Math.min(heading[1].length, 4);
      out.push(`<h${level}>${inline(heading[2] || "")}</h${level}>`);
    } else if (bq) {
      flushList();
      if (!quote) quote = [];
      quote.push(bq[1]);
    } else if (ul) {
      flushQuote();
      if (!list || list.tag !== "ul") { flushList(); list = { tag: "ul", items: [] }; }
      list.items.push(inline(ul[1]));
    } else if (ol) {
      flushQuote();
      if (!list || list.tag !== "ol") { flushList(); list = { tag: "ol", items: [] }; }
      list.items.push(inline(ol[1]));
    } else if (line.trim() === "") {
      flush();
      out.push("");
    } else {
      flush();
      out.push(`<p>${inline(line)}</p>`);
    }
  }
  flush();

  let html = out.filter((s) => s !== "").join("\n");

  // 4. Restore inline code, then fenced blocks.
  html = html.replace(new RegExp(`${SENT}IC(\\d+)${SENT}`, "g"), (_, i) => inlineCodes[+i]);
  html = html.replace(new RegExp(`${SENT}CB(\\d+)${SENT}`, "g"), (_, i) => codeBlocks[+i]);
  return html;

  // Inline transforms applied to already-escaped, code-extracted text.
  function inline(s) {
    return s
      .replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, (m, alt, url) => {
        const href = safeHref(url);
        return href
          ? `<img class="md-img" src="${escapeHtml(href)}" alt="${alt}" />`
          : alt;
      })
      .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, label, url) => {
        const href = safeHref(url);
        return href
          ? `<a href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">${label}</a>`
          : label;
      })
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/__([^_]+)__/g, "<strong>$1</strong>")
      .replace(/~~([^~]+)~~/g, "<del>$1</del>")
      .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
      .replace(/(^|[^_\w])_([^_\n]+)_/g, "$1<em>$2</em>");
  }
}

function addMsg(role, text, images) {
  const el = document.createElement("div");
  el.className = `msg ${role}`;
  if (role === "assistant") {
    el.innerHTML = renderMarkdown(text);
  } else {
    if (text) {
      const t = document.createElement("div");
      t.className = "msg-text";
      t.textContent = text;
      el.appendChild(t);
    }
    if (images && images.length) {
      const gallery = document.createElement("div");
      gallery.className = "msg-images";
      for (const im of images) {
        const img = document.createElement("img");
        img.src = `data:${im.media_type};base64,${im.data}`;
        img.alt = im.name || "image";
        gallery.appendChild(img);
      }
      el.appendChild(gallery);
    }
  }
  chat.appendChild(el);
  chat.scrollTop = chat.scrollHeight;
  return el;
}

// Collapsed-by-default green box listing the sources of one search.
function addSearchDetails(query, results) {
  const el = document.createElement("details");
  el.className = "msg tool search-details";

  const summary = document.createElement("summary");
  const n = (results || []).length;
  summary.textContent = `🔎 ค้นหา: ${query || ""} · ${n} ผลลัพธ์`;
  el.appendChild(summary);

  const body = document.createElement("div");
  body.className = "search-body";
  body.innerHTML = (results || [])
    .slice(0, 5)
    .map((r) => {
      const href = safeHref(r.url);
      const title = escapeHtml(r.title || r.url || "");
      return href
        ? `<a href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">${title}</a>`
        : `<span>${title}</span>`;
    })
    .join("");
  el.appendChild(body);

  chat.appendChild(el);
  chat.scrollTop = chat.scrollHeight;
  return el;
}

// Collapsed box noting a page the agent fetched in full.
function addFetchDetails(url, title, chars) {
  const el = document.createElement("details");
  el.className = "msg tool search-details";

  const summary = document.createElement("summary");
  summary.textContent = `🔗 อ่านหน้าเว็บ: ${title || url || ""} · ${chars || 0} ตัวอักษร`;
  el.appendChild(summary);

  const body = document.createElement("div");
  body.className = "search-body";
  const href = safeHref(url);
  body.innerHTML = href
    ? `<a href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">${escapeHtml(url)}</a>`
    : `<span>${escapeHtml(url || "")}</span>`;
  el.appendChild(body);

  chat.appendChild(el);
  chat.scrollTop = chat.scrollHeight;
  return el;
}

// ===================== Capabilities =====================
async function loadCapabilities() {
  try {
    const res = await fetch("/v1/capabilities");
    const caps = await res.json();
    const llms = caps.llm_providers.available.length
      ? caps.llm_providers.available
      : caps.llm_providers.all;
    providerSel.innerHTML = llms.map((p) => `<option>${p}</option>`).join("");
    if (llms.includes(caps.llm_providers.default)) {
      providerSel.value = caps.llm_providers.default;
    }

    const backends = caps.search_backends.available.length
      ? caps.search_backends.available
      : caps.search_backends.all;
    backendSel.innerHTML = backends.map((b) => `<option>${b}</option>`).join("");
    if (backends.includes(caps.search_backends.default)) {
      backendSel.value = caps.search_backends.default;
    }
  } catch (e) {
    addMsg("error", "โหลด capabilities ไม่ได้: " + e);
  }
}

// ===================== Send / stream =====================
async function send(text) {
  // Capture and clear pending attachments for this turn.
  const images = pendingAttachments.images;
  const documents = pendingAttachments.documents;
  pendingAttachments = { images: [], documents: [] };
  renderAttachPreview();

  const userMsg = { role: "user", content: text };
  if (images.length) userMsg.images = images;
  if (documents.length) userMsg.documents = documents;
  activeMessages.push(userMsg);

  addMsg("user", text, images);

  const assistantEl = addMsg("assistant", "");
  let answer = "";
  const turn = { buffer: "", lastQuery: "", lastTool: "" };

  const res = await fetch("/v1/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeader() },
    body: JSON.stringify({
      messages: activeMessages,
      conversation_id: activeId,
      provider: providerSel.value || null,
      search_backend: backendSel.value || null,
      enable_search: enableSearch.checked,
      deep_research: deepResearch.checked,
      stream: true,
    }),
  });

  if (res.status === 401) {
    assistantEl.remove();
    logout("เซสชันหมดอายุ กรุณาเข้าสู่ระบบใหม่");
    return;
  }
  if (!res.ok) {
    assistantEl.remove();
    addMsg("error", `HTTP ${res.status}: ${await res.text()}`);
    return;
  }

  // Parse the SSE stream. sse-starlette separates events with CRLF, so split
  // on either \r\n\r\n or \n\n.
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  const processChunk = (chunk) => {
    let eventType = "message";
    let data = "";
    for (const line of chunk.split(/\r?\n/)) {
      if (line.startsWith("event:")) eventType = line.slice(6).trim();
      else if (line.startsWith("data:")) data += line.slice(5).trim();
    }
    if (!data) return;
    let payload;
    try { payload = JSON.parse(data); } catch { return; }
    handleEvent(eventType, payload, assistantEl, turn, (t) => (answer = t));
  };

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const chunks = buffer.split(/\r?\n\r?\n/);
    buffer = chunks.pop();
    for (const chunk of chunks) processChunk(chunk);
  }
  if (buffer.trim()) processChunk(buffer);

  if (answer) {
    activeMessages.push({ role: "assistant", content: answer });
    // Refresh the sidebar from the server (picks up the new chat, its
    // server-assigned title, and updated ordering). The server persisted both
    // the user and assistant messages.
    await loadConversations();
  }
}

function handleEvent(type, payload, assistantEl, turn, setAnswer) {
  if (type === "conversation") {
    // Server emits this first; bind a brand-new chat to its assigned id.
    if (!activeId) { activeId = payload.conversation_id; updateShareBtn(); }
  } else if (type === "token") {
    // Re-render the full buffer each token: markdown depends on full context
    // (e.g. an unclosed code fence) so incremental textContent won't work.
    turn.buffer += payload.text;
    assistantEl.innerHTML = renderMarkdown(turn.buffer);
  } else if (type === "tool_call") {
    const args = payload.arguments || {};
    turn.lastTool = payload.name || "";
    turn.lastQuery = args.query || args.q || args.url || JSON.stringify(args);
  } else if (type === "tool_result") {
    if (payload.name === "fetch_url") {
      if (payload.error) {
        addMsg("error", `⚠️ fetch error: ${payload.error}`);
      } else {
        addFetchDetails(payload.url, payload.title, payload.chars);
      }
    } else if (payload.error) {
      addMsg("error", `⚠️ search error: ${payload.error}`);
    } else {
      addSearchDetails(turn.lastQuery, payload.results);
    }
  } else if (type === "done") {
    setAnswer(payload.content || turn.buffer);
  } else if (type === "error") {
    addMsg("error", payload.message);
  }
  chat.scrollTop = chat.scrollHeight;
}

// ===================== Composer wiring =====================
$("composer").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("input");
  const text = input.value.trim();
  const hasAttachments =
    pendingAttachments.images.length || pendingAttachments.documents.length;
  if (!text && !hasAttachments) return;
  input.value = "";
  autoGrow();
  $("send").disabled = true;
  try {
    await send(text);
  } catch (err) {
    addMsg("error", String(err));
  } finally {
    $("send").disabled = false;
  }
});

// ----- Options popover (model + search backend) -----
const optionsBtn = $("optionsBtn");
const optionsMenu = $("optionsMenu");
optionsBtn.addEventListener("click", (e) => {
  e.stopPropagation();
  const open = optionsMenu.hidden;
  optionsMenu.hidden = !open;
  optionsBtn.setAttribute("aria-expanded", String(open));
});
document.addEventListener("click", (e) => {
  if (!optionsMenu.hidden && !e.target.closest(".options-wrap")) {
    optionsMenu.hidden = true;
    optionsBtn.setAttribute("aria-expanded", "false");
  }
});

// ----- Attachment wiring (type chooser popover, file input, drag-drop, paste) -----
const attachBtn = $("attachBtn");
const attachMenu = $("attachMenu");
attachBtn.addEventListener("click", (e) => {
  e.stopPropagation();
  const open = attachMenu.hidden;
  attachMenu.hidden = !open;
  attachBtn.setAttribute("aria-expanded", String(open));
});
attachMenu.querySelectorAll(".menu-item").forEach((item) =>
  item.addEventListener("click", () => {
    $("fileInput").accept = item.dataset.accept;  // filter picker to chosen type
    attachMenu.hidden = true;
    attachBtn.setAttribute("aria-expanded", "false");
    $("fileInput").click();
  })
);
document.addEventListener("click", (e) => {
  if (!attachMenu.hidden && !e.target.closest(".attach-wrap")) {
    attachMenu.hidden = true;
    attachBtn.setAttribute("aria-expanded", "false");
  }
});
$("fileInput").addEventListener("change", async (e) => {
  await addFiles(e.target.files);
  e.target.value = "";  // allow re-selecting the same file
});

const composerBox = document.querySelector(".composer-box");
["dragenter", "dragover"].forEach((ev) =>
  composerBox.addEventListener(ev, (e) => {
    e.preventDefault();
    composerBox.classList.add("dragover");
  }));
["dragleave", "drop"].forEach((ev) =>
  composerBox.addEventListener(ev, (e) => {
    e.preventDefault();
    composerBox.classList.remove("dragover");
  }));
composerBox.addEventListener("drop", async (e) => {
  if (e.dataTransfer && e.dataTransfer.files.length) await addFiles(e.dataTransfer.files);
});

$("input").addEventListener("paste", async (e) => {
  const items = e.clipboardData && e.clipboardData.items;
  if (!items) return;
  const files = [];
  for (const it of items) {
    if (it.kind === "file") {
      const f = it.getAsFile();
      if (f) files.push(f);
    }
  }
  if (files.length) { e.preventDefault(); await addFiles(files); }
});

function autoGrow() {
  const input = $("input");
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 200) + "px";
}

$("input").addEventListener("input", autoGrow);
$("input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $("composer").requestSubmit();
  }
});

$("newChat").addEventListener("click", newConversation);
$("convSearch").addEventListener("input", (e) => {
  convQuery = e.target.value;
  renderSidebar();
});
$("logout").addEventListener("click", () => logout());

// ----- Delete-confirmation modal wiring -----
$("confirmCancel").addEventListener("click", closeDeleteModal);
$("confirmOk").addEventListener("click", async () => {
  const id = pendingDeleteId;
  closeDeleteModal();
  if (id) await deleteConversation(id);
});
$("confirmModal").addEventListener("click", (e) => {
  if (e.target.id === "confirmModal") closeDeleteModal();   // click backdrop to dismiss
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!$("confirmModal").hidden) closeDeleteModal();
  if (!$("shareModal").hidden) closeShareModal();
});

// ----- Share modal wiring -----
function openShareModal(link) {
  $("shareLink").value = link;
  $("shareModal").hidden = false;
  $("shareLink").select();
}
function closeShareModal() { $("shareModal").hidden = true; }

$("shareBtn").addEventListener("click", async () => {
  if (!activeId) return;
  $("shareBtn").disabled = true;
  try {
    const res = await fetch(
      "/v1/conversations/" + encodeURIComponent(activeId) + "/share",
      { method: "POST", headers: authHeader() }
    );
    if (res.status === 401) { logout("เซสชันหมดอายุ กรุณาเข้าสู่ระบบใหม่"); return; }
    if (!res.ok) { addMsg("error", "สร้างลิงค์แชร์ไม่สำเร็จ"); return; }
    const data = await res.json();
    openShareModal(`${location.origin}/?s=${encodeURIComponent(data.token)}`);
  } catch (e) {
    addMsg("error", "สร้างลิงค์แชร์ไม่สำเร็จ: " + e);
  } finally {
    $("shareBtn").disabled = false;
  }
});

$("shareCopy").addEventListener("click", () => {
  navigator.clipboard.writeText($("shareLink").value).then(() => {
    const btn = $("shareCopy");
    btn.textContent = "✓ แล้ว";
    setTimeout(() => { btn.textContent = "คัดลอก"; }, 1500);
  });
});

$("shareStop").addEventListener("click", async () => {
  if (!activeId) { closeShareModal(); return; }
  try {
    const res = await fetch(
      "/v1/conversations/" + encodeURIComponent(activeId) + "/share",
      { method: "DELETE", headers: authHeader() }
    );
    if (res.status === 401) { logout("เซสชันหมดอายุ กรุณาเข้าสู่ระบบใหม่"); return; }
  } catch (e) {
    addMsg("error", "หยุดแชร์ไม่สำเร็จ: " + e);
    return;
  }
  closeShareModal();
});

$("shareClose").addEventListener("click", closeShareModal);
$("shareModal").addEventListener("click", (e) => {
  if (e.target.id === "shareModal") closeShareModal();
});

// Copy-to-clipboard for code blocks (delegated: assistant messages re-render on stream).
chat.addEventListener("click", (e) => {
  const btn = e.target.closest(".codeblock-copy");
  if (!btn) return;
  const code = btn.closest(".codeblock")?.querySelector("code");
  if (!code) return;
  navigator.clipboard.writeText(code.textContent).then(() => {
    btn.textContent = "Copied";
    setTimeout(() => { btn.textContent = "Copy"; }, 1500);
  });
});

// ===================== Login wiring =====================
$("loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const username = $("loginUser").value.trim();
  const password = $("loginPass").value;
  if (!username || !password) return;
  $("loginBtn").disabled = true;
  try {
    await login(username, password);
    $("loginPass").value = "";
    await startApp();
  } catch (err) {
    showLogin(String(err.message || err));
  } finally {
    $("loginBtn").disabled = false;
  }
});

// ===================== Init =====================
async function startApp() {
  showApp();
  await loadCapabilities();
  await loadConversations();
  if (convSummaries.length) { await selectConversation(convSummaries[0].id); }
  else { newConversation(); }
}

// Public read-only view: load a shared conversation by token, no login needed.
async function enterSharedMode(token) {
  // Show the app shell but strip everything used to converse or navigate.
  $("login").hidden = true;
  $("app").hidden = false;
  $("sidebar").hidden = true;
  $("composer").hidden = true;
  $("shareBtn").hidden = true;
  $("sharedBanner").hidden = false;

  try {
    const res = await fetch("/shared/" + encodeURIComponent(token));
    if (!res.ok) {
      chat.innerHTML = "";
      addMsg("error", "ลิงค์นี้ใช้ไม่ได้แล้ว");
      return;
    }
    const conv = await res.json();
    activeMessages = conv.messages || [];
    document.title = (conv.title || "แชทที่แชร์") + " · Onebix Harness";
    renderMessages();
  } catch (e) {
    chat.innerHTML = "";
    addMsg("error", "โหลดแชทที่แชร์ไม่สำเร็จ: " + e);
  }
}

async function init() {
  const sharedToken = new URLSearchParams(location.search).get("s");
  if (sharedToken) { await enterSharedMode(sharedToken); return; }

  if (!getToken()) { showLogin(); return; }
  // Validate the stored token before showing the app.
  try {
    const res = await fetch("/auth/me", { headers: authHeader() });
    if (!res.ok) { logout(); return; }
  } catch {
    logout();
    return;
  }
  await startApp();
}

init();
