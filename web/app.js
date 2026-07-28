const $ = (id) => document.getElementById(id);
const state = { role: location.hash === "#supplier" ? "supplier" : "admin", bootstrap: null, demands: [] };

async function api(path, options = {}) {
  const response = await fetch(path, { credentials: "same-origin", ...options, headers: { "Content-Type": "application/json", ...(options.headers || {}) } });
  let data = {};
  try { data = await response.json(); } catch (_) {}
  if (!response.ok) { const error = new Error(data.error || `请求失败 (${response.status})`); error.status = response.status; throw error; }
  return data;
}

function esc(value) { return String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char])); }
function pct(value) { return value == null ? "暂无样本" : `${Number(value).toFixed(1)}%`; }
function show(id) { $(id).classList.remove("hidden"); }
function hide(id) { $(id).classList.add("hidden"); }

function configureLogin() {
  const supplier = state.role === "supplier";
  $("loginEyebrow").textContent = supplier ? "供应商入口" : "管理员入口";
  $("loginTitle").textContent = supplier ? "查看并完成补号需求" : "进入补号控制台";
  $("loginDescription").textContent = supplier ? "输入管理员为你创建的供应商密钥。你只能看到当前缺口，无法访问账号明细和上游配置。" : "管理密钥只保存在服务端，浏览器仅获得安全会话。";
  $("loginCredentialLabel").textContent = supplier ? "供应商密钥" : "访问密码";
  $("loginPassword").placeholder = supplier ? "输入 sup_ 开头的供应商密钥" : "输入访问密码";
  $("portalLink").textContent = supplier ? "返回管理员入口 →" : "前往供应商入口 →";
  $("portalLink").href = supplier ? "/" : "/#supplier";
}

async function login(event) {
  event.preventDefault();
  $("loginError").textContent = "";
  const endpoint = state.role === "supplier" ? "/api/supplier/auth/login" : "/api/auth/login";
  try {
    await api(endpoint, { method: "POST", body: JSON.stringify({ password: $("loginPassword").value }) });
    $("loginPassword").value = "";
    await enterApp();
  } catch (error) { $("loginError").textContent = error.message; }
}

async function enterApp() {
  try {
    if (state.role === "supplier") await loadSupplier(); else await loadAdmin();
    hide("loginView"); show("topActions");
  } catch (error) {
    if (error.status === 401) { show("loginView"); hide("adminView"); hide("supplierView"); hide("topActions"); return; }
    $("loginError").textContent = error.message;
  }
}

async function logout() { try { await api("/api/auth/logout", { method: "POST", body: "{}" }); } catch (_) {} location.reload(); }

async function loadAdmin() {
  const data = await api("/api/admin/bootstrap");
  state.bootstrap = data;
  hide("supplierView"); show("adminView");
  $("connectionState").textContent = `已连接 · ${data.account_count} 个账号`;
  renderAdmin();
  await Promise.all([loadAudit(), loadSuppliers(), loadSupplies()]);
}

function renderAdmin() {
  const data = state.bootstrap;
  const triggered = data.evaluations.filter((item) => item.triggered).length;
  const usable = data.evaluations.reduce((sum, item) => sum + item.usable_count, 0);
  const recommended = data.evaluations.reduce((sum, item) => sum + item.recommended_count, 0);
  $("adminMetrics").innerHTML = [
    [data.account_count, "全部账号", "上游地址仅保存在服务端"],
    [usable, "分组可用计数", "跨分组账号可能重复计数"],
    [triggered, "触发策略", triggered ? "需要关注" : "当前稳定"],
    [recommended, "建议补充", "受单次上限约束"]
  ].map(([value, label, note]) => `<div class="metric"><div class="metric-label">${esc(label)}</div><div class="metric-value">${esc(value)}</div><div class="metric-note">${esc(note)}</div></div>`).join("");

  const global = data.settings.global;
  $("evaluationInterval").value = global.evaluation_interval_minutes;
  $("cooldownMinutes").value = global.replenish_cooldown_minutes;
  $("maxPerRun").value = global.max_accounts_per_run;
  $("triggerMode").value = global.trigger_mode;
  $("supplierAutoImport").checked = global.supplier_auto_import;

  const policies = new Map(data.settings.groups.map((item) => [item.group_id, item]));
  const groupOptions = (selected) => data.groups.map((group) => `<option value="${group.id}" ${selected.includes(Number(group.id)) ? "selected" : ""}>${esc(group.name)} (ID ${group.id})</option>`).join("");
  $("policyRows").innerHTML = data.evaluations.map((item) => {
    const p = policies.get(item.group_id);
    const badgeClass = !p.enabled ? "disabled" : item.triggered ? "triggered" : "";
    const badgeText = !p.enabled ? "未启用" : item.in_cooldown ? "冷却中" : item.triggered ? "需要补号" : "正常";
    return `<tr data-group-id="${item.group_id}">
      <td><input class="policy-enabled" type="checkbox" ${p.enabled ? "checked" : ""} aria-label="启用 ${esc(item.group_name)}"></td>
      <td><span class="group-name"><i class="group-dot" style="--group-color:${esc(item.color || "#777")}"></i>${esc(item.group_name)}</span><span class="subtext">ID ${item.group_id} · ${esc(badgeText)}</span></td>
      <td><strong>${item.usable_count}</strong> / ${item.total_count}<span class="subtext">剩余 ${pct(item.remaining_7d_percent)}</span></td>
      <td><input class="target-count" type="number" min="0" max="10000" value="${p.target_usable_count}"></td>
      <td><input class="usable-threshold" type="number" min="0" max="10000" value="${p.min_usable_count}"></td>
      <td><input class="remaining-threshold" type="number" min="0" max="100" step="0.1" value="${p.min_remaining_7d_percent}"></td>
      <td><div class="checkbox-stack"><label><input class="trigger-usable" type="checkbox" ${p.trigger_on_usable ? "checked" : ""}>可用数</label><label><input class="trigger-remaining" type="checkbox" ${p.trigger_on_remaining_7d ? "checked" : ""}>7d 额度</label></div></td>
      <td><select class="target-groups" multiple aria-label="补号目标分组">${groupOptions(p.target_group_ids || [item.group_id])}</select><span class="subtext">可多选，例如 Plus + 分流</span></td>
      <td><span class="state-badge ${badgeClass}">${item.recommended_count} 个</span><span class="subtext">${esc(item.reasons.join("；") || "未触发")}</span></td>
      <td><input class="supplier-note" type="text" maxlength="500" value="${esc(p.supplier_note || "")}" placeholder="如：仅接受 Pro"></td>
    </tr>`;
  }).join("");
}

