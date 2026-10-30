/* PyKnora 最小前端：纯静态、无构建步骤。所有请求带 Bearer token，工作区/知识库作用域由后端校验。 */
"use strict";

const API = "/api/v1";
const state = {
  token: localStorage.getItem("pyknora.token") || "",
  workspaces: [],
  knowledgeBases: [],
  workspaceId: "",
  knowledgeBaseId: "",
  conversationId: "",
};

const $ = (id) => document.getElementById(id);

function log(message) {
  const box = $("log");
  box.classList.remove("hidden");
  const line = `[${new Date().toLocaleTimeString()}] ${message}\n`;
  $("log-body").textContent = (line + $("log-body").textContent).slice(0, 8000);
}

function show(id, visible) {
  $(id).classList.toggle("hidden", !visible);
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  if (options.json !== undefined) {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(options.json);
  }
  const response = await fetch(`${API}${path}`, { ...options, headers });
  const text = await response.text();
  const data = text ? JSON.parse(text) : null;
  if (!response.ok) {
    const code = data?.error?.code || response.status;
    const message = data?.error?.message || response.statusText;
    throw new Error(`${code}: ${message}`);
  }
  return data;
}

function setSession(token, email) {
  state.token = token;
  localStorage.setItem("pyknora.token", token);
  localStorage.setItem("pyknora.email", email);
  $("whoami").textContent = email;
  show("auth", false);
  show("workspace-area", true);
  show("kb-area", true);
  show("ask-area", true);
  show("log", true);
}

function clearSession() {
  state.token = "";
  localStorage.removeItem("pyknora.token");
  $("whoami").textContent = "未登录";
}

function fillSelect(select, items, labelKey, placeholder) {
  select.innerHTML = "";
  const head = document.createElement("option");
  head.value = "";
  head.textContent = placeholder;
  select.append(head);
  for (const item of items) {
    const option = document.createElement("option");
    option.value = item.id;
    option.textContent = item[labelKey];
    select.append(option);
  }
}

async function loadWorkspaces() {
  state.workspaces = await api("/workspaces");
  fillSelect($("workspace-select"), state.workspaces, "name", "选择工作区");
  log(`加载工作区 ${state.workspaces.length} 个`);
}

async function loadKnowledgeBases() {
  if (!state.workspaceId) return;
  state.knowledgeBases = await api(`/workspaces/${state.workspaceId}/knowledge-bases`);
  fillSelect($("kb-select"), state.knowledgeBases, "name", "选择知识库");
  log(`加载知识库 ${state.knowledgeBases.length} 个`);
}

async function ensureConversation() {
  if (state.conversationId) return state.conversationId;
  const conversation = await api(`/workspaces/${state.workspaceId}/conversations`, {
    method: "POST",
    json: { workspace_id: state.workspaceId, title: "前端提问" },
  });
  state.conversationId = conversation.id;
  return state.conversationId;
}

async function handleAuth(kind) {
  const email = $("email").value.trim();
  const password = $("password").value;
  if (!email || password.length < 8) {
    log("邮箱必填，口令至少 8 位");
    return;
  }
  try {
    const path = kind === "register" ? "/auth/register" : "/auth/login";
    const body =
      kind === "register"
        ? { email, password, display_name: email.split("@")[0] }
        : { email, password };
    const result = await api(path, { method: "POST", json: body });
    setSession(result.access_token, email);
    log(`${kind === "register" ? "注册" : "登录"}成功：${email}`);
    await loadWorkspaces();
  } catch (error) {
    log(`认证失败 ${error.message}`);
  }
}

async function createWorkspace() {
  const name = $("workspace-name").value.trim();
  if (!name) return;
  try {
    const workspace = await api("/workspaces", { method: "POST", json: { name } });
    $("workspace-name").value = "";
    await loadWorkspaces();
    $("workspace-select").value = workspace.id;
    state.workspaceId = workspace.id;
    await loadKnowledgeBases();
    log(`已建工作区 ${workspace.name}`);
  } catch (error) {
    log(`建工作区失败 ${error.message}`);
  }
}

