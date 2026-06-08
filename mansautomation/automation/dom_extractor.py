"""Adaptive DOM extractor that maps form fields to canonical descriptors.

The extractor runs a single in-page script that walks the live DOM, supporting
React/Vue/Next.js/SPA workloads, lazy-loading, and dynamic re-renders. It emits
rich descriptors used by the form detection engine, so we never have to rely
on hard-coded site-specific selectors.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import Frame, Page


# JavaScript executed inside the page. Keeping this as a single function makes
# it cheap to evaluate over and over and avoids round-trips through Playwright.
_EXTRACT_SCRIPT = r"""
() => {
  const FIELD_TAGS = new Set(['INPUT', 'SELECT', 'TEXTAREA']);
  const SKIPPED_INPUT_TYPES = new Set([
    'hidden', 'submit', 'button', 'reset', 'image', 'file'
  ]);

  const visible = (el) => {
    if (!(el instanceof Element)) return false;
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
      return false;
    }
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };

  const textOf = (el) => {
    if (!el) return '';
    const t = (el.innerText || el.textContent || '').trim();
    return t.replace(/\s+/g, ' ').slice(0, 160);
  };

  const labelFor = (el) => {
    const labels = [];
    if (el.labels && el.labels.length) {
      for (const l of el.labels) {
        labels.push(textOf(l));
      }
    }
    if (el.id) {
      const lbl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lbl) labels.push(textOf(lbl));
    }
    let parent = el.parentElement;
    let depth = 0;
    while (parent && depth < 4) {
      if (parent.tagName === 'LABEL') {
        labels.push(textOf(parent));
        break;
      }
      parent = parent.parentElement;
      depth += 1;
    }
    return labels.filter(Boolean).join(' | ');
  };

  const aria = (el, attr) => (el.getAttribute(attr) || '').trim();

  const nearbyText = (el) => {
    const out = [];
    const previous = el.previousElementSibling;
    if (previous) out.push(textOf(previous));
    const parent = el.parentElement;
    if (parent) {
      out.push(textOf(parent));
      const grand = parent.parentElement;
      if (grand) out.push(textOf(grand));
    }
    return out.filter(Boolean).slice(0, 3).join(' | ');
  };

  const stableSelector = (el) => {
    if (el.id) return `#${CSS.escape(el.id)}`;
    if (el.getAttribute('name')) {
      return `${el.tagName.toLowerCase()}[name="${CSS.escape(el.getAttribute('name'))}"]`;
    }
    const parts = [];
    let cur = el;
    let depth = 0;
    while (cur && cur.nodeType === 1 && depth < 4) {
      const sibs = cur.parentElement
        ? Array.from(cur.parentElement.children).filter(c => c.tagName === cur.tagName)
        : [cur];
      const idx = sibs.indexOf(cur) + 1;
      parts.unshift(`${cur.tagName.toLowerCase()}:nth-of-type(${idx})`);
      cur = cur.parentElement;
      depth += 1;
    }
    return parts.join(' > ');
  };

  const optionList = (el) => {
    if (el.tagName !== 'SELECT') return null;
    const opts = [];
    for (const opt of el.options) {
      opts.push({ value: opt.value, label: (opt.textContent || '').trim() });
    }
    return opts;
  };

  const fields = [];
  const all = document.querySelectorAll('input, select, textarea, [role="combobox"], [role="textbox"], [contenteditable="true"]');
  for (const el of all) {
    if (FIELD_TAGS.has(el.tagName)) {
      const type = (el.getAttribute('type') || el.type || '').toLowerCase();
      if (SKIPPED_INPUT_TYPES.has(type)) continue;
      if (el.disabled || el.readOnly) continue;
    }
    if (!visible(el)) continue;

    fields.push({
      tag: el.tagName.toLowerCase(),
      type: (el.getAttribute('type') || el.type || '').toLowerCase(),
      id: el.id || null,
      name: el.getAttribute('name') || null,
      placeholder: aria(el, 'placeholder') || null,
      autocomplete: aria(el, 'autocomplete') || null,
      ariaLabel: aria(el, 'aria-label') || null,
      ariaLabelledBy: aria(el, 'aria-labelledby') || null,
      label: labelFor(el),
      nearby: nearbyText(el),
      role: aria(el, 'role') || null,
      contentEditable: el.getAttribute('contenteditable') || null,
      required: el.required || el.getAttribute('aria-required') === 'true',
      maxLength: el.maxLength && el.maxLength > 0 ? el.maxLength : null,
      pattern: aria(el, 'pattern') || null,
      value: el.value || el.textContent || '',
      options: optionList(el),
      selector: stableSelector(el),
    });
  }
  return {
    url: location.href,
    title: document.title,
    fields,
  };
}
"""


@dataclass(slots=True)
class FieldDescriptor:
    """Information about a single live form field."""

    tag: str
    type: str
    selector: str
    label: str = ""
    name: str | None = None
    id: str | None = None
    placeholder: str | None = None
    autocomplete: str | None = None
    aria_label: str | None = None
    aria_labelled_by: str | None = None
    nearby_text: str = ""
    role: str | None = None
    content_editable: str | None = None
    required: bool = False
    max_length: int | None = None
    pattern: str | None = None
    value: str = ""
    options: list[dict[str, str]] | None = None
    frame_url: str = ""

    @property
    def is_select(self) -> bool:
        return self.tag == "select"

    @property
    def is_checkbox(self) -> bool:
        return self.tag == "input" and self.type == "checkbox"

    @property
    def is_radio(self) -> bool:
        return self.tag == "input" and self.type == "radio"

    @property
    def is_contenteditable(self) -> bool:
        return self.content_editable in {"true", "plaintext-only"}

    def signature_text(self) -> str:
        """Combined text used by heuristics."""

        return " | ".join(
            value
            for value in (
                self.label,
                self.placeholder,
                self.aria_label,
                self.nearby_text,
                self.name or "",
                self.id or "",
                self.autocomplete or "",
            )
            if value
        )


@dataclass(slots=True)
class FormSnapshot:
    url: str
    title: str
    fields: list[FieldDescriptor] = field(default_factory=list)


class DomExtractor:
    """Drives the in-page script across the page and all child frames."""

    async def extract(self, page: Page) -> FormSnapshot:
        snapshot = FormSnapshot(url=page.url, title=await _safe_title(page))
        await self._extract_frame(page.main_frame, snapshot)
        for child in page.frames:
            if child is page.main_frame:
                continue
            try:
                await self._extract_frame(child, snapshot)
            except Exception:  # noqa: BLE001
                continue
        return snapshot

    async def _extract_frame(self, frame: Frame, snapshot: FormSnapshot) -> None:
        try:
            payload: Any = await frame.evaluate(_EXTRACT_SCRIPT)
        except Exception:
            return
        if not isinstance(payload, dict):
            return
        snapshot.title = snapshot.title or str(payload.get("title", ""))
        for raw in payload.get("fields", []) or []:
            try:
                snapshot.fields.append(_to_descriptor(raw, frame.url or ""))
            except Exception:  # noqa: BLE001
                continue


def _to_descriptor(raw: dict[str, Any], frame_url: str) -> FieldDescriptor:
    options_raw = raw.get("options")
    options: list[dict[str, str]] | None = None
    if isinstance(options_raw, list):
        options = [
            {
                "value": str(item.get("value", "")),
                "label": str(item.get("label", "")),
            }
            for item in options_raw
            if isinstance(item, dict)
        ]
    return FieldDescriptor(
        tag=str(raw.get("tag", "")),
        type=str(raw.get("type", "")),
        selector=str(raw.get("selector", "")),
        label=str(raw.get("label") or ""),
        name=raw.get("name") or None,
        id=raw.get("id") or None,
        placeholder=raw.get("placeholder") or None,
        autocomplete=raw.get("autocomplete") or None,
        aria_label=raw.get("ariaLabel") or None,
        aria_labelled_by=raw.get("ariaLabelledBy") or None,
        nearby_text=str(raw.get("nearby") or ""),
        role=raw.get("role") or None,
        content_editable=raw.get("contentEditable") or None,
        required=bool(raw.get("required", False)),
        max_length=raw.get("maxLength"),
        pattern=raw.get("pattern") or None,
        value=str(raw.get("value") or ""),
        options=options,
        frame_url=frame_url,
    )


async def _safe_title(page: Page) -> str:
    try:
        return await page.title()
    except Exception:  # noqa: BLE001
        return ""
