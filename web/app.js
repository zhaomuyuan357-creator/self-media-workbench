const $ = (selector) => document.querySelector(selector);
let state = { account: null, jobs: [], settings: {}, monitor: [], generator: {}, imageGenerator: {}, toapis: {}, trendItems: [], generatedImages: [], agentReach: {} };
let selectedImageIds = [];
let selectedSources = [];
let verifyJobId = null;
let toastTimer = null;
let currentPage = "account";
let profileDirty = false;
let connectorMessage = "";
let monitorTimer = null;
let toapisPollTimer = null;
let activeToapisGeneration = "";
let selectedGeneratorProfileId = "";

const pageTitles = {
  account: "账号定位", content: "内容制作", monitor: "账号监测",
  records: "发布记录", settings: "服务设置",
};
const statusLabels = {
  draft: ["草稿", ""], prepared: ["待发布", ""], submitting: ["处理中", "pending"],
  submitted: ["已提交 · 待核验", "pending"], under_review: ["平台审核中", "pending"],
  verified_visible: ["已核实可见", ""], failed: ["未成功", "failed"],
  unknown_manual_check: ["结果不明 · 请核验", "pending"], simulated: ["模拟完成 · 未发布", ""],
};

async function api(path, options = {}) {
  const response = await fetch(path, options);
  let value = {};
  try { value = await response.json(); } catch (_) { /* empty response */ }
  if (!response.ok) {
    const error = new Error(value.error || `请求失败 (${response.status})`);
    error.status = response.status;
    error.jobId = value.job_id;
    throw error;
  }
  return value;
}

function toast(message, error = false) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.toggle("error", error);
  node.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.remove("show"), 3400);
}

function formatBytes(bytes) {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(bytes > 1024 * 1024 * 100 ? 0 : 1)} MB`;
}

function formatDate(value) {
  if (!value) return "—";
  try { return new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(new Date(value)); }
  catch (_) { return value; }
}

function jsonPost(value) {
  return { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(value) };
}

function fillAccount(account) {
  state.account = account;
  const profile = account.profile || {};
  $("#accountName").value = account.display_name || "";
  $("#profileLane").value = profile.content_lane || "";
  $("#positioning").value = profile.positioning || "";
  $("#audience").value = profile.audience || "";
  $("#tone").value = profile.tone || "";
  $("#structure").value = profile.structure || "";
  $("#boundaries").value = profile.boundaries || "";
  $("#factRules").value = profile.fact_rules || "";
  $("#topics").value = (profile.preferred_topics || []).join(", ");
  $("#profileVersion").textContent = account.configured ? `定位 v${account.current_profile_version}` : "未完成";
  $("#contentAccountName").textContent = account.display_name || "请先配置账号";
  $("#previewAccountName").textContent = account.display_name || "小红书账号";
  if (!$("#trendQuery").value.trim()) $("#trendQuery").value = profile.content_lane || "";
  const ready = Boolean(account.configured);
  $("#profileNavState").textContent = ready ? "已完成" : "待设置";
  $("#profileNavState").classList.toggle("ready", ready);
  $("#profileStatusCard").classList.toggle("ready", ready);
  $("#profileStatusTitle").textContent = ready ? "账号定位已完成" : "账号定位待完成";
  $("#profileStatusHint").textContent = ready ? `当前使用定位 v${account.current_profile_version}` : "完成必填项后解锁内容制作";
  $("#workflowContentStep").classList.toggle("ready", ready);
  $("#workflowContentStep").classList.toggle("locked", !ready);
  $("#goContentButton").disabled = !ready;
  $("#goContentButton").textContent = ready ? "进入内容制作 →" : "完成定位后继续";
  $("#contentLocked").classList.toggle("hidden", ready);
  $("#contentWorkspace").classList.toggle("hidden", !ready);
  $("#checkAccount").textContent = ready ? "✓" : "○";
  $("#checkAccount").classList.toggle("done", ready);
  profileDirty = false;
}

function readAccountForm() {
  return {
    display_name: $("#accountName").value.trim(),
    profile: {
      content_lane: $("#profileLane").value.trim(),
      positioning: $("#positioning").value.trim(),
      audience: $("#audience").value.trim(),
      tone: $("#tone").value.trim(),
      structure: $("#structure").value.trim(),
      boundaries: $("#boundaries").value.trim(),
      fact_rules: $("#factRules").value.trim(),
      preferred_topics: $("#topics").value.split(",").map((item) => item.trim().replace(/^#/, "")).filter(Boolean),
    },
  };
}

async function saveProfile() {
  $("#profileSaveState").textContent = "正在保存账号定位…";
  try {
    const account = await api("/api/account", jsonPost(readAccountForm()));
    fillAccount(account);
    $("#profileSaveState").textContent = account.configured
      ? `已保存 · 定位 v${account.current_profile_version}`
      : "草稿已保存；请补齐必填项后解锁内容制作。";
    if (account.configured) {
      toast("账号定位已保存，可以开始制作内容");
      navigate("content");
    } else {
      toast("定位草稿已保存，请补齐账号名称、赛道、受众、定位和语气", true);
    }
    return account;
  } catch (error) {
    $("#profileSaveState").textContent = "保存失败，请检查填写内容";
    toast(error.message, true);
    throw error;
  }
}

function navigate(page, updateHash = true) {
  if (!pageTitles[page]) page = "account";
  if (page === "content" && profileDirty) {
    toast("账号定位有未保存的修改，请先保存", true);
    page = "account";
  }
  clearInterval(monitorTimer);
  monitorTimer = null;
  currentPage = page;
  document.querySelectorAll(".page-view").forEach((node) => node.classList.toggle("active", node.id === `page-${page}`));
  document.querySelectorAll("[data-page]").forEach((node) => {
    if (node.classList.contains("nav-item")) node.classList.toggle("active", node.dataset.page === page);
  });
  $("#breadcrumbTitle").textContent = pageTitles[page];
  if (updateHash && location.hash !== `#${page}`) history.replaceState(null, "", `#${page}`);
  if (page === "monitor") openMonitorPage();
}