function collectSettings() {
  const rows = [...document.querySelectorAll("#policyRows tr")];
  return {
    version: 1,
    updated_at: new Date().toISOString(),
    global: {
      evaluation_interval_minutes: Number($("evaluationInterval").value),
      replenish_cooldown_minutes: Number($("cooldownMinutes").value),
      max_accounts_per_run: Number($("maxPerRun").value),
      trigger_mode: $("triggerMode").value,
      supplier_auto_import: $("supplierAutoImport").checked
    },
    groups: rows.map((row) => ({
      group_id: Number(row.dataset.groupId), enabled: row.querySelector(".policy-enabled").checked,
      target_usable_count: Number(row.querySelector(".target-count").value), min_usable_count: Number(row.querySelector(".usable-threshold").value),
      min_remaining_7d_percent: Number(row.querySelector(".remaining-threshold").value), trigger_on_usable: row.querySelector(".trigger-usable").checked,
      trigger_on_remaining_7d: row.querySelector(".trigger-remaining").checked,
      target_group_ids: [...row.querySelector(".target-groups").selectedOptions].map((option) => Number(option.value)),
      supplier_note: row.querySelector(".supplier-note").value.trim()
    }))
  };
}

async function saveSettings() {
  $("saveState").textContent = "保存中…";
  try {
    await api("/api/admin/settings", { method: "PUT", body: JSON.stringify(collectSettings()) });
    $("saveState").textContent = "已保存";
    await loadAdmin();
  } catch (error) { $("saveState").textContent = error.message; }
}

async function loadAudit() {
  const data = await api("/api/admin/audit");
  $("auditList").innerHTML = data.entries.length ? data.entries.map((entry) => `<div class="audit-row"><time>${new Date(entry.time).toLocaleString()}</time><span>${esc(entry.event)}</span><div>${esc(entry.message)}</div><strong>${entry.count ? `${entry.count} 个` : ""}</strong></div>`).join("") : `<div class="empty">暂无审计记录</div>`;
}

async function loadSuppliers() {
  const data = await api("/api/admin/suppliers");
  $("supplierRows").innerHTML = data.suppliers.length ? data.suppliers.map((item) => `<tr><td><strong>${esc(item.name)}</strong><span class="subtext">ID ${item.id} · ${new Date(item.created_at).toLocaleString()}</span></td><td><code>${esc(item.key_prefix)}…</code></td><td><span class="state-badge ${item.enabled ? "" : "disabled"}">${item.enabled ? "启用" : "已禁用"}</span></td><td>${item.accepted_count}</td><td>${item.last_used_at ? new Date(item.last_used_at).toLocaleString() : "从未"}</td><td><button class="button ghost supplier-toggle" data-id="${item.id}" data-enabled="${item.enabled}">${item.enabled ? "禁用" : "启用"}</button></td></tr>`).join("") : `<tr><td colspan="6" class="empty">尚未创建供应商</td></tr>`;
  document.querySelectorAll(".supplier-toggle").forEach((button) => button.addEventListener("click", async () => {
    await api(`/api/admin/suppliers/${button.dataset.id}`, { method: "PATCH", body: JSON.stringify({ enabled: button.dataset.enabled !== "true" }) });
    await loadSuppliers();
  }));
}

async function createSupplier() {
  const name = $("newSupplierName").value.trim();
  if (!name) return;
  const button = $("createSupplier");
  button.disabled = true; button.textContent = "创建中…";
  try {
    const result = await api("/api/admin/suppliers", { method: "POST", body: JSON.stringify({ name }) });
    $("supplierKeyReveal").textContent = `请立即复制（只显示一次）：${result.key}`;
    $("newSupplierName").value = "";
    await loadSuppliers();
  } catch (error) { $("supplierKeyReveal").textContent = error.message; }
  finally { button.disabled = false; button.textContent = "创建供应商密钥"; }
}

