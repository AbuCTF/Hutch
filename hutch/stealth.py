"""Built-in browser stealth evasions — works without playwright-stealth."""

import importlib
import logging

log = logging.getLogger(__name__)

_stealth_mod = None
_stealth_checked = False

EVASION_SCRIPT = """
(() => {
  // 1. navigator.webdriver
  Object.defineProperty(navigator, 'webdriver', {get: () => undefined});

  // 2. navigator.plugins — inject realistic Chrome PDF plugins
  const _plugins = [
    {name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format',
     length: 1, item: function(i) { return this[i]; },
     0: {type: 'application/x-google-chrome-pdf', suffixes: 'pdf', description: 'Portable Document Format', enabledPlugin: null}},
    {name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '',
     length: 1, item: function(i) { return this[i]; },
     0: {type: 'application/pdf', suffixes: 'pdf', description: '', enabledPlugin: null}},
    {name: 'Native Client', filename: 'internal-nacl-plugin', description: '',
     length: 2, item: function(i) { return this[i]; },
     0: {type: 'application/x-nacl', suffixes: '', description: 'Native Client Executable', enabledPlugin: null},
     1: {type: 'application/x-pnacl', suffixes: '', description: 'Portable Native Client Executable', enabledPlugin: null}},
  ];
  for (const p of _plugins) { p[Symbol.iterator] = function*() { for (let i=0; i<this.length; i++) yield this[i]; }; }
  Object.defineProperty(navigator, 'plugins', {get: () => {
    const arr = Object.create(PluginArray.prototype);
    _plugins.forEach((p, i) => { arr[i] = p; arr[p.name] = p; });
    Object.defineProperty(arr, 'length', {get: () => _plugins.length});
    arr.item = i => arr[i];
    arr.namedItem = n => arr[n];
    arr.refresh = () => {};
    arr[Symbol.iterator] = function*() { for (let i=0; i<this.length; i++) yield this[i]; };
    return arr;
  }});

  // 3. navigator.mimeTypes
  const _mimes = [
    {type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format'},
    {type: 'application/x-google-chrome-pdf', suffixes: 'pdf', description: 'Portable Document Format'},
    {type: 'application/x-nacl', suffixes: '', description: 'Native Client Executable'},
    {type: 'application/x-pnacl', suffixes: '', description: 'Portable Native Client Executable'},
  ];
  Object.defineProperty(navigator, 'mimeTypes', {get: () => {
    const arr = Object.create(MimeTypeArray.prototype);
    _mimes.forEach((m, i) => { arr[i] = m; arr[m.type] = m; });
    Object.defineProperty(arr, 'length', {get: () => _mimes.length});
    arr.item = i => arr[i];
    arr.namedItem = n => arr[n];
    arr[Symbol.iterator] = function*() { for (let i=0; i<this.length; i++) yield this[i]; };
    return arr;
  }});

  // 4. chrome.runtime — headless lacks this
  if (!window.chrome) window.chrome = {};
  if (!window.chrome.runtime) {
    window.chrome.runtime = {
      connect: () => {},
      sendMessage: () => {},
      id: undefined,
      OnInstalledReason: {CHROME_UPDATE: 'chrome_update', INSTALL: 'install', SHARED_MODULE_UPDATE: 'shared_module_update', UPDATE: 'update'},
      OnRestartRequiredReason: {APP_UPDATE: 'app_update', OS_UPDATE: 'os_update', PERIODIC: 'periodic'},
      PlatformArch: {ARM: 'arm', ARM64: 'arm64', MIPS: 'mips', MIPS64: 'mips64', X86_32: 'x86-32', X86_64: 'x86-64'},
      PlatformNaclArch: {ARM: 'arm', MIPS: 'mips', MIPS64: 'mips64', X86_32: 'x86-32', X86_64: 'x86-64'},
      PlatformOs: {ANDROID: 'android', CROS: 'cros', LINUX: 'linux', MAC: 'mac', OPENBSD: 'openbsd', WIN: 'win'},
      RequestUpdateCheckStatus: {ALREADY_UP_TO_DATE: 'already_up_to_date', NO_UPDATE: 'no_update', THROTTLED: 'throttled', UPDATE_AVAILABLE: 'update_available'},
    };
  }

  // 5. chrome.csi + chrome.loadTimes
  if (!window.chrome.csi) {
    window.chrome.csi = () => ({
      startE: Date.now(), onloadT: Date.now() + 100,
      pageT: performance.now(), tran: 15,
    });
  }
  if (!window.chrome.loadTimes) {
    window.chrome.loadTimes = () => ({
      commitLoadTime: Date.now() / 1000,
      connectionInfo: 'h2', connectioninfo: 'h2',
      finishDocumentLoadTime: Date.now() / 1000 + 0.1,
      finishLoadTime: Date.now() / 1000 + 0.2,
      firstPaintAfterLoadTime: 0, firstPaintTime: Date.now() / 1000 + 0.05,
      navigationType: 'Other', npnNegotiatedProtocol: 'h2',
      requestTime: Date.now() / 1000 - 0.5,
      startLoadTime: Date.now() / 1000 - 0.5,
      wasAlternateProtocolAvailable: false, wasFetchedViaSpdy: true,
      wasNpnNegotiated: true,
    });
  }

  // 6. permissions.query — Notification returns "prompt" (headless returns "denied")
  const _origQuery = Permissions.prototype.query;
  Permissions.prototype.query = function(desc) {
    if (desc && desc.name === 'notifications') {
      return Promise.resolve({state: 'prompt', onchange: null, addEventListener: () => {}, removeEventListener: () => {}});
    }
    return _origQuery.call(this, desc);
  };

  // 7. WebGL vendor/renderer — hide SwiftShader/ANGLE
  const _getParam = WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter = function(p) {
    if (p === 0x9245) return 'Google Inc. (NVIDIA)';     // UNMASKED_VENDOR_WEBGL
    if (p === 0x9246) return 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)'; // UNMASKED_RENDERER_WEBGL
    return _getParam.call(this, p);
  };
  if (typeof WebGL2RenderingContext !== 'undefined') {
    const _getParam2 = WebGL2RenderingContext.prototype.getParameter;
    WebGL2RenderingContext.prototype.getParameter = function(p) {
      if (p === 0x9245) return 'Google Inc. (NVIDIA)';
      if (p === 0x9246) return 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)';
      return _getParam2.call(this, p);
    };
  }

  // 8. canvas fingerprint noise
  const _toDataURL = HTMLCanvasElement.prototype.toDataURL;
  HTMLCanvasElement.prototype.toDataURL = function(type, quality) {
    const ctx = this.getContext('2d');
    if (ctx) {
      const shift = ((Date.now() % 255) * 0.001);
      const img = ctx.getImageData(0, 0, Math.min(this.width, 2), Math.min(this.height, 2));
      if (img.data.length > 0) img.data[0] = (img.data[0] + shift) & 0xFF;
      ctx.putImageData(img, 0, 0);
    }
    return _toDataURL.call(this, type, quality);
  };

  // 9. window.outerWidth/outerHeight — match inner + chrome frame
  Object.defineProperty(window, 'outerWidth', {get: () => window.innerWidth + 0});
  Object.defineProperty(window, 'outerHeight', {get: () => window.innerHeight + 85});

  // 10. navigator.connection — headless sometimes lacks this
  if (!navigator.connection) {
    Object.defineProperty(navigator, 'connection', {get: () => ({
      downlink: 10, effectiveType: '4g', rtt: 50, saveData: false,
      addEventListener: () => {}, removeEventListener: () => {},
    })});
  }
})();
"""


