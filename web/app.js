const $ = (id) => document.getElementById(id);
const state = { role: location.hash === "#supplier" ? "supplier" : "admin", bootstrap: null, demands: [], suppliers: [], directImportEnabled: false, lastSupplierKey: "" };

async function api(path, options = {}) {
  const response = await fetch(path, { credentials: "same-origin", ...options, headers: { "Content-Type": "application/json", ...(options.headers || {}) } });
  let data = {};
  try { data = await response.json(); } catch (_) {}
  if (!response.ok) { const error = new Error(data.error || `请求失败 (${response.status})`); error.status = response.status; throw error; }
  return data;
}

function esc(value) { return String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char])); }
function pct(value) { return value == null ? "暂无样本" : `${Number(value).toFixed(1)}%`; }
function durationText(minutes) {
  if (minutes == null) return "待验活";
  const total = Math.max(0, Number(minutes) || 0);
  const hours = Math.floor(total / 60); const remainder = total % 60;
  return hours ? `${hours} 小时 ${remainder} 分钟` : `${remainder} 分钟`;
}
function show(id) { $(id).classList.remove("hidden"); }
function hide(id) { $(id).classList.add("hidden"); }

async function copyPlainText(value) {
  try { await navigator.clipboard.writeText(value); return true; }
  catch (_) {
    const area = document.createElement("textarea");
    area.value = value; area.setAttribute("readonly", "");
    area.style.position = "fixed"; area.style.opacity = "0";
    document.body.appendChild(area); area.select();
    const copied = document.execCommand("copy"); area.remove();
    return copied;
  }
}

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
  await loadSuppliers();
  await Promise.all([loadAudit(), loadSupplies()]);
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
  $("accountHealthInterval").value = global.account_health_interval_minutes;
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
      account_health_interval_minutes: Number($("accountHealthInterval").value),
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
  const query = new URLSearchParams();
  if ($("auditFilterSupplier").value) query.set("supplier_id", $("auditFilterSupplier").value);
  if ($("auditFilterRole").value) query.set("role", $("auditFilterRole").value);
  if ($("auditFilterEvent").value.trim()) query.set("event", $("auditFilterEvent").value.trim());
  if ($("auditFilterFrom").value) query.set("date_from", $("auditFilterFrom").value);
  if ($("auditFilterTo").value) query.set("date_to", $("auditFilterTo").value);
  const data = await api(`/api/admin/audit${query.size ? `?${query}` : ""}`);
  const supplierNames = new Map(state.suppliers.map((item) => [Number(item.id), item.name]));
  $("auditList").innerHTML = data.entries.length ? data.entries.map((entry) => `<div class="audit-row"><time>${new Date(entry.time).toLocaleString()}</time><span>${esc(entry.event)}</span><div>${esc(entry.message)}${entry.supplier_id ? `<small class="subtext">供应商：${esc(supplierNames.get(Number(entry.supplier_id)) || `已删除 #${entry.supplier_id}`)}</small>` : ""}</div><strong>${entry.count ? `${entry.count} 个` : ""}</strong></div>`).join("") : `<div class="empty">没有符合筛选条件的审计记录</div>`;
}