function updatePreview() {
  const selectedImages = selectedImageIds.map((id) => state.generatedImages.find((item) => item.id === id)).filter(Boolean);
  const image = selectedImages[0];
  const imageNode = $("#notePreviewImage");
  const placeholder = $("#noteImagePlaceholder");
  if (image) { imageNode.src = image.url; imageNode.classList.remove("hidden"); placeholder.classList.add("hidden"); }
  else { imageNode.removeAttribute("src"); imageNode.classList.add("hidden"); placeholder.classList.remove("hidden"); }
  $("#previewAccountName").textContent = $("#accountName").value.trim() || "小红书账号";
  $("#previewTitle").textContent = $("#postTitle").value.trim() || "标题预览";
  $("#previewBody").textContent = $("#postBody").value.trim() || "生成文案后，这里会同步展示小红书正文。";
  $("#bodyCount").textContent = String($("#postBody").value.length);
  const tags = $("#postTags").value.split(",").map((tag) => tag.trim().replace(/^#/, "")).filter(Boolean).slice(0, 10);
  const tagRoot = $("#previewTags"); tagRoot.replaceChildren();
  const renderedTags = tags.length ? tags : ((state.account?.profile?.preferred_topics || []).slice(0, 3));
  (renderedTags.length ? renderedTags : ["账号话题"]).forEach((tag) => {
    const item = document.createElement("span"); item.textContent = `#${tag}`; tagRoot.append(item);
  });
  const hasCopy = Boolean($("#postTitle").value.trim() && $("#postBody").value.trim());
  const hasImage = selectedImages.length > 0;
  $("#publishButton").disabled = !hasImage || !hasCopy || !state.account?.configured;
  $("#checkImage").textContent = hasImage ? "✓" : "○";
  $("#checkImage").classList.toggle("done", hasImage);
  $("#checkCopy").textContent = hasCopy ? "✓" : "○";
  $("#checkCopy").classList.toggle("done", hasCopy);
  $("#generateImageButton").disabled = !hasCopy || !state.imageGenerator?.configured;
}

function setPublisherSettings(settings) {
  state.settings = settings;
  const mode = settings.publisher_mode || "mock";
  $("#publisherMode").value = mode;
  $("#mcpEndpoint").value = settings.mcp_endpoint || "http://127.0.0.1:18060/mcp";
  const mock = mode === "mock";
  $("#settingsConnectionBadge").textContent = mock ? "模拟模式" : "需要检查";
  $("#settingsConnectionBadge").className = `status-pill ${mock ? "muted" : "pending"}`;
  $("#sidebarConnectionText").textContent = mock ? "模拟模式 · 不会上传" : "本机 MCP · 登录状态待检查";
  $("#sidebarConnectionDot").classList.toggle("offline", mock);
  $("#topConnectionPill").classList.toggle("mock", mock);
  $("#topConnectionPill span").textContent = mock ? "模拟模式" : "检查登录状态";
  $("#contentConnectionText").textContent = mock ? "模拟模式 · 不会向平台提交" : "本机小红书连接待检查";
  $("#contentConnectionDot").classList.toggle("offline", mock);
}

const generatorProviderNames = {
  custom: "自定义接口", openai: "OpenAI", deepseek: "DeepSeek",
  qwen: "通义千问", aixhan: "AixHan 中转",
};

function currentGeneratorProfile() {
  return (state.generator?.profiles || []).find((item) => item.id === selectedGeneratorProfileId) || null;
}

function syncGeneratorProfileForm() {
  const profile = currentGeneratorProfile();
  if (!profile) return;
  profile.name = $("#generatorProfileName").value.trim();
  profile.provider = $("#generatorProvider").value;
  profile.base_url = $("#generatorBaseUrl").value.trim();
  profile.protocol = $("#generatorProtocol").value;
  profile.model = $("#generatorModel").value.trim();
}

function loadGeneratorProfileForm(profile) {
  if (!profile) {
    $("#generatorProfileName").value = "";
    $("#generatorProvider").value = "custom";
    $("#generatorBaseUrl").value = "https://api.openai.com/v1";
    $("#generatorProtocol").value = "chat_completions";
    $("#generatorModel").value = "";
    $("#generatorApiKey").value = "";
    $("#generatorApiKey").placeholder = "输入这套模型方案对应的 API Key";
    $("#clearGeneratorKey").checked = false;
    return;
  }
  $("#generatorProfileName").value = profile?.name || "";
  $("#generatorProvider").value = profile?.provider || "custom";
  $("#generatorBaseUrl").value = profile?.base_url || "https://api.openai.com/v1";
  $("#generatorProtocol").value = profile?.protocol || "chat_completions";
  $("#generatorModel").value = profile?.model || "";
  $("#generatorApiKey").value = "";
  $("#generatorApiKey").placeholder = profile?.key_in_memory
    ? "已保存本次运行的密钥；留空保留，重启后需重新输入"
    : "输入这套模型方案对应的 API Key";
  $("#clearGeneratorKey").checked = false;
}

function renderGeneratorProfiles() {
  const root = $("#generatorProfiles");
  const profiles = state.generator?.profiles || [];
  root.replaceChildren();
  if (!profiles.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "还没有模型方案，添加一套兼容接口开始配置。";
    root.append(empty);
  }
  profiles.forEach((profile, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `generator-profile-item${profile.id === selectedGeneratorProfileId ? " selected" : ""}`;
    button.setAttribute("aria-pressed", profile.id === selectedGeneratorProfileId ? "true" : "false");
    const rank = document.createElement("span");
    rank.className = "generator-profile-rank";
    rank.textContent = String(index + 1).padStart(2, "0");
    const copy = document.createElement("span");
    copy.className = "generator-profile-copy";
    const name = document.createElement("b");
    name.textContent = profile.name || "未命名方案";
    const detail = document.createElement("small");
    detail.textContent = `${generatorProviderNames[profile.provider] || "兼容接口"} · ${profile.model || "待填写模型 ID"}`;
    copy.append(name, detail);
    const status = document.createElement("span");
    status.className = `generator-profile-state${profile.key_in_memory ? " ready" : ""}`;
    status.textContent = profile.key_in_memory ? (index === 0 ? "首选 · 已配置" : "已配置") : (index === 0 ? "首选 · 待填 Key" : "待填 Key");
    button.append(rank, copy, status);
    button.addEventListener("click", () => {
      syncGeneratorProfileForm();
      selectedGeneratorProfileId = profile.id;
      loadGeneratorProfileForm(profile);
      renderGeneratorProfiles();
    });
    root.append(button);
  });
  const selectedIndex = profiles.findIndex((item) => item.id === selectedGeneratorProfileId);
  $("#moveGeneratorProfileUpButton").disabled = selectedIndex <= 0;
  $("#moveGeneratorProfileDownButton").disabled = selectedIndex < 0 || selectedIndex >= profiles.length - 1;
  $("#deleteGeneratorProfileButton").disabled = selectedIndex < 0;
}

function setGeneratorSettings(settings) {
  state.generator = settings || {};
  state.generator.profiles = Array.isArray(settings?.profiles) ? settings.profiles : [];
  $("#generatorAutoFallback").checked = settings?.auto_fallback !== false;
  if (!state.generator.profiles.some((item) => item.id === selectedGeneratorProfileId)) {
    selectedGeneratorProfileId = state.generator.profiles[0]?.id || "";
  }
  renderGeneratorProfiles();
  loadGeneratorProfileForm(currentGeneratorProfile());
  const configuredCount = state.generator.profiles.filter((item) => item.key_in_memory).length;
  const configured = configuredCount > 0;
  const primary = state.generator.profiles[0];
  $("#settingsGeneratorBadge").textContent = state.generator.profiles.length
    ? `${configuredCount}/${state.generator.profiles.length} 套已配置`
    : "未配置";
  $("#settingsGeneratorBadge").className = `status-pill ${configured ? "pending" : "muted"}`;
  $("#generatorStatusPill").textContent = configured
    ? `首选 · ${primary?.model || "模型方案"}` : "模型未配置";
  $("#generatorStatusPill").className = `status-pill ${configured ? "pending" : "muted"}`;
  $("#generationHint").textContent = configured
    ? `已配置 ${configuredCount} 套模型；首选方案按列表顺序执行，模型仍需逐个测试。`
    : "模型未配置；点击生成会带你去连接模型服务。";
  if (configured) {
    $("#generatorState").textContent = "模型方案已保存；可分别测试连接。API Key 仅在本次运行中有效。";
    $("#generatorState").className = "settings-result pending";
  } else {
    $("#generatorState").textContent = "添加模型方案后，可分别测试并在内容制作中使用。";
    $("#generatorState").className = "settings-result";
  }
}

function setImageGeneratorSettings(settings) {
  state.imageGenerator = settings || {};
  $("#imageBaseUrl").value = settings.base_url || "https://api.openai.com/v1";
  $("#imageModel").value = settings.model || "gpt-image-1";
  const configured = Boolean(settings.configured);
  $("#settingsImageBadge").textContent = configured ? "已配置" : "未配置";
  $("#settingsImageBadge").className = `status-pill ${configured ? "success" : "muted"}`;
  $("#imageGeneratorStatusPill").textContent = configured ? `已连接 · ${settings.model}` : "生图服务未配置";
  $("#imageGeneratorStatusPill").className = `status-pill ${configured ? "success" : "muted"}`;
  $("#imageGeneratorState").textContent = configured
    ? "生图服务已就绪；每次生成会调用已配置的模型接口。"
    : "配置后可在内容制作页生成配图。API Key 只保存在本次工作台进程内存。";
  updatePreview();
}

function setToapisSettings(settings) {
  state.toapis = settings || {};
  $("#toapisRegion").value = settings.region || "com";
  const configured = Boolean(settings.configured);
  $("#settingsToapisBadge").textContent = configured ? "已配置" : "未连接";
  $("#settingsToapisBadge").className = `status-pill ${configured ? "success" : "muted"}`;
  $("#toapisMediaStatus").textContent = configured ? "ToAPIs 已配置" : "未连接";
  $("#toapisMediaStatus").className = `status-pill ${configured ? "success" : "muted"}`;
  $("#toapisGenerationState").textContent = configured
    ? "读取模型和参数说明后即可提交任务；生成任务可能需要数分钟。"
    : "请先在服务设置中配置 ToAPIs Key 并完成账号定位。";
  $("#toapisGenerateButton").disabled = !configured || !state.account?.configured;
}

async function saveToapisSettings(showToast = true) {
  const apiKey = $("#toapisApiKey").value.trim();
  const previousRegion = state.toapis?.region;
  const region = $("#toapisRegion").value;
  const settings = await api("/api/toapis/settings", jsonPost({
    region, api_key: apiKey, clear_api_key: $("#clearToapisKey").checked,
  }));
  $("#toapisApiKey").value = "";
  $("#clearToapisKey").checked = false;
  setToapisSettings(settings);
  if (showToast) {
    toast(previousRegion && previousRegion !== region && !apiKey
      ? "站点已切换，旧 Key 已清除；请填入此站点的 ToAPIs Key"
      : settings.configured ? "ToAPIs 设置已保存" : "站点设置已保存；输入 ToAPIs Key 后可连接");
  }
  return settings;
}

async function loadToapisModels() {
  const button = $("#toapisLoadModelsButton");
  button.disabled = true; button.textContent = "正在读取…";
  try {
    if ($("#toapisApiKey").value.trim()) await saveToapisSettings(false);
    const mediaType = $("#toapisMediaType").value;
    const value = await api(`/api/toapis/models?type=${encodeURIComponent(mediaType)}`);
    $("#toapisModelList").textContent = value.models || "ToAPIs 没有返回模型列表。";
    const hasModels = value.has_models !== false;
    $("#toapisSettingsState").textContent = hasModels
      ? "模型列表读取成功。选择模型后读取它的参数说明。"
      : "连接成功，但当前 Key 没有返回可用模型。请检查 ToAPIs 控制台的模型授权和站点。";
    $("#toapisSettingsState").className = `settings-result ${hasModels ? "success" : "pending"}`;
    $("#toapisMediaStatus").textContent = hasModels ? "服务可用" : "没有可用模型";
    $("#toapisMediaStatus").className = `status-pill ${hasModels ? "success" : "pending"}`;
  } catch (error) {
    $("#toapisModelList").textContent = error.message;
    $("#toapisSettingsState").textContent = error.message;
    $("#toapisSettingsState").className = "settings-result error";
    toast(error.message, true);
  } finally { button.disabled = false; button.textContent = "读取模型列表"; }
}

async function checkToapis() {
  const button = $("#checkToapisButton");
  button.disabled = true; button.textContent = "正在检查…";
  try {
    if ($("#toapisApiKey").value.trim()) await saveToapisSettings(false);
    const result = await api("/api/toapis/check");
    const models = result.models || {};
    const balance = result.balance || {};
    const modelDetails = Object.entries(models.by_type || {}).map(([type, item]) =>
      `${type === "image" ? "图片" : "视频"}模型${item.ok ? `（${item.count || 0} 个）` : ""}：\n${item.ok ? item.value : `读取失败：${item.error || "未知错误"}`}`
    );
    $("#toapisModelList").textContent = modelDetails.join("\n\n") || models.value || "ToAPIs 没有返回模型列表。";
    if (models.schema) {
      const schema = models.schema;
      $("#toapisModelList").textContent += `\n\nlist_models 要求字段：${schema.required?.join("、") || "无"}`;
      if (schema.type_enum?.length) {
        $("#toapisModelList").textContent += `\ntype 可选值：${schema.type_enum.join("、")}`;
      }
      if (schema.properties?.length) {
        $("#toapisModelList").textContent += `\n其他字段：${schema.properties.join("、")}`;
      }
    }
    const modelState = !models.ok
      ? `模型列表读取有失败项：${models.error || "未知错误"}`
      : models.has_models ? `模型列表读取成功，共 ${models.total_count} 个可用项`
      : "模型列表接口已连接，但当前 Key 返回 0 个可用模型";
    const balanceState = balance.ok ? `额度读取成功：\n${balance.value}` : `额度读取失败：${balance.error || "未知错误"}`;
    const allOk = models.ok && models.has_models && balance.ok;
    $("#toapisSettingsState").textContent = `${modelState}\n\n${balanceState}`;
    $("#toapisSettingsState").className = `settings-result ${allOk ? "success" : result.connected ? "pending" : "error"}`;
    $("#settingsToapisBadge").textContent = allOk ? "连接正常" : models.ok && !models.has_models ? "没有可用模型" : result.connected ? "部分可用" : "连接失败";
    $("#settingsToapisBadge").className = `status-pill ${allOk ? "success" : models.ok && !models.has_models ? "pending" : result.connected ? "pending" : "failed"}`;
    if (models.ok) {
      $("#toapisMediaStatus").textContent = models.has_models ? "服务可用" : "没有可用模型";
      $("#toapisMediaStatus").className = `status-pill ${models.has_models ? "success" : "pending"}`;
    }
    const toastMessage = allOk ? "ToAPIs 连接、模型和额度检查成功"
      : models.ok && !models.has_models ? "ToAPIs 已连接，但当前没有列出可用模型"
      : result.connected ? "ToAPIs 已连接，部分查询失败；查看各项错误"
      : "ToAPIs 连接失败，请检查服务站点和 Key";
    toast(toastMessage, !allOk);
  } catch (error) {
    $("#toapisSettingsState").textContent = error.message;
    $("#toapisSettingsState").className = "settings-result error";
    toast(error.message, true);
  } finally { button.disabled = false; button.textContent = "检查服务和额度"; }
}

async function loadToapisDocs() {
  const model = $("#toapisModel").value.trim();
  if (!model) { toast("先填写模型 ID", true); return; }
  const button = $("#toapisLoadDocsButton");
  button.disabled = true; button.textContent = "正在读取…";
  try {
    if ($("#toapisApiKey").value.trim()) await saveToapisSettings(false);
    const docs = await api("/api/toapis/models/docs", jsonPost({ model }));
    $("#toapisModelDocs").textContent = docs.docs || "该模型没有返回参数说明。";
    $("#toapisParams").dataset.documented = JSON.stringify(docs.parameter_names || []);
    if (!$("#toapisParams").value.trim() && (docs.parameter_names || []).some((name) => name.toLowerCase() === "prompt")) {
      const prompt = $("#toapisMediaType").value === "image" ? suggestedImagePrompt() : $("#contentIdea").value.trim();
      if (prompt) $("#toapisParams").value = JSON.stringify({ prompt }, null, 2);
    }
    $("#toapisGenerationState").textContent = "已读取模型文档。请按字段说明调整参数 JSON，再提交生成。";
    $("#toapisGenerationState").className = "settings-result success";
  } catch (error) {
    $("#toapisModelDocs").textContent = error.message;
    $("#toapisGenerationState").textContent = error.message;
    $("#toapisGenerationState").className = "settings-result error";
    toast(error.message, true);
  } finally { button.disabled = false; button.textContent = "读取模型参数说明"; }
}

function renderToapisVideo(asset) {
  if (!asset || asset.media_type !== "video") return;
  const root = $("#toapisMediaResults");
  const card = document.createElement("article"); card.className = "generated-image-card toapis-video-card";
  const video = document.createElement("video"); video.src = asset.url; video.controls = true; video.preload = "metadata";
  const title = document.createElement("small"); title.textContent = `${asset.original_name} · ${formatBytes(asset.size_bytes)}`;
  const link = document.createElement("a"); link.href = asset.url; link.download = asset.original_name; link.textContent = "下载视频";
  card.append(video, title, link); root.prepend(card);
}

function showToapisGeneration(record) {
  activeToapisGeneration = record.client_request_id;
  const stateText = record.state;
  if (record.asset) {
    if (record.media_type === "image") {
      const images = [record.asset, ...state.generatedImages];
      renderGeneratedImages(images.filter((image, index) => images.findIndex((candidate) => candidate.id === image.id) === index));
    }
    else renderToapisVideo(record.asset);
  }
  const labels = { starting: "正在提交", running: "生成中", unknown: "结果待确认", completed: "已完成并保存到本机", failed: "生成失败", delivery_error: "生成完成但保存失败" };
  $("#toapisGenerationState").textContent = `${labels[stateText] || stateText}${record.task_id ? ` · 任务 ${record.task_id}` : ""}${record.error_text ? `\n${record.error_text}` : ""}`;
  $("#toapisGenerationState").className = `settings-result ${stateText === "completed" ? "success" : ["failed", "delivery_error"].includes(stateText) ? "error" : "pending"}`;
  $("#toapisRetryDownloadButton").hidden = stateText !== "delivery_error";
  if (["running", "starting"].includes(stateText)) {
    clearTimeout(toapisPollTimer);
    toapisPollTimer = setTimeout(pollToapisGeneration, Math.max(1, record.poll_after_seconds || 5) * 1000);
  } else {
    clearTimeout(toapisPollTimer); toapisPollTimer = null;
    $("#toapisGenerateButton").disabled = !state.toapis?.configured || !state.account?.configured || stateText === "delivery_error";
    $("#toapisGenerateButton").textContent = stateText === "unknown" ? "查询任务状态" : stateText === "delivery_error" ? "请先重试下载" : "提交 ToAPIs 生成任务";
  }
}

async function pollToapisGeneration() {
  if (!activeToapisGeneration) return;
  try {
    const record = await api(`/api/toapis/generations/${encodeURIComponent(activeToapisGeneration)}`);
    showToapisGeneration(record);
  } catch (error) {
    $("#toapisGenerationState").textContent = `${error.message}\n请求状态尚未确认；请稍后查询，不要重复提交。`;
    $("#toapisGenerationState").className = "settings-result error";
    $("#toapisGenerateButton").disabled = false;
    $("#toapisGenerateButton").textContent = "查询任务状态";
  }
}

async function submitToapisGeneration() {
  if (activeToapisGeneration && $("#toapisGenerateButton").textContent === "查询任务状态") {
    await pollToapisGeneration(); return;
  }
  let params;
  try { params = JSON.parse($("#toapisParams").value); }
  catch (_) { toast("模型参数 JSON 格式不正确", true); return; }
  const button = $("#toapisGenerateButton");
  button.disabled = true; button.textContent = "正在提交…";
  $("#toapisGenerationState").textContent = "正在核对模型列表和参数文档，再提交一次生成请求。";
  const clientRequestId = `toapis-${Math.floor(Date.now() / 1000)}-${crypto.randomUUID().replaceAll("-", "").slice(0, 12)}`;
  activeToapisGeneration = clientRequestId;
  try {
    const record = await api("/api/toapis/generations", jsonPost({
      media_type: $("#toapisMediaType").value,
      model: $("#toapisModel").value.trim(), params, client_request_id: clientRequestId,
    }));
    showToapisGeneration(record);
    if (record.state === "unknown") toast("任务结果暂时无法确认，请查询任务状态，不要重复生成", true);
  } catch (error) {
    if (error.status === 400 || error.status === 409) {
      activeToapisGeneration = "";
      $("#toapisGenerationState").textContent = error.message;
      $("#toapisGenerationState").className = "settings-result error";
      button.disabled = !state.toapis?.configured || !state.account?.configured;
      button.textContent = "提交 ToAPIs 生成任务";
      toast(error.message, true);
    } else {
      $("#toapisGenerationState").textContent = `${error.message}\n请求可能已发出；请查询这个任务状态，不要重新提交。`;
      $("#toapisGenerationState").className = "settings-result pending";
      button.disabled = false; button.textContent = "查询任务状态";
      toast("请求结果尚未确认，请查询任务状态", true);
    }
  }
}

async function retryToapisDownload() {
  if (!activeToapisGeneration) return;
  const button = $("#toapisRetryDownloadButton");
  button.disabled = true; button.textContent = "正在重试下载…";
  try {
    const record = await api(
      `/api/toapis/generations/${encodeURIComponent(activeToapisGeneration)}/retry-download`,
      jsonPost({}),
    );
    showToapisGeneration(record);
    if (record.state === "completed") toast("生成结果已保存到本机素材库");
  } catch (error) {
    $("#toapisGenerationState").textContent = `${error.message}\n可再次重试下载；不会重新提交生成任务。`;
    $("#toapisGenerationState").className = "settings-result error";
    toast(error.message, true);
  } finally {
    button.disabled = false; button.textContent = "重试下载结果";
  }
}

async function saveImageGeneratorSettings(showToast = true) {
  const apiKey = $("#imageApiKey").value.trim();
  const settings = await api("/api/image-generator/settings", jsonPost({
    base_url: $("#imageBaseUrl").value.trim(),
    model: $("#imageModel").value.trim(),
    api_key: apiKey,
    clear_api_key: $("#clearImageKey").checked,
  }));
  $("#imageApiKey").value = "";
  $("#clearImageKey").checked = false;
  setImageGeneratorSettings(settings);
  if (showToast) toast(settings.configured ? "生图服务设置已保存" : "生图地址已保存；输入 API Key 后即可生成");
  return settings;
}

function renderGeneratedImages(images) {
  state.generatedImages = (images || []).filter((image) => image.media_type === "image");
  selectedImageIds = selectedImageIds.filter((id) => state.generatedImages.some((image) => image.id === id));
  const root = $("#generatedImageGrid"); root.replaceChildren();
  if (!state.generatedImages.length) {
    const empty = document.createElement("div"); empty.className = "inline-empty"; empty.textContent = "配图生成后会显示在这里。"; root.append(empty);
    updatePreview(); return;
  }
  state.generatedImages.forEach((image) => {
    const card = document.createElement("article");
    const selected = selectedImageIds.includes(image.id);
    card.className = `generated-image-card${selected ? " selected" : ""}`;
    const preview = document.createElement("img"); preview.src = image.url; preview.alt = "生成的笔记配图";
    const action = document.createElement("button"); action.type = "button";
    action.textContent = selected ? "已加入笔记 · 移除" : "加入笔记";
    action.addEventListener("click", () => {
      if (selectedImageIds.includes(image.id)) selectedImageIds = selectedImageIds.filter((id) => id !== image.id);
      else if (selectedImageIds.length < 9) selectedImageIds.push(image.id);
      else toast("一篇笔记最多选择 9 张图片", true);
      renderGeneratedImages(state.generatedImages);
    });
    card.append(preview, action); root.append(card);
  });
  updatePreview();
}

function setTrendStatus(agentReach) {
  state.agentReach = agentReach || {};
  const installed = Boolean(agentReach?.installed);
  const channels = agentReach?.channels || [];
  const activeNames = channels.filter((item) => item.status !== "not_added").map((item) => item.name);
  const missing = channels.filter((item) => item.status === "not_added").map((item) => item.name);
  $("#trendAvailability").textContent = installed
    ? `AIHOT + ${activeNames.length} 个 Agent-Reach 渠道`
    : "仅 AIHOT 可用";
  $("#trendAvailability").className = `status-pill ${installed ? "success" : "pending"}`;
  $("#trendSettingsStatus").textContent = installed
    ? `${agentReach.version} · ${activeNames.join("、") || "无默认渠道"}${missing.length ? ` · 未启用：${missing.join("、")}` : ""}`
    : "Agent-Reach 未连接";
  $("#trendSettingsStatus").className = `status-pill ${installed ? "success" : "pending"}`;
  $("#trendLimits").textContent = installed
    ? `默认渠道：${activeNames.join("、") || "无"}。${missing.length ? `尚未安装的渠道：${missing.join("、")}；Reddit 和 X 可尝试公开网页搜索。` : ""}搜索结果受各平台公开范围限制，不代表全网完整覆盖。`
    : "Agent-Reach 不可用；当前只扫描 AIHOT。Reddit 和 X 的公开网页搜索也需要 Agent-Reach 正常运行；来源覆盖有限，不代表全网完整扫描。";
}

async function saveConnectionSettings(showToast = true) {
  const settings = await api("/api/settings", jsonPost({
    publisher_mode: $("#publisherMode").value,
    mcp_endpoint: $("#mcpEndpoint").value.trim(),
  }));
  setPublisherSettings(settings);
  if (showToast) toast("平台连接设置已保存");
  return settings;
}

async function saveGeneratorSettings(showToast = true) {
  syncGeneratorProfileForm();
  const profiles = (state.generator?.profiles || []).map((profile) => ({
    id: profile.id,
    name: profile.name,
    provider: profile.provider,
    base_url: profile.base_url,
    protocol: profile.protocol,
    model: profile.model,
  }));
  const apiKey = $("#generatorApiKey").value.trim();
  const settings = await api("/api/generator/settings", jsonPost({
    profiles,
    auto_fallback: $("#generatorAutoFallback").checked,
    api_key_profile_id: selectedGeneratorProfileId,
    api_key: apiKey,
    clear_api_key: $("#clearGeneratorKey").checked,
  }));
  setGeneratorSettings(settings);
  if (showToast) toast(settings.configured ? "模型方案和优先顺序已保存" : "模型方案已保存；填写 API Key 后即可连接");
  return settings;
}

function addGeneratorProfile() {
  syncGeneratorProfileForm();
  const id = globalThis.crypto?.randomUUID?.() || `model-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
  const profile = {
    id, name: "", provider: "custom", base_url: "https://api.openai.com/v1",
    protocol: "chat_completions", model: "", key_in_memory: false, configured: false,
  };
  state.generator.profiles.push(profile);
  selectedGeneratorProfileId = id;
  renderGeneratorProfiles();
  loadGeneratorProfileForm(profile);
  $("#generatorProfileName").focus();
}

async function moveGeneratorProfile(direction) {
  syncGeneratorProfileForm();
  const profiles = state.generator.profiles || [];
  const index = profiles.findIndex((item) => item.id === selectedGeneratorProfileId);
  const next = index + direction;
  if (index < 0 || next < 0 || next >= profiles.length) return;
  [profiles[index], profiles[next]] = [profiles[next], profiles[index]];
  renderGeneratorProfiles();
  try { await saveGeneratorSettings(false); toast("模型优先顺序已更新"); }
  catch (error) {
    [profiles[index], profiles[next]] = [profiles[next], profiles[index]];
    renderGeneratorProfiles();
    toast(error.message, true);
  }
}

async function deleteGeneratorProfile() {
  const profile = currentGeneratorProfile();
  if (!profile || !window.confirm(`删除模型方案“${profile.name || profile.model || "未命名方案"}”？`)) return;
  const profiles = state.generator.profiles.filter((item) => item.id !== profile.id);
  const nextSelection = profiles[0]?.id || "";
  try {
    const settings = await api("/api/generator/settings", jsonPost({
      profiles: profiles.map(({ id, name, provider, base_url, protocol, model }) => ({ id, name, provider, base_url, protocol, model })),
      auto_fallback: $("#generatorAutoFallback").checked,
    }));
    selectedGeneratorProfileId = nextSelection;
    setGeneratorSettings(settings);
    toast("模型方案已删除");
  } catch (error) { toast(error.message, true); }
}

function applyGeneratorProviderPreset() {
  const presets = {
    openai: { base_url: "https://api.openai.com/v1", protocol: "chat_completions" },
    deepseek: { base_url: "https://api.deepseek.com", protocol: "chat_completions" },
    qwen: { base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1", protocol: "chat_completions" },
    aixhan: { base_url: "https://api.aixhan.com/v1", protocol: "responses" },
  };
  const preset = presets[$("#generatorProvider").value];
  if (preset) {
    $("#generatorBaseUrl").value = preset.base_url;
    $("#generatorProtocol").value = preset.protocol;
  }
  syncGeneratorProfileForm();
}

function renderJobs(jobs) {
  state.jobs = jobs;
  const root = $("#jobsList"); root.replaceChildren();
  $("#recordCount").textContent = String(jobs.length);
  $("#recordSummaryCount").textContent = String(jobs.length);
  $("#recordSummarySubmitted").textContent = String(jobs.filter((job) => ["submitted", "under_review", "verified_visible"].includes(job.status)).length);
  $("#recordSummaryPending").textContent = String(jobs.filter((job) => ["unknown_manual_check", "submitting"].includes(job.status)).length);
  if (!jobs.length) {
    const empty = document.createElement("div"); empty.className = "empty-state"; empty.textContent = "还没有发布任务。完成第一条内容后，记录会出现在这里。"; root.append(empty); return;
  }
  for (const job of jobs) {
    const row = document.createElement("article"); row.className = "job-row";
    const icon = document.createElement("div"); icon.className = "job-icon"; icon.textContent = job.content_type === "note" ? "▧" : "▶";
    const info = document.createElement("div"); info.className = "job-info";
    const title = document.createElement("b"); title.textContent = job.title || "未命名内容";
    const details = document.createElement("small");
    const message = job.attempt?.result?.message || job.attempt?.error || "";
    const mediaLabel = job.content_type === "note"
      ? `图文笔记 · ${job.asset_ids?.length || 1} 张图片`
      : `视频笔记 · ${job.asset_name || "视频素材"}`;
    details.textContent = `${mediaLabel} · ${formatDate(job.created_at)} · 定位 v${job.persona_version}${message ? ` · ${message}` : ""}`;
    info.append(title, details);
    if (job.source_refs?.length) {
      const source = document.createElement("small"); source.className = "job-source-count"; source.textContent = `研究来源 ${job.source_refs.length} 条`;
      info.append(source);
    }
    const side = document.createElement("div"); side.className = "job-side";
    const chip = document.createElement("span");
    const [label, extra] = statusLabels[job.status] || [job.status, ""];
    chip.className = `status-chip ${extra}`; chip.textContent = job.publisher_id === "mock" ? `模拟 · ${label}` : label;
    side.append(chip);
    if (job.publisher_id !== "mock" && ["submitted", "under_review", "unknown_manual_check"].includes(job.status)) {
      const verify = document.createElement("button"); verify.type = "button"; verify.className = "verify-button"; verify.textContent = "核验";
      verify.addEventListener("click", () => openVerify(job)); side.append(verify);
    }
    row.append(icon, info, side); root.append(row);
  }
}

function openVerify(job) {
  verifyJobId = job.id;
  $("#verifyJobTitle").textContent = `${job.title} · ${job.asset_name}`;
  $("#verifyOutcome").value = "visible";
  $("#verifyNote").value = "";
  $("#verifyDialog").showModal();
}

function renderTrendSources(sources) {
  const root = $("#trendSources"); root.replaceChildren();
  (sources || []).forEach((source) => {
    const chip = document.createElement("span");
    chip.className = `source-state${source.status !== "ok" ? " error" : ""}`;
    const name = document.createElement("b"); name.textContent = source.source;
    const detail = document.createElement("span");
    detail.textContent = source.status === "ok" ? `${source.count} 条` : (source.message || "暂不可用");
    chip.append(name, detail); root.append(chip);
  });
}

function renderTrendResults(items) {
  state.trendItems = items || [];
  selectedSources = [];
  const root = $("#trendResults"); root.replaceChildren();
  if (!state.trendItems.length) {
    const empty = document.createElement("div"); empty.className = "inline-empty"; empty.textContent = "本次没有取得热点结果；可以调整主题后重试。"; root.append(empty); return;
  }
  state.trendItems.forEach((item, index) => {
    const row = document.createElement("article"); row.className = "trend-result";
    const checkbox = document.createElement("input"); checkbox.type = "checkbox"; checkbox.dataset.index = String(index);
    const content = document.createElement("div"); content.className = "trend-result-content";
    const title = document.createElement("b"); title.textContent = item.title || "未命名热点";
    content.append(title);
    if (item.summary) { const summary = document.createElement("p"); summary.textContent = item.summary; content.append(summary); }
    const meta = document.createElement("div"); meta.className = "trend-result-meta";
    const source = document.createElement("span"); source.textContent = item.source || "来源待确认"; meta.append(source);
    if (item.published_at) { const date = document.createElement("span"); date.textContent = item.published_at; meta.append(date); }
    if (item.url) { const link = document.createElement("a"); link.href = item.url; link.target = "_blank"; link.rel = "noreferrer"; link.textContent = "查看来源"; meta.append(link); }
    content.append(meta);
    const use = document.createElement("button"); use.className = "trend-result-use"; use.type = "button"; use.textContent = "作为选题";
    use.addEventListener("click", () => {
      $("#contentIdea").value = item.title || "";
      checkbox.checked = true;
      updateSelectedTrendSources();
      $("#contentIdea").focus();
      toast("热点已带入选题，也会作为文案参考");
    });
    checkbox.addEventListener("change", () => { row.classList.toggle("selected", checkbox.checked); updateSelectedTrendSources(); });
    row.append(checkbox, content, use); root.append(row);
  });
}

function updateSelectedTrendSources() {
  selectedSources = [...$("#trendResults").querySelectorAll(".trend-result input:checked")]
    .map((checkbox) => state.trendItems[Number(checkbox.dataset.index)])
    .filter(Boolean)
    .map((item) => ({ source: item.source, title: item.title, url: item.url, published_at: item.published_at, summary: item.summary }));
  const suffix = selectedSources.length ? `已选 ${selectedSources.length} 条热点作为生成参考。` : "当前没有选中热点；可以继续使用手动主题。";
  $("#trendScanState").textContent = suffix;
}

async function scanHotspots() {
  const query = $("#trendQuery").value.trim() || state.account?.profile?.content_lane || "";
  if (!query) { toast("先设置账号赛道或输入扫描主题", true); return; }
  const button = $("#trendScanButton"); button.disabled = true; button.textContent = "正在扫描多来源热点…";
  $("#trendScanState").textContent = "正在查询 AIHOT 和所选 Agent-Reach 来源…";
  try {
    const channels = [...document.querySelectorAll("input[name='trendChannel']:checked")].map((node) => node.value);
    const result = await api("/api/trends/scan", jsonPost({ query, channels }));
    setTrendStatus(result.agent_reach);
    renderTrendSources(result.sources || []);
    renderTrendResults(result.items || []);
    const succeeded = (result.sources || []).filter((source) => source.status === "ok").length;
    $("#trendScanState").textContent = `扫描完成 · ${succeeded} 个来源有结果 · 共 ${result.items?.length || 0} 条，可勾选热点供下一步生成文案。`;
  } catch (error) { $("#trendScanState").textContent = error.message; toast(error.message, true); }
  finally { button.disabled = false; button.textContent = "扫描热点"; }
}

function renderMonitor(history) {
  state.monitor = history || [];
  const latest = state.monitor[0];
  const previous = state.monitor[1];
  if (!latest) return;
  const loggedIn = latest.login_status === "logged_in";
  $("#monitorNickname").textContent = latest.nickname || "账号主页暂不可读";
  $("#monitorCheckedAt").textContent = `上次检查：${formatDate(latest.checked_at)}${latest.red_id ? ` · 小红书号 ${latest.red_id}` : ""}`;
  $("#monitorLoginBadge").className = `login-badge ${loggedIn ? "online" : latest.login_status === "logged_out" ? "offline" : "unknown"}`;
  $("#monitorLoginBadge span").textContent = loggedIn ? "登录有效" : latest.login_status === "logged_out" ? "已退出" : "读取失败";
  $("#monitorLoginBadge i").textContent = "";
  const metrics = latest.metrics || {};
  const old = previous?.metrics || {};
  $("#metricFans").textContent = metrics.fans || "—";
  $("#metricFollows").textContent = metrics.follows || "—";
  $("#metricLikes").textContent = metrics.likes_and_saves || "—";
  $("#metricNotes").textContent = metrics.loaded_notes == null ? "—" : String(metrics.loaded_notes);
  $("#deltaFans").textContent = metricDelta(metrics.fans, old.fans);
  $("#deltaFollows").textContent = metricDelta(metrics.follows, old.follows);
  $("#deltaLikes").textContent = metricDelta(metrics.likes_and_saves, old.likes_and_saves);
  $("#notesUpdated").textContent = `同步于 ${formatDate(latest.checked_at)}`;
  const notes = $("#monitorNotes"); notes.replaceChildren();
  if (!latest.notes?.length) {
    const tr = document.createElement("tr"); const td = document.createElement("td"); td.colSpan = 5; td.className = "table-empty"; td.textContent = "本次没有读取到主页笔记。"; tr.append(td); notes.append(tr);
  } else {
    latest.notes.forEach((note) => {
      const tr = document.createElement("tr");
      const titleCell = document.createElement("td");
      const title = document.createElement("span"); title.className = "note-title-cell"; title.textContent = note.title || "未命名笔记";
      if (note.id) { const link = document.createElement("a"); link.href = `https://www.xiaohongshu.com/explore/${encodeURIComponent(note.id)}`; link.target = "_blank"; link.rel = "noreferrer"; link.textContent = "打开"; titleCell.append(title, link); }
      else titleCell.append(title);
      tr.append(titleCell, tableCell(note.likes), tableCell(note.comments), tableCell(note.saves), tableCell(note.shares));
      notes.append(tr);
    });
  }
}

