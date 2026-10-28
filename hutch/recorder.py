"""Record what happens in a tab (human or agent, from real DOM events) as replayable steps."""

import json

SCRIPT = r"""(() => {
  if (window.__hutchRecorder) return;
  window.__hutchRecorder = true;
  const send = step => { if (typeof window.__hutch_record === "function") window.__hutch_record(step); };
  const q = s => JSON.stringify(s);
  const clean = s => (s || "").trim().replace(/\s+/g, " ").slice(0, 80);
  const unique = sel => { try { return document.querySelectorAll(sel).length === 1; } catch (e) { return false; } };
  const field = "input:not([type=checkbox]):not([type=radio]):not([type=submit]):not([type=button]):not([type=reset]), textarea";

  function role(el) {
    const r = el.getAttribute("role");
    if (r) return r;
    const t = el.tagName.toLowerCase(), type = (el.getAttribute("type") || "").toLowerCase();
    if (t === "button" || (t === "input" && ["button", "submit", "reset"].includes(type))) return "button";
    if (t === "a" && el.hasAttribute("href")) return "link";
    if (t === "input" && type === "checkbox") return "checkbox";
    if (t === "input" && type === "radio") return "radio";
    if (t === "select") return "combobox";
    if (t === "textarea" || t === "input") return "textbox";
    return null;
  }
  function label(el) {
    if (el.labels && el.labels.length) return clean(el.labels[0].innerText);
    const by = el.getAttribute("aria-labelledby");
    const ref = by && document.getElementById(by.split(" ")[0]);
    return clean(el.getAttribute("aria-label") || (ref && ref.innerText) || "");
  }
  function selector(el) {
    for (const a of ["data-testid", "data-test", "data-qa"]) {
      const v = el.getAttribute(a);
      if (v) return `[${a}=${q(v)}]`;
    }
    if (el.id && !/\d{4,}|[:.]/.test(el.id) && unique("#" + CSS.escape(el.id))) return "#" + CSS.escape(el.id);
    const r = role(el);
    const named = ["textbox", "checkbox", "radio", "combobox"].includes(r)
      ? label(el) : clean(el.getAttribute("aria-label") || el.innerText || el.value);
    if (r && named) return `role=${r}[name=${q(named)}]`;
    const tag = el.tagName.toLowerCase();
    for (const a of ["name", "placeholder"]) {
      const v = el.getAttribute(a);
      if (v && unique(`${tag}[${a}=${q(v)}]`)) return `${tag}[${a}=${q(v)}]`;
    }
    const parts = [];
    for (let e = el; e && e.nodeType === 1 && e !== document.documentElement && parts.length < 6; e = e.parentElement) {
      let p = e.tagName.toLowerCase();
      const same = e.parentElement ? [...e.parentElement.children].filter(c => c.tagName === e.tagName) : [];
      if (same.length > 1) p += `:nth-of-type(${same.indexOf(e) + 1})`;
      parts.unshift(p);
      if (unique(parts.join(" > "))) break;
    }
    return parts.join(" > ");
  }
  const origin = e => {
    let el = e.composedPath ? e.composedPath()[0] : e.target;
    if (el && el.nodeType !== 1) el = el.parentElement;
    return el;
  };
  const fill = el => send({ action: "fill", selector: selector(el), value: el.value,
                             password: (el.getAttribute("type") || "").toLowerCase() === "password" });

  document.addEventListener("click", e => {
    const raw = origin(e);
    if (!raw) return;
    const el = raw.closest("button, a[href], input, select, textarea, label, summary, [role], [onclick], [tabindex]") || raw;
    if (el.matches(field + ", select")) return;
    if (el.matches("label") && el.control) return;
    if (el.matches("input[type=checkbox], input[type=radio]")) {
      send({ action: el.checked ? "check" : "uncheck", selector: selector(el) });
      return;
    }
    send({ action: "click", selector: selector(el) });
  }, true);
  document.addEventListener("change", e => {
    const el = origin(e);
    if (!el) return;
    if (el.matches("select")) send({ action: "select", selector: selector(el), value: el.value });
    else if (el.matches("input[type=file]")) send({ action: "upload", selector: selector(el), files: [...el.files].map(f => f.name) });
    else if (el.matches(field)) fill(el);
  }, true);
  document.addEventListener("keydown", e => {
    const el = origin(e);
    if (!el || !el.matches || !el.matches(field) || !["Enter", "Escape"].includes(e.key)) return;
    if (e.key === "Enter") fill(el);
    send({ action: "press", selector: selector(el), key: e.key });
  }, true);
})();"""


def add_step(recording, step):
    steps = recording["steps"]
    if step["action"] == "fill" and steps and steps[-1]["action"] == "fill" \
            and steps[-1]["selector"] == step["selector"]:
        steps[-1] = step
        return
    steps.append(step)


def load(source):
    if isinstance(source, dict):
        return source
    with open(source) as f:
        return json.load(f)


def to_python(recording):
    """A standalone Playwright script; secrets come from HUTCH_SECRET_<NAME> variables."""
    import re
    lines = [
        "import asyncio",
        "import os",
        "",
        "from playwright.async_api import async_playwright",
        "",
        "",
        "async def main():",
        "    async with async_playwright() as p:",
        "        browser = await p.chromium.launch(headless=False)",
        "        page = await browser.new_page()",
    ]

    def value(v):
        m = re.fullmatch(r"\{\{\s*([\w.-]+)\s*\}\}", v or "")
        if m:
            return f'os.environ["HUTCH_SECRET_{m.group(1).upper().replace("-", "_").replace(".", "_")}"]'
        return repr(v)

    if recording.get("start"):
        lines.append(f"        await page.goto({recording['start']!r})")
    for s in recording["steps"]:
        a, sel = s["action"], repr(s.get("selector"))
        if a == "goto":
            lines.append(f"        await page.goto({s['url']!r})")
        elif a == "wait_url":
            lines.append(f"        await page.wait_for_url({s['url'].split('?')[0] + '**'!r})")
        elif a == "click":
            lines.append(f"        await page.click({sel})")
        elif a == "fill":
            lines.append(f"        await page.fill({sel}, {value(s['value'])})")
        elif a == "press":
            lines.append(f"        await page.press({sel}, {s['key']!r})")
        elif a == "select":
            lines.append(f"        await page.select_option({sel}, {s['value']!r})")
        elif a in ("check", "uncheck"):
            lines.append(f"        await page.{a}({sel})")
        elif a == "upload":
            lines.append(f"        # upload {s.get('files')} into {s.get('selector')}")
    lines += ["        await browser.close()", "", "", "asyncio.run(main())", ""]
    return "\n".join(lines)
