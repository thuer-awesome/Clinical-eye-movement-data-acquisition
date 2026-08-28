(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const refs = {
    environment: $("environment-chip"), connection: $("connection-chip"), connectionLabel: $("connection-label"),
    clock: $("clock"), notice: $("notice-strip"), noticeText: $("notice-text"),
    pageTitle: $("page-title"), pageDescription: $("page-description"), badge: $("session-badge"), badgeLabel: $("session-badge-label"),
    metricReady: $("metric-ready"), metricReadyFoot: $("metric-ready-foot"), metricFps: $("metric-fps"), metricFpsFoot: $("metric-fps-foot"), metricState: $("metric-state"), metricStateFoot: $("metric-state-foot"),
    cameraGrid: $("camera-grid"), sessionList: $("session-list"), reviewSection: $("review-section"), screeningPanel: $("screening-panel"),
    form: $("session-form"), start: $("start-button"), stop: $("stop-button"), preflight: $("preflight-button"), restart: $("restart-button"), shutdown: $("shutdown-button"), refreshSessions: $("refresh-sessions"),
    deviceSettingsButton: $("device-settings-button"), deviceSettingsPanel: $("device-settings-panel"), deviceSettingsForm: $("device-settings-form"), deviceSettingsState: $("device-settings-state"),
    scanCameras: $("scan-cameras-button"), cameraCandidates: $("camera-candidates"), rightCameraIndex: $("right-camera-index"), leftCameraIndex: $("left-camera-index"),
    rightCameraTransform: $("right-camera-transform"), leftCameraTransform: $("left-camera-transform"), swapCameras: $("swap-cameras-button"), cancelDeviceSettings: $("cancel-device-settings"),
    screeningForm: $("screening-form"), exportResult: $("export-result"), exportPath: $("export-path"), exportSize: $("export-size"), copyExportPath: $("copy-export-path"), downloadExport: $("download-export"), toast: $("toast"),
  };

  const state = {
    status: null,
    sessions: [],
    busy: false,
    selectedSession: null,
    exportInfo: null,
    cameraKey: "",
    toastTimer: null,
    pollTimer: null,
    deviceSettings: null,
    settingsOpen: false,
    cameraCandidates: [],
    // A DirectShow device can occasionally block one worker after a reconfigure.
    // Keep recovery in the browser so the operator does not have to restart manually.
    recovery: { lowFpsPolls: 0, inFlight: false, lastAttemptAt: 0 },
  };

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char]));
  }

  async function request(path, options = {}, timeoutMs = 6000) {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(path, { ...options, signal: controller.signal, headers: { "Content-Type": "application/json", ...(options.headers || {}) } });
      const text = await response.text();
      let payload = {};
      try { payload = text ? JSON.parse(text) : {}; } catch { payload = { detail: text }; }
      if (!response.ok) throw new Error(payload.detail || `请求失败（${response.status}）`);
      return payload;
    } catch (error) {
      if (error && error.name === "AbortError") throw new Error("本地服务响应超时，请查看设备状态后重试");
      throw error;
    } finally { window.clearTimeout(timer); }
  }

  function showToast(message) {
    refs.toast.textContent = message;
    refs.toast.classList.add("visible");
    window.clearTimeout(state.toastTimer);
    state.toastTimer = window.setTimeout(() => refs.toast.classList.remove("visible"), 4800);
  }

  function setNotice(message, kind = "") {
    refs.noticeText.textContent = message;
    refs.notice.classList.toggle("success", kind === "success");
    refs.notice.classList.toggle("error", kind === "error");
  }

  function formatTime(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value).slice(0, 19).replace("T", " ");
    return date.toLocaleString("zh-CN", { hour12: false, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  function stateLabel(value) {
    return ({ idle: "待机", starting: "启动中", ready: "可采集", recording: "采集中", stopping: "保存中", completed: "已完成", error: "需处理" }[value] || value || "—");
  }

  function cameraStateLabel(value) {
    return ({ offline: "离线", starting: "启动中", reconnecting: "正在重连", ready: "就绪", recording: "录制中", error: "故障" }[value] || value || "—");
  }

  function cameraNameLabel(value) {
    return ({ right_eye: "右眼", left_eye: "左眼" }[value] || value || "—");
  }

  function cameraTransformLabel(value) {
    return ({ none: "正常", rotate_180: "旋转 180°", flip_horizontal: "水平镜像", flip_vertical: "上下翻转" }[value] || value || "—");
  }

  function cameraMappingLabel(camera) {
    const index = Number.isFinite(Number(camera?.index)) ? Number(camera.index) : "—";
    const transform = cameraTransformLabel(camera?.frame_transform || "none");
    return `物理相机 ${index} · ${transform}`;
  }

  function updateClock() {
    refs.clock.textContent = new Date().toLocaleTimeString("zh-CN", { hour12: false });
  }

  function formPayload() {
    const ageText = $("age").value.trim();
    return {
      subject_code: $("subject-code").value.trim(), visit_id: $("visit-id").value.trim(), operator: $("operator").value.trim(),
      age: ageText ? Number(ageText) : null, sex: $("sex").value, protocol: $("protocol").value, screening: $("screening").value,
      notes: $("notes").value, consent_confirmed: $("consent").checked,
    };
  }

  function renderHeader() {
    const status = state.status;
    if (!status) return;
    refs.environment.textContent = status.simulate ? "模拟采集" : "现场采集";
    refs.environment.classList.toggle("simulation", !!status.simulate);
    refs.environment.classList.toggle("production", !status.simulate);
    refs.connection.classList.add("online"); refs.connection.classList.remove("offline"); refs.connectionLabel.textContent = "本地服务在线";
    const appState = status.app_state;
    const recording = appState === "recording" || appState === "stopping";
    const needsDeviceConfirmation = !status.simulate && !status.device_settings?.confirmed;
    refs.badge.classList.toggle("hidden", !recording);
    refs.badgeLabel.textContent = appState === "stopping" ? "正在保存" : "正在采集";
    refs.pageTitle.textContent = recording ? "高速采集" : (needsDeviceConfirmation ? "设备设置" : (status.last_session ? "复核与导出" : "设备检查"));
    const alignment = status.preflight?.pupil_alignment;
    refs.pageDescription.textContent = recording
      ? "采集进程直接写盘，浏览器预览不会影响原始帧。"
      : needsDeviceConfirmation
        ? "确认左右眼相机和画面方向后，再开始临床会话。"
        : status.last_session
          ? "确认质量指标并附加筛查备注，再导出会话包。"
          : alignment?.required && !alignment?.ready
            ? "请根据双眼实时提示微调相机，瞳孔置信度稳定达标后才可开始采集。"
            : "确认双眼相机持续出帧后，再开始临床会话。";
  }

  function renderMetrics() {
    const status = state.status;
    const preflight = status?.preflight;
    if (!status || !preflight) return;
    const cameras = Object.values(preflight.cameras || {});
    const required = cameras.filter((camera) => camera.required);
    const readyCount = required.filter((camera) => ["ready", "recording"].includes(camera.state)).length;
    refs.metricReady.textContent = `${readyCount}/${required.length || "—"}`;
    refs.metricReadyFoot.textContent = preflight.ready ? "预检通过" : (preflight.blocking_reasons?.[0] || "等待检查");
    const fps = required.map((camera) => Number(camera.capture_fps || 0)).filter((value) => value > 0);
    refs.metricFps.textContent = fps.length ? `${(fps.reduce((a, b) => a + b, 0) / fps.length).toFixed(1)}` : "—";
    const targets = required.map((camera) => Number(camera.target_fps || 0)).filter((value) => value > 0);
    const minimums = required.map((camera) => Number(camera.minimum_fps || 0)).filter((value) => value > 0);
    refs.metricFpsFoot.textContent = targets.length ? `目标 ${Math.round(targets.reduce((a, b) => a + b, 0) / targets.length)} · 合格 ≥ ${Math.round(minimums.reduce((a, b) => a + b, 0) / Math.max(1, minimums.length))} fps` : "等待目标帧率";
    const blockedWhileIdle = status.app_state === "ready" && !preflight.ready;
    refs.metricState.textContent = blockedWhileIdle ? "需处理" : stateLabel(status.app_state);
    refs.metricStateFoot.textContent = status.last_error || (blockedWhileIdle ? (preflight.blocking_reasons?.[0] || "设备检查未通过") : (status.simulate ? "模拟模式 · 可用于流程演练" : "实时设备模式"));
  }

  function renderCameras() {
    const order = { right_eye: 0, left_eye: 1 };
    const cameras = Object.values(state.status?.preflight?.cameras || {}).sort((a, b) => (order[a.name] ?? 99) - (order[b.name] ?? 99));
    if (!cameras.length) {
      refs.cameraGrid.innerHTML = '<div class="empty-state">尚未收到设备状态</div>';
      state.cameraKey = "";
      return;
    }
    const key = cameras.map((camera) => `${camera.name}:${camera.required ? 1 : 0}:${camera.index}:${camera.frame_transform}`).join("|");
    if (state.cameraKey !== key) {
      refs.cameraGrid.innerHTML = cameras.map((camera) => {
        const previewUrl = `/api/preview/${encodeURIComponent(camera.name)}.jpg`;
        return `<article class="camera-card ${camera.required ? "" : "optional"}" data-camera="${escapeHtml(camera.name)}">
          <div class="camera-header"><div><span class="camera-name" data-field="name">${escapeHtml(cameraNameLabel(camera.name))}</span><span class="camera-role" data-field="mapping">${camera.required ? "必需" : "可选"} · ${escapeHtml(cameraMappingLabel(camera))}</span><span class="camera-mode" data-field="mode">实际模式检测中</span></div><span class="camera-state" data-field="state">等待</span></div>
          <div class="camera-preview"><img data-preview="${escapeHtml(camera.name)}" src="${previewUrl}?t=${Date.now()}" alt="${escapeHtml(cameraNameLabel(camera.name))} 实时预览" /><div class="preview-empty">等待画面</div></div>
          <div class="camera-footer"><div class="camera-stat"><label>采集帧率</label><strong data-field="fps">—</strong></div><div class="camera-stat"><label>黑帧</label><strong data-field="black">—</strong></div><div class="camera-stat"><label>已写入</label><strong data-field="frames">—</strong></div><div class="camera-stat"><label>重连</label><strong data-field="reconnect">0</strong></div><div class="camera-stat pupil-stat"><label>瞳孔捕捉</label><strong data-field="pupil">检测中</strong></div><div class="pupil-alignment" data-field="alignment">等待瞳孔对准提示</div><div class="quality-bar" title="相对目标帧率"><span data-field="quality"></span></div></div>
        </article>`;
      }).join("");
      state.cameraKey = key;
      refs.cameraGrid.querySelectorAll("img[data-preview]").forEach((image) => {
        const empty = image.parentElement?.querySelector(".preview-empty");
        if (!empty) return;
        image.addEventListener("load", () => { image.dataset.loading = "0"; empty.classList.add("hidden"); });
        image.addEventListener("error", () => { image.dataset.loading = "0"; empty.classList.remove("hidden"); });
        image.dataset.loading = "0";
      });
    }
    cameras.forEach((camera) => {
      const card = Array.from(refs.cameraGrid.querySelectorAll("[data-camera]"))
        .find((node) => node.dataset.camera === camera.name);
      if (!card) return;
      const stateClass = String(camera.state || "offline");
      const fps = Number(camera.capture_fps || 0);
      const minimumFps = Number(camera.minimum_fps || 0);
      const fpsLow = fps > 0 && minimumFps > 0 && fps < minimumFps;
      card.classList.toggle("error", stateClass === "error" || fpsLow);
      card.classList.toggle("ready", stateClass === "ready");
      card.classList.toggle("recording", stateClass === "recording");
      card.classList.toggle("reconnecting", stateClass === "reconnecting");
      const stateNode = card.querySelector('[data-field="state"]');
      if (stateNode) { stateNode.className = `camera-state ${escapeHtml(fpsLow ? "error" : stateClass)}`; stateNode.textContent = fpsLow ? "帧率不足" : cameraStateLabel(stateClass); }
      const target = Number(camera.target_fps || 240);
      const quality = Math.max(0, Math.min(100, fps ? (fps / Math.max(1, target) * 100) : 0));
      card.querySelector('[data-field="fps"]').textContent = fps ? `${fps.toFixed(1)} fps` : "—";
      card.querySelector('[data-field="black"]').textContent = `${Number(camera.black_percent || 0).toFixed(1)}%`;
      card.querySelector('[data-field="frames"]').textContent = `${Number(camera.recorded_frames || 0).toLocaleString()} 帧`;
      const reconnectNode = card.querySelector('[data-field="reconnect"]');
      if (reconnectNode) {
        const reconnects = Number(camera.reconnect_count || 0);
        reconnectNode.textContent = reconnects ? `${reconnects} 次` : "0";
        reconnectNode.classList.toggle("pupil-warn", reconnects > 0);
      }
      const modeNode = card.querySelector('[data-field="mode"]');
      if (modeNode) {
        const transformState = camera.transform_verified
          ? ` · 方向已应用（${cameraTransformLabel(camera.frame_transform)}）`
          : ` · 等待方向验证（${cameraTransformLabel(camera.frame_transform)}）`;
        modeNode.textContent = `${camera.actual ? `实际 ${camera.actual}` : `请求 ${camera.requested || "—"}`}${transformState}`;
        const reconnects = Number(camera.reconnect_count || 0);
        modeNode.title = `${camera.recording_codec ? `录像编码 ${camera.recording_codec} · ` : ""}运行期间重连 ${reconnects} 次`;
      }
      const mappingNode = card.querySelector('[data-field="mapping"]');
      if (mappingNode) {
        mappingNode.textContent = `${camera.required ? "必需" : "可选"} · ${cameraMappingLabel(camera)}`;
        mappingNode.title = `逻辑角色 ${cameraNameLabel(camera.name)}，当前绑定 ${cameraMappingLabel(camera)}`;
      }
      const pupil = camera.pupil || {};
      const pupilNode = card.querySelector('[data-field="pupil"]');
      if (pupilNode) {
        pupilNode.classList.toggle("pupil-good", !!pupil.valid);
        pupilNode.classList.toggle("pupil-warn", !pupil.valid);
        if (pupil.state === "error") pupilNode.textContent = "算法不可用";
        else if (pupil.valid) pupilNode.textContent = `已捕捉 · r ${Number(pupil.radius || 0).toFixed(1)} px · ${(Number(pupil.confidence || 0) * 100).toFixed(0)}% · ${Number(pupil.detector_fps || 0).toFixed(0)} Hz / ${Number(pupil.latency_ms || 0).toFixed(1)} ms`;
        else if (pupil.state === "ready") pupilNode.textContent = `未锁定 · ${(Number(pupil.confidence || 0) * 100).toFixed(0)}% · ${Number(pupil.detector_fps || 0).toFixed(0)} Hz`;
        else pupilNode.textContent = "检测中";
        pupilNode.title = pupil.latency_ms == null ? "" : `检测延迟 ${Number(pupil.latency_ms).toFixed(1)} ms`;
      }
      const alignment = pupil.alignment || {};
      const alignmentNode = card.querySelector('[data-field="alignment"]');
      if (alignmentNode) {
        const alignmentState = String(alignment.state || "searching");
        alignmentNode.className = `pupil-alignment ${alignmentState}`;
        const progress = Math.max(0, Math.min(1, Number(alignment.progress || 0)));
        alignmentNode.style.setProperty("--alignment-progress", `${(progress * 100).toFixed(0)}%`);
        alignmentNode.textContent = state.status?.simulate && state.status?.preflight?.pupil_alignment?.required === false
          ? "模拟模式 · 跳过对准门禁"
          : (alignment.guidance || "等待瞳孔对准提示");
        alignmentNode.title = alignment.threshold == null ? "" : `目标置信度 ≥ ${(Number(alignment.threshold) * 100).toFixed(0)}%`;
      }
      const qualityNode = card.querySelector('[data-field="quality"]');
      if (qualityNode) qualityNode.style.width = `${quality}%`;
      const empty = card.querySelector(".preview-empty");
      if (empty && camera.error) { empty.textContent = camera.error; empty.classList.remove("hidden"); }
    });
  }

  function updatePreviewSources() {
    refs.cameraGrid.querySelectorAll("img[data-preview]").forEach((image) => {
      const name = image.dataset.preview;
      if (name && image.dataset.loading !== "1") {
        image.dataset.loading = "1";
        image.src = `/api/preview/${encodeURIComponent(name)}.jpg?t=${Date.now()}`;
      }
    });
  }

  function renderWorkflow() {
    const appState = state.status?.app_state;
    const active = appState === "recording" || appState === "stopping" ? "record" : (state.status?.last_session ? "review" : (appState === "ready" ? "check" : "patient"));
    const order = ["patient", "check", "record", "review"];
    document.querySelectorAll(".workflow-step").forEach((node) => {
      const step = node.dataset.step; const index = order.indexOf(step); const activeIndex = order.indexOf(active);
      node.classList.toggle("active", step === active); node.classList.toggle("done", index < activeIndex);
    });
  }

  function renderFormState() {
    const status = state.status;
    const recording = status?.app_state === "recording" || status?.app_state === "stopping";
    const alignment = status?.preflight?.pupil_alignment;
    const alignmentReady = !alignment?.required || !!alignment?.ready;
    const canStart = !state.busy && !recording && status?.app_state === "ready" && !!status?.preflight?.ready && alignmentReady && $("consent").checked && $("subject-code").value.trim() && $("visit-id").value.trim() && $("operator").value.trim();
    refs.start.disabled = !canStart;
    refs.start.classList.toggle("hidden", recording);
    refs.stop.classList.toggle("hidden", !recording);
    refs.stop.disabled = state.busy || status?.app_state === "stopping";
    refs.preflight.disabled = state.busy || recording;
    refs.preflight.textContent = !status?.simulate && !status?.device_settings?.confirmed ? "确认设备" : (status?.app_state === "ready" && !status?.preflight?.ready ? "恢复设备" : "重新检查");
    refs.deviceSettingsButton.disabled = state.busy || recording;
    refs.deviceSettingsForm.querySelectorAll("button, select").forEach((control) => { control.disabled = state.busy || recording; });
  }

  function renderSessions() {
    if (!state.sessions.length) {
      refs.sessionList.innerHTML = '<tr><td colspan="5" class="empty-state">暂无已完成会话</td></tr>';
      return;
    }
    refs.sessionList.innerHTML = state.sessions.slice(0, 20).map((session) => {
      const error = session.state === "error";
      return `<tr><td class="session-id">${escapeHtml(session.session_id)}</td><td>${escapeHtml(session.metadata?.subject_code || "—")}</td><td>${escapeHtml(formatTime(session.created_at))}</td><td class="session-status ${error ? "error" : ""}">${escapeHtml(stateLabel(session.state))}</td><td><button class="table-action export-action" type="button" data-session="${escapeHtml(session.session_id)}">导出 ZIP</button></td></tr>`;
    }).join("");
    refs.sessionList.querySelectorAll("[data-session]").forEach((button) => {
      button.addEventListener("click", () => exportSession(button.dataset.session));
    });
  }

  function renderReviewPanel() {
    const last = state.status?.last_session;
    const show = !!last && ["completed", "error", "cancelled"].includes(last.state);
    refs.screeningPanel.classList.toggle("hidden", !show);
    if (show) {
      state.selectedSession = last.session_id;
      $("screening-type").value = last.metadata?.screening || "none";
    }
    renderExportInfo();
  }

  function setDeviceSettingsDraft(settings) {
    const maximum = Math.max(0, Math.min(15, Number(settings?.max_camera_index ?? 15)));
    const options = Array.from({ length: maximum + 1 }, (_, index) => `<option value="${index}">相机 ${index}</option>`).join("");
    refs.rightCameraIndex.innerHTML = options;
    refs.leftCameraIndex.innerHTML = options;
    refs.rightCameraIndex.value = String(settings?.cameras?.right_eye?.index ?? 1);
    refs.leftCameraIndex.value = String(settings?.cameras?.left_eye?.index ?? 0);
    refs.rightCameraTransform.value = settings?.cameras?.right_eye?.frame_transform || "rotate_180";
    refs.leftCameraTransform.value = settings?.cameras?.left_eye?.frame_transform || "none";
  }

  function renderCameraCandidates() {
    const items = state.cameraCandidates || [];
    refs.cameraCandidates.classList.toggle("hidden", !items.length);
    if (!items.length) {
      refs.cameraCandidates.innerHTML = "";
      return;
    }
    refs.cameraCandidates.innerHTML = items.map((camera) => `<article class="camera-candidate">
      ${camera.preview_data_url ? `<img src="${escapeHtml(camera.preview_data_url)}" alt="相机 ${Number(camera.index)} 扫描预览" />` : '<div class="preview-empty">无预览</div>'}
      <div class="candidate-body"><div class="candidate-meta"><strong>相机 ${Number(camera.index)}</strong><span>${Number(camera.width)}×${Number(camera.height)} · ${Number(camera.fps || 0).toFixed(0)} fps</span></div>
      <div class="candidate-actions"><button type="button" data-role="right_eye" data-index="${Number(camera.index)}">设为右眼</button><button type="button" data-role="left_eye" data-index="${Number(camera.index)}">设为左眼</button></div></div>
    </article>`).join("");
    refs.cameraCandidates.querySelectorAll("button[data-role]").forEach((button) => {
      button.addEventListener("click", () => {
        const target = button.dataset.role === "right_eye" ? refs.rightCameraIndex : refs.leftCameraIndex;
        target.value = String(button.dataset.index || "0");
      });
    });
  }

  function renderDeviceSettings() {
    const confirmed = !!state.deviceSettings?.confirmed;
    refs.deviceSettingsPanel.classList.toggle("hidden", !state.settingsOpen);
    refs.deviceSettingsButton.textContent = confirmed ? "设备设置" : "确认设备";
    const configured = state.status?.preflight?.cameras || {};
    const right = configured.right_eye;
    const left = configured.left_eye;
    const mapping = right && left ? `右眼 ${Number(right.index)} · 左眼 ${Number(left.index)}` : "等待实时映射";
    refs.deviceSettingsState.textContent = confirmed ? `已在本机确认 · ${mapping}` : `尚未确认 · ${mapping}`;
    refs.deviceSettingsState.classList.toggle("confirmed", confirmed);
    renderCameraCandidates();
  }

  function evaluateCameraRecovery() {
    const status = state.status;
    const recovery = state.recovery;
    // Never restart while recording or while the operator is editing a mapping.
    if (!status || status.simulate || status.app_state !== "ready" || !status.device_settings?.confirmed || !state.deviceSettings?.confirmed || state.busy || state.settingsOpen) {
      recovery.lowFpsPolls = 0;
      return;
    }
    const cameras = Object.values(status.preflight?.cameras || {});
    const unhealthy = cameras.filter((camera) => {
      if (!camera.required) return false;
      const fps = Number(camera.capture_fps || 0);
      const minimum = Number(camera.minimum_fps || 0);
      const stale = Number(camera.last_frame_age_ms || 0) > 2500;
      return camera.state === "error" || (minimum > 0 && fps > 0 && fps < minimum) || stale;
    });
    if (!unhealthy.length) {
      recovery.lowFpsPolls = 0;
      return;
    }
    recovery.lowFpsPolls += 1;
    // Require two consecutive status samples and throttle retries to avoid a restart loop.
    if (recovery.lowFpsPolls < 2 || recovery.inFlight || Date.now() - recovery.lastAttemptAt < 15000) return;
    recovery.inFlight = true;
    recovery.lastAttemptAt = Date.now();
    const names = unhealthy.map((camera) => `${cameraNameLabel(camera.name)}（相机 ${Number(camera.index)}）`).join("、");
    setNotice(`检测到 ${names} 出帧异常，正在自动恢复采集设备。`, "error");
    request("/api/restart", { method: "POST", body: "{}" }, 30000)
      .then((result) => {
        if (result.preflight?.ready) {
          setNotice("采集设备已自动恢复并通过检查。", "success");
          showToast("低帧率设备已恢复");
        } else {
          showToast(result.preflight?.blocking_reasons?.[0] || "设备仍未恢复，请检查 USB 连接");
        }
      })
      .catch((error) => showToast(error.message || "设备自动恢复失败，请点击重新检查"))
      .finally(() => {
        recovery.inFlight = false;
        recovery.lowFpsPolls = 0;
        refresh({ quiet: true });
      });
  }

  async function loadDeviceSettings({ open = false } = {}) {
    const settings = await request("/api/device-settings");
    state.deviceSettings = settings;
    state.settingsOpen = open || (!state.status?.simulate && !settings.confirmed);
    setDeviceSettingsDraft(settings);
    renderDeviceSettings();
    if (settings.load_error) showToast(settings.load_error);
    return settings;
  }

  function deviceSettingsPayload() {
    return {
      version: Number(state.deviceSettings?.version || 1),
      confirmed: true,
      cameras: {
        right_eye: { index: Number(refs.rightCameraIndex.value), frame_transform: refs.rightCameraTransform.value },
        left_eye: { index: Number(refs.leftCameraIndex.value), frame_transform: refs.leftCameraTransform.value },
      },
    };
  }

  function formatBytes(value) {
    const bytes = Number(value || 0);
    if (!bytes) return "—";
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }

  function renderExportInfo() {
    const info = state.exportInfo;
    const visible = !!info && info.session_id === state.selectedSession;
    refs.exportResult.classList.toggle("hidden", !visible);
    if (!visible) return;
    refs.exportPath.textContent = info.path || "—";
    refs.exportSize.textContent = formatBytes(info.size_bytes);
    refs.downloadExport.href = info.download_url || "#";
    refs.downloadExport.download = info.filename || "clinical-session.zip";
  }

  function renderAll() {
    renderHeader(); renderMetrics(); renderCameras(); renderWorkflow(); renderSessions(); renderReviewPanel(); renderDeviceSettings(); renderFormState();
  }

  async function refresh({ quiet = false } = {}) {
    try {
      const [statusPayload, sessionsPayload] = await Promise.all([request("/api/status"), request("/api/sessions")]);
      state.status = statusPayload; state.sessions = sessionsPayload.items || [];
      refs.connection.classList.add("online"); refs.connection.classList.remove("offline"); refs.connectionLabel.textContent = "本地服务在线";
      const alignment = statusPayload.preflight?.pupil_alignment;
      const guidance = alignment?.required && !alignment?.ready ? alignment.guidance : null;
      setNotice(statusPayload.last_error || (statusPayload.preflight?.ready ? "设备预检通过，可以开始采集。" : (guidance || statusPayload.preflight?.blocking_reasons?.[0] || "等待设备检查。")), statusPayload.preflight?.ready ? "success" : (statusPayload.last_error ? "error" : ""));
      renderAll();
      evaluateCameraRecovery();
    } catch (error) {
      refs.connection.classList.remove("online"); refs.connection.classList.add("offline"); refs.connectionLabel.textContent = "服务不可用";
      setNotice(error.message || "无法连接本地采集服务", "error");
      if (!quiet) showToast(error.message || "无法连接本地采集服务");
    }
  }

  async function withBusy(action) {
    if (state.busy) return;
    state.busy = true; renderFormState();
    try { await action(); } catch (error) { showToast(error.message || "操作失败"); } finally { state.busy = false; await refresh({ quiet: true }); }
  }

  async function exportSession(sessionId) {
    if (!sessionId) return;
    state.selectedSession = sessionId;
    await withBusy(async () => {
      // Annotation is generated from every recorded frame; a long session can
      // legitimately take more than the normal API timeout.
      const info = await request(`/api/session/${encodeURIComponent(sessionId)}/export-info`, {}, 180000);
      state.exportInfo = info;
      renderExportInfo();
      setNotice(`已导出到：${info.path}`, "success");
      showToast(`导出完成：${info.filename}`);
    });
  }

  refs.form.addEventListener("submit", (event) => {
    event.preventDefault();
    withBusy(async () => {
      const payload = formPayload();
      const result = await request("/api/session/start", { method: "POST", body: JSON.stringify(payload) }, 12000);
      state.selectedSession = result.session?.session_id || null;
      setNotice("采集已经开始。请保持设备位置稳定，结束时点击“结束并保存”。", "success");
    });
  });

  refs.stop.addEventListener("click", () => {
    if (!window.confirm("确认结束本次采集并保存文件吗？")) return;
    withBusy(async () => {
      const result = await request("/api/session/stop", { method: "POST", body: JSON.stringify({}) }, 12000);
      state.selectedSession = result.session?.session_id || null;
      setNotice(result.session?.state === "completed" ? "会话已保存，建议先查看质量指标再导出。" : (result.session?.error || "会话已结束，但质量检查需要人工复核。"), result.session?.state === "completed" ? "success" : "error");
    });
  });

  refs.preflight.addEventListener("click", () => withBusy(async () => {
    if (!state.status?.simulate && !state.status?.device_settings?.confirmed) {
      await loadDeviceSettings({ open: true });
      setNotice("请确认左右眼相机和画面方向。", "error");
      return;
    }
    const check = await request("/api/preflight", { method: "POST", body: "{}" });
    if (check.ready) {
      setNotice("设备检查已刷新。", "success");
      return;
    }
    setNotice("发现设备异常，正在重新启动双眼采集进程。", "error");
    const restarted = await request("/api/restart", { method: "POST", body: "{}" }, 20000);
    if (!restarted.preflight?.ready) {
      throw new Error(restarted.preflight?.blocking_reasons?.[0] || "设备重启后仍未通过检查，请检查相机连接");
    }
    setNotice("双眼采集设备已恢复并通过检查。", "success");
  }));
  refs.deviceSettingsButton.addEventListener("click", () => withBusy(async () => {
    await loadDeviceSettings({ open: true });
  }));
  refs.cancelDeviceSettings.addEventListener("click", () => {
    state.settingsOpen = false;
    renderDeviceSettings();
  });
  refs.swapCameras.addEventListener("click", () => {
    const rightIndex = refs.rightCameraIndex.value;
    const rightTransform = refs.rightCameraTransform.value;
    refs.rightCameraIndex.value = refs.leftCameraIndex.value;
    refs.rightCameraTransform.value = refs.leftCameraTransform.value;
    refs.leftCameraIndex.value = rightIndex;
    refs.leftCameraTransform.value = rightTransform;
  });
  refs.scanCameras.addEventListener("click", () => withBusy(async () => {
    state.settingsOpen = true;
    setNotice("正在扫描本机相机，完成后会自动恢复实时预览。", "");
    const result = await request("/api/cameras/scan", { method: "POST", body: JSON.stringify({ max_index: 3 }) }, 45000);
    state.cameraCandidates = result.cameras || [];
    renderDeviceSettings();
    if (state.cameraCandidates.length) {
      setNotice(`发现 ${state.cameraCandidates.length} 个可用相机。`, "success");
    } else {
      throw new Error("未发现可用相机，请检查 USB、驱动和 Windows 相机权限");
    }
  }));
  refs.deviceSettingsForm.addEventListener("submit", (event) => {
    event.preventDefault();
    withBusy(async () => {
      const payload = deviceSettingsPayload();
      if (payload.cameras.right_eye.index === payload.cameras.left_eye.index) throw new Error("左右眼不能选择同一个相机");
      setNotice("正在应用设置并重启双眼相机。", "");
      const result = await request("/api/device-settings", { method: "PUT", body: JSON.stringify(payload) }, 40000);
      state.deviceSettings = result.settings;
      state.cameraCandidates = [];
      state.settingsOpen = false;
      setDeviceSettingsDraft(result.settings);
      renderDeviceSettings();
      setNotice("相机设置已保存，实时预览已恢复。", "success");
    });
  });
  refs.restart.addEventListener("click", () => { if (window.confirm("重新启动采集进程？正在录制的会话会先安全结束。")) withBusy(async () => { await request("/api/restart", { method: "POST", body: "{}" }, 15000); setNotice("采集进程正在重新启动。", "success"); }); });
  refs.shutdown.addEventListener("click", () => { if (window.confirm("停止本地采集服务？正在录制的会话会先安全保存。")) withBusy(async () => { await request("/api/shutdown", { method: "POST", body: "{}" }, 15000); setNotice("本地采集服务已停止，可以关闭此页面。", "success"); }); });
  refs.refreshSessions.addEventListener("click", () => withBusy(async () => { await refresh({ quiet: true }); }));
  refs.copyExportPath.addEventListener("click", async () => {
    const path = state.exportInfo?.path;
    if (!path) return;
    try {
      await navigator.clipboard.writeText(path);
      showToast("导出路径已复制");
    } catch {
      // Clipboard permissions can be disabled in a locked-down clinic browser.
      window.prompt("请复制导出路径", path);
    }
  });

  refs.screeningForm.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!state.selectedSession) { showToast("没有可附加的会话"); return; }
    withBusy(async () => {
      const scoreText = $("screening-score").value.trim();
      await request(`/api/session/${encodeURIComponent(state.selectedSession)}/screening`, { method: "POST", body: JSON.stringify({ screening: $("screening-type").value, label: $("screening-label").value.trim(), score: scoreText ? Number(scoreText) : null, notes: "" }) });
      setNotice("筛查备注已保存到会话。", "success");
    });
  });

  ["subject-code", "visit-id", "operator", "consent"].forEach((id) => $(id).addEventListener("input", renderFormState));
  $("consent").addEventListener("change", renderFormState);
  window.addEventListener("beforeunload", (event) => { if (state.status?.app_state === "recording") { event.preventDefault(); event.returnValue = "正在采集"; } });
  window.setInterval(updateClock, 1000); updateClock();
  window.setInterval(() => { if (state.status?.app_state === "recording" || state.status?.app_state === "ready") updatePreviewSources(); }, 120);
  window.setInterval(() => { refresh({ quiet: true }); }, 1000);
  (async () => {
    await refresh();
    try { await loadDeviceSettings(); } catch (error) { showToast(error.message || "无法读取本机相机设置"); }
  })();
})();