function metricDelta(now, before) {
  if (before == null || now == null || now === "—" || before === "—") return "等待下一次同步";
  const a = Number(String(now).replace(/,/g, ""));
  const b = Number(String(before).replace(/,/g, ""));
  if (!Number.isFinite(a) || !Number.isFinite(b)) return "已记录快照";
  const diff = a - b;
  return diff > 0 ? `较上次 +${diff}` : diff < 0 ? `较上次 ${diff}` : "与上次持平";
}

function tableCell(value) {
  const td = document.createElement("td"); td.textContent = value || "—"; return td;
}

async function refreshMonitor(showToast = true) {
  const button = $("#refreshMonitorButton");
  button.disabled = true; button.innerHTML = "<span>↻</span>正在读取…";
  try {
    const snapshot = await api("/api/monitor/refresh", jsonPost({}));
    const value = await api("/api/monitor");
    renderMonitor([snapshot, ...(value.history || []).filter((item) => item.id !== snapshot.id)]);
    if (showToast) toast(snapshot.login_status === "logged_in" ? `账号数据已更新：${snapshot.nickname || "已登录"}` : snapshot.message || "账号状态已更新");
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false; button.innerHTML = "<span>↻</span>立即更新";
  }
}

async function openMonitorPage() {
  if (state.monitor?.length) renderMonitor(state.monitor);
  if (state.settings.publisher_mode !== "xiaohongshu-mcp") {
    $("#monitorLoginBadge").className = "login-badge unknown";
    $("#monitorLoginBadge span").textContent = "请先连接 MCP";
    return;
  }
  const latest = state.monitor?.[0];
  const age = latest ? Date.now() - new Date(latest.checked_at).getTime() : Infinity;
  if (age > 30 * 60 * 1000) await refreshMonitor(false);
  monitorTimer = setInterval(() => {
    if (currentPage === "monitor") refreshMonitor(false);
  }, 30 * 60 * 1000);
}