async function loadSuppliers() {
  const data = await api("/api/admin/suppliers");
  state.suppliers = data.suppliers;
  $("supplierRows").innerHTML = data.suppliers.length ? data.suppliers.map((item) => `<tr><td><strong>${esc(item.name)}</strong><span class="subtext">ID ${item.id} · ${new Date(item.created_at).toLocaleString()}</span></td><td><code>${esc(item.key_prefix)}…</code></td><td><span class="state-badge ${item.enabled ? "" : "disabled"}">${item.enabled ? "启用" : "已禁用"}</span></td><td>${item.accepted_count}</td><td>${item.last_used_at ? new Date(item.last_used_at).toLocaleString() : "从未"}</td><td><div class="heading-actions"><button class="button ghost supplier-rotate" type="button" data-id="${item.id}" data-name="${esc(item.name)}">重置并复制</button><button class="button ghost supplier-toggle" type="button" data-id="${item.id}" data-enabled="${item.enabled}">${item.enabled ? "禁用" : "启用"}</button><button class="button ghost supplier-delete" type="button" data-id="${item.id}" data-name="${esc(item.name)}">删除</button></div></td></tr>`).join("") : `<tr><td colspan="6" class="empty">尚未创建供应商</td></tr>`;
  document.querySelectorAll(".supplier-rotate").forEach((button) => button.addEventListener("click", async () => {
    if (!window.confirm(`确定重置供应商“${button.dataset.name}”的密钥？旧密钥和现有登录会立即失效。`)) return;
    button.disabled = true; button.textContent = "重置中…";
    try {
      const result = await api(`/api/admin/suppliers/${button.dataset.id}/rotate-key`, { method: "POST", body: "{}" });
      state.lastSupplierKey = result.key;
      $("supplierKeyReveal").textContent = `新密钥（请交给供应商）：${result.key}`;
      $("copySupplierKey").disabled = false;
      $("copySupplierKey").textContent = "复制密钥";
      await copySupplierKey();
      await loadSuppliers();
    } catch (error) {
      $("supplierKeyReveal").textContent = error.message;
      button.disabled = false; button.textContent = "重置并复制";
    }
  }));
  document.querySelectorAll(".supplier-toggle").forEach((button) => button.addEventListener("click", async () => {
    await api(`/api/admin/suppliers/${button.dataset.id}`, { method: "PATCH", body: JSON.stringify({ enabled: button.dataset.enabled !== "true" }) });
    await loadSuppliers();
  }));
  document.querySelectorAll(".supplier-delete").forEach((button) => button.addEventListener("click", async () => {
    if (!window.confirm(`确定删除供应商“${button.dataset.name}”？该密钥将立即失效，历史补号记录会保留。`)) return;
    await api(`/api/admin/suppliers/${button.dataset.id}`, { method: "DELETE", body: "{}" });
    await loadSuppliers();
  }));
  for (const id of ["supplyFilterSupplier", "auditFilterSupplier"]) {
    const select = $(id); const previous = select.value;
    select.innerHTML = `<option value="">全部供应商</option>${data.suppliers.map((item) => `<option value="${item.id}">${esc(item.name)}</option>`).join("")}`;
    if ([...select.options].some((option) => option.value === previous)) select.value = previous;
  }
}

async function createSupplier() {
  const name = $("newSupplierName").value.trim();
  if (!name) return;
  const button = $("createSupplier");
  button.disabled = true; button.textContent = "创建中…";
  try {
    const result = await api("/api/admin/suppliers", { method: "POST", body: JSON.stringify({ name }) });
    state.lastSupplierKey = result.key;
    $("supplierKeyReveal").textContent = `请立即复制（只显示一次）：${result.key}`;
    $("copySupplierKey").disabled = false;
    $("copySupplierKey").textContent = "复制密钥";
    $("newSupplierName").value = "";
    await loadSuppliers();
  } catch (error) { $("supplierKeyReveal").textContent = error.message; }
  finally { button.disabled = false; button.textContent = "创建供应商密钥"; }
}

async function copySupplierKey() {
  if (!state.lastSupplierKey) return;
  try {
    await navigator.clipboard.writeText(state.lastSupplierKey);
    $("copySupplierKey").textContent = "已复制";
  } catch (_) {
    const area = document.createElement("textarea");
    area.value = state.lastSupplierKey; area.setAttribute("readonly", "");
    area.style.position = "fixed"; area.style.opacity = "0";
    document.body.appendChild(area); area.select();
    const copied = document.execCommand("copy"); area.remove();
    if (copied) $("copySupplierKey").textContent = "已复制";
    else $("supplierKeyReveal").textContent = `复制失败，请手动复制：${state.lastSupplierKey}`;
  }
}

