window.addEventListener('message', (event) => {
  if (!event.origin.includes('alterus.io')) return;
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
    window.postMessage({ type: 'ALTERUS_CONNECTED', success: true }, '*');
    console.log('✅ Alterus extension connected for', name);
  });
});