async function loadState() {
  const result = await api("/api/bootstrap");
  fillAccount(result.account);
  setPublisherSettings(result.settings);
  setGeneratorSettings(result.generator || {});
  setImageGeneratorSettings(result.image_generator || {});
  setToapisSettings(result.toapis || {});
  const trendResult = await api("/api/trends/status").catch(() => ({}));
  setTrendStatus(trendResult.agent_reach || {});
  renderGeneratedImages(result.assets || []);
  renderJobs(result.jobs || []);
  renderMonitor(result.monitor || []);
  (result.toapis_generations || []).filter((item) => item.asset?.media_type === "video").forEach(renderToapisVideo);
  const latestToapisGeneration = (result.toapis_generations || []).find((item) => ["starting", "running", "unknown", "delivery_error"].includes(item.state));
  if (latestToapisGeneration) showToapisGeneration(latestToapisGeneration);
  if (!$("#postTags").value && result.account?.profile?.preferred_topics?.length) {
    $("#postTags").value = result.account.profile.preferred_topics.join(", ");
  }
  updatePreview();
  const requested = (location.hash || "#account").slice(1);
  navigate(pageTitles[requested] ? requested : "account", false);
  if (state.settings.publisher_mode === "xiaohongshu-mcp") checkConnection(false).catch(() => {});
}

