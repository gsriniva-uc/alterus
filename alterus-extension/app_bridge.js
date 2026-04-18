/**
 * app_bridge.js
 * Runs on app.alterus.io
 * Listens for user profile from the dashboard
 * and saves it to extension storage
 */

window.addEventListener('message', (event) => {
  if (event.origin !== 'https://app.alterus.io') return;
  if (event.data?.type !== 'ALTERUS_CONNECT') return;

  const { name, email, userId } = event.data;

  chrome.storage.local.set({
    userConfig: {
      name,
      email,
      userId,
      connectedViaApp: true,
    }
  }, () => {
    // Send confirmation back to the page
    window.postMessage({ type: 'ALTERUS_CONNECTED', success: true }, '*');
    console.log('✅ Alterus extension connected for', name);
  });
});
