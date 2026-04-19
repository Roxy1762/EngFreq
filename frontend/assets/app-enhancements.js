(function () {
  let APP_CONFIG = null;
  const state = { rawOrder: "desc", lemmaOrder: "desc", familyOrder: "desc", vocabKey: "score", vocabOrder: "desc" };
  const originalShowApp = typeof showApp === "function" ? showApp : null;
  const originalSortData = typeof sortData === "function" ? sortData : null;

  function escAttr(value) {
    return String(value || "").replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  function setValue(id, value, isCheckbox) {
    const element = document.getElementById(id);
    if (!element || value === undefined || value === null) return;
    if (isCheckbox) element.checked = Boolean(value);
    else element.value = value;
  }

  function providerOptions(selected) {
    const providers = (APP_CONFIG && APP_CONFIG.providers) || ["claude", "free_dict", "merriam_webster"];
    const labels = { claude: "Claude AI", free_dict: "Free Dictionary", merriam_webster: "Merriam-Webster" };
    return `<option value="">默认提供源</option>` + providers.map((name) => `<option value="${name}"${selected === name ? " selected" : ""}>${labels[name] || name}</option>`).join("");
  }

  function applyConfig() {
    if (!APP_CONFIG) return;
    const defaults = APP_CONFIG.defaults || {};
    const analysis = defaults.analysis || {};
    ensureAnalysisControls();
    ensureSortControls();
    ensureAdminUi();
    setValue("min_word_length", analysis.min_word_length);
    setValue("top_n", analysis.top_n);
    setValue("vocab-top-n", analysis.top_n);
    setValue("filter_stopwords", analysis.filter_stopwords, true);
    setValue("keep_proper_nouns", analysis.keep_proper_nouns, true);
    setValue("filter_numbers", analysis.filter_numbers, true);
    setValue("filter_basic_words", analysis.filter_basic_words, true);
    setValue("basic_words_threshold", analysis.basic_words_threshold);
    setValue("weight_body", analysis.weight_body);
    setValue("weight_stem", analysis.weight_stem);
    setValue("weight_option", analysis.weight_option);
    const provider = APP_CONFIG.provider_default || defaults.vocab_provider || "";
    const analyzeProvider = document.getElementById("vocab_provider");
    const resultProvider = document.getElementById("vocab-provider-select");
    if (analyzeProvider) analyzeProvider.innerHTML = providerOptions(analyzeProvider.value || provider);
    if (resultProvider) resultProvider.innerHTML = providerOptions(resultProvider.value || provider);
    updateBasicWordHint();
  }

  async function loadConfig(force) {
    if (APP_CONFIG && !force) return APP_CONFIG;
    try {
      const response = await fetch("/api/config");
      if (!response.ok) throw new Error("配置加载失败");
      APP_CONFIG = await response.json();
      applyConfig();
    } catch (error) {
      console.warn(error);
    }
    return APP_CONFIG;
  }

  function ensureAnalysisControls() {
    const grid = document.querySelector(".options-grid");
    if (!grid || document.getElementById("basic-words-group")) return;
    grid.insertAdjacentHTML("beforeend", `
      <div class="opt-group toggle-row" id="basic-words-group">
        <label>进阶过滤</label>
        <label class="toggle"><input type="checkbox" id="filter_basic_words" /> 过滤基础词汇</label>
        <div class="enhanced-inline">
          <span style="font-size:.82rem;color:var(--muted)">Zipf 阈值</span>
          <input type="number" id="basic_words_threshold" min="3.0" max="8.0" step="0.1" style="width:90px" />
        </div>
        <div class="enhanced-note" id="basic-words-hint"></div>
      </div>
    `);
    document.getElementById("filter_basic_words").addEventListener("change", updateBasicWordHint);
    document.getElementById("basic_words_threshold").addEventListener("input", updateBasicWordHint);
  }

  function updateBasicWordHint() {
    const checkbox = document.getElementById("filter_basic_words");
    const threshold = document.getElementById("basic_words_threshold");
    const hint = document.getElementById("basic-words-hint");
    if (!checkbox || !threshold || !hint) return;
    hint.textContent = checkbox.checked
      ? `已启用基础词过滤，当前优先排除 Zipf 频率 >= ${Number(threshold.value || 5.7).toFixed(1)} 的高频词。`
      : "开启后会优先过滤基础词，让词汇表更聚焦真正需要学习的词。";
  }

  function addOrderSelect(selector, elementId, value, onChange) {
    const row = document.querySelector(selector);
    if (!row || document.getElementById(elementId)) return;
    row.insertAdjacentHTML("beforeend", `
      <label>顺序</label>
      <select id="${elementId}" class="sort-order-select">
        <option value="desc"${value === "desc" ? " selected" : ""}>倒序</option>
        <option value="asc"${value === "asc" ? " selected" : ""}>正序</option>
      </select>
    `);
    document.getElementById(elementId).addEventListener("change", (event) => onChange(event.target.value));
  }

  function ensureSortControls() {
    addOrderSelect("#tab-raw .sort-row", "raw-order", state.rawOrder, (value) => { state.rawOrder = value; renderRaw(); });
    addOrderSelect("#tab-lemma .sort-row", "lemma-order", state.lemmaOrder, (value) => { state.lemmaOrder = value; renderLemma(); });
    addOrderSelect("#tab-family .sort-row", "family-order", state.familyOrder, (value) => { state.familyOrder = value; renderFamily(); });
    const vocabPanel = document.getElementById("tab-vocab");
    if (vocabPanel && !document.getElementById("vocab-toolbar-enhanced")) {
      vocabPanel.insertAdjacentHTML("afterbegin", `
        <div class="vocab-toolbar" id="vocab-toolbar-enhanced">
          <label style="font-size:.82rem;font-weight:700;color:var(--muted)">词汇排序</label>
          <select id="vocab-sort-key" class="vocab-sort-select">
            <option value="score">学习优先级</option>
            <option value="total">总频次</option>
            <option value="headword">字母顺序</option>
          </select>
          <select id="vocab-sort-order" class="sort-order-select">
            <option value="desc">倒序</option>
            <option value="asc">正序</option>
          </select>
        </div>
      `);
      document.getElementById("vocab-sort-key").addEventListener("change", (event) => { state.vocabKey = event.target.value; renderVocab((resultData && resultData.vocab_table) || []); });
      document.getElementById("vocab-sort-order").addEventListener("change", (event) => { state.vocabOrder = event.target.value; renderVocab((resultData && resultData.vocab_table) || []); });
    }
  }

  function sortWithOrder(rows, key, order) {
    let result = originalSortData ? originalSortData(rows, key) : rows.slice();
    if (order === "asc") result = result.slice().reverse();
    return result;
  }

  function canEditVocab() {
    return Boolean(CURRENT_USER && currentTaskId && document.getElementById("main-app").style.display !== "none");
  }

  window.vocabCardHtml = function (entry) {
    const action = canEditVocab()
      ? `<button class="vocab-delete-btn" onclick="deleteCurrentVocabWord('${escAttr(entry.headword || entry.lemma)}')">删除</button>`
      : "";
    return `<div class="vocab-card">
      <div class="vocab-card-top">
        <div>
          <div class="hw">${esc(entry.headword)}</div>
          <div class="meta">${esc(entry.pos || "")}${entry.family ? " · 词族: " + esc(entry.family) : ""} · 来源: ${esc(entry.source || "")}</div>
        </div>
        <div class="vocab-card-actions">${action}</div>
      </div>
      ${entry.chinese_meaning ? `<div class="cn">${esc(entry.chinese_meaning)}</div>` : ""}
      ${entry.english_definition ? `<div class="en">${esc(entry.english_definition)}</div>` : ""}
      ${entry.example_sentence ? `<div class="ex">${esc(entry.example_sentence)}</div>` : ""}
      ${entry.notes ? `<div class="note">提示: ${esc(entry.notes)}</div>` : ""}
      <div class="counts">
        <span class="c-badge">正文 ${entry.body_count}</span>
        <span class="c-badge">题干 ${entry.stem_count}</span>
        <span class="c-badge" style="background:#ffedd5;color:#c2410c">选项 ${entry.option_count}</span>
        <span class="c-badge">总计 ${entry.total_count}</span>
        <span class="c-badge" style="background:#dbeafe;color:#1d4ed8">分数 ${entry.score}</span>
      </div>
    </div>`;
  };

  function sortVocab(vocab) {
    const rows = vocab.slice().sort((a, b) => {
      if (state.vocabKey === "headword") return String(a.headword || "").localeCompare(String(b.headword || ""));
      if (state.vocabKey === "total") return (b.total_count || 0) - (a.total_count || 0) || (b.score || 0) - (a.score || 0);
      return (b.score || 0) - (a.score || 0) || (b.total_count || 0) - (a.total_count || 0);
    });
    return state.vocabOrder === "asc" ? rows.reverse() : rows;
  }

  window.renderRaw = function (sortKey) {
    if (sortKey) rawSortKey = sortKey;
    if (!resultData) return;
    const query = (document.getElementById("raw-search") || { value: "" }).value.toLowerCase();
    let rows = sortWithOrder(resultData.word_table || [], rawSortKey, state.rawOrder);
    if (query) rows = rows.filter((row) => row.surface.includes(query) || row.lemma.includes(query));
    document.getElementById("raw-tbody").innerHTML = rows.slice(0, 500).map((row, index) => `
      <tr>
        <td>${index + 1}</td><td><b>${esc(row.surface)}</b></td><td>${esc(row.lemma)}</td>
        <td>${esc(row.pos)}</td><td><small>${esc(row.family_id || "")}</small></td>
        <td class="num">${row.body_count}</td><td class="num">${row.stem_count}</td>
        <td class="num"><b style="color:var(--warn)">${row.option_count}</b></td>
        <td class="num">${row.total_count}</td><td class="num score-cell">${row.score}</td>
      </tr>`).join("");
  };

  window.renderLemma = function (sortKey) {
    if (sortKey) lemmaSortKey = sortKey;
    if (!resultData) return;
    const query = (document.getElementById("lemma-search") || { value: "" }).value.toLowerCase();
    let rows = sortWithOrder(resultData.lemma_table || [], lemmaSortKey, state.lemmaOrder);
    if (query) rows = rows.filter((row) => row.lemma.includes(query));
    document.getElementById("lemma-tbody").innerHTML = rows.slice(0, 500).map((row, index) => `
      <tr>
        <td>${index + 1}</td><td><b>${esc(row.lemma)}</b></td><td>${esc(row.pos)}</td>
        <td><small>${esc(row.family_id || "")}</small></td>
        <td>${(row.surface_forms || []).map((surface) => `<span class="tag">${esc(surface)}</span>`).join("")}</td>
        <td class="num">${row.body_count}</td><td class="num">${row.stem_count}</td>
        <td class="num"><b style="color:var(--warn)">${row.option_count}</b></td>
        <td class="num">${row.total_count}</td><td class="num score-cell">${row.score}</td>
      </tr>`).join("");
  };

  window.renderFamily = function (sortKey) {
    if (sortKey) familySortKey = sortKey;
    if (!resultData) return;
    const rows = sortWithOrder(resultData.family_table || [], familySortKey, state.familyOrder);
    document.getElementById("family-tbody").innerHTML = rows.slice(0, 300).map((row, index) => `
      <tr>
        <td>${index + 1}</td><td><b>${esc(row.family_id)}</b></td>
        <td>${(row.members || []).map((member) => `<span class="tag tag-orange">${esc(member)}</span>`).join("")}</td>
        <td class="num">${row.body_count}</td><td class="num">${row.stem_count}</td>
        <td class="num"><b style="color:var(--warn)">${row.option_count}</b></td>
        <td class="num">${row.total_count}</td><td class="num score-cell">${row.score}</td>
      </tr>`).join("");
  };

  window.renderVocab = function (vocab) {
    const empty = document.getElementById("vocab-empty");
    const grid = document.getElementById("vocab-grid");
    if (!empty || !grid) return;
    if (!vocab || vocab.length === 0) {
      empty.style.display = "";
      grid.innerHTML = "";
      return;
    }
    empty.style.display = "none";
    grid.innerHTML = sortVocab(vocab).map((entry) => vocabCardHtml(entry)).join("");
  };

  window.startAnalysis = async function () {
    if (!currentFile) return;
    const fd = new FormData();
    fd.append("file", currentFile);
    fd.append("min_word_length", g("min_word_length"));
    fd.append("filter_stopwords", g("filter_stopwords", true));
    fd.append("keep_proper_nouns", g("keep_proper_nouns", true));
    fd.append("filter_numbers", g("filter_numbers", true));
    fd.append("filter_basic_words", g("filter_basic_words", true));
    fd.append("basic_words_threshold", g("basic_words_threshold"));
    fd.append("weight_body", g("weight_body"));
    fd.append("weight_stem", g("weight_stem"));
    fd.append("weight_option", g("weight_option"));
    fd.append("top_n", g("top_n"));
    fd.append("generate_vocab", g("generate_vocab", true));
    document.getElementById("analyze-btn").disabled = true;
    document.getElementById("codes-section").style.display = "none";
    document.getElementById("results-section").style.display = "none";
    showProgress(0, "提交任务中…", "processing");
    try {
      const response = await apiFetch("/api/analyze", { method: "POST", body: fd });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || response.statusText);
      currentTaskId = data.task_id;
      pollStatus();
    } catch (error) {
      showProgress(0, `提交失败：${error.message}`, "error");
      document.getElementById("analyze-btn").disabled = false;
    }
  };

  async function waitForVocab() {
    const startedAt = Date.now();
    while (Date.now() - startedAt < 90000) {
      const response = await fetch(`/api/tasks/${currentTaskId}`);
      const data = await response.json();
      if (data.message && String(data.message).startsWith("Vocab error")) throw new Error(data.message);
      if (data.result && data.result.vocab_table && data.result.vocab_table.length > 0) return data;
      await sleep(1200);
    }
    throw new Error("Vocabulary generation timed out");
  }

  window.generateVocab = async function () {
    if (!currentTaskId) return;
    const button = document.getElementById("vocab-btn");
    const fd = new FormData();
    fd.append("top_n", (document.getElementById("vocab-top-n") || {}).value || (document.getElementById("top_n") || {}).value || 50);
    const provider = (document.getElementById("vocab-provider-select") || {}).value || "";
    if (provider) fd.append("provider", provider);
    button.disabled = true;
    button.textContent = "Working...";
    try {
      const response = await apiFetch(`/api/tasks/${currentTaskId}/vocab`, { method: "POST", body: fd });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Vocabulary generation failed");
      const done = await waitForVocab();
      resultData = done.result;
      renderVocab(done.result.vocab_table || []);
      switchTab("tab-vocab", document.querySelectorAll("#results-section .tab-btn")[3]);
      if (done.dict_code) updateDictCode(done.dict_code);
    } catch (error) {
      alert(error.message);
    } finally {
      button.disabled = false;
      button.textContent = "✨ 生成词汇表";
    }
  };

  window.deleteCurrentVocabWord = async function (headword) {
    if (!currentTaskId || !confirm(`Delete "${headword}"?`)) return;
    try {
      const response = await apiFetch(`/api/tasks/${currentTaskId}/vocab-entry?headword=${encodeURIComponent(headword)}`, { method: "DELETE" });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Delete failed");
      resultData = data.result;
      renderVocab((resultData && resultData.vocab_table) || []);
    } catch (error) {
      alert(error.message);
    }
  };

  function currentTopN() {
    return (document.getElementById("vocab-top-n") || {}).value || (document.getElementById("top_n") || {}).value || 50;
  }

  function currentProvider() {
    return (document.getElementById("vocab-provider-select") || {}).value || "";
  }

  window.rebuildExamVocab = async function (examCode) {
    if (!confirm(`Rebuild vocabulary for ${examCode}?`)) return;
    const fd = new FormData();
    fd.append("top_n", currentTopN());
    if (currentProvider()) fd.append("provider", currentProvider());
    try {
      const response = await apiFetch(`/api/exams/${encodeURIComponent(examCode)}/vocab`, { method: "POST", body: fd });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Build failed");
      if ((document.getElementById("exam-code-val") || {}).textContent === data.exam_code) {
        resultData = data.result;
        renderResults(data.result);
        if (data.dict_code) updateDictCode(data.dict_code);
      }
      await showMyCodes();
    } catch (error) {
      alert(error.message);
    }
  };

  window.deleteExamRecord = async function (examCode) {
    if (!confirm(`Delete exam ${examCode}?`)) return;
    try {
      const response = await apiFetch(`/api/exams/${encodeURIComponent(examCode)}`, { method: "DELETE" });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Delete failed");
      await showMyCodes();
    } catch (error) {
      alert(error.message);
    }
  };

  window.deleteDictRecord = async function (dictCode) {
    if (!confirm(`Delete vocabulary sheet ${dictCode}?`)) return;
    try {
      const response = await apiFetch(`/api/dicts/${encodeURIComponent(dictCode)}`, { method: "DELETE" });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Delete failed");
      if ((document.getElementById("dict-code-val") || {}).textContent === dictCode && resultData) {
        resultData.vocab_table = [];
        renderVocab([]);
      }
      await showMyCodes();
    } catch (error) {
      alert(error.message);
    }
  };

  window.showMyCodes = async function () {
    document.getElementById("my-codes-modal").style.display = "flex";
    document.getElementById("my-codes-content").innerHTML = '<div class="empty">Loading...</div>';
    try {
      const response = await apiFetch("/api/codes");
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Load failed");
      let html = `<h3 style="font-size:.95rem;font-weight:800;margin-bottom:.75rem;color:var(--muted)">试卷码（${data.exams.length}）</h3>`;
      html += data.exams.length ? data.exams.map((exam) => `
        <div class="code-list-item">
          <span class="cli-code">${esc(exam.exam_code)}</span>
          <span class="cli-file">${esc(exam.filename)} · vocab ${exam.dict_count || 0}</span>
          <span class="cli-date">${fmtDate(exam.created_at)}</span>
          <div class="code-actions">
            <button class="copy-btn btn-sm" onclick="navigator.clipboard.writeText('${escAttr(exam.exam_code)}')">Copy</button>
            <button class="btn btn-outline btn-sm" onclick="viewExamInTab('${escAttr(exam.exam_code)}')">View</button>
            <button class="btn btn-success btn-sm" onclick="rebuildExamVocab('${escAttr(exam.exam_code)}')">${exam.dict_count ? "Rebuild Vocab" : "Build Vocab"}</button>
            <button class="btn btn-danger btn-sm" onclick="deleteExamRecord('${escAttr(exam.exam_code)}')">Delete</button>
          </div>
        </div>`).join("") : '<div class="empty" style="padding:1rem">No exams</div>';

      html += `<h3 style="font-size:.95rem;font-weight:800;margin-top:1.5rem;margin-bottom:.75rem;color:var(--muted)">词汇表（${data.dicts.length}）</h3>`;
      html += data.dicts.length ? data.dicts.map((record) => `
        <div class="code-list-item">
          <span class="cli-code" style="color:var(--success)">${esc(record.dict_code)}</span>
          <span class="cli-file">${esc(record.filename)}${record.exam_code ? " · exam " + esc(record.exam_code) : ""}</span>
          <span class="cli-date">${fmtDate(record.created_at)}</span>
          <div class="code-actions">
            <button class="copy-btn btn-sm" onclick="navigator.clipboard.writeText('${escAttr(record.dict_code)}')">Copy</button>
            <button class="btn btn-outline btn-sm" onclick="viewDictInTab('${escAttr(record.dict_code)}')">View</button>
            <button class="btn btn-danger btn-sm" onclick="deleteDictRecord('${escAttr(record.dict_code)}')">Delete</button>
          </div>
        </div>`).join("") : '<div class="empty" style="padding:1rem">No vocabulary sheets</div>';
      document.getElementById("my-codes-content").innerHTML = html;
    } catch (error) {
      document.getElementById("my-codes-content").innerHTML = `<div class="error-box">${error.message}</div>`;
    }
  };

  function ensureAdminUi() {
    const tabs = document.querySelector(".admin-tabs");
    if (!tabs) return;
    if (!document.getElementById("admin-overview-strip")) tabs.insertAdjacentHTML("beforebegin", `<div id="admin-overview-strip"></div>`);
    if (!document.getElementById("admin-config-tab")) tabs.insertAdjacentHTML("beforeend", `<button class="admin-tab" id="admin-config-tab" onclick="switchAdminTab('config', this)">系统配置</button>`);
    if (!document.getElementById("admin-config-panel")) document.getElementById("admin-codes-panel").insertAdjacentHTML("afterend", `<div id="admin-config-panel" style="display:none"></div>`);
    if (!document.getElementById("admin-panel-link")) {
      const titleEl = document.querySelector("#admin-modal .modal-title");
      if (titleEl) titleEl.insertAdjacentHTML("afterbegin", `<a id="admin-panel-link" href="/admin-panel" target="_blank" style="font-size:.78rem;font-weight:600;color:var(--primary);text-decoration:none;margin-right:.75rem" title="在新标签打开独立管理后台">↗ 完整后台</a>`);
    }
  }

  async function loadAdminOverview() {
    const response = await apiFetch("/admin/overview");
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Overview failed");
    const caps = data.ocr_capabilities || {};
    document.getElementById("admin-overview-strip").innerHTML = `
      <div class="admin-summary">
        <div class="summary-card"><div class="label">Users</div><div class="value">${data.users || 0}</div></div>
        <div class="summary-card"><div class="label">Admins</div><div class="value">${data.admins || 0}</div></div>
        <div class="summary-card"><div class="label">Exams</div><div class="value">${data.exams || 0}</div></div>
        <div class="summary-card"><div class="label">Dicts</div><div class="value">${data.dicts || 0}</div></div>
      </div>
      <div style="margin-bottom:1rem">
        <span class="mini-chip ${caps.rapidocr ? "ok" : "warn"}">RapidOCR ${caps.rapidocr ? "on" : "off"}</span>
        <span class="mini-chip ${caps.pytesseract ? "ok" : "warn"}">Tesseract py ${caps.pytesseract ? "on" : "off"}</span>
        <span class="mini-chip ${caps.tesseract_configured ? "ok" : "warn"}">Tesseract path ${caps.tesseract_configured ? "ok" : "missing"}</span>
      </div>`;
    return data;
  }

  window.showAdmin = async function () {
    ensureAdminUi();
    document.getElementById("admin-modal").style.display = "flex";
    await loadAdminOverview();
    await switchAdminTab("users", document.querySelector(".admin-tab.active") || document.querySelector(".admin-tab"));
  };

  window.switchAdminTab = async function (tab, button) {
    ensureAdminUi();
    document.querySelectorAll(".admin-tab").forEach((item) => item.classList.remove("active"));
    if (button) button.classList.add("active");
    document.getElementById("admin-users-panel").style.display = tab === "users" ? "" : "none";
    document.getElementById("admin-codes-panel").style.display = tab === "codes" ? "" : "none";
    document.getElementById("admin-config-panel").style.display = tab === "config" ? "" : "none";
    await loadAdminOverview();
    if (tab === "users" && typeof loadAdminUsers === "function") await loadAdminUsers();
    if (tab === "codes") await loadAdminCodes();
    if (tab === "config") await loadAdminConfig();
  };

  window.loadAdminCodes = async function () {
    const panel = document.getElementById("admin-codes-panel");
    panel.innerHTML = '<div class="empty">Loading...</div>';
    try {
      const response = await apiFetch("/admin/codes");
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Load failed");
      panel.innerHTML = `
        <h3 style="font-size:.9rem;font-weight:800;color:var(--muted);margin-bottom:.75rem">试卷码（${data.exams.length}）</h3>
        <div class="tbl-wrap"><table><thead><tr><th>试卷码</th><th>文件名</th><th>上传者</th><th>时间</th><th>操作</th></tr></thead><tbody>
        ${(data.exams.length ? data.exams.map((exam) => `<tr><td><span class="cli-code">${esc(exam.exam_code)}</span></td><td>${esc(exam.filename)}</td><td>${esc(exam.username)}</td><td>${fmtDate(exam.created_at)}</td><td><div class="admin-actions"><button class="btn btn-outline btn-sm" onclick="viewExamInTab('${escAttr(exam.exam_code)}')">View</button><button class="btn btn-danger btn-sm" onclick="deleteExamRecord('${escAttr(exam.exam_code)}')">Delete</button></div></td></tr>`).join("") : '<tr><td colspan="5" class="empty">No data</td></tr>')}
        </tbody></table></div>
        <h3 style="font-size:.9rem;font-weight:800;color:var(--muted);margin-top:1.5rem;margin-bottom:.75rem">词汇表（${data.dicts.length}）</h3>
        <div class="tbl-wrap"><table><thead><tr><th>词汇表码</th><th>关联试卷码</th><th>文件名</th><th>上传者</th><th>时间</th><th>操作</th></tr></thead><tbody>
        ${(data.dicts.length ? data.dicts.map((record) => `<tr><td><span class="cli-code" style="color:var(--success)">${esc(record.dict_code)}</span></td><td>${record.exam_code ? esc(record.exam_code) : "—"}</td><td>${esc(record.filename)}</td><td>${esc(record.username)}</td><td>${fmtDate(record.created_at)}</td><td><div class="admin-actions"><button class="btn btn-outline btn-sm" onclick="viewDictInTab('${escAttr(record.dict_code)}')">View</button><button class="btn btn-danger btn-sm" onclick="deleteDictRecord('${escAttr(record.dict_code)}')">Delete</button></div></td></tr>`).join("") : '<tr><td colspan="6" class="empty">No data</td></tr>')}
        </tbody></table></div>`;
    } catch (error) {
      panel.innerHTML = `<div class="error-box">${error.message}</div>`;
    }
  };

  window.loadAdminConfig = async function () {
    const panel = document.getElementById("admin-config-panel");
    panel.innerHTML = '<div class="empty">Loading...</div>';
    try {
      const response = await apiFetch("/admin/config");
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Load failed");
      const cfg = data.config || {};
      const analysis = cfg.analysis || {};
      const ocr = cfg.ocr || {};
      const mineru = cfg.mineru || {};
      panel.innerHTML = `
        <div class="settings-grid">
          <div class="settings-card">
            <h3>Analysis</h3>
            <div class="settings-field"><label>Parse backend</label><select class="settings-select" id="cfg-parse-backend">${(data.parse_backends || ["local", "mineru"]).map((name) => `<option value="${name}"${cfg.parse_backend === name ? " selected" : ""}>${name}</option>`).join("")}</select></div>
            <div class="settings-field"><label>Store raw parse result</label><label class="settings-switch"><input id="cfg-save-raw-parse" type="checkbox"${cfg.save_raw_parse_result ? " checked" : ""} /> Enable</label></div>
            <div class="settings-field"><label>Min word length</label><input class="settings-input" id="cfg-min-word-length" type="number" value="${analysis.min_word_length}" /></div>
            <div class="settings-field"><label>Top N</label><input class="settings-input" id="cfg-top-n" type="number" value="${analysis.top_n}" /></div>
            <div class="settings-field"><label>Filter stopwords</label><label class="settings-switch"><input id="cfg-filter-stopwords" type="checkbox"${analysis.filter_stopwords ? " checked" : ""} /> Enable</label></div>
            <div class="settings-field"><label>Keep proper nouns</label><label class="settings-switch"><input id="cfg-keep-proper" type="checkbox"${analysis.keep_proper_nouns ? " checked" : ""} /> Enable</label></div>
            <div class="settings-field"><label>Filter numbers</label><label class="settings-switch"><input id="cfg-filter-numbers" type="checkbox"${analysis.filter_numbers ? " checked" : ""} /> Enable</label></div>
            <div class="settings-field"><label>Filter basic words by default</label><label class="settings-switch"><input id="cfg-filter-basic" type="checkbox"${analysis.filter_basic_words ? " checked" : ""} /> Enable</label></div>
            <div class="settings-field"><label>Basic word threshold</label><input class="settings-input" id="cfg-basic-threshold" type="number" step="0.1" value="${analysis.basic_words_threshold}" /></div>
            <div class="settings-field"><label>Vocabulary provider</label><select class="settings-select" id="cfg-provider">${(data.providers || []).map((name) => `<option value="${name}"${cfg.vocab_provider === name ? " selected" : ""}>${name}</option>`).join("")}</select></div>
          <div class="settings-field"><label>AI model</label><input class="settings-input" id="cfg-ai-model" type="text" value="${escAttr(cfg.ai_model || "")}" /></div>
          <div class="settings-field"><label>AI batch size</label><input class="settings-input" id="cfg-ai-batch-size" type="number" min="1" max="50" value="${cfg.ai_batch_size ?? 20}" /></div>
          <div class="settings-field"><label>Open registration</label><label class="settings-switch"><input id="cfg-registration" type="checkbox"${cfg.registration_enabled ? " checked" : ""} /> Enable</label></div>
          </div>
          <div class="settings-card">
            <h3>Weights</h3>
            <div class="settings-field"><label>Body</label><input class="settings-input" id="cfg-weight-body" type="number" step="0.1" value="${analysis.weight_body}" /></div>
            <div class="settings-field"><label>Stem</label><input class="settings-input" id="cfg-weight-stem" type="number" step="0.1" value="${analysis.weight_stem}" /></div>
            <div class="settings-field"><label>Option</label><input class="settings-input" id="cfg-weight-option" type="number" step="0.1" value="${analysis.weight_option}" /></div>
          </div>
          <div class="settings-card">
            <h3>OCR</h3>
            <div class="settings-field"><label>Engine</label><select class="settings-select" id="cfg-ocr-engine"><option value="auto"${ocr.engine === "auto" ? " selected" : ""}>auto</option><option value="tesseract"${ocr.engine === "tesseract" ? " selected" : ""}>tesseract</option><option value="rapidocr"${ocr.engine === "rapidocr" ? " selected" : ""}>rapidocr</option></select></div>
            <div class="settings-field"><label>Language</label><input class="settings-input" id="cfg-ocr-language" type="text" value="${escAttr(ocr.language || "eng")}" /></div>
            <div class="settings-field"><label>PDF DPI</label><input class="settings-input" id="cfg-pdf-dpi" type="number" value="${ocr.pdf_dpi}" /></div>
            <div class="settings-field"><label>PDF OCR threshold (chars)</label><input class="settings-input" id="cfg-pdf-ocr-threshold" type="number" min="0" max="2000" value="${ocr.pdf_ocr_threshold ?? 50}" /></div>
            <div class="settings-field"><label>PSM</label><input class="settings-input" id="cfg-psm" type="number" value="${ocr.page_segmentation_mode}" /></div>
            <div class="settings-field"><label>Threshold</label><input class="settings-input" id="cfg-threshold" type="number" value="${ocr.binary_threshold}" /></div>
            <div class="settings-field"><label>Upscale</label><input class="settings-input" id="cfg-upscale" type="number" step="0.1" value="${ocr.upscale_factor}" /></div>
            <div class="settings-field"><label>Preprocess</label><label class="settings-switch"><input id="cfg-preprocess" type="checkbox"${ocr.preprocess ? " checked" : ""} /> Enable</label></div>
            <div class="settings-field"><label>Sharpen</label><label class="settings-switch"><input id="cfg-sharpen" type="checkbox"${ocr.sharpen ? " checked" : ""} /> Enable</label></div>
            <div class="settings-field"><label>Fallback to Tesseract</label><label class="settings-switch"><input id="cfg-fallback" type="checkbox"${ocr.fallback_to_tesseract ? " checked" : ""} /> Enable</label></div>
          </div>
          <div class="settings-card">
            <h3>MinerU</h3>
            <div class="settings-field"><label>Enable MinerU</label><label class="settings-switch"><input id="cfg-mineru-enabled" type="checkbox"${mineru.enabled ? " checked" : ""} /> Enable</label></div>
            <div class="settings-field"><label>API base</label><input class="settings-input" id="cfg-mineru-api-base" type="text" value="${escAttr(mineru.api_base || "https://mineru.net/api/v1/agent")}" /></div>
            <div class="settings-field"><label>Language</label><input class="settings-input" id="cfg-mineru-language" type="text" value="${escAttr(mineru.language || "en")}" /></div>
            <div class="settings-field"><label>Page range</label><input class="settings-input" id="cfg-mineru-page-range" type="text" value="${escAttr(mineru.page_range || "")}" /></div>
            <div class="settings-field"><label>Enable table</label><label class="settings-switch"><input id="cfg-mineru-enable-table" type="checkbox"${mineru.enable_table ? " checked" : ""} /> Enable</label></div>
            <div class="settings-field"><label>Enable formula</label><label class="settings-switch"><input id="cfg-mineru-enable-formula" type="checkbox"${mineru.enable_formula ? " checked" : ""} /> Enable</label></div>
            <div class="settings-field"><label>Use OCR in MinerU</label><label class="settings-switch"><input id="cfg-mineru-is-ocr" type="checkbox"${mineru.is_ocr ? " checked" : ""} /> Enable</label></div>
            <div class="settings-field"><label>Poll timeout sec</label><input class="settings-input" id="cfg-mineru-timeout" type="number" value="${mineru.poll_timeout_sec || 300}" /></div>
            <div class="settings-field"><label>Poll interval sec</label><input class="settings-input" id="cfg-mineru-interval" type="number" value="${mineru.poll_interval_sec || 3}" /></div>
            <div class="settings-field"><label>Fallback to local</label><label class="settings-switch"><input id="cfg-mineru-fallback-local" type="checkbox"${mineru.fallback_to_local ? " checked" : ""} /> Enable</label></div>
          </div>
        </div>
        <div class="btn-row"><button class="btn btn-primary" onclick="saveAdminConfig()">Save</button><span id="admin-config-status" style="font-size:.85rem;color:var(--muted)"></span></div>`;
    } catch (error) {
      panel.innerHTML = `<div class="error-box">${error.message}</div>`;
    }
  };

  window.saveAdminConfig = async function () {
    const status = document.getElementById("admin-config-status");
    const payload = {
      parse_backend: document.getElementById("cfg-parse-backend").value,
      save_raw_parse_result: document.getElementById("cfg-save-raw-parse").checked,
      vocab_provider: document.getElementById("cfg-provider").value,
      ai_model: (document.getElementById("cfg-ai-model") || {}).value || undefined,
      ai_batch_size: Number((document.getElementById("cfg-ai-batch-size") || {}).value || 20),
      registration_enabled: (document.getElementById("cfg-registration") || {checked: true}).checked,
      analysis: {
        min_word_length: Number(document.getElementById("cfg-min-word-length").value),
        top_n: Number(document.getElementById("cfg-top-n").value),
        filter_stopwords: document.getElementById("cfg-filter-stopwords").checked,
        keep_proper_nouns: document.getElementById("cfg-keep-proper").checked,
        filter_numbers: document.getElementById("cfg-filter-numbers").checked,
        filter_basic_words: document.getElementById("cfg-filter-basic").checked,
        basic_words_threshold: Number(document.getElementById("cfg-basic-threshold").value),
        weight_body: Number(document.getElementById("cfg-weight-body").value),
        weight_stem: Number(document.getElementById("cfg-weight-stem").value),
        weight_option: Number(document.getElementById("cfg-weight-option").value),
      },
      ocr: {
        engine: document.getElementById("cfg-ocr-engine").value,
        language: document.getElementById("cfg-ocr-language").value,
        pdf_dpi: Number(document.getElementById("cfg-pdf-dpi").value),
        pdf_ocr_threshold: Number((document.getElementById("cfg-pdf-ocr-threshold") || {value: 50}).value),
        page_segmentation_mode: Number(document.getElementById("cfg-psm").value),
        binary_threshold: Number(document.getElementById("cfg-threshold").value),
        upscale_factor: Number(document.getElementById("cfg-upscale").value),
        preprocess: document.getElementById("cfg-preprocess").checked,
        sharpen: document.getElementById("cfg-sharpen").checked,
        fallback_to_tesseract: document.getElementById("cfg-fallback").checked,
      },
      mineru: {
        enabled: document.getElementById("cfg-mineru-enabled").checked,
        api_base: document.getElementById("cfg-mineru-api-base").value.trim(),
        language: document.getElementById("cfg-mineru-language").value.trim(),
        page_range: document.getElementById("cfg-mineru-page-range").value.trim() || null,
        enable_table: document.getElementById("cfg-mineru-enable-table").checked,
        enable_formula: document.getElementById("cfg-mineru-enable-formula").checked,
        is_ocr: document.getElementById("cfg-mineru-is-ocr").checked,
        poll_timeout_sec: Number(document.getElementById("cfg-mineru-timeout").value),
        poll_interval_sec: Number(document.getElementById("cfg-mineru-interval").value),
        fallback_to_local: document.getElementById("cfg-mineru-fallback-local").checked,
      },
    };
    status.textContent = "Saving...";
    try {
      const response = await apiFetch("/admin/config", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Save failed");
      APP_CONFIG = {
        defaults: data.config,
        providers: data.providers,
        provider_default: data.config.vocab_provider,
        ocr_capabilities: data.ocr_capabilities,
        parse_backends: data.parse_backends || ["local", "mineru"],
      };
      applyConfig();
      status.textContent = "Saved";
    } catch (error) {
      status.textContent = error.message;
    }
  };

  window.showApp = async function () {
    if (originalShowApp) originalShowApp.apply(this, arguments);
    await loadConfig(true);
  };

  ensureAnalysisControls();
  ensureSortControls();
  ensureAdminUi();
  loadConfig();
})();