def _build_dynamic_script(fingerprint=None):
    """Build fingerprint-specific overrides that depend on session config."""
    parts = []

    hw_concurrency = 8
    device_memory = 8
    platform = "Win32"
    languages = '["en-US", "en"]'

    if fingerprint:
        platform = getattr(fingerprint, 'platform', None) or "Win32"
        locale = getattr(fingerprint, 'locale', 'en-US')
        lang = locale.split('-')[0] if '-' in locale else locale
        languages = f'["{locale}", "{lang}"]'
        if 'Mac' in platform:
            hw_concurrency = 10
            device_memory = 16

    parts.append(f"Object.defineProperty(navigator, 'hardwareConcurrency', {{get: () => {hw_concurrency}}});")
    parts.append(f"Object.defineProperty(navigator, 'deviceMemory', {{get: () => {device_memory}}});")
    parts.append(f"Object.defineProperty(navigator, 'platform', {{get: () => '{platform}'}});")
    parts.append(f"Object.defineProperty(navigator, 'languages', {{get: () => {languages}}});")

    return "(() => {\n" + "\n".join(parts) + "\n})();"


async def apply_stealth(context, *, fingerprint=None):
    """Apply comprehensive stealth evasions to a browser context.

    Always applies built-in evasions. Also applies playwright-stealth on top
    if the optional package is available.
    """
    await context.add_init_script(EVASION_SCRIPT)
    await context.add_init_script(_build_dynamic_script(fingerprint))

    global _stealth_mod, _stealth_checked
    if not _stealth_checked:
        _stealth_checked = True
        try:
            _stealth_mod = importlib.import_module("playwright_stealth")
        except ImportError:
            _stealth_mod = None

    if _stealth_mod:
        try:
            if hasattr(_stealth_mod, "Stealth"):
                stealth = _stealth_mod.Stealth()
                await stealth.apply_stealth_async(context)
            elif hasattr(_stealth_mod, "stealth_async"):
                await _stealth_mod.stealth_async(context)
            log.debug("playwright-stealth applied on top of built-in evasions")
        except Exception as e:
            log.warning("playwright-stealth failed (built-in evasions still active): %s", e)

    return True


def is_available():
    return True
