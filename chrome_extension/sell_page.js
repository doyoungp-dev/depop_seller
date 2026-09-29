// Runs on the local Sell page. Tells the page the helper is installed and remembers which
// item to add when the page asks, so the Depop tab can pick it up after it opens.
document.documentElement.dataset.depopSellerExt = chrome.runtime.getManifest().version;

window.addEventListener('message', (event) => {
  if (event.source !== window || !event.data || event.data.type !== 'depop-seller:open') return;
  const { batch, item, server } = event.data;
  chrome.storage.local.set({ pending: { batch, item, server, at: Date.now() } }, () => {
    window.postMessage({ type: 'depop-seller:stored', batch, item }, '*');
  });
});