async function generateCopy() {
  if (!state.generator?.configured) {
    navigate("settings");
    toast("先添加并连接至少一套兼容文案模型", true);
    $("#generatorApiKey").focus();
    return;
  }
  const idea = $("#contentIdea").value.trim();
  if (!idea) { toast("先写下这条内容的主题", true); $("#contentIdea").focus(); return; }
  const button = $("#generateButton"); button.disabled = true; button.textContent = "正在按账号定位生成…";
  try {
    const result = await api("/api/generate", jsonPost({ idea, brief: $("#contentBrief").value, sources: selectedSources }));
    $("#postTitle").value = result.draft.title;
    $("#postBody").value = result.draft.body;
    $("#postTags").value = (result.draft.tags || []).join(", ");
    const routeNote = result.fallback_used ? ` · 已切换至备用方案 ${result.profile_name}` : ` · ${result.profile_name}`;
    $("#draftVersionHint").textContent = `基于定位 v${result.persona_version}${routeNote} · ${result.model} · 可编辑`;
    updatePreview();
    toast(result.fallback_used
      ? `首选模型不可用，已由 ${result.profile_name} 完成生成`
      : "文案草稿已生成，检查并编辑后再发布");
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; button.innerHTML = "<span>✦</span>按账号定位生成文案"; }
}

