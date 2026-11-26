"""Safe, bounded inspection and preparation of browser form controls."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from playwright.async_api import Page

logger = logging.getLogger("carryflow.automation.automator")
DISALLOWED_SUBMIT_SELECTORS = {"#submit-application", "button[type='submit']", "input[type='submit']", ".submit-button"}

INSPECT_SCRIPT = r"""() => {
  const all = [...document.querySelectorAll('input,textarea,select')];
  const fields = []; let excluded = 0; const seenChoiceGroups = new Set();
  const textFor = (el, selector) => selector ? (document.getElementById(selector)?.innerText || '').trim() : '';
  for (let i = 0; i < all.length && fields.length < 200; i++) {
    const el = all[i], type = (el.type || el.tagName.toLowerCase()).toLowerCase();
    const isChoice = ['radio','checkbox'].includes(type), groupName = el.name || el.id || `choice-${i}`;
    if (isChoice) { const key=`${type}|${groupName}`; if(seenChoiceGroups.has(key)) continue; seenChoiceGroups.add(key); }
    const groupEls = isChoice && el.name ? [...document.querySelectorAll(`input[type="${type}"][name="${CSS.escape(el.name)}"]`)] : [el];
    const identity = `${el.name||''}|${el.id||''}|${type}|${i}`;
    const fieldId = `cf-${i}-${btoa(unescape(encodeURIComponent(identity))).replace(/[^a-zA-Z0-9]/g,'').slice(0,16)}`;
    const labels = el.labels ? [...el.labels].map(x => x.innerText.trim()).filter(Boolean) : [];
    const legendLabel = el.closest('fieldset')?.querySelector('legend')?.innerText.trim() || '';
    let label = isChoice && legendLabel ? legendLabel : labels.join(' / ');
    if (!label && el.getAttribute('aria-labelledby')) label = el.getAttribute('aria-labelledby').split(/\s+/).map(id=>textFor(el,id)).filter(Boolean).join(' / ');
    if (!label) label = (el.getAttribute('aria-label') || '').trim();
    if (!label) label = legendLabel;
    const auth = `${el.name||''} ${el.id||''} ${el.autocomplete||''} ${label} ${el.getAttribute('aria-label')||''} ${el.placeholder||''}`.toLowerCase();
    if (['password','hidden','submit','button','reset','image'].includes(type) || /password|passcode|\bpin\b|captcha|one.?time|\botp\b|two.?factor|\bmfa\b|verification.?code|security.?code|credit.?card|card.?number|cc-(number|csc|exp|name)|cvv|access.?token|refresh.?token|auth.?token|session.?token|api.?key|csrf|xsrf|\b(username|login)\b/.test(auth)) { excluded++; continue; }
    const style = getComputedStyle(el), rect = el.getBoundingClientRect();
    const visible = style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0' && rect.width > 0 && rect.height > 0 && !el.disabled && !el.closest('[aria-hidden="true"]');
    const description = [el.getAttribute('aria-description'), el.getAttribute('aria-describedby')?.split(/\s+/).map(id=>textFor(el,id)).join(' '), el.title].filter(Boolean).join(' ').slice(0,240);
    let options = el.tagName === 'SELECT' ? [...el.options].slice(0,60).map(o=>({label:o.label||o.textContent.trim(),value:o.value,disabled:o.disabled})) : [];
    const constraints = {min:el.min||null,max:el.max||null,step:el.step||null,minlength:el.minLength>=0?el.minLength:null,maxlength:el.maxLength>=0?el.maxLength:null,pattern:el.pattern||null,autocomplete:el.autocomplete||null};
    groupEls.forEach(choice=>choice.setAttribute('data-carryflow-field-id',fieldId));
    if (isChoice) options = groupEls.slice(0,60).map(choice=>({label:choice.labels?.[0]?.innerText.trim()||choice.value||'Choice',value:choice.value||''}));
    fields.push({field_id:fieldId,fingerprint:'',field_type:isChoice?`${type}-group`:type,label:label || (isChoice ? groupName : ''),name:el.name||'',element_id:el.id||'',placeholder:el.placeholder||'',required:groupEls.some(choice=>!!choice.required),options,constraints,visible,accessible_name:label || (isChoice?groupName:''),description,unsupported_reason: type==='file'?'File selection and resume upload are intentionally unsupported.':(!visible?'Field is hidden, disabled, or not visible.':(isChoice?'Choice controls are displayed for review but are not filled automatically.':(!['text','email','tel','number','date','url','textarea','select-one','select-multiple'].includes(type)?'Unsupported form control type.':'')))});
  }
  return {fields,excluded_count:excluded};
}"""


class FormAutomator:
    """Inspect and fill only approved fields; never submit or handle credentials."""

    @staticmethod
    async def inspect_fields(page: Page) -> dict[str, Any]:
        raw = await page.evaluate(INSPECT_SCRIPT)
        fields = []
        for item in raw.get("fields", []) if isinstance(raw, dict) else []:
            safe = dict(item)
            safe["options"] = [x.get("label", "") for x in safe.get("options", []) if not x.get("disabled")]
            signature = {key: safe.get(key) for key in ("field_type", "name", "element_id", "label", "required", "options")}
            safe["fingerprint"] = hashlib.sha256(json.dumps(signature, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            await page.locator(f'[data-carryflow-field-id="{safe["field_id"]}"]').evaluate_all("(els, fingerprint) => els.forEach(el => el.setAttribute('data-carryflow-fingerprint', fingerprint))", safe["fingerprint"])
            fields.append(safe)
        return {"fields": fields, "excluded_count": int(raw.get("excluded_count", 0)) if isinstance(raw, dict) else 0}

    @staticmethod
    async def observe_fields(page: Page) -> dict[str, str]:
        result = await page.evaluate("""() => { const out={}, marked=[...document.querySelectorAll('[data-carryflow-field-id]')]; const nodes=marked.length?marked:document.querySelectorAll('input:not([type=password]):not([type=hidden]),textarea,select'); for (const el of nodes) { const id=el.getAttribute('data-carryflow-field-id')||el.id||el.name; if(id) out[id]=(el.value||'').trim(); } return out; }""")
        return dict(result) if isinstance(result, dict) else {}

    @classmethod
    async def fill_fields(cls, page: Page, field_values: dict[str, str], expected_fingerprints: dict[str, str] | None = None) -> dict[str, Any]:
        filled: list[str] = []
        errors: dict[str, str] = {}
        for field_id, value in list(field_values.items())[:200]:
            if field_id in {"submit", "submit-application"}:
                errors[field_id] = "Submission controls are never automated."
                continue
            try:
                locator = page.locator(f'[data-carryflow-field-id="{field_id}"]').first if field_id.startswith("cf-") else page.locator(f'#{field_id}').first
                if await locator.count() == 0:
                    errors[field_id] = "Reviewed field no longer exists."
                    continue
                if expected_fingerprints and field_id in expected_fingerprints:
                    observed_fingerprint = await locator.get_attribute("data-carryflow-fingerprint")
                    if observed_fingerprint != expected_fingerprints[field_id] or not await locator.is_visible():
                        errors[field_id] = "Reviewed field changed or became hidden before filling."
                        continue
                tag = (await locator.evaluate("el => el.tagName.toLowerCase()"))
                kind = (await locator.evaluate("el => (el.type || '').toLowerCase()"))
                if tag == "select":
                    await locator.select_option(label=value)
                elif tag not in {"input", "textarea"} or kind not in {"text", "email", "tel", "number", "date", "url", ""}:
                    errors[field_id] = "This control type cannot be filled safely."
                    continue
                else:
                    await locator.fill(value)
                await locator.evaluate("el => { el.dispatchEvent(new Event('input',{bubbles:true})); el.dispatchEvent(new Event('change',{bubbles:true})); }")
                filled.append(field_id)
            except Exception as exc:
                logger.info("Unable to prepare reviewed field %s: %s", field_id, exc)
                errors[field_id] = "The field rejected the prepared value or changed during preparation."
        return {"filled": filled, "errors": errors, "observed": await cls.observe_fields(page)}