async function loadSupplies() {
  const query = new URLSearchParams();
  if ($("supplyFilterSupplier").value) query.set("supplier_id", $("supplyFilterSupplier").value);
  if ($("supplyFilterStatus").value) query.set("status", $("supplyFilterStatus").value);
  if ($("supplyFilterHealth").value) query.set("health", $("supplyFilterHealth").value);
  if ($("supplyFilterFrom").value) query.set("date_from", $("supplyFilterFrom").value);
  if ($("supplyFilterTo").value) query.set("date_to", $("supplyFilterTo").value);
  const data = await api(`/api/admin/supplies${query.size ? `?${query}` : ""}`);
  const groupNames = new Map((state.bootstrap?.groups || []).map((item) => [Number(item.id), item.name]));
  $("suppliedAccountRows").innerHTML = data.accounts.length ? data.accounts.map((item) => {
    const health = item.status !== "accepted" ? `<span class="subtext">不适用</span>` : item.health_alive === true
      ? `<span class="status-ok">存活</span><span class="subtext">${esc(item.health_status || "active")} · ${new Date(item.health_checked_at).toLocaleString()}</span>`
      : item.health_alive === false
        ? `<span class="status-bad">${item.health_status === "not_found" ? "上游未找到" : "不可用"}</span><span class="subtext">${esc(item.health_status || "unknown")} · ${new Date(item.health_checked_at).toLocaleString()}</span>`
        : `<span class="subtext">等待首次定时验活</span>`;
    const aliveDuration = item.alive_minutes != null ? durationText(item.alive_minutes) : item.health_checked_at ? "未记录" : "待验活";
    return `<tr><td>${new Date(item.created_at).toLocaleString()}</td><td>${esc(item.supplier_name)}</td><td>${item.upstream_account_id ? `ID ${item.upstream_account_id}` : "未创建"}<span class="subtext">${esc(item.account_name || "")}</span></td><td>${esc(item.email || "-")}</td><td><span class="${item.status === "accepted" ? "status-ok" : "status-bad"}">${item.status === "accepted" ? "通过" : "拒绝"}</span>${item.error_message ? `<span class="subtext">${esc(item.error_message)}</span>` : ""}</td><td>${health}</td><td>${item.status === "accepted" ? esc(aliveDuration) : "-"}</td><td>${item.target_group_ids.map((id) => esc(groupNames.get(Number(id)) || `ID ${id}`)).join(" + ")}</td></tr>`;
  }).join("") : `<tr><td colspan="8" class="empty">没有符合筛选条件的补号账号</td></tr>`;
}

async function checkSuppliesHealth() {
  const button = $("checkSuppliesHealth");
  button.disabled = true; button.textContent = "验活中…"; $("supplyHealthState").textContent = "正在批量比对";
  try {
    const result = await api("/api/admin/supplies/health-check", { method: "POST", body: "{}" });
    $("supplyHealthState").textContent = `已检查 ${result.checked} · 存活 ${result.alive} · 不可用 ${result.unavailable + result.missing}`;
    await Promise.all([loadSupplies(), loadAudit()]);
  } catch (error) { $("supplyHealthState").textContent = error.message; }
  finally { button.disabled = false; button.textContent = "立即验活"; }
}

function resetSupplyFilters() {
  for (const id of ["supplyFilterSupplier", "supplyFilterStatus", "supplyFilterHealth", "supplyFilterFrom", "supplyFilterTo"]) $(id).value = "";
  loadSupplies();
}

function resetAuditFilters() {
  for (const id of ["auditFilterSupplier", "auditFilterRole", "auditFilterEvent", "auditFilterFrom", "auditFilterTo"]) $(id).value = "";
  loadAudit();
}

