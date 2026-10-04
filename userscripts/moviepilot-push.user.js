// ==UserScript==
// @name         Moviepilot下载推送
// @namespace    http://tampermonkey.net/
// @version      2.10.2
// @description  moviepilots名称测试（使用API Key），深度适配各大PT站，融合Emby自动检测，单行紧凑UI排版
// @author       yubanmeiqin9048 & Kiro & Optimized
// @match        https://*/detail/*
// @match        https://*/details.php?id=*
// @match        https://*/details_movie.php?id=*
// @match        https://*/details_tv.php?id=*
// @match        https://*/details_animate.php?id=*
// @match        https://bangumi.moe/*
// @match        https://*.acgnx.se/*
// @match        https://*.dmhy.org/*
// @match        https://nyaa.si/*
// @match        https://mikanani.me/*
// @match        https://*.skyey2.com/*
// @match        https://totheglory.im/*
// @grant        GM_log
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_registerMenuCommand
// @connect      *
// @license      MIT
// ==/UserScript==

(function () {
  "use strict";

  /*
   * 更新日志
   * 2.10.2
   * - 推送携带 MoviePilot 站点的代理设置，避免已开启代理的站点仍被直连下载种子
   * - 预加载与点击推送复用同一站点请求，避免请求未完成时重复获取
   * - 日志记录识别、搜索、站点查询与下载提交耗时（不包含标题、URL 或凭据）
   * 2.10.1
   * - 兼容 MoviePilot v3 的 success / message / data 响应包装，修复识别、搜索与站点 Cookie 解析
   * - 兼容 GM 请求的对象与文本 JSON 响应，保留认证、服务与响应格式错误提示
   * - 兼容“电视剧 / 剧集”类型，避免搜索兜底和 Emby 检测误判为电影
   * 2.10.0
   * - PT 站详情页推送改走 MoviePilot 原生 POST /api/v1/download/add（按 TMDB 编号识别分类与下载目录），不再依赖 DownloaderApi 插件
   * - 同一 TMDB 编号既是电影又是剧集时，首次返回“无法识别媒体信息”且识别结果为电影，则去掉 media_id 改按种子名识别重试
   * - 识别失败不再静默下载（无分类会落在下载目录根下），改为“指定 TMDB 编号推送”，可填编号或 TMDB 链接
   * - 日志与按钮提示统一脱敏：隐藏链接及 passkey / token 等参数；API Key 只放请求头
   * - 识别失败提示区分“无法连接 MoviePilot”与“MoviePilot 返回 401/5xx”
   * - search 兜底：去掉 S01 / Season 1 等季集标记与画质后缀再搜；仍搜不到时改用副标题中的中文片名，并带年/不带年各试一次
   * - 动漫站（bangumi / mikan / dmhy / skyey）无 TMDB 识别，仍走 DownloaderApi 插件
   * 2.9.3
   * - 修复 recognize reject/resolve 逻辑错误：API 返回 200 但 media_info 为 null 时不应 reject，否则混淆"网络异常"和"识别失败"
   * - 新增 search 兜底：recognize 无结果时自动调用 /api/v1/media/search 尝试识别（解决含标点等特殊标题的识别问题）
   * 2.9.2
   * - 移除废弃的 downloadTorrent / createEmptyTorrentInfo 死代码
   * - 通用 NexusPHP 处理器增加大小提取后备逻辑（兼容 HDSky 联合行结构）
   * - 增加 getSite 预加载失败日志，改进降级提示
   * - 识别失败后的"直接下载"按钮改为优先通过 getSite 获取站点 cookie 再下载（修复 KEEPFRDS 等站点无认证下载失败）
   * - 当 h1 标题以【或「开头（中文描述性标题）时，自动清洗标题或从副标题中提取英文片名传给 MP 识别，修复 TTG / KEEPFRDS 等站点的媒体识别问题
   * 2.9.1
   * - 修复 HDSky（天空）下载链接提取失败：通用 NexusPHP 处理器的选择器改为 a[href*="download.php"]，兼容绝对 URL
   * 2.9.0
   * - 识别成功后改用 DownloaderApi 插件直接下载，不再经过 MP 标准下载 API，避免 "无法打开链接" 问题
   * 2.8.8
   * - 修复 downloadTorrent 推送后只检查 HTTP 200 未检查 JSON success 字段，导致显示虚假成功的问题
   * 2.8.9
   * - Emby 检测时跳过 .strm 占位文件（Film Playlist 插件生成的代理文件），避免误判为已入库
   */

  const windowPopup = true;

  const CONFIG_KEYS = {
    moviepilotUrl: "moviepilotUrl",
    apiKey: "moviepilotApiKey",
    embyServer: "embyServer",
    embyUsername: "embyUsername",
    embyPassword: "embyPassword",
    embyApiKey: "embyApiKey",
    enableEmby: "enableEmby",
  };

  const DEFAULT_CONFIG = {
    moviepilotUrl: "http://192.168.31.100:3000",
    apiKey: "",
    embyServer: "http://192.168.31.100:8096",
    embyUsername: "",
    embyPassword: "",
    embyApiKey: "",
    enableEmby: true,
  };

  function getConfigValue(key) {
    return GM_getValue(CONFIG_KEYS[key], DEFAULT_CONFIG[key]);
  }

  function setConfigValue(key, value) {
    GM_setValue(CONFIG_KEYS[key], value);
  }

  function normalizeUrl(url) {
    return (url || "").trim().replace(/\/+$/, "");
  }

  function loadConfig() {
    return {
      moviepilotUrl: normalizeUrl(getConfigValue("moviepilotUrl")),
      apiKey: (getConfigValue("apiKey") || "").trim(),
      embyServer: normalizeUrl(getConfigValue("embyServer")),
      embyUsername: (getConfigValue("embyUsername") || "").trim(),
      embyPassword: getConfigValue("embyPassword") || "",
      embyApiKey: (getConfigValue("embyApiKey") || "").trim(),
      enableEmby: Boolean(getConfigValue("enableEmby")),
    };
  }

  function saveConfig(config) {
    Object.entries(config).forEach(([key, value]) =>
      setConfigValue(key, value),
    );
  }

  function openSettings() {
    const current = loadConfig();
    const moviepilotUrlInput = prompt(
      "MoviePilot 地址",
      current.moviepilotUrl || DEFAULT_CONFIG.moviepilotUrl,
    );
    if (moviepilotUrlInput === null) return;
    const apiKeyInput = prompt("MoviePilot API Key", current.apiKey || "");
    if (apiKeyInput === null) return;
    const enableEmbyInput = prompt(
      "启用 Emby 检测？输入 yes 或 no",
      current.enableEmby ? "yes" : "yes",
    );
    if (enableEmbyInput === null) return;

    const nextConfig = {
      moviepilotUrl: normalizeUrl(moviepilotUrlInput),
      apiKey: apiKeyInput.trim(),
      enableEmby: !["no", "n", "false", "0"].includes(
        enableEmbyInput.trim().toLowerCase(),
      ),
      embyServer: current.embyServer,
      embyUsername: current.embyUsername,
      embyPassword: current.embyPassword,
      embyApiKey: current.embyApiKey,
    };

    if (nextConfig.enableEmby) {
      const embyServerInput = prompt(
        "Emby 地址",
        current.embyServer || DEFAULT_CONFIG.embyServer,
      );
      if (embyServerInput === null) return;
      const embyApiKeyInput = prompt(
        "Emby API Key（留空则使用账号密码）",
        current.embyApiKey || "",
      );
      if (embyApiKeyInput === null) return;
      nextConfig.embyServer = normalizeUrl(embyServerInput);
      nextConfig.embyApiKey = embyApiKeyInput.trim();
      if (!nextConfig.embyApiKey) {
        const embyUsernameInput = prompt(
          "Emby 用户名",
          current.embyUsername || "",
        );
        if (embyUsernameInput === null) return;
        const embyPasswordInput = prompt(
          "Emby 密码",
          current.embyPassword || "",
        );
        if (embyPasswordInput === null) return;
        nextConfig.embyUsername = embyUsernameInput.trim();
        nextConfig.embyPassword = embyPasswordInput;
      }
    }
    saveConfig(nextConfig);
    alert("脚本配置已保存，刷新页面后生效。");
  }

  GM_registerMenuCommand("MoviePilot 检测 - 配置", openSettings);

  const scriptConfig = loadConfig();
  const moviepilotUrl = scriptConfig.moviepilotUrl;
  const apiKey = scriptConfig.apiKey;
  const enableEmby = scriptConfig.enableEmby;
  let embyServer = scriptConfig.embyServer;
  let embyUsername = scriptConfig.embyUsername;
  let embyPassword = scriptConfig.embyPassword;
  let embyApiKey = scriptConfig.embyApiKey;

  let embyAccessToken = "";

  let ptype = "";
  let btype = "";
  let site_domain = window.location.hostname;
  let siteInfoCache = null;
  let siteInfoPromise = null;
  const recognizeCache = new Map();

  function getAuthHeaders() {
    return {
      "user-agent": navigator.userAgent,
      "content-type": "application/json",
      "X-API-KEY": apiKey,
    };
  }

  // 详情页下载链接常带 passkey，日志与界面一律脱敏后再输出。
  function redact(text) {
    return String(text ?? "")
      .replace(/https?:\/\/[^\s"'<>]+/gi, "[链接已隐藏]")
      .replace(
        /\b(passkey|token|authkey|sign|apikey|cookie)=[^&\s"']+/gi,
        "$1=***",
      );
  }

  function logError(label, err) {
    const detail =
      typeof err === "string"
        ? err
        : err?.message || err?.error || `请求失败（${err?.status ?? "网络错误"}）`;
    GM_log(`${label} ${redact(detail)}`);
  }

  function httpRequestPromised(options) {
    const stage = options.url.includes("/api/v1/media/recognize")
      ? "媒体识别"
      : options.url.includes("/api/v1/media/search")
        ? "媒体搜索"
        : options.url.includes("/api/v1/site/domain/")
          ? "站点信息"
          : options.url.includes("/api/v1/download/add")
            ? "下载提交"
            : "";
    const startedAt = Date.now();
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        ...options,
        onload: (response) => {
          if (stage)
            GM_log(`MoviePilot 耗时：${stage} ${Date.now() - startedAt} ms，HTTP ${response.status}`);
          resolve(response);
        },
        onerror: (error) => {
          if (stage)
            GM_log(`MoviePilot 耗时：${stage} ${Date.now() - startedAt} ms，网络错误`);
          reject(error);
        },
      });
    });
  }

  // v3 使用 { success, message, data } 包装；旧版直接返回媒体、站点对象或数组。
  function parseMoviePilotData(res) {
    if (res.status !== 200) {
      throw new Error(
        res.status === 401 || res.status === 403
          ? `MoviePilot 返回 ${res.status}，请检查 API Key`
          : `MoviePilot 返回 ${res.status}`,
      );
    }
    let payload;
    try {
      payload =
        res.response !== null && typeof res.response === "object"
          ? res.response
          : JSON.parse(res.responseText);
    } catch (err) {
      throw new Error("MoviePilot 返回非 JSON 响应，请检查地址或反向代理");
    }
    if (payload?.success === false)
      throw new Error(`MoviePilot：${redact(payload.message || "请求失败")}`);
    if (
      payload &&
      typeof payload.success === "boolean" &&
      Object.prototype.hasOwnProperty.call(payload, "data")
    )
      return payload.data;
    return payload;
  }

  function isTvType(type) {
    return ["tv", "电视剧", "剧集"].includes(String(type || "").toLowerCase());
  }

  async function authenticateEmby() {
    if (!enableEmby || !embyServer) return null;
    if (embyApiKey) {
      embyAccessToken = embyApiKey;
      return embyAccessToken;
    }
    if (!embyUsername || !embyPassword) return null;
    try {
      const response = await httpRequestPromised({
        method: "POST",
        url: `${embyServer}/emby/Users/AuthenticateByName`,
        headers: {
          "Content-Type": "application/json",
          "X-Emby-Authorization":
            'MediaBrowser Client="MoviepilotToEmby", Device="Browser", DeviceId="TM-Script", Version="1.0.0"',
        },
        data: JSON.stringify({ Username: embyUsername, Pw: embyPassword }),
      });
      if (response.status === 200) {
        const data = JSON.parse(response.responseText);
        embyAccessToken = data.AccessToken;
        return embyAccessToken;
      }
    } catch (error) {
      logError("Emby登录错误:", error);
    }
    return null;
  }

  async function searchEmbyByNameAndYear(name, year, isTv) {
    if (!enableEmby) return null;
    if (!embyAccessToken) {
      await authenticateEmby();
      if (!embyAccessToken) return null;
    }
    let yearParam = year
      ? `&Years=${year},${parseInt(year) - 1},${parseInt(year) + 1}`
      : "";
    let includeItemTypes = "IncludeItemTypes=movie";
    if (isTv) {
      yearParam = "";
      includeItemTypes = "IncludeItemTypes=Series";
    }
    const response = await httpRequestPromised({
      method: "GET",
      url: `${embyServer}/emby/Items?Recursive=true&Fields=Path&${includeItemTypes}&SearchTerm=${encodeURIComponent(name)}${yearParam}`,
      headers: { "X-Emby-Token": embyAccessToken },
    });
    if (response.status === 401) {
      if (embyApiKey) return null;
      embyAccessToken = await authenticateEmby();
      if (embyAccessToken) return searchEmbyByNameAndYear(name, year, isTv);
      return null;
    }
    const data = JSON.parse(response.responseText);
    if (response.status === 200 && data.TotalRecordCount > 0) {
      for (let i = 0; i < data.Items.length; i++) {
        if (data.Items[i].Name === name) {
          // 跳过 .strm 占位文件（Film Playlist 插件生成的代理文件）
          if (
            data.Items[i].Path &&
            data.Items[i].Path.toLowerCase().endsWith(".strm")
          ) {
            continue;
          }
          return data.Items[i];
        }
      }
    }
    return null;
  }

  function createEmbyStatusHtml(embyInfo) {
    const svgPath =
      "M11.041 0c-.007 0-1.456 1.43-3.219 3.176L4.615 6.352l.512.513.512.512-2.819 2.791L0 12.961l1.83 1.848a3468.32 3468.32 0 0 0 3.182 3.209l1.351 1.359.508-.496c.28-.273.515-.498.524-.498.008 0 1.266 1.264 2.794 2.808L12.97 24l.187-.182c.23-.225 5.007-4.95 5.717-5.656l.52-.516-.502-.513c-.276-.282-.5-.52-.496-.53.003-.009 1.264-1.26 2.802-2.783 1.538-1.522 2.8-2.776 2.803-2.785.005-.012-3.617-3.684-6.107-6.193L17.65 4.6l-.505.505c-.279.278-.517.501-.53.497-.013-.005-1.27-1.267-2.793-2.805A449.655 449.655 0 0 0 11.041 0zM9.223 7.367c.091.038 7.951 4.608 7.957 4.627.003.013-1.781 1.056-3.965 2.32a999.898 999.898 0 0 1-3.996 2.307c-.019.006-.026-1.266-.026-4.629 0-3.7.007-4.634.03-4.625z";
    const greenEmbyIcon = `<svg fill="#52b54b" width="16px" height="16px" viewBox="0 0 24.00 24.00" role="img" xmlns="http://www.w3.org/2000/svg" style="margin-right: 4px;"><path d="${svgPath}"></path></svg>`;
    if (embyInfo) {
      return `<a href="${embyServer}/web/index.html#!/item?id=${embyInfo.Id}&serverId=${embyInfo.ServerId}" target="_blank" style="display: inline-flex; align-items: center; background-color: #e8f5e9; color: #2e7d32 !important; padding: 4px 12px; border-radius: 16px; font-size: 13px; font-weight: 500; text-decoration: none; transition: background-color 0.2s; margin:0;" onmouseover="this.style.backgroundColor='#c8e6c9'" onmouseout="this.style.backgroundColor='#e8f5e9'" title="点击跳转至 Emby">${greenEmbyIcon}✔ Emby已入库</a>`;
    } else {
      return `<span style="display: inline-flex; align-items: center; background-color: #ffebee; color: #c62828; padding: 4px 12px; border-radius: 16px; font-size: 13px; font-weight: 500; cursor: default; margin:0;" title="Emby 中未找到该影视">${greenEmbyIcon}✘ Emby未入库</span>`;
    }
  }

  // ---------------- MoviePilot 核心解析 ----------------
  function waitForElements(selectors, timeout = 30000) {
    return new Promise((resolve, reject) => {
      const interval = 50;
      let tries = 0;
      const checkExist = setInterval(() => {
        let allFound = true;
        const elements = selectors.map((selector) => {
          const foundElements = document.querySelectorAll(selector);
          if (foundElements.length === 0) allFound = false;
          return foundElements;
        });
        if (allFound) {
          clearInterval(checkExist);
          resolve(elements);
        } else if (tries >= timeout / interval) {
          clearInterval(checkExist);
          reject(new Error("Elements not found"));
        }
        tries++;
      }, interval);
    });
  }

  function renderTag(string, bg) {
    return `<span style="background-color:${bg};color:#fff;border-radius:4px;font-size:12px;padding:3px 8px;font-weight:500;display:inline-flex;align-items:center;">${string}</span>`;
  }

  function buildRouteLabel(data) {
    const c = data?.media_info?.category || "";
    const t = data?.media_info?.type || "";
    if (c) return c;
    if (isTvType(t) || !!data?.meta_info?.season_episode) return "剧集";
    if (t === "movie") return "电影";
    if (t === "anime") return "动画";
    if (t === "documentary") return "纪录片";
    return "未识别分类";
  }

  function buildRecognizeSummary(data) {
    return {
      routeLabel: buildRouteLabel(data),
      title: data?.media_info?.title || "未识别标题",
      metaText: [data?.meta_info?.season_episode, data?.meta_info?.year]
        .filter(Boolean)
        .join(" · "),
    };
  }

  function getErrorMessage(err) {
    if (!err) return "操作失败";
    if (typeof err === "string") return redact(err);
    return redact(err.message || "操作失败");
  }

  function renderMoviepilotTag(ptype, tagHtml) {
    if (ptype === "common")
      return `<td class="rowhead nowrap" valign="top" align="right">MoviePilot</td><td class="rowfollow" valign="top" align="left">${tagHtml}</td>`;
    if (ptype === "m-team")
      return `<th class="ant-descriptions-item-label" style="width:135px;text-align:right" colspan="1"><span>MoviePilot</span></th><td class="ant-descriptions-item-content" colspan="1"><span>${tagHtml}</span></td>`;
    return tagHtml;
  }

  function getSize(s) {
    if (!s) return 0;
    const m = s.match(/(\d+(?:\.\d+)?)\s*(GB|MB|KB|TB)/i);
    if (!m) return 0;
    const v = parseFloat(m[1]);
    const u = m[2].toLowerCase();
    return (
      {
        kb: v * 1024,
        mb: v * 1048576,
        gb: v * 1073741824,
        tb: v * 1099511627776,
      }[u] || 0
    );
  }

  function getRecognizeCacheKey(title, subtitle) {
    return `${title || ""}@@${subtitle || ""}`;
  }

  async function recognize(title, subtitle) {
    const k = getRecognizeCacheKey(title, subtitle);
    if (recognizeCache.has(k)) return recognizeCache.get(k);
    try {
      const res = await httpRequestPromised({
        url: `${moviepilotUrl}/api/v1/media/recognize?title=${encodeURIComponent(title)}&subtitle=${encodeURIComponent(subtitle || "")}`,
        method: "GET",
        headers: getAuthHeaders(),
      });
      if (res.status === 200) {
        const r = parseMoviePilotData(res);
        if (r && r.media_info) {
          recognizeCache.set(k, r);
          return r;
        }
        // recognize 无结果，尝试 search API 兜底
      } else if (res.status !== 404 && res.status !== 200) {
        throw new Error(
          res.status === 401 || res.status === 403
            ? `MoviePilot 返回 ${res.status}，请检查 API Key`
            : `MoviePilot 返回 ${res.status}`,
        );
      }
      const mock = await doSearchFallback(title, subtitle);
      if (mock) {
        recognizeCache.set(k, mock);
        return mock;
      }
      return {};
    } catch (err) {
      logError("识别请求失败:", err);
      // 已带 MoviePilot 状态说明的原样抛出；其余（onerror 等）说明根本没连上。
      throw err instanceof Error && err.message.startsWith("MoviePilot")
        ? err
        : new Error("无法连接 MoviePilot，请检查地址与代理直连规则");
    }
  }

  // 去掉 S01 / S01E02 / Season 1 等季集标记，避免带着它们去搜 TMDB。
  function stripSeasonMarks(text) {
    return text
      .replace(/\bS\d{1,2}(?:\s*-?\s*E\d{1,3})?\b/gi, " ")
      .replace(/\bSeason[\s.]*\d{1,2}\b/gi, " ")
      .replace(/\s{2,}/g, " ")
      .trim();
  }

  // 副标题里的中文片名：去掉【】说明、括号说明与“第X季/集”及其后内容。
  function chineseNameFromSubtitle(subtitle) {
    const name = String(subtitle || "")
      .replace(/[【\[「].*$/, "")
      .replace(/[（(][^）)]*[）)]/g, "")
      .replace(/\s*第[\d一二三四五六七八九十百]+[季集部].*$/, "")
      .split(/[\/|｜]/)[0]
      .trim();
    return /[一-龥]/.test(name) ? name : "";
  }

  async function searchMediaByName(searchTitle, year) {
    const url = `${moviepilotUrl}/api/v1/media/search?title=${encodeURIComponent(searchTitle)}${year ? `&year=${year}` : ""}`;
    const res = await httpRequestPromised({
      method: "GET",
      url: url,
      headers: getAuthHeaders(),
    });
    const list = parseMoviePilotData(res);
    if (list == null) return null;
    if (!Array.isArray(list)) throw new Error("MoviePilot 搜索响应格式异常");
    if (list.length === 0) return null;
    const match = year
      ? list.find((item) => String(item.year) === String(year)) || list[0]
      : list[0];
    return match && match.tmdb_id ? match : null;
  }

  async function doSearchFallback(rawTitle, subtitle) {
    try {
      // 先去掉开头的【】「」等前缀
      const cleaned = rawTitle
        .replace(/^[【「\[][^】」\]】]*[】」\]】]\s*/, "")
        .trim();
      // 提取纯片名（年份前的部分），去掉季集标记与画质编码等后缀
      const nameMatch = cleaned.match(/^(.+?)\s*\b(19\d{2}|20\d{2})\b/);
      const year = nameMatch ? nameMatch[2] : "";
      const searchTitle = stripSeasonMarks(
        (nameMatch ? nameMatch[1] : cleaned.split(/\b(?:2160p|1080p|720p|480p|BluRay|WEB-DL|HDTV|REMUX)\b/i)[0]).trim(),
      );
      const chineseName = chineseNameFromSubtitle(subtitle);
      // 依次尝试：英文名带年 → 英文名不带年 → 中文名带年 → 中文名不带年
      const attempts = [];
      [
        [searchTitle, year],
        [searchTitle, ""],
        [chineseName, year],
        [chineseName, ""],
      ].forEach(([t, y]) => {
        if (t && !attempts.some(([t2, y2]) => t2 === t && y2 === y))
          attempts.push([t, y]);
      });
      let match = null;
      let usedTitle = searchTitle;
      for (const [t, y] of attempts) {
        match = await searchMediaByName(t, y);
        if (match) {
          usedTitle = t;
          break;
        }
      }
      if (!match) return null;
      // 中文类型名映射为英文（search API 返回的是中文）
      const rawType = (match.type || "").toLowerCase();
      const mediaType =
        rawType === "电影"
          ? "movie"
          : isTvType(rawType)
            ? "tv"
            : rawType === "动漫" || rawType === "动画"
              ? "anime"
              : rawType;
      const isTv = mediaType === "tv";
      return {
        meta_info: {
          year: match.year || year,
          season_episode: "",
        },
        media_info: {
          title: match.title || match.original_title || usedTitle,
          en_title: match.original_title || match.title,
          year: match.year || year,
          type: mediaType,
          category: match.category || (isTv ? "剧集" : "电影"),
          tmdb_id: match.tmdb_id,
          detail_link: `https://www.themoviedb.org/${isTv ? "tv" : "movie"}/${match.tmdb_id}`,
        },
      };
    } catch (e) {
      logError("searchFallback 失败:", e);
      throw e;
    }
  }

  function getSite() {
    if (siteInfoCache) return Promise.resolve(siteInfoCache);
    if (siteInfoPromise) return siteInfoPromise;
    siteInfoPromise = httpRequestPromised({
      url: `${moviepilotUrl}/api/v1/site/domain/${site_domain}`,
      method: "GET",
      headers: getAuthHeaders(),
      responseType: "json",
    })
      .then((res) => {
        if (res.status === 404) throw new Error("站点不存在");
        const site = parseMoviePilotData(res);
        if (!site || typeof site !== "object" || Array.isArray(site) || !site.name)
          throw new Error("MoviePilot 站点响应格式异常");
        siteInfoCache = site;
        return site;
      })
      .catch((err) => {
        logError("站点信息请求失败:", err);
        throw err;
      })
      .finally(() => {
        siteInfoPromise = null;
      });
    return siteInfoPromise;
  }

  function ensureMoviePilotConfigured() {
    if (!moviepilotUrl) throw new Error("请先配置 MoviePilot 地址");
    if (!apiKey) throw new Error("请先配置 MoviePilot API Key");
  }

  function markPushed(btn) {
    btn.textContent = "推送成功 ✔";
    btn.disabled = true;
    btn.style.cssText +=
      "background-color:#b7eb8f!important;color:#1f1f1f!important;border-color:#b7eb8f!important;";
  }

  function isMovieType(type) {
    return ["movie", "电影"].includes(String(type || "").toLowerCase());
  }

  // 从用户输入解析 TMDB 编号：纯数字，或 themoviedb.org/movie|tv/<id> 链接。
  function parseTmdbInput(text) {
    const input = (text || "").trim();
    const m = input.match(/themoviedb\.org\/(movie|tv)\/(\d+)/i);
    if (m) return { tmdbId: m[2], mediaType: m[1].toLowerCase() };
    if (/^\d+$/.test(input)) return { tmdbId: input, mediaType: "" };
    return null;
  }

  async function postDownloadAdd(torrentIn, tmdbId) {
    const body = { torrent_in: torrentIn };
    if (tmdbId) {
      body.media_source = "themoviedb";
      body.media_id = String(tmdbId);
    }
    const res = await httpRequestPromised({
      method: "POST",
      url: `${moviepilotUrl}/api/v1/download/add`,
      headers: getAuthHeaders(),
      data: JSON.stringify(body),
    });
    let result = {};
    try {
      result = JSON.parse(res.responseText);
    } catch (e) {
      // 非 JSON 响应按失败处理
    }
    if (res.status !== 200 && !result.message)
      result = { success: false, message: `MoviePilot 返回 ${res.status}` };
    return result;
  }

  // 经 MoviePilot 原生接口下载：按 TMDB 编号识别分类并选择下载目录。
  async function pushToMoviePilot(ctx) {
    ensureMoviePilotConfigured();
    if (!ctx.link) throw new Error("未找到下载链接");
    if (!ctx.tmdbId) throw new Error("缺少 TMDB 编号，无法识别分类");
    const torrentIn = {
      title: ctx.title,
      description: ctx.description || "",
      enclosure: ctx.link,
      size: ctx.size || 0,
      site_ua: navigator.userAgent,
    };
    try {
      const site = await getSite();
      torrentIn.site_name = site.name;
      torrentIn.site_cookie = site.cookie || "";
      // download/add 不自动采用站点配置，种子请求依据 torrent_in.site_proxy 选路。
      torrentIn.site_proxy = site.proxy === true || Number(site.proxy) === 1;
    } catch (err) {
      logError("获取站点信息失败，按无站点信息提交:", err);
    }
    let result = await postDownloadAdd(torrentIn, ctx.tmdbId);
    // 同一 TMDB 编号既是电影又是剧集时 MoviePilot 只凭编号分不清类型：
    // 识别结果是电影的话，改按种子名识别（分类与下载目录照常）。
    if (
      result.success === false &&
      String(result.message || "").includes("无法识别媒体信息") &&
      isMovieType(ctx.mediaType)
    ) {
      result = await postDownloadAdd(torrentIn, "");
    }
    if (!result.success) throw new Error(result.message || "推送失败");
  }

  function runPush(btn, ctx) {
    btn.disabled = true;
    btn.textContent = "下载中...";
    return pushToMoviePilot(ctx)
      .then(() => markPushed(btn))
      .catch((err) => {
        btn.disabled = false;
        btn.textContent = getErrorMessage(err);
      });
  }

  // 动漫站没有 TMDB 识别，仍走 DownloaderApi 插件。
  function downloadViaPlugin(
    downloadButton,
    download_link,
    siteName,
    siteCookie,
  ) {
    downloadButton.textContent = "下载中...";
    downloadButton.disabled = true;
    return new Promise((resolve, reject) => {
      try {
        ensureMoviePilotConfigured();
      } catch (e) {
        reject(e);
        return;
      }
      let url = `${moviepilotUrl}/api/v1/plugin/DownloaderApi/download_torrent_notest?apikey=${apiKey}&torrent_url=${encodeURIComponent(download_link)}`;
      if (siteName) url += `&site_name=${encodeURIComponent(siteName)}`;
      if (siteCookie) url += `&site_cookie=${encodeURIComponent(siteCookie)}`;
      httpRequestPromised({
        method: "GET",
        responseType: "json",
        url: url,
        headers: getAuthHeaders(),
      })
        .then((res) => {
          const s = JSON.parse(res.responseText);
          if (s.success) resolve();
          else reject(new Error(s.message));
        })
        .catch(reject);
    });
  }

  function creatRecognizeRow(
    row,
    ptype,
    torrent_name,
    torrent_description,
    download_link,
    torrent_size,
  ) {
    row.innerHTML = renderMoviepilotTag(
      ptype,
      "<span style='color:#666;'>MoviePilot 识别中...</span>",
    );
    getSite().catch((err) => logError("getSite 预加载失败:", err));
    const pushCtx = {
      title: torrent_name,
      description: torrent_description,
      link: download_link,
      size: torrent_size,
    };
    // 当 h1 标题以【或「开头（中文描述性标题），清洗后尝试提取英文片名提高识别率
    let recogTitle = torrent_name;
    let recogSub = torrent_description;
    if (/^[【「]/.test(torrent_name)) {
      const cleaned = torrent_name
        .replace(/^[【「][^】」]*[】」]\s*/, "")
        .trim();
      // 优先从清洗后的标题中提取英文名（如 TTG: 「...」The Lion King 1994...）
      const engFromTitle = cleaned.match(
        /^([A-Za-z][A-Za-z\s\-'\.:0-9]*?)\s+\d{4}\b/,
      );
      if (engFromTitle) {
        recogTitle = engFromTitle[1].trim();
        recogSub = cleaned;
      } else if (torrent_description) {
        // 后备：从副标题中提取（如 KEEPFRDS 副标题含英文名）
        const engFromDesc = torrent_description.match(
          /^([A-Za-z][A-Za-z\s\-'\.:]*?)\s+\d{4}\b/,
        );
        if (engFromDesc) {
          recogTitle = engFromDesc[1].trim();
          recogSub = torrent_description;
        }
      }
    }
    recognize(recogTitle, recogSub)
      .then(async (data) => {
        if (data.media_info) {
          const summary = buildRecognizeSummary(data);
          const rid = `mp-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
          const bid = `${rid}-dl`;
          const eid = `${rid}-emby`;
          let html =
            '<div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center;">';
          html += renderTag(summary.routeLabel, "#2775b6");
          html += renderTag(summary.title, "#c54640");
          if (summary.metaText) html += renderTag(summary.metaText, "#e6702e");
          const bs =
            "cursor:pointer;padding:4px 14px;border:1px solid #d9d9d9;border-radius:4px;background-color:#fafafa;color:#333;font-size:13px;font-weight:500;transition:all .3s;margin:0;";
          html += `<button id="${bid}" style="${bs}">推送到MP</button>`;
          if (enableEmby)
            html += `<span id="${eid}" style="display:inline-flex;align-items:center;font-size:13px;color:#999;margin:0;">Emby 检测中...</span>`;
          html += "</div>";
          row.innerHTML = renderMoviepilotTag(ptype, html);
          const btn = document.getElementById(bid);
          if (btn)
            btn.addEventListener("click", () =>
              runPush(btn, {
                ...pushCtx,
                tmdbId: data.media_info.tmdb_id,
                mediaType: data.media_info.type,
              }),
            );
          if (enableEmby && data.media_info.title) {
            searchEmbyByNameAndYear(
              data.media_info.title,
              data.meta_info?.year,
              !!data.meta_info?.season_episode || isTvType(data.media_info.type),
            )
              .then((info) => {
                const el = document.getElementById(eid);
                if (el) el.innerHTML = createEmbyStatusHtml(info);
              })
              .catch(() => {
                const el = document.getElementById(eid);
                if (el) el.textContent = "Emby 检测失败";
              });
          } else if (enableEmby) {
            const el = document.getElementById(eid);
            if (el) el.innerHTML = "";
          }
        } else {
          renderRecognizeFailure(row, ptype, "MoviePilot 识别失败", pushCtx);
        }
      })
      .catch((error) => {
        logError("识别失败:", error);
        renderRecognizeFailure(
          row,
          ptype,
          `识别失败：${getErrorMessage(error)}`,
          pushCtx,
        );
      });
  }

  // 识别失败不静默下载（无分类会落在下载目录根下），改为手动指定 TMDB 编号后再推送。
  function renderRecognizeFailure(row, ptype, message, pushCtx) {
    const fid = `mp-fb-${Date.now()}`;
    row.innerHTML = renderMoviepilotTag(
      ptype,
      `<span style="color:#e74c3c;font-weight:500;">${message}</span><button id="${fid}" style="cursor:pointer;padding:3px 12px;border:1px solid #d9d9d9;border-radius:4px;background-color:#fafafa;color:#333;font-size:13px;font-weight:500;margin-left:8px;">指定 TMDB 编号推送</button>`,
    );
    document.getElementById(fid)?.addEventListener("click", function () {
      const parsed = parseTmdbInput(
        prompt(
          "输入 TMDB 编号或 TMDB 链接\n（如 https://www.themoviedb.org/movie/603，带链接可区分电影与剧集）",
        ),
      );
      if (!parsed) return;
      runPush(this, { ...pushCtx, ...parsed });
    });
  }

  function creatPushButton(btype, element, download_link) {
    const bid = `mp-push-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    let html = "";
    if (btype === "dmhy")
      html = `<p><strong>MoviePilot:</strong>&nbsp;<a id="${bid}" style="cursor:pointer;">推送到MP</a></p>`;
    else if (btype === "bangumi")
      html = `<button id="${bid}" class="md-primary md-button md-default-theme" style="float:right;cursor:pointer;" tabindex="0"><i class="fa fa-file ng-scope"></i> 推送到MP</button>`;
    else if (btype === "skyey")
      html = `<b>[<a id="${bid}" style="cursor:pointer;">推送到MP</a>]</b>`;
    else
      html = `<a class="btn episode-btn" id="${bid}" style="cursor:pointer;">推送到MP</a>`;
    element.insertAdjacentHTML("afterbegin", html);
    element.querySelector(`#${bid}`).addEventListener("click", function () {
      downloadViaPlugin(this, download_link)
        .then(() => {
          this.textContent = "推送成功 ✔";
          this.style.cssText +=
            "background-color:#b7eb8f!important;color:#1f1f1f!important;border-radius:4px!important;padding:2px 6px!important;text-decoration:none!important;display:inline-block!important;";
        })
        .catch((err) => {
          this.disabled = false;
          this.textContent = getErrorMessage(err);
        });
    });
  }

  function creatRecognizeTip(tip, text) {
    tip.showText("识别中...");
    recognize(text, "")
      .then((data) => {
        const s = buildRecognizeSummary(data);
        let html = `分流：${s.routeLabel}<br>标题：${s.title}<br>`;
        if (s.metaText) html += `附加：${s.metaText}<br>`;
        html += data.media_info.tmdb_id
          ? `tmdb：<a href="${data.media_info.detail_link}" target="_blank">${data.media_info.tmdb_id}</a>`
          : "tmdb：未识别";
        tip.showText(html);
      })
      .catch((error) => {
        logError("划词识别失败:", error);
        tip.showText("识别失败");
      });
  }

  // ================= 初始化 =================
  class RecognizeTip {
    constructor() {
      const div = document.createElement("div");
      div.hidden = true;
      div.setAttribute(
        "style",
        "position:absolute!important;font-size:13px!important;overflow:auto!important;background:#fff!important;font-family:sans-serif,Arial!important;font-weight:normal!important;text-align:left!important;color:#000!important;padding:.5em 1em!important;line-height:1.5em!important;border-radius:5px!important;border:1px solid #ccc!important;box-shadow:4px 4px 8px #888!important;max-width:350px!important;max-height:216px!important;z-index:2147483647!important;",
      );
      document.documentElement.appendChild(div);
      div.addEventListener("mouseup", (e) => e.stopPropagation());
      this._tip = div;
    }
    showText(t) {
      this._tip.innerHTML = t;
      this._tip.hidden = false;
    }
    hide() {
      this._tip.innerHTML = "";
      this._tip.hidden = true;
    }
    pop(ev) {
      this._tip.style.top = ev.pageY + "px";
      this._tip.style.left =
        (ev.pageX + 350 <= document.body.clientWidth
          ? ev.pageX
          : document.body.clientWidth - 350) + "px";
    }
  }
  const tip = new RecognizeTip();

  class Icon {
    constructor() {
      const icon = document.createElement("span");
      icon.hidden = true;
      icon.innerHTML =
        '<svg style="margin:4px!important;" width="16" height="16" viewBox="0 0 24 24"><path d="M12 2L22 12L12 22L2 12Z" style="fill:none;stroke:#3e84f4;stroke-width:2;"></path></svg>';
      icon.setAttribute(
        "style",
        "width:24px!important;height:24px!important;background:#fff!important;border-radius:50%!important;box-shadow:4px 4px 8px #888!important;position:absolute!important;z-index:2147483647!important;cursor:pointer;",
      );
      document.documentElement.appendChild(icon);
      icon.addEventListener("mousedown", (e) => e.preventDefault(), true);
      icon.addEventListener("mouseup", (ev) => ev.preventDefault(), true);
      icon.addEventListener("click", (ev) => {
        if (ev.ctrlKey)
          navigator.clipboard
            .readText()
            .then((t) => this.queryText(t.trim(), ev))
            .catch(() => {});
        else {
          const t = window
            .getSelection()
            .toString()
            .trim()
            .replace(/\s{2,}/g, " ");
          this.queryText(t, ev);
        }
      });
      this._icon = icon;
    }
    pop(ev) {
      this._icon.style.top = ev.pageY + 9 + "px";
      this._icon.style.left = ev.pageX - 18 + "px";
      this._icon.hidden = false;
      setTimeout(() => this.hide(), 2000);
    }
    hide() {
      this._icon.hidden = true;
    }
    queryText(text, ev) {
      if (text) {
        this._icon.hidden = true;
        tip.pop(ev);
        creatRecognizeTip(tip, text);
      }
    }
  }
  const icon = new Icon();

  document.addEventListener("mouseup", function (e) {
    const t = window.getSelection().toString().trim();
    if (!t) {
      icon.hide();
      tip.hide();
    } else if (windowPopup) icon.pop(e);
  });

  // ================= 站点匹配 =================
  const handlers = {
    "m-team": () =>
      waitForElements([".ant-descriptions-row"]).then(([rows]) => {
        ptype = "m-team";
        const r = rows[0];
        const row = r.parentNode.insertRow(2);
        row.className = "ant-descriptions-row";
        if (r.firstElementChild?.nextElementSibling)
          creatRecognizeRow(
            row,
            ptype,
            r.firstElementChild.nextElementSibling.outerText.split("\n")[0],
            rows[1]?.textContent || "",
            "",
            "",
          );
      }),
    hhanclub: () =>
      waitForElements([".font-bold.leading-6"]).then(([divs]) => {
        ptype = "hhanclub";
        const dl = document.querySelector("a.index");
        divs[3].insertAdjacentHTML(
          "afterend",
          '<div class="font-bold leading-6">moviepilot</div><div class="font-light leading-6 flex flex-wrap"><div id="mp-row" class="font-light leading-6 flex" style="width:100%"></div></div>',
        );
        const row = document.getElementById("mp-row");
        if (row && divs[3])
          creatRecognizeRow(
            row,
            ptype,
            divs[3].innerText,
            divs[5]?.innerText || "",
            dl?.href || "",
            getSize(divs[7]?.nextElementSibling?.innerText),
          );
      }),
    bangumi: () =>
      waitForElements(["md-actions", ".torrent-info"]).then(
        ([btns, titles]) => {
          btype = "bangumi";
          const base = btns[0].firstElementChild.formAction.replace(
            site_domain,
            site_domain + "/download",
          );
          creatPushButton(
            btype,
            btns[0],
            base + "/" + titles[0].outerText.replaceAll("/", "_") + ".torrent",
          );
        },
      ),
    mikanani: () =>
      waitForElements([".leftbar-nav"]).then(([el]) => {
        btype = "mikan";
        creatPushButton(btype, el[0], el[0].firstElementChild.href);
      }),
    dmhy: () =>
      waitForElements([
        ".dis.ui-tabs-panel.ui-widget-content.ui-corner-bottom",
      ]).then(([el]) => {
        btype = "dmhy";
        creatPushButton(
          btype,
          el[0],
          el[0].firstElementChild.lastElementChild.href,
        );
      }),
    skyey2: () =>
      waitForElements([".pi"]).then(([el]) => {
        btype = "skyey";
        creatPushButton(
          btype,
          el[4],
          encodeURIComponent(el[4].childNodes[13].firstElementChild.href),
        );
      }),
    "totheglory.im": () =>
      waitForElements(["body"]).then(() => {
        ptype = "common";
        const titleEl =
          document.querySelector("h1#top") || document.querySelector("h1");
        let name = titleEl ? titleEl.innerText.trim() : "";
        name = name.replace(/^\[.*?\]\./, "").trim();
        const dlA =
          document.querySelector('a[href*="/dl/"]') ||
          document.querySelector('a[href^="/dl/"]');
        const link = dlA?.href || "";
        let desc = "",
          sizeStr = "";
        document.querySelectorAll("td, th, div, span").forEach((n) => {
          const t = (n.innerText || "").trim();
          if (
            !desc &&
            (t.includes("副标题") || t.includes("Small Description"))
          )
            desc = n.nextElementSibling?.innerText?.trim() || "";
          if (
            !sizeStr &&
            (t === "大小" ||
              t === "Size" ||
              t.includes("大小") ||
              t.includes("Size"))
          )
            sizeStr = n.nextElementSibling?.innerText?.trim() || "";
        });
        if (!desc)
          desc =
            (
              document.querySelector("#subtitle") ||
              document.querySelector(".subtitle") ||
              document.querySelector(".small")
            )?.innerText?.trim() || "";
        const table =
          document.querySelector("#form_torrent table") ||
          document.querySelector("table.mainouter table") ||
          document.querySelector("table");
        const tbody = table
          ? table.tBodies[0] || table.querySelector("tbody")
          : null;
        if (tbody && name && link)
          creatRecognizeRow(
            tbody.insertRow(2),
            ptype,
            name,
            desc,
            link,
            getSize(sizeStr),
          );
      }),
  };
  for (const [domain, handler] of Object.entries(handlers)) {
    if (site_domain.includes(domain)) {
      handler().catch((e) => logError("站点处理失败:", e));
      return;
    }
  }
  // 通用 NexusPHP
  waitForElements([".rowhead"])
    .then(([rows]) => {
      ptype = "common";
      const titleEl =
        document.querySelector("h1#top") || document.querySelector("h1");
      let name = titleEl
        ? titleEl.innerText.trim()
        : rows[0]?.nextElementSibling?.innerText?.trim() || "";
      name = name.replace(/^\[.*?\]\./, "").trim();
      let desc = "",
        sizeStr = "",
        link = "";
      const dlA =
        document.querySelector('a[href*="download.php"]') ||
        document.querySelector('a[href*="/dl/"]');
      if (dlA) link = dlA.href;
      for (let i = 0; i < rows.length; i++) {
        const h = rows[i].innerText;
        if (h.includes("副标题") || h.includes("Small Description"))
          desc = rows[i].nextElementSibling?.innerText?.trim() || "";
        if (h.includes("大小") || h.includes("Size"))
          sizeStr = rows[i].nextElementSibling?.innerText?.trim() || "";
      }
      // 后备：扫描 rowfollow 单元格中的大小信息（某些站点如 HDSky 的大小嵌在联合行中）
      if (!sizeStr) {
        document.querySelectorAll(".rowfollow").forEach((td) => {
          const m = (td.innerText || "").match(
            /(\d+(?:\.\d+)?)\s*(GB|MB|KB|TB)/i,
          );
          if (m) sizeStr = m[0];
        });
      }
      const tbody = rows[0].closest("tbody");
      if (tbody && name)
        creatRecognizeRow(
          tbody.insertRow(2),
          ptype,
          name,
          desc,
          link,
          getSize(sizeStr),
        );
    })
    .catch((e) => logError("通用站点处理失败:", e));
})();