function suggestedImagePrompt() {
  const idea = $("#contentIdea").value.trim();
  const title = $("#postTitle").value.trim();
  const bodyLead = $("#postBody").value.trim().split(/[。！？\n]/)[0].slice(0, 100);
  const topic = title || idea || bodyLead;
  if (!topic) return "";
  return `为小红书图文笔记创作一张竖版封面插画。主题：${topic}。\n\n画面要有清晰主体、简洁构图和醒目的视觉层次，整体适合手机阅读，风格现代、可信、友好。使用与账号定位相符的配色：${state.account?.profile?.tone || "清爽克制"}。不要出现文字、字母、Logo、水印、界面截图或无法核实的图表数据。`;
}

async function generateNoteImage() {
  if (!state.imageGenerator?.configured) {
    navigate("settings");
    toast("先在服务设置中连接生图模型", true);
    $("#imageApiKey").focus();
    return;
  }
  if (!$("#postTitle").value.trim() || !$("#postBody").value.trim()) {
    toast("先完成文案，再生成配图", true);
    return;
  }
  const prompt = $("#imagePrompt").value.trim() || suggestedImagePrompt();
  if (!prompt) { toast("请先填写配图描述", true); return; }
  $("#imagePrompt").value = prompt;
  const button = $("#generateImageButton");
  button.disabled = true; button.textContent = "正在生成配图…";
  $("#imageState").textContent = "正在调用生图接口，完成后图片会保存在本机。";
  try {
    const image = await api("/api/images/generate", jsonPost({ prompt, size: $("#imageSize").value }));
    const canSelect = selectedImageIds.length < 9;
    if (canSelect) selectedImageIds = [...selectedImageIds, image.id];
    renderGeneratedImages([image, ...state.generatedImages]);
    $("#imageState").textContent = canSelect
      ? "配图已生成并加入当前笔记；你可以继续生成或移除图片。"
      : "配图已保存到本机，但当前笔记已选满 9 张；移除一张图片后再加入。";
    toast(canSelect ? "配图已生成并加入笔记" : "配图已保存；笔记最多选择 9 张", !canSelect);
  } catch (error) {
    $("#imageState").textContent = error.message;
    toast(error.message, true);
  } finally {
    button.textContent = "生成配图";
    updatePreview();
  }
}