async function loadSupplies() {
  const data = await api("/api/admin/supplies");
  const groupNames = new Map((state.bootstrap?.groups || []).map((item) => [Number(item.id), item.name]));
  $("suppliedAccountRows").innerHTML = data.accounts.length ? data.accounts.map((item) => `<tr><td>${new Date(item.created_at).toLocaleString()}</td><td>${esc(item.supplier_name)}</td><td>${item.upstream_account_id ? `ID ${item.upstream_account_id}` : "未创建"}<span class="subtext">${esc(item.account_name || "")}</span></td><td>${esc(item.email || "-")}</td><td><span class="${item.status === "accepted" ? "status-ok" : "status-bad"}">${item.status === "accepted" ? "存活并已入组" : "拒绝"}</span>${item.error_message ? `<span class="subtext">${esc(item.error_message)}</span>` : ""}</td><td>${item.target_group_ids.map((id) => esc(groupNames.get(Number(id)) || `ID ${id}`)).join(" + ")}</td></tr>`).join("") : `<tr><td colspan="6" class="empty">暂无供应商补号记录</td></tr>`;
}

async function loadSupplier() {
  const data = await api("/api/supplier/demand");
  state.demands = data.demands;
  hide("adminView"); show("supplierView");
  $("connectionState").textContent = "供应商会话";
  $("supplierModeText").textContent = data.direct_import_enabled ? "管理员已开启直接补号。" : "管理员尚未开启直接补号，目前只能查看需求。";
  $("demandGrid").innerHTML = data.demands.length ? data.demands.map((item) => `<article class="demand-card"><div class="demand-top"><span class="group-name"><i class="group-dot" style="--group-color:${esc(item.color || "#777")}"></i>检查：${esc(item.group_name)}</span><span class="state-badge ${item.accepting ? "triggered" : "disabled"}">${item.accepting ? "可提交" : "暂不接收"}</span></div><div class="demand-number">${item.needed}</div><span class="subtext">需要补充的存活账号</span><ul>${(item.reasons || []).map((reason) => `<li>${esc(reason)}</li>`).join("")}<li>入组：${(item.target_group_names || []).map(esc).join(" + ")}</li></ul>${item.note ? `<p class="subtext">备注：${esc(item.note)}</p>` : ""}</article>`).join("") : `<div class="empty">当前没有已启用的补号策略</div>`;
  $("supplyGroup").innerHTML = data.demands.map((item) => `<option value="${item.group_id}" ${item.accepting ? "" : "disabled"}>${esc(item.group_name)} · 需要 ${item.needed}</option>`).join("");
  $("submitSupply").disabled = !data.direct_import_enabled || !data.demands.some((item) => item.accepting);
  updateSupplierLimit();
}

function updateSupplierLimit() {
  const selected = state.demands.find((item) => item.group_id === Number($("supplyGroup").value));
  const tokenCount = $("supplyTokens").value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean).length;
  $("supplierTokenCount").textContent = `${tokenCount} 个 Token`;
  $("supplierLimit").textContent = `当前最多接收 ${selected?.needed || 0} 个`;
}

async function submitSupply(event) {
  event.preventDefault();
  const selected = state.demands.find((item) => item.group_id === Number($("supplyGroup").value));
  if (!selected || !selected.accepting) return;
  const count = $("supplyTokens").value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean).length;
  if (!window.confirm(`将向 ${selected.group_name} 提交最多 ${Math.min(count, selected.needed)} 个账号。确认继续？`)) return;
  $("submitSupply").disabled = true; $("supplyMessage").textContent = "提交中…";
  try {
    const result = await api("/api/supplier/supply", { method: "POST", body: JSON.stringify({ group_id: selected.group_id, tokens: $("supplyTokens").value, proxy_url: $("supplyProxy").value.trim() }) });
    $("supplyTokens").value = ""; $("supplyMessage").textContent = `存活并入组 ${result.accepted} 个，拒绝 ${result.failed} 个`;
    await loadSupplier();
  } catch (error) { $("supplyMessage").textContent = error.message; } finally { updateSupplierLimit(); }
}

window.addEventListener("hashchange", () => location.reload());
$("loginForm").addEventListener("submit", login); $("logoutButton").addEventListener("click", logout);
$("refreshAdmin").addEventListener("click", loadAdmin); $("saveSettings").addEventListener("click", saveSettings); $("refreshAudit").addEventListener("click", loadAudit);
$("createSupplier").addEventListener("click", createSupplier); $("refreshSupplies").addEventListener("click", loadSupplies);
$("refreshSupplier").addEventListener("click", loadSupplier); $("supplyGroup").addEventListener("change", updateSupplierLimit); $("supplyTokens").addEventListener("input", updateSupplierLimit); $("supplyForm").addEventListener("submit", submitSupply);
configureLogin(); enterApp();
