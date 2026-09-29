// Runs on depop.com. When the local Sell page has queued an item and this tab is on
// "List an item", fetch the item's photos from the local server and put them into Depop's
// photo input exactly as a drag-and-drop from Explorer would. Never touches the Post button.

const FILE_INPUT = 'input[type=file]';
let busy = false;

function banner(text, isError) {
  let b = document.getElementById('depop-seller-banner');
  if (!b) {
    b = document.createElement('div');
    b.id = 'depop-seller-banner';
    b.style.cssText = 'position:fixed;top:12px;right:12px;z-index:2147483647;background:#111;color:#fff;font:14px system-ui;padding:10px 14px;border-radius:8px;box-shadow:0 4px 16px rgba(0,0,0,.3);max-width:360px';
    document.body.appendChild(b);
  }
  b.style.background = isError ? '#b91c1c' : '#111';
  b.textContent = 'Depop Seller: ' + text;
  if (!isError) clearTimeout(b._t), b._t = setTimeout(() => b.remove(), 8000);
}

// React ignores a plain `el.value = x`; go through the native setter and fire an input event.
function setReactValue(el, value) {
  const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, value);
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
}

const DESCRIPTION_SELECTORS = [
  'textarea[name*="description" i]', 'textarea[id*="description" i]', 'textarea[placeholder*="worn" i]',
  'textarea', '[contenteditable="true"][aria-label*="description" i]', '[contenteditable="true"]',
  'input[name*="description" i]',
];

// Depop's description box may be a plain textarea (React) or an editable div (Slate/Lexical style):
// try each in turn, using the mechanism that box type actually listens to.
async function fillDescription(text) {
  for (const sel of DESCRIPTION_SELECTORS) {
    const el = await waitFor(sel, sel === 'textarea' ? 6000 : 800);
    if (!el || !isVisible(el)) continue;
    el.focus();
    if (el.isContentEditable) {
      document.execCommand('selectAll', false, null);
      let ok = false;
      try { ok = document.execCommand('insertText', false, text); } catch (e) { ok = false; }
      if (!ok) {
        el.textContent = text;
        el.dispatchEvent(new InputEvent('input', { bubbles: true, data: text, inputType: 'insertText' }));
      }
    } else {
      setReactValue(el, text);
    }
    await new Promise(r => setTimeout(r, 300));
    const now = el.isContentEditable ? el.textContent : el.value;
    if (now && now.trim().startsWith(text.trim().slice(0, 20))) return true;
  }
  return false;
}

function isVisible(el) { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; }

function waitFor(selector, ms) {
  return new Promise(resolve => {
    const t0 = Date.now();
    (function look() {
      const el = document.querySelector(selector);
      if (el) return resolve(el);
      if (Date.now() - t0 > ms) return resolve(null);
      setTimeout(look, 300);
    })();
  });
}

async function pendingJob() {
  const { pending } = await chrome.storage.local.get('pending');
  if (!pending) return null;
  if (Date.now() - pending.at > 30 * 60 * 1000) { await chrome.storage.local.remove('pending'); return null; }
  return pending;
}

async function addPhotos(job) {
  const input = document.querySelector(FILE_INPUT);
  if (!input) return false;
  busy = true;
  try {
    banner(`fetching photos for item ${job.item}…`);
    const server = job.server || 'http://127.0.0.1:8765';
    const res = await fetch(`${server}/sell/photos?batch=${encodeURIComponent(job.batch)}&item=${job.item}`);
    if (!res.ok) throw new Error(`local server said ${res.status} - is sell.cmd still running?`);
    const { photos, description } = await res.json();
    const dt = new DataTransfer();
    for (let i = 0; i < photos.length; i++) {
      banner(`loading photo ${i + 1} of ${photos.length}…`);
      const blob = await (await fetch(photos[i].url)).blob();
      dt.items.add(new File([blob], photos[i].name, { type: 'image/jpeg', lastModified: Date.now() }));
    }
    banner(`adding ${photos.length} photos to the form…`);
    if (input.multiple) {
      input.files = dt.files;
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.dispatchEvent(new Event('change', { bubbles: true }));
    } else {
      // one photo per slot: the form re-renders after each upload, so re-query every time
      for (let i = 0; i < dt.files.length; i++) {
        const inputs = document.querySelectorAll(FILE_INPUT);
        const target = inputs[Math.min(i, inputs.length - 1)];
        const one = new DataTransfer();
        one.items.add(dt.files[i]);
        target.files = one.files;
        target.dispatchEvent(new Event('input', { bubbles: true }));
        target.dispatchEvent(new Event('change', { bubbles: true }));
        await new Promise(r => setTimeout(r, 1500));
      }
    }
    let pasted = false;
    if (description && description.trim()) {
      banner('pasting the description…');
      pasted = await fillDescription(description);
    }
    await chrome.storage.local.remove('pending');
    if (description && !pasted) {
      banner(`${photos.length} photos added, but no description box was found on this page (tried: textarea, editable box, input). Paste it by hand from the Sell page.`, true);
    } else {
      banner(`${photos.length} photos${pasted ? ' and the description' : ''} added for item ${job.item}. Check everything, fill in the rest, and post when you're ready.`);
    }
    fetch(`${server}/sell/item?batch=${encodeURIComponent(job.batch)}&item=${job.item}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ status: 'opened' }),
    }).catch(() => {});
    return true;
  } catch (e) {
    banner('failed: ' + e.message, true);
    await chrome.storage.local.remove('pending');
    return true;
  } finally {
    busy = false;
  }
}

async function tick() {
  if (busy) return;
  if (!location.pathname.startsWith('/products/create')) return;
  const job = await pendingJob();
  if (!job) return;
  if (!document.querySelector(FILE_INPUT)) { banner(`item ${job.item} queued - waiting for the photo form (log in if Depop asks)`); return; }
  await addPhotos(job);
}

setInterval(tick, 1500);
tick();