function makePublishDescription() {
  const accountName = $("#accountName").value.trim() || "当前账号";
  const images = selectedImageIds.map((id) => state.generatedImages.find((item) => item.id === id)).filter(Boolean);
  const latest = state.monitor?.[0];
  const identity = latest?.login_status === "logged_in" ? latest.nickname : "登录状态待核验";
  $("#confirmHeading").textContent = state.settings.publisher_mode === "mock" ? "创建图文模拟任务？" : "确认提交图文笔记？";
  $("#confirmCopy").textContent = `工作台账号：${accountName} · 实际登录：${identity} · 图文笔记 · ${images.length} 张图片 · 标题：${$("#postTitle").value.trim()}`;
  $("#confirmWarning").classList.toggle("hidden", state.settings.publisher_mode === "mock");
}

async function beginPublish() {
  const button = $("#publishButton"); button.disabled = true;
  button.querySelector("span").textContent = "正在创建发布任务…";
  try {
    const payload = {
      confirmed: true,
      content_type: "note",
      asset_ids: selectedImageIds,
      title: $("#postTitle").value,
      body: $("#postBody").value,
      tags: $("#postTags").value.split(",").map((tag) => tag.trim().replace(/^#/, "")).filter(Boolean),
      source_refs: selectedSources,
    };
    const job = await api("/api/publish", jsonPost(payload));
    $("#publishDialog").close();
    toast(job.publisher_id === "mock" ? "模拟任务已创建，没有上传小红书" : "任务已提交给连接器，正在读取结果");
    await refreshJobs();
    pollJob(job.id);
  } catch (error) {
    $("#publishDialog").close(); toast(error.message, true);
    if (error.status === 409 && error.jobId) {
      const existing = await api(`/api/jobs/${error.jobId}`).catch(() => null);
      if (existing) openVerify(existing);
    }
  } finally {
    button.querySelector("span").textContent = "确认并发布图文笔记";
    updatePreview();
  }
}

async function pollJob(id) {
  for (let attempt = 0; attempt < 60; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 1500));
    const job = await api(`/api/jobs/${id}`).catch(() => null);
    if (!job) return;
    await refreshJobs();
    if (job.status !== "submitting") {
      if (job.status === "unknown_manual_check") toast("结果暂不明确，请先到小红书核验，不要重复发布", true);
      return;
    }
  }
  toast("连接器仍在处理；可在发布记录查看状态", true);
}

async function refreshJobs() {
  const result = await api("/api/bootstrap"); renderJobs(result.jobs || []);
}