async function createKnowledgeBase() {
  const name = $("kb-name").value.trim();
  if (!name || !state.workspaceId) return;
  try {
    const kb = await api(`/workspaces/${state.workspaceId}/knowledge-bases`, {
      method: "POST",
      json: { name },
    });
    $("kb-name").value = "";
    await loadKnowledgeBases();
    $("kb-select").value = kb.id;
    state.knowledgeBaseId = kb.id;
    log(`已建知识库 ${kb.name}`);
  } catch (error) {
    log(`建知识库失败 ${error.message}`);
  }
}

async function uploadDocument() {
  const file = $("file").files[0];
  if (!file || !state.knowledgeBaseId) {
    log("先选知识库和文件");
    return;
  }
  const form = new FormData();
  form.append("file", file);
  try {
    const document = await api(`/knowledge-bases/${state.knowledgeBaseId}/documents`, {
      method: "POST",
      body: form,
    });
    log(`上传完成 ${document.filename} status=${document.status} chunks=${document.chunk_count}`);
  } catch (error) {
    log(`上传失败 ${error.message}`);
  }
}

async function importUrl() {
  const url = $("url").value.trim();
  if (!url || !state.knowledgeBaseId) {
    log("先选知识库并填 URL");
    return;
  }
  try {
    const document = await api(`/knowledge-bases/${state.knowledgeBaseId}/documents/from-url`, {
      method: "POST",
      json: { url },
    });
    log(`URL 导入完成 ${document.filename} status=${document.status}`);
  } catch (error) {
    log(`URL 导入失败 ${error.message}`);
  }
}

async function ask() {
  const question = $("question").value.trim();
  if (!question || !state.workspaceId) {
    log("先选工作区并输入问题");
    return;
  }
  try {
    const conversationId = await ensureConversation();
    const answer = await api(`/conversations/${conversationId}/messages`, {
      method: "POST",
      json: { question, knowledge_base_ids: state.knowledgeBaseId ? [state.knowledgeBaseId] : null },
    });
    $("answer").classList.remove("hidden");
    $("answer-meta").textContent = `状态 ${answer.status} · 引用 ${answer.citations.length} 条 · ${answer.trace?.chat_model || "chat"}`;
    $("answer-text").textContent = answer.content;
    const list = $("citations");
    list.innerHTML = "";
    for (const citation of answer.citations) {
      const item = document.createElement("li");
      item.textContent = `[${citation.index}] ${citation.document_title} #${citation.ordinal} — ${citation.snippet}`;
      list.append(item);
    }
    log(`问答完成 status=${answer.status} cites=${answer.citations.length}`);
  } catch (error) {
    log(`提问失败 ${error.message}`);
  }
}

$("btn-login").addEventListener("click", () => handleAuth("login"));
$("btn-register").addEventListener("click", () => handleAuth("register"));
$("btn-create-workspace").addEventListener("click", createWorkspace);
$("btn-create-kb").addEventListener("click", createKnowledgeBase);
$("btn-upload").addEventListener("click", uploadDocument);
$("btn-import-url").addEventListener("click", importUrl);
$("btn-ask").addEventListener("click", ask);
$("question").addEventListener("keydown", (event) => {
  if (event.key === "Enter") ask();
});
$("workspace-select").addEventListener("change", async (event) => {
  state.workspaceId = event.target.value;
  state.conversationId = "";
  await loadKnowledgeBases();
});
$("kb-select").addEventListener("change", (event) => {
  state.knowledgeBaseId = event.target.value;
});

if (state.token) {
  setSession(state.token, localStorage.getItem("pyknora.email") || "已登录");
  loadWorkspaces().catch((error) => {
    log(`会话已过期：${error.message}`);
    clearSession();
    show("auth", true);
  });
} else {
  show("auth", true);
}
