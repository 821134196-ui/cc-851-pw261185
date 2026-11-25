/* Backup-chain timeline UI: all dynamic strings are inserted via textContent. */
(() => {
  "use strict";

  const $ = (selector) => document.querySelector(selector);
  const els = {
    repoMeta: $("#repoMeta"),
    refreshBtn: $("#refreshBtn"),
    resetDemoBtn: $("#resetDemoBtn"),
    atInput: $("#atInput"),
    slider: $("#secondSlider"),
    minLabel: $("#minLabel"),
    maxLabel: $("#maxLabel"),
    prev: $("#prevSecond"),
    next: $("#nextSecond"),
    restoreBtn: $("#restoreBtn"),
    spanBar: $("#spanBar"),
    resultSubtitle: $("#resultSubtitle"),
    resultStats: $("#resultStats"),
    banner: $("#recoveryBanner"),
    rowsTable: $("#rowsTable"),
    chainView: $("#chainView"),
    selectedSegmentLabel: $("#selectedSegmentLabel"),
    retainInput: $("#retainInput"),
    useFull2Btn: $("#useFull2Btn"),
    planBtn: $("#planBtn"),
    depPlan: $("#depPlan"),
    forceAck: $("#forceAck"),
    deleteSegmentBtn: $("#deleteSegmentBtn"),
    pruneBtn: $("#pruneBtn"),
    applyPruneBtn: $("#applyPruneBtn"),
    pruneOutput: $("#pruneOutput"),
    upsertForm: $("#upsertForm"),
    deleteRowForm: $("#deleteRowForm"),
    fullBtn: $("#fullBtn"),
    rollBtn: $("#rollBtn"),
    corruptBtn: $("#corruptBtn"),
    hideBtn: $("#hideBtn"),
    unhideBtn: $("#unhideBtn"),
    overlapSource: $("#overlapSource"),
    overlapTarget: $("#overlapTarget"),
    overlapBtn: $("#overlapBtn"),
    toast: $("#toast"),
  };

  const state = {
    timeline: null,
    selectedAt: null,
    selectedSegment: 1,
    restore: null,
    plan: null,
    restoreRequest: 0,
    planRequest: 0,
    toastTimer: null,
  };

  // ---------- utilities ----------

  function toast(message) {
    els.toast.textContent = message;
    els.toast.hidden = false;
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => {
      els.toast.hidden = true;
    }, 4200);
  }

  async function request(path, options = {}) {
    const init = { method: options.method || "GET", headers: {} };
    if (options.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.body);
    }
    const response = await fetch(path, init);
    let data = null;
    try {
      data = await response.json();
    } catch {
      data = null;
    }
    if (!response.ok) {
      const error = new Error(data?.error?.message || data?.plan?.recommendation || `HTTP ${response.status}`);
      error.data = data;
      throw error;
    }
    return data;
  }

  function fmt(ts) {
    if (ts === null || ts === undefined) return "--";
    return new Date(Number(ts) * 1000).toLocaleString("zh-CN", { hour12: false });
  }

  function fmtClock(ts) {
    if (ts === null || ts === undefined) return "--";
    return new Date(Number(ts) * 1000).toLocaleTimeString("zh-CN", { hour12: false });
  }

  function pad(n) {
    return String(n).padStart(2, "0");
  }

  function toInputValue(ts) {
    const d = new Date(Number(ts) * 1000);
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  }

  function fromInputValue(value) {
    if (!value) return null;
    const ms = new Date(value).getTime();
    return Number.isFinite(ms) ? Math.floor(ms / 1000) : null;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function addText(parent, tag, text, className) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    node.textContent = text ?? "";
    parent.appendChild(node);
    return node;
  }

  function statusLabel(status) {
    const labels = {
      ok: "正常",
      missing: "缺失",
      corrupt: "损坏",
      deleted: "已按计划删除",
      deleted_but_present: "删除标记/文件并存",
      unknown: "未知",
    };
    return labels[status] || status || "未知";
  }

  function segmentStatusClass(segment) {
    if (segment.status === "ok" && !segment.deleted) return "ok";
    if (segment.status === "deleted" || segment.deleted) return "deleted";
    if (["corrupt", "missing"].includes(segment.status)) return segment.status;
    return "error";
  }

  // ---------- data loading ----------

  function applyTimeline(timeline, { keepPoint = false } = {}) {
    state.timeline = timeline;
    if (!keepPoint || state.selectedAt === null) {
      state.selectedAt = timeline.newest_at;
    }
    if (state.selectedAt !== null) {
      state.selectedAt = Math.min(Math.max(state.selectedAt, timeline.oldest_at), timeline.newest_at);
    }
    renderAll();
  }

  async function loadTimeline({ restorePoint = true } = {}) {
    const data = await request("/api/timeline");
    applyTimeline(data.timeline);
    if (restorePoint && state.selectedAt !== null) {
      await loadRestore(state.selectedAt);
    }
  }

  async function loadRestore(at) {
    if (at === null || at === undefined) return;
    state.selectedAt = Number(at);
    updateTimeInputs();
    const requestId = ++state.restoreRequest;
    try {
      const data = await request(`/api/restore?at=${encodeURIComponent(state.selectedAt)}`);
      if (requestId !== state.restoreRequest) return;
      state.restore = data.result;
      renderRestore();
      renderChain();
    } catch (error) {
      if (requestId !== state.restoreRequest) return;
      toast(error.message);
    }
  }

  async function mutate(path, body, { selectNewest = false, restoreCurrent = true } = {}) {
    try {
      const data = await request(path, { method: "POST", body });
      if (data.timeline) applyTimeline(data.timeline, { keepPoint: !selectNewest });
      if (selectNewest && state.timeline) {
        state.selectedAt = state.timeline.newest_at;
        updateTimeInputs();
      }
      if (restoreCurrent && state.selectedAt !== null) await loadRestore(state.selectedAt);
      if (state.selectedSegment) loadPlan().catch(() => {});
      if (data.prune_plan) els.pruneOutput.hidden = false, (els.pruneOutput.textContent = JSON.stringify(data, null, 2));
      return data;
    } catch (error) {
      if (error.data?.plan) {
        state.plan = error.data.plan;
        renderPlan();
      }
      toast(error.message);
      throw error;
    }
  }

  // ---------- rendering ----------

  function renderAll() {
    renderMeta();
    updateTimeInputs();
    renderSpanBar();
    renderChain();
    renderSegmentSelects();
    if (state.selectedSegment) loadPlan().catch(() => {});
  }

  function renderMeta() {
    const t = state.timeline;
    if (!t) return;
    els.repoMeta.textContent = t.oldest_at
      ? `${t.seconds} 秒窗口 · ${t.fulls.length} 个完整备份 · ${t.segments.length} 个段${t.demo ? " · DEMO" : ""}`
      : "仓库尚未创建完整备份";
    if (!t.oldest_at) {
      els.minLabel.textContent = "--";
      els.maxLabel.textContent = "--";
      return;
    }
    els.slider.min = String(t.oldest_at);
    els.slider.max = String(t.newest_at);
    els.slider.value = String(state.selectedAt ?? t.newest_at);
    els.minLabel.textContent = fmt(t.oldest_at);
    els.maxLabel.textContent = fmt(t.newest_at);
  }

  function updateTimeInputs() {
    if (state.selectedAt !== null) {
      els.slider.value = String(state.selectedAt);
      els.atInput.value = toInputValue(state.selectedAt);
    }
  }

  function renderSpanBar() {
    clear(els.spanBar);
    const t = state.timeline;
    if (!t?.spans?.length || !t.seconds) {
      addText(els.spanBar, "span", "暂无时间线", "empty-table");
      return;
    }
    for (const span of t.spans) {
      const width = Math.max(0.8, (span.second_count / t.seconds) * 100);
      const button = document.createElement("button");
      button.type = "button";
      button.className = "span-block";
      if (!span.ok) button.classList.add(span.error_code === "SEGMENT_DELETED" ? "deleted" : "fault");
      else button.classList.add("ok");
      button.style.flex = `${span.second_count} 1 0`;
      const detail = span.ok
        ? `可恢复，基础 ${span.base_full_id}，段 ${span.segment_ids.join(", ") || "无"}`
        : `停止：${span.error?.code || "FAULT"} ${span.error?.message || ""}`;
      button.title = `${fmt(span.from)} – ${fmt(span.to)}（${span.second_count}秒）\n${detail}`;
      button.setAttribute("aria-label", button.title);
      button.addEventListener("click", () => loadRestore(span.from));
      els.spanBar.appendChild(button);
    }
  }

  function renderChain() {
    clear(els.chainView);
    const t = state.timeline;
    if (!t) return;
    const restore = state.restore;
    const usedSegmentIds = new Set(restore?.used_segments?.map((s) => s.id) || []);
    const baseFullId = restore?.base_full?.id;

    addText(els.chainView, "div", "完整备份", "chain-section-title");
    if (!t.fulls.length) addText(els.chainView, "p", "还没有完整备份", "hint");
    for (const full of t.fulls) {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "chain-item";
      if (full.id === baseFullId) item.classList.add("route");
      const row = addText(item, "div", "", "chain-title-row");
      addText(row, "strong", full.id);
      const pill = addText(row, "span", statusLabel(full.status), `pill ${full.status === "ok" ? "ok" : "corrupt"}`);
      pill.classList.add(full.status === "ok" ? "ok" : "corrupt");
      addText(item, "div", `${fmt(full.ts)} · LSN ≤ ${full.lsn} · ${full.row_count ?? 0} 行`, "chain-meta");
      item.title = "点击恢复到该完整备份时间";
      item.addEventListener("click", () => loadRestore(full.ts));
      els.chainView.appendChild(item);
    }

    addText(els.chainView, "div", "变更段（按 LSN 顺序）", "chain-section-title");
    for (const segment of t.segments) {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "chain-item";
      const cls = segmentStatusClass(segment);
      if (state.selectedSegment === segment.seq) item.classList.add("selected");
      if (usedSegmentIds.has(segment.id)) item.classList.add("route");
      if (["corrupt", "missing"].includes(segment.status)) item.classList.add("bad");
      if (segment.deleted || segment.status === "deleted") item.classList.add("deleted");

      const row = addText(item, "div", "", "chain-title-row");
      addText(row, "strong", segment.id);
      const pills = addText(row, "span", "", "chain-meta");
      if (segment.seq === t.manifest.active_segment_seq) {
        addText(pills, "span", "追加中", "pill active");
      }
      addText(pills, "span", statusLabel(segment.status), `pill ${cls}`);

      const lsn = segment.first_lsn ? `LSN ${segment.first_lsn}→${segment.last_lsn}` : "无有效记录";
      const time = segment.first_ts ? `${fmtClock(segment.first_ts)}→${fmtClock(segment.last_ts)}` : "--";
      addText(item, "div", `${lsn} · ${segment.record_count} 条有效记录 · ${time}`, "chain-meta");
      if (segment.error?.message) addText(item, "div", segment.error.message, "chain-meta");
      item.addEventListener("click", () => {
        state.selectedSegment = segment.seq;
        state.plan = null;
        renderChain();
        renderSegmentSelects();
        loadPlan();
      });
      els.chainView.appendChild(item);
    }
    updateSelectedSegmentLabel();
  }

  function updateSelectedSegmentLabel() {
    const segment = state.timeline?.segments?.find((s) => s.seq === state.selectedSegment);
    els.selectedSegmentLabel.textContent = segment
      ? `${segment.id} · ${statusLabel(segment.status)} · ${segment.record_count} 条有效记录`
      : "未选择段";
  }

  function renderSegmentSelects() {
    const segments = state.timeline?.segments || [];
    for (const select of [els.overlapSource, els.overlapTarget]) {
      const previous = select.value;
      clear(select);
      for (const segment of segments) {
        const option = document.createElement("option");
        option.value = String(segment.seq);
        option.textContent = segment.id;
        select.appendChild(option);
      }
      if (previous) select.value = previous;
    }
    if (!els.overlapSource.value && segments[0]) els.overlapSource.value = String(segments[0].seq);
    if (state.selectedSegment) els.overlapTarget.value = String(state.selectedSegment);
  }

  function renderRestore() {
    const r = state.restore;
    if (!r) return;
    els.resultSubtitle.textContent = `请求秒：${fmt(r.requested_at)}（${r.requested_at}）`;
    clear(els.resultStats);
    const stats = [
      [r.row_count, "实际行数"],
      [r.applied_records, "重放变更"],
      [r.last_trusted?.lsn ?? "—", "可信 LSN"],
    ];
    for (const [value, label] of stats) {
      const box = addText(els.resultStats, "div", "", "stat");
      addText(box, "strong", String(value));
      addText(box, "span", label);
    }

    clear(els.banner);
    if (r.ok) {
      els.banner.className = "banner good";
      els.banner.appendChild(document.createTextNode(`✓ 该秒恢复可信：已校验并恢复 ${r.row_count} 行。`));
      addText(els.banner, "small", `验证终点：${fmt(r.last_trusted?.at)} · ${r.last_trusted?.label || r.base_full?.id}`);
    } else {
      els.banner.className = "banner critical";
      const first = addText(els.banner, "div", `■ 恢复已停止：${r.error?.code || "FAULT"} — ${r.error?.message || "未知错误"}`);
      first.style.fontWeight = "800";
      const trusted = r.last_trusted;
      addText(
        els.banner,
        "small",
        `最后可信点：${trusted ? fmt(trusted.at) : "无"} · ${trusted?.label || "没有可作为前缀的完整备份"}。下表显示该可信前缀的实际行，而不是请求点的猜测状态。`
      );
      if (trusted && trusted.at !== r.requested_at) {
        const jump = document.createElement("button");
        jump.type = "button";
        jump.className = "btn primary jump";
        jump.textContent = "跳到最后可信秒并重新查看";
        jump.addEventListener("click", () => loadRestore(trusted.at));
        els.banner.appendChild(jump);
      } else if (trusted) {
        addText(els.banner, "small", "请求秒与最后可信秒相同；下表就是已校验前缀的实际行。修复故障后可点“恢复”重新校验。");
      }
    }

    renderRowsTable(r.rows || []);
  }

  function renderRowsTable(rows) {
    const thead = els.rowsTable.querySelector("thead");
    const tbody = els.rowsTable.querySelector("tbody");
    clear(thead);
    clear(tbody);
    if (!rows.length) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 20;
      td.className = "empty-table";
      td.textContent = "该可信点没有任何行（可能已全部删除，或尚未写入）。";
      tr.appendChild(td);
      tbody.appendChild(tr);
      return;
    }

    const preferred = ["id", "name", "email", "plan", "balance", "status"];
    const columns = [...preferred.filter((k) => rows.some((r) => k in r))];
    for (const row of rows) {
      for (const key of Object.keys(row)) if (!columns.includes(key)) columns.push(key);
    }
    const headRow = document.createElement("tr");
    for (const col of columns) {
      const th = document.createElement("th");
      th.scope = "col";
      th.textContent = col;
      headRow.appendChild(th);
    }
    thead.appendChild(headRow);

    for (const row of rows) {
      const tr = document.createElement("tr");
      for (const col of columns) {
        const td = document.createElement("td");
        const value = row[col];
        td.textContent = value === undefined || value === null ? "—" : typeof value === "object" ? JSON.stringify(value) : String(value);
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    }
  }

  function focusFaultSpan() {
    const fault = state.timeline?.spans?.find((span) => !span.ok);
    if (fault) return loadRestore(fault.from);
    toast("故障已注入，但没有影响当前时间线区间；它可能已被更新的完整备份绕过。");
  }

  // ---------- deletion planning ----------

  function retainEpoch() {
    return fromInputValue(els.retainInput.value);
  }

  async function loadPlan() {
    const segmentSeq = state.selectedSegment;
    if (!segmentSeq) return;
    const retain = retainEpoch();
    const params = new URLSearchParams({ segment: String(segmentSeq) });
    if (retain !== null) params.set("retain_from", String(retain));
    const requestId = ++state.planRequest;
    try {
      const data = await request(`/api/deletion-plan?${params}`);
      if (requestId !== state.planRequest || segmentSeq !== state.selectedSegment) return;
      state.plan = data.plan;
      renderPlan();
    } catch (error) {
      if (requestId !== state.planRequest || segmentSeq !== state.selectedSegment) return;
      toast(error.message);
    }
  }

  function appendRangeList(parent, ranges, className, prefix) {
    if (!ranges?.length) return;
    const list = addText(parent, "div", "", "range-list");
    for (const range of ranges) {
      addText(
        list,
        "div",
        `${prefix || ""}${fmt(range.from)} → ${fmt(range.to)} · ${range.second_count} 秒`,
        `range-chip ${className || ""}`
      );
    }
  }

  function planRecommendationText(p) {
    if (p.reason_code === "already_deleted" || p.already_deleted) {
      return "该段已删除；以下范围是其历史依赖点，访问这些秒会停在最后可信前缀。";
    }
    if (p.reason_code === "active_segment" || p.active) {
      return "这是当前追加段，不能直接删除；请先滚动到新段。";
    }
    if (p.retain_from !== null && p.retained_dependent_point_count > 0) {
      return "阻止删除：保留起点内仍有恢复点依赖该段。可调整保留起点，或显式强制确认。";
    }
    if (p.retain_from !== null) {
      return "可按保留策略删除：只有保留起点之前、将放弃的恢复点依赖它。";
    }
    if (p.dependent_point_count > 0) {
      return "阻止删除：仍有恢复点依赖该段。请设置保留起点，或勾选强制确认。";
    }
    if (p.safe_without_retention) {
      return "可安全删除：没有恢复点依赖该段。";
    }
    return p.recommendation || "删除前请以服务端重新计算的依赖计划为准。";
  }

  function renderPlan() {
    const p = state.plan;
    clear(els.depPlan);
    if (!p) return;
    const summary = addText(els.depPlan, "div", "", "plan-summary");
    const boxes = [
      [p.dependent_point_count, "总依赖恢复秒"],
      [p.retained_dependent_point_count, retainEpoch() === null ? "未设保留起点" : "保留期内阻塞秒"],
      [p.dependent_range_count, "连续依赖区间"],
    ];
    for (const [value, label] of boxes) {
      const box = addText(summary, "div", "", "plan-box");
      addText(box, "strong", String(value));
      addText(box, "span", label);
    }

    const recommendation = planRecommendationText(p);
    const blocked = p.active
      || (p.retain_from === null ? p.dependent_point_count > 0 : p.retained_dependent_point_count > 0);
    addText(els.depPlan, "div", recommendation, `recommendation ${blocked ? "block" : "safe"}`);

    addText(els.depPlan, "strong", "所有依赖该段的恢复区间");
    appendRangeList(els.depPlan, p.dependent_ranges, "");

    if (p.retain_from !== null) {
      addText(els.depPlan, "strong", "保留期内仍依赖（删除会被阻止）");
      appendRangeList(els.depPlan, p.retained_dependent_ranges, "block", "阻塞 · ");
      addText(els.depPlan, "strong", "保留期外将放弃的恢复点");
      appendRangeList(els.depPlan, p.expired_ranges, "expired", "过期 · ");
    }

    if (p.recovery_points && p.recovery_points.length <= 120) {
      const details = document.createElement("details");
      const summaryEl = document.createElement("summary");
      summaryEl.textContent = `精确恢复秒列表（${p.recovery_points.length}）`;
      details.appendChild(summaryEl);
      const list = addText(details, "div", "", "range-list");
      list.style.marginTop = "8px";
      for (const point of p.recovery_points) {
        addText(list, "span", fmt(point.at), "range-chip");
      }
      els.depPlan.appendChild(details);
    }
  }

  async function deleteSelected() {
    if (!state.selectedSegment) return;
    const retain = retainEpoch();
    const acknowledged = els.forceAck.checked;
    const plan = state.plan?.segment_seq === state.selectedSegment ? state.plan : null;
    if (plan?.dependent_point_count > 0 && retain === null && !acknowledged) {
      toast("存在依赖恢复点；请勾选强制确认，或设置 retain_from 保留起点。");
      return;
    }
    if (retain !== null && plan?.retained_dependent_point_count > 0 && !acknowledged) {
      toast("仍有保留期内恢复点依赖；如确需强制删除，请勾选确认。");
      return;
    }
    try {
      await mutate("/api/delete-segment", {
        segment: state.selectedSegment,
        retain_from: retain,
        force: acknowledged,
        acknowledge_dependents: acknowledged,
      });
      toast("段已删除；时间线已更新，受影响区间会在恢复时停在最后可信点。");
      await loadPlan();
    } catch {
      /* mutate already surfaced the blocked plan */
    }
  }

  async function prune(apply) {
    const retain = retainEpoch();
    if (retain === null) {
      toast("请先选择 retain_from 保留起点。");
      return;
    }
    try {
      const data = await mutate("/api/prune", { retain_from: retain, apply });
      els.pruneOutput.hidden = false;
      els.pruneOutput.textContent = JSON.stringify(apply ? data : data.prune_plan, null, 2);
      toast(apply ? "GC 已执行" : `预演完成：${data.prune_plan.candidate_count} 个可删段`);
    } catch {
      /* surfaced */
    }
  }

  // ---------- events ----------

  els.refreshBtn.addEventListener("click", () => {
    loadTimeline().catch((error) => toast(`刷新时间线失败：${error.message}`));
  });
  els.slider.addEventListener("input", () => loadRestore(Number(els.slider.value)));
  els.prev.addEventListener("click", () => state.selectedAt !== null && loadRestore(state.selectedAt - 1));
  els.next.addEventListener("click", () => state.selectedAt !== null && loadRestore(state.selectedAt + 1));
  els.restoreBtn.addEventListener("click", () => {
    const at = fromInputValue(els.atInput.value);
    if (at === null) toast("请选择有效的秒级时间。");
    else loadRestore(at);
  });
  els.atInput.addEventListener("change", () => {
    const at = fromInputValue(els.atInput.value);
    if (at !== null) loadRestore(at);
  });

  els.planBtn.addEventListener("click", loadPlan);
  els.retainInput.addEventListener("change", loadPlan);
  els.deleteSegmentBtn.addEventListener("click", deleteSelected);
  els.pruneBtn.addEventListener("click", () => prune(false));
  els.applyPruneBtn.addEventListener("click", () => prune(true));
  els.useFull2Btn.addEventListener("click", () => {
    const secondFull = state.timeline?.fulls?.[1];
    if (!secondFull) return toast("当前仓库没有第 2 个完整备份。");
    els.retainInput.value = toInputValue(secondFull.ts);
    loadPlan();
  });

  els.upsertForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(els.upsertForm);
    const id = String(form.get("id") || "").trim();
    if (!id) return toast("ID 必填");
    const balanceText = String(form.get("balance") || "").trim();
    const row = {
      name: String(form.get("name") || ""),
      plan: String(form.get("plan") || ""),
      balance: balanceText === "" ? null : Number(balanceText),
    };
    try {
      await mutate("/api/changes", { op: "upsert", id, row }, { selectNewest: true });
      toast(`已追加 ${id} 的 upsert 变更。`);
    } catch { /* surfaced */ }
  });

  els.deleteRowForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(els.deleteRowForm);
    const id = String(form.get("id") || "").trim();
    if (!id) return toast("请输入要删除的行 ID");
    try {
      await mutate("/api/changes", { op: "delete", id }, { selectNewest: true });
      toast(`已追加 ${id} 的删除变更。`);
    } catch { /* surfaced */ }
  });

  els.fullBtn.addEventListener("click", async () => {
    try {
      await mutate("/api/fulls", {}, { selectNewest: true });
      toast("完整备份已创建，并在需要时滚动到新变更段。");
    } catch { /* surfaced */ }
  });

  els.rollBtn.addEventListener("click", async () => {
    try {
      const data = await mutate("/api/roll", {});
      state.selectedSegment = data.segment.seq;
      state.plan = null;
      renderChain();
      renderSegmentSelects();
      await loadPlan();
      toast(`已滚动到 ${data.segment.id}。`);
    } catch { /* surfaced */ }
  });

  els.corruptBtn.addEventListener("click", async () => {
    try {
      await mutate("/api/faults/corrupt", { segment: state.selectedSegment, mode: "checksum" });
      toast(`已向所选段追加校验错误；正在跳到受影响区间。`);
      await focusFaultSpan();
    } catch { /* surfaced */ }
  });

  els.hideBtn.addEventListener("click", async () => {
    try {
      await mutate("/api/faults/hide", { segment: state.selectedSegment });
      toast("段文件已移走；正在跳到第一个受影响秒。");
      await focusFaultSpan();
    } catch { /* surfaced */ }
  });

  els.unhideBtn.addEventListener("click", async () => {
    try {
      await mutate("/api/faults/unhide", { segment: state.selectedSegment });
      toast("段文件已恢复。");
    } catch { /* surfaced */ }
  });

  els.overlapBtn.addEventListener("click", async () => {
    const source = Number(els.overlapSource.value);
    const target = Number(els.overlapTarget.value);
    if (!source || !target) return toast("请选择源段和目标段。");
    if (source === target) return toast("源段和目标段必须不同。");
    try {
      await mutate("/api/faults/overlap", { source, target });
      state.selectedSegment = target;
      state.plan = null;
      renderChain();
      renderSegmentSelects();
      await loadPlan();
      await focusFaultSpan();
      toast("已制造 LSN 重叠；恢复到重叠之后的秒会停止。");
    } catch { /* surfaced */ }
  });

  els.resetDemoBtn.addEventListener("click", async () => {
    try {
      await request("/api/demo/reset", { method: "POST", body: {} });
      state.selectedAt = null;
      state.selectedSegment = 1;
      state.plan = null;
      els.forceAck.checked = false;
      els.retainInput.value = "";
      els.pruneOutput.hidden = true;
      await loadTimeline();
      toast("演示仓库已重置。");
    } catch (error) {
      toast(error.message);
    }
  });

  loadTimeline().catch((error) => {
    toast(`加载时间线失败：${error.message}`);
    els.repoMeta.textContent = "仓库不可用";
  });
})();
