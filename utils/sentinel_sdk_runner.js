const fs = require("fs");
const vm = require("vm");

const input = JSON.parse(fs.readFileSync(0, "utf8"));
const sdkUrl = input.sdkUrl || "https://sentinel.openai.com/sentinel/sdk.js";
const origin = new URL(sdkUrl).origin;
const version = (new URL(sdkUrl).pathname.match(/\/sentinel\/([^/]+)\/sdk\.js/) || [])[1] || "";
const parentListeners = new Map();
const iframeListeners = new Map();
let parentWindow = null;
let iframeWindow = null;
let capturedBody = "";

function addListener(map, type, fn) {
  if (!map.has(type)) map.set(type, []);
  map.get(type).push(fn);
}

function dispatch(map, type, event) {
  for (const fn of map.get(type) || []) {
    setTimeout(() => fn(event), 0);
  }
}

function makeStorage() {
  const values = new Map();
  return {
    getItem: (key) => (values.has(String(key)) ? values.get(String(key)) : null),
    setItem: (key, value) => values.set(String(key), String(value)),
    removeItem: (key) => values.delete(String(key)),
    clear: () => values.clear(),
    key: (index) => Array.from(values.keys())[index] || null,
    get length() {
      return values.size;
    },
  };
}

function makeContext(kind) {
  const listeners = kind === "parent" ? parentListeners : iframeListeners;
  const locationHref =
    kind === "iframe"
      ? `${origin}/backend-api/sentinel/frame.html?sv=${encodeURIComponent(version)}`
      : `${origin}/`;
  const screen = {
    width: 1920,
    height: 1080,
    availWidth: 1920,
    availHeight: 1040,
    colorDepth: 24,
    pixelDepth: 24,
  };
  const win = {
    self: null,
    top: null,
    location: new URL(locationHref),
    __sentinel_init_pending: [],
    __sentinel_token_pending: [],
    innerWidth: 1920,
    innerHeight: 1080,
    outerWidth: 1920,
    outerHeight: 1080,
    devicePixelRatio: 1,
    screenX: 0,
    screenY: 0,
    scrollX: 0,
    scrollY: 0,
    screen,
    chrome: { runtime: {} },
    localStorage: makeStorage(),
    sessionStorage: makeStorage(),
    indexedDB: {},
    caches: {},
    scheduler: {},
    visualViewport: { width: 1920, height: 1080, scale: 1 },
    matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
    requestAnimationFrame: (cb) => setTimeout(() => cb(Date.now()), 16),
    cancelAnimationFrame: (id) => clearTimeout(id),
  };
  win.self = win;
  win.top = kind === "parent" ? win : parentWindow;
  win.addEventListener = (type, fn) => addListener(listeners, type, fn);
  win.postMessage = (message) => {
    if (kind === "parent") {
      dispatch(parentListeners, "message", { data: message, origin, source: iframeWindow });
    } else {
      dispatch(iframeListeners, "message", { data: message, origin, source: parentWindow });
    }
  };

  const currentScript = {
    src: sdkUrl,
    getAttribute: (name) => (name === "src" ? sdkUrl : null),
  };
  const document = {
    currentScript,
    scripts: [currentScript],
    cookie: `oai-did=${encodeURIComponent(input.deviceId || "")}`,
    location: win.location,
    documentURI: String(win.location.href),
    compatMode: "CSS1Compat",
    implementation: {},
    documentElement: {
      getAttribute: (name) => (name === "data-build" ? input.dataBuild || "" : null),
    },
    createElement: (tag) => {
      const localListeners = new Map();
      const element = {
        style: {},
        addEventListener: (type, fn) => addListener(localListeners, type, fn),
        get src() {
          return this._src || "";
        },
        set src(value) {
          this._src = value;
        },
      };
      if (tag === "iframe") element.contentWindow = iframeWindow;
      element._dispatch = (type) => dispatch(localListeners, type, { type });
      return element;
    },
    body: {
      appendChild: (element) => {
        setTimeout(() => element._dispatch && element._dispatch("load"), 10);
        return element;
      },
    },
    head: { appendChild: (element) => element },
    addEventListener() {},
  };
  const navigator = {
    userAgent: input.userAgent || "",
    language: "en-US",
    languages: ["en-US", "en"],
    hardwareConcurrency: 8,
    cookieEnabled: true,
    vendor: "Google Inc.",
    product: "Gecko",
    webdriver: false,
    pdfViewerEnabled: true,
    mimeTypes: [],
    plugins: [],
    permissions: { query: async () => ({ state: "prompt" }) },
    mediaDevices: {},
    storage: {},
    locks: {},
    credentials: {},
    serviceWorker: {},
    userAgentData: {
      brands: [
        { brand: "Google Chrome", version: "145" },
        { brand: "Chromium", version: "145" },
        { brand: "Not?A_Brand", version: "8" },
      ],
      mobile: false,
      platform: "Windows",
    },
  };
  const performance = {
    now: () => Date.now() % 100000,
    timeOrigin: Date.now() - 12345,
    memory: { jsHeapSizeLimit: 4294705152 },
  };
  const fetchImpl = async (_url, opts = {}) => {
    capturedBody = String(opts.body || "");
    return { json: async () => input.reqData || { token: "dummy-token" } };
  };
  const context = {
    window: win,
    self: win,
    top: win.top,
    document,
    navigator,
    location: win.location,
    screen,
    performance,
    crypto: globalThis.crypto,
    TextEncoder,
    URL,
    URLSearchParams,
    console,
    setTimeout,
    clearTimeout,
    queueMicrotask,
    Promise,
    Map,
    WeakMap,
    Uint8Array,
    Array,
    Object,
    Number,
    Math,
    Date,
    String,
    JSON,
    Error,
    atob: (value) => Buffer.from(value, "base64").toString("binary"),
    btoa: (value) => Buffer.from(value, "binary").toString("base64"),
    fetch: fetchImpl,
    localStorage: win.localStorage,
    sessionStorage: win.sessionStorage,
    chrome: win.chrome,
    matchMedia: win.matchMedia,
    requestAnimationFrame: win.requestAnimationFrame,
  };
  context.globalThis = context;
  return vm.createContext(context);
}

const parentContext = makeContext("parent");
parentWindow = parentContext.window;
const iframeContext = makeContext("iframe");
iframeWindow = iframeContext.window;
iframeWindow.top = parentWindow;
iframeContext.top = parentWindow;

vm.runInContext(input.sdkSource, iframeContext, { timeout: 5000 });
vm.runInContext(input.sdkSource, parentContext, { timeout: 5000 });

(async () => {
  const sdk = parentContext.SentinelSDK;
  const token = await sdk.token(input.flow);
  if (input.waitMs) {
    await new Promise((resolve) => setTimeout(resolve, Number(input.waitMs)));
  }
  const soToken = await sdk.sessionObserverToken(input.flow);
  process.stdout.write(JSON.stringify({ version, token, soToken, capturedBody }));
})().catch((error) => {
  process.stderr.write(String((error && error.stack) || error));
  process.exit(1);
});