async function loadSupplier() {
  const data = await api("/api/supplier/demand");
  const previousGroupId = Number($("supplyGroup").value || 0);
  state.demands = data.demands;
  state.directImportEnabled = data.direct_import_enabled;
  hide("adminView"); show("supplierView");
  $("connectionState").textContent = "供应商会话";
  const snapshotTime = data.updated_at ? new Date(data.updated_at).toLocaleString() : "等待首次同步";
  $("supplierModeText").textContent = `${data.direct_import_enabled ? "管理员已开启直接补号。" : "管理员尚未开启直接补号，目前只能查看需求。"} 账号快照：${snapshotTime}`;
  $("demandGrid").innerHTML = data.demands.length ? data.demands.map((item) => `<article class="demand-card"><div class="demand-top"><span class="group-name"><i class="group-dot" style="--group-color:${esc(item.color || "#777")}"></i>检查：${esc(item.group_name)}</span><span class="state-badge ${item.accepting ? "triggered" : "disabled"}">${esc(item.status_text || (item.accepting ? "可提交" : "暂不可提交"))}</span></div><div class="demand-number">${item.needed}</div><span class="subtext">需要补充的存活账号</span><ul>${(item.reasons || []).map((reason) => `<li>${esc(reason)}</li>`).join("")}</ul>${item.note ? `<p class="subtext">备注：${esc(item.note)}</p>` : ""}</article>`).join("") : `<div class="empty">当前没有已启用的补号策略</div>`;
  $("supplyGroup").innerHTML = data.demands.map((item) => `<option value="${item.group_id}">${esc(item.group_name)} · 需要 ${item.needed} · ${esc(item.status_text || (item.accepting ? "可提交" : "暂不可提交"))}</option>`).join("");
  const selected = data.demands.find((item) => item.group_id === previousGroupId) || data.demands.find((item) => item.accepting) || data.demands[0];
  if (selected) $("supplyGroup").value = String(selected.group_id);
  $("supplyGroup").disabled = data.demands.length === 0;
  renderSupplierApiGuide();
  updateSupplierLimit();
}

function renderSupplierApiGuide() {
  const base = location.origin;
  $("supplierDemandCurl").textContent = [
    "curl --request GET \\",
    `  --url '${base}/api/supplier/v1/demand' \\`,
    "  --header 'X-Supplier-Key: sup_xxx'"
  ].join("\n");
  $("supplierSupplyCurl").textContent = [
    "curl --request POST \\",
    `  --url '${base}/api/supplier/v1/supply' \\`,
    "  --header 'X-Supplier-Key: sup_xxx' \\",
    "  --header 'Content-Type: application/json' \\",
    `  --data '{"group_id":1,"refresh_tokens":"rt_one\\nrt_two","proxy_url":""}'`
  ].join("\n");
}

async function copyApiExample(event) {
  const button = event.currentTarget;
  const target = $(button.dataset.copyTarget);
  if (!target) return;
  const original = button.textContent;
  button.textContent = await copyPlainText(target.textContent) ? "已复制" : "复制失败";
  window.setTimeout(() => { button.textContent = original; }, 1600);
}

function updateSupplierLimit() {
  const selected = state.demands.find((item) => item.group_id === Number($("supplyGroup").value));
  const tokenCount = $("supplyTokens").value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean).length;
  $("supplierTokenCount").textContent = `${tokenCount} 个 Token`;
  $("supplierLimit").textContent = selected?.accepting ? `当前最多接收 ${selected.needed} 个` : selected ? `${selected.status_text || "暂不可提交"}：${(selected.reasons || []).join("；") || "策略未触发"}` : "当前没有检查策略";
  $("submitSupply").disabled = !state.directImportEnabled || !selected?.accepting;
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
$("createSupplier").addEventListener("click", createSupplier); $("copySupplierKey").addEventListener("click", copySupplierKey); $("refreshSupplies").addEventListener("click", loadSupplies);
$("checkSuppliesHealth").addEventListener("click", checkSuppliesHealth);
$("applySupplyFilters").addEventListener("click", loadSupplies); $("resetSupplyFilters").addEventListener("click", resetSupplyFilters);
$("applyAuditFilters").addEventListener("click", loadAudit); $("resetAuditFilters").addEventListener("click", resetAuditFilters);
$("refreshSupplier").addEventListener("click", loadSupplier); $("supplyGroup").addEventListener("change", updateSupplierLimit); $("supplyTokens").addEventListener("input", updateSupplierLimit); $("supplyForm").addEventListener("submit", submitSupply);
document.querySelectorAll(".api-copy").forEach((button) => button.addEventListener("click", copyApiExample));
configureLogin(); enterApp();
