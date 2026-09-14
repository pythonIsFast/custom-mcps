/*
 * Untis browser inspector
 * Paste this complete script into the DevTools console on a WebUntis page.
 *
 * It records endpoint paths, request methods, field names/types, and response
 * schemas. It deliberately never exports cookies, header values, form values,
 * query values, request values, or response values.
 */
(() => {
  "use strict";

  if (window.__UNTIS_INSPECTOR__) {
    console.warn("Untis inspector is already active. Use __UNTIS_INSPECTOR__.download().");
    return;
  }

  const MAX_RECORDS = 500;
  const MAX_BODY_BYTES = 100_000;
  const STORAGE_KEY = "__untis_inspector_records_v1";
  let records = [];
  try {
    const saved = JSON.parse(sessionStorage.getItem(STORAGE_KEY) || "[]");
    if (Array.isArray(saved)) records = saved.slice(-MAX_RECORDS);
  } catch (_) { /* start with an empty report */ }
  const xhrState = new WeakMap();
  const originalFetch = window.fetch;
  const originalXhrOpen = XMLHttpRequest.prototype.open;
  const originalXhrSend = XMLHttpRequest.prototype.send;
  const originalXhrSetRequestHeader = XMLHttpRequest.prototype.setRequestHeader;

  const now = () => new Date().toISOString();
  const trim = (value, limit = 200) => String(value).slice(0, limit);

  function endpoint(url) {
    try {
      const parsed = new URL(url, location.href);
      return {
        origin: parsed.origin,
        path: parsed.pathname,
        query_keys: [...parsed.searchParams.keys()].sort(),
      };
    } catch (_) {
      return { path: trim(url) };
    }
  }

  function schema(value, depth = 0) {
    if (depth > 5) return { type: "max-depth" };
    if (value === null) return { type: "null" };
    if (Array.isArray(value)) {
      return {
        type: "array",
        length: value.length,
        item: value.length ? schema(value[0], depth + 1) : undefined,
      };
    }
    switch (typeof value) {
      case "object": {
        const fields = {};
        for (const key of Object.keys(value).slice(0, 100)) {
          fields[key] = schema(value[key], depth + 1);
        }
        return { type: "object", fields };
      }
      case "string": return { type: "string" };
      case "number": return { type: "number" };
      case "boolean": return { type: "boolean" };
      default: return { type: typeof value };
    }
  }

  function bodySchema(body, contentType = "") {
    if (body == null) return undefined;
    if (body instanceof FormData) {
      return { type: "form-data", keys: [...body.keys()].sort() };
    }
    if (body instanceof URLSearchParams) {
      return { type: "url-encoded", keys: [...body.keys()].sort() };
    }
    if (body instanceof Blob) return { type: "blob", size: body.size, mime: body.type || undefined };
    if (body instanceof ArrayBuffer || ArrayBuffer.isView(body)) return { type: "binary" };
    if (typeof body !== "string") return { type: typeof body };

    if (body.length > MAX_BODY_BYTES) return { type: "text", bytes: body.length, skipped: "too-large" };
    const mime = contentType.toLowerCase();
    if (mime.includes("json") || /^[\s[{]/.test(body)) {
      try { return { type: "json", schema: schema(JSON.parse(body)) }; } catch (_) { /* continue */ }
    }
    if (mime.includes("x-www-form-urlencoded")) {
      return { type: "url-encoded", keys: [...new URLSearchParams(body).keys()].sort() };
    }
    return { type: "text", bytes: body.length };
  }

  async function responseSchema(response) {
    const contentType = response.headers.get("content-type") || "";
    const result = { status: response.status, mime: contentType.split(";")[0] || undefined };
    if (!contentType.toLowerCase().includes("json")) return result;
    const length = Number(response.headers.get("content-length"));
    if (Number.isFinite(length) && length > MAX_BODY_BYTES) {
      result.skipped = "too-large";
      return result;
    }
    try {
      const text = await response.clone().text();
      if (text.length > MAX_BODY_BYTES) result.skipped = "too-large";
      else result.schema = schema(JSON.parse(text));
    } catch (_) {
      result.schema_error = "JSON could not be read";
    }
    return result;
  }

  function persist() {
    try { sessionStorage.setItem(STORAGE_KEY, JSON.stringify(records)); } catch (_) { /* storage unavailable */ }
  }

  function add(record) {
    if (records.length >= MAX_RECORDS) records.shift();
    if (!record.time) record.time = now();
    records.push(record);
    persist();
    return record;
  }

  function formSnapshot() {
    return [...document.forms].map((form) => ({
      id: form.id || undefined,
      name: form.name || undefined,
      action: endpoint(form.action || location.href),
      method: (form.method || "get").toUpperCase(),
      fields: [...form.elements].map((field) => ({
        name: field.name || undefined,
        id: field.id || undefined,
        tag: field.tagName.toLowerCase(),
        type: field.type || undefined,
        required: Boolean(field.required),
        disabled: Boolean(field.disabled),
      })),
    }));
  }

  window.fetch = async function untisInspectorFetch(input, init = {}) {
    const request = input instanceof Request ? input : new Request(input, init);
    const requestHeaders = [...request.headers.keys()].sort();
    const record = {
      transport: "fetch",
      request: {
        method: request.method,
        endpoint: endpoint(request.url),
        header_names: requestHeaders,
      },
    };
    try {
      if (!["GET", "HEAD"].includes(request.method)) {
        record.request.body = bodySchema(await request.clone().text(), request.headers.get("content-type") || "");
      }
    } catch (_) {
      record.request.body = { type: "unreadable" };
    }
    add(record);
    try {
      const response = await originalFetch.apply(this, arguments);
      record.response = await responseSchema(response);
      persist();
      return response;
    } catch (error) {
      record.error = String(error && error.message || error);
      persist();
      throw error;
    }
  };

  XMLHttpRequest.prototype.open = function untisInspectorOpen(method, url) {
    xhrState.set(this, {
      transport: "xhr",
      request: { method: String(method).toUpperCase(), endpoint: endpoint(url), header_names: [] },
    });
    return originalXhrOpen.apply(this, arguments);
  };

  XMLHttpRequest.prototype.setRequestHeader = function untisInspectorSetHeader(name) {
    const state = xhrState.get(this);
    if (state) state.request.header_names.push(String(name).toLowerCase());
    return originalXhrSetRequestHeader.apply(this, arguments);
  };

  XMLHttpRequest.prototype.send = function untisInspectorSend(body) {
    const state = xhrState.get(this);
    if (state) {
      state.request.header_names = [...new Set(state.request.header_names)].sort();
      state.request.body = bodySchema(body);
      add(state);
      this.addEventListener("loadend", () => {
        state.response = { status: this.status, mime: this.getResponseHeader("content-type") || undefined };
        if ((state.response.mime || "").toLowerCase().includes("json")) {
          try {
            if (this.responseType === "json") {
              state.response.schema = schema(this.response);
            } else if (typeof this.responseText === "string" && this.responseText.length <= MAX_BODY_BYTES) {
              state.response.schema = schema(JSON.parse(this.responseText));
            } else {
              state.response.skipped = "too-large-or-unreadable";
            }
          } catch (_) {
            state.response.schema_error = "JSON could not be read";
          }
        }
        persist();
      }, { once: true });
    }
    return originalXhrSend.apply(this, arguments);
  };

  for (const entry of performance.getEntriesByType("resource")) {
    if (/untis|api|jsonrpc/i.test(entry.name)) {
      add({ transport: "performance", request: { endpoint: endpoint(entry.name) } });
    }
  }

  const api = {
    version: "1.3",
    snapshot() {
      return {
        exported_at: now(),
        page: { origin: location.origin, path: location.pathname, title: document.title },
        forms: formSnapshot(),
        requests: records,
        storage_keys: {
          local: Object.keys(localStorage),
          session: Object.keys(sessionStorage).filter((key) => key !== STORAGE_KEY),
        },
        note: "No cookies, header values, query values, form values, request values, or response values are included.",
      };
    },
    download() {
      const data = JSON.stringify(this.snapshot(), null, 2);
      const link = document.createElement("a");
      link.href = URL.createObjectURL(new Blob([data], { type: "application/json" }));
      link.download = `untis-inspection-${new Date().toISOString().replace(/[:.]/g, "-")}.json`;
      link.click();
      setTimeout(() => URL.revokeObjectURL(link.href), 1000);
      console.info("Untis inspection downloaded. Review it before sharing.");
    },
    clear() {
      records = [];
      try { sessionStorage.removeItem(STORAGE_KEY); } catch (_) { /* storage unavailable */ }
      console.info("Stored Untis inspection records cleared.");
    },
    stop() {
      window.fetch = originalFetch;
      XMLHttpRequest.prototype.open = originalXhrOpen;
      XMLHttpRequest.prototype.send = originalXhrSend;
      XMLHttpRequest.prototype.setRequestHeader = originalXhrSetRequestHeader;
      delete window.__UNTIS_INSPECTOR__;
      console.info("Untis inspector stopped.");
    },
  };

  Object.defineProperty(window, "__UNTIS_INSPECTOR__", { value: api, configurable: true });
  console.info("Untis inspector active. Use the site normally, then run __UNTIS_INSPECTOR__.download().");
})();