async function checkConnection(showToast = true) {
  const button = $("#checkConnectionButton"); const status = $("#connectionState");
  button.disabled = true; status.textContent = "正在检查本机服务和登录状态…"; status.className = "settings-result pending";
  try {
    const settings = await saveConnectionSettings(false);
    const result = await api("/api/connector/check", jsonPost({}));
    connectorMessage = result.message || "连接正常";
    status.textContent = connectorMessage;
    status.className = `settings-result ${result.ok ? "success" : "error"}`;
    $("#settingsConnectionBadge").textContent = result.ok ? "连接正常" : "需要处理";
    $("#settingsConnectionBadge").className = `status-pill ${result.ok ? "success" : "failed"}`;
    const nameMatch = connectorMessage.match(/用户名[:：]\s*([^\n]+)/);
    const identity = nameMatch?.[1]?.trim() || (result.ok ? "登录有效" : "登录状态待处理");
    $("#sidebarConnectionText").textContent = settings.publisher_mode === "mock" ? "模拟模式 · 不会上传" : identity;
    $("#sidebarConnectionDot").classList.toggle("offline", !result.ok || settings.publisher_mode === "mock");
    $("#topConnectionPill span").textContent = settings.publisher_mode === "mock" ? "模拟模式" : result.ok ? `已登录 · ${identity}` : "登录需检查";
    $("#topConnectionPill").classList.toggle("mock", settings.publisher_mode === "mock");
    $("#contentConnectionText").textContent = settings.publisher_mode === "mock" ? "模拟模式 · 不会向平台提交" : `${result.ok ? "已登录" : "登录需检查"} · ${identity}`;
    $("#publishIdentity").textContent = settings.publisher_mode === "mock" ? "模拟模式，不会上传" : result.ok ? identity : "请先确认登录状态";
    if (showToast) toast(result.ok ? "小红书连接和登录检查完成" : result.message || "连接检查未通过", !result.ok);
    return result;
  } catch (error) {
    status.textContent = error.message; status.className = "settings-result error";
    $("#settingsConnectionBadge").textContent = "连接失败"; $("#settingsConnectionBadge").className = "status-pill failed";
    $("#sidebarConnectionText").textContent = "本机连接不可用"; $("#sidebarConnectionDot").classList.add("offline");
    $("#contentConnectionText").textContent = "本机小红书连接不可用";
    if (showToast) toast(error.message, true);
  } finally { button.disabled = false; }
}

async function testGenerator() {
  const button = $("#testGeneratorButton"); button.disabled = true; button.textContent = "正在测试…";
  try {
    if (!selectedGeneratorProfileId) throw new Error("先添加一套模型方案");
    await saveGeneratorSettings(false);
    const result = await api("/api/generator/test", jsonPost({ profile_id: selectedGeneratorProfileId }));
    const settings = await api("/api/generator/settings"); setGeneratorSettings(settings);
    $("#settingsGeneratorBadge").textContent = "验证通过";
    $("#settingsGeneratorBadge").className = "status-pill success";
    $("#generatorStatusPill").textContent = `已连接 · ${result.model}`;
    $("#generatorStatusPill").className = "status-pill success";
    $("#generationHint").textContent = `${result.profile_name} · ${result.model} 已通过生成测试。`;
    $("#generatorState").textContent = `模型调用成功 · ${result.profile_name} · 示例标题：${result.sample.title}`;
    $("#generatorState").className = "settings-result success";
    toast(`${result.profile_name} 连接和生成测试成功`);
  } catch (error) {
    if (state.generator?.configured) {
      $("#settingsGeneratorBadge").textContent = "验证失败";
      $("#settingsGeneratorBadge").className = "status-pill failed";
      $("#generatorStatusPill").textContent = "模型验证失败";
      $("#generatorStatusPill").className = "status-pill failed";
    }
    $("#generatorState").textContent = error.message; $("#generatorState").className = "settings-result error";
    toast(error.message, true);
  } finally { button.disabled = false; button.textContent = "测试生成"; }
}

document.querySelectorAll("[data-page]").forEach((node) => node.addEventListener("click", () => navigate(node.dataset.page)));
document.querySelectorAll(".nav-item").forEach((node) => node.addEventListener("click", () => {
  if (node.dataset.page === "content" && !state.account?.configured) toast("先完成账号定位，再开始内容制作", true);
}));
window.addEventListener("hashchange", () => navigate((location.hash || "#account").slice(1), false));

$("#saveProfileButton").addEventListener("click", () => saveProfile().catch(() => {}));
$("#goContentButton").addEventListener("click", () => navigate("content"));
["#accountName", "#profileLane", "#audience", "#positioning", "#tone", "#structure", "#boundaries", "#factRules", "#topics"].forEach((selector) => {
  $(selector).addEventListener("input", () => { profileDirty = true; updatePreview(); });
});
["#postTitle", "#postBody", "#postTags"].forEach((selector) => $(selector).addEventListener("input", updatePreview));
$("#trendScanButton").addEventListener("click", scanHotspots);
$("#trendQuery").addEventListener("keydown", (event) => { if (event.key === "Enter") scanHotspots(); });
$("#generateButton").addEventListener("click", generateCopy);
$("#generateImageButton").addEventListener("click", generateNoteImage);
$("#imagePrompt").addEventListener("focus", () => {
  if (!$("#imagePrompt").value.trim()) $("#imagePrompt").value = suggestedImagePrompt();
});
$("#refreshMonitorButton").addEventListener("click", () => refreshMonitor(true));
$("#saveConnectionButton").addEventListener("click", () => saveConnectionSettings(true).catch((error) => toast(error.message, true)));
$("#checkConnectionButton").addEventListener("click", () => checkConnection(true));
$("#publisherMode").addEventListener("change", () => saveConnectionSettings(false).catch((error) => toast(error.message, true)));
$("#saveGeneratorButton").addEventListener("click", () => saveGeneratorSettings(true).catch((error) => toast(error.message, true)));
$("#addGeneratorProfileButton").addEventListener("click", addGeneratorProfile);
$("#moveGeneratorProfileUpButton").addEventListener("click", () => moveGeneratorProfile(-1));
$("#moveGeneratorProfileDownButton").addEventListener("click", () => moveGeneratorProfile(1));
$("#deleteGeneratorProfileButton").addEventListener("click", deleteGeneratorProfile);
$("#generatorProvider").addEventListener("change", applyGeneratorProviderPreset);
$("#generatorBaseUrl").addEventListener("input", () => {
  if ($("#generatorProvider").value !== "custom") {
    $("#generatorProvider").value = "custom";
  }
  syncGeneratorProfileForm();
});
$("#generatorProfileName").addEventListener("input", syncGeneratorProfileForm);
$("#generatorModel").addEventListener("input", syncGeneratorProfileForm);
$("#generatorProtocol").addEventListener("change", syncGeneratorProfileForm);
$("#generatorAutoFallback").addEventListener("change", () => saveGeneratorSettings(false).then(() => toast("自动切换设置已保存")).catch((error) => toast(error.message, true)));
$("#testGeneratorButton").addEventListener("click", testGenerator);
$("#saveToapisSettingsButton").addEventListener("click", () => saveToapisSettings(true).catch((error) => toast(error.message, true)));
$("#checkToapisButton").addEventListener("click", checkToapis);
$("#toapisLoadModelsButton").addEventListener("click", loadToapisModels);
$("#toapisLoadDocsButton").addEventListener("click", loadToapisDocs);
$("#toapisGenerateButton").addEventListener("click", submitToapisGeneration);
$("#toapisRetryDownloadButton").addEventListener("click", retryToapisDownload);
$("#toapisMediaType").addEventListener("change", () => {
  $("#toapisModelDocs").textContent = "生成类型已切换；请按当前模型 ID 重新读取参数说明。";
  $("#toapisParams").value = "";
  $("#toapisParams").dataset.documented = "";
});
$("#saveImageSettingsButton").addEventListener("click", () => saveImageGeneratorSettings(true).catch((error) => toast(error.message, true)));
$("#refreshButton").addEventListener("click", () => {
  if (currentPage === "monitor") refreshMonitor(true);
  else if (currentPage === "records") refreshJobs();
  else loadState().catch((error) => toast(error.message, true));
});
$("#publishButton").addEventListener("click", () => { makePublishDescription(); $("#publishDialog").showModal(); });
$("#confirmPublishButton").addEventListener("click", (event) => { event.preventDefault(); $("#publishDialog").close("confirm"); beginPublish(); });
$("#publishDialog").addEventListener("close", () => { if ($("#publishDialog").returnValue !== "confirm") updatePreview(); });
$("#cancelVerifyButton").addEventListener("click", () => $("#verifyDialog").close());
$("#verifyForm").addEventListener("submit", async (event) => {
  event.preventDefault(); if (!verifyJobId) return;
  try {
    await api(`/api/jobs/${verifyJobId}/verification`, jsonPost({ outcome: $("#verifyOutcome").value, note: $("#verifyNote").value }));
    $("#verifyDialog").close(); toast("核验记录已保存"); await refreshJobs();
  } catch (error) { toast(error.message, true); }
});

loadState().catch((error) => toast(error.message, true));
