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
  $("loginDescription").textContent = supplier ? "供应商只能看到当前缺口，无法访问账号明细和管理密钥。" : "管理密钥只保存在服务端，浏览器仅获得安全会话。";
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
  await loadAudit();
}

function renderAdmin() {
  const data = state.bootstrap;
  const triggered = data.evaluations.filter((item) => item.triggered).length;
  const usable = data.evaluations.reduce((sum, item) => sum + item.usable_count, 0);
  const recommended = data.evaluations.reduce((sum, item) => sum + item.recommended_count, 0);
  $("adminMetrics").innerHTML = [
    [data.account_count, "全部账号", data.base_url],
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
      trigger_on_remaining_7d: row.querySelector(".trigger-remaining").checked, supplier_note: row.querySelector(".supplier-note").value.trim()
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

async function loadSupplier() {
  const data = await api("/api/supplier/demand");
  state.demands = data.demands;
  hide("adminView"); show("supplierView");
  $("connectionState").textContent = "供应商会话";
  $("supplierModeText").textContent = data.direct_import_enabled ? "管理员已开启直接补号。" : "管理员尚未开启直接补号，目前只能查看需求。";
  $("demandGrid").innerHTML = data.demands.length ? data.demands.map((item) => `<article class="demand-card"><div class="demand-top"><span class="group-name"><i class="group-dot" style="--group-color:${esc(item.color || "#777")}"></i>${esc(item.group_name)}</span><span class="state-badge ${item.accepting ? "triggered" : "disabled"}">${item.accepting ? "可提交" : "暂不接收"}</span></div><div class="demand-number">${item.needed}</div><span class="subtext">建议补充账号</span><ul>${(item.reasons || []).map((reason) => `<li>${esc(reason)}</li>`).join("")}</ul>${item.note ? `<p class="subtext">备注：${esc(item.note)}</p>` : ""}</article>`).join("") : `<div class="empty">当前没有已启用的补号策略</div>`;
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
    $("supplyTokens").value = ""; $("supplyMessage").textContent = `成功提交 ${result.submitted} 个账号`;
    await loadSupplier();
  } catch (error) { $("supplyMessage").textContent = error.message; } finally { updateSupplierLimit(); }
}

window.addEventListener("hashchange", () => location.reload());
$("loginForm").addEventListener("submit", login); $("logoutButton").addEventListener("click", logout);
$("refreshAdmin").addEventListener("click", loadAdmin); $("saveSettings").addEventListener("click", saveSettings); $("refreshAudit").addEventListener("click", loadAudit);
$("refreshSupplier").addEventListener("click", loadSupplier); $("supplyGroup").addEventListener("change", updateSupplierLimit); $("supplyTokens").addEventListener("input", updateSupplierLimit); $("supplyForm").addEventListener("submit", submitSupply);
configureLogin(); enterApp();
