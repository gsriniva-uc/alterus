window.addEventListener('message', (event) => {
  if (!event.origin.includes('alterus.io') && 
      !event.origin.includes('netlify.app') &&
      !event.origin.includes('localhost')) return;
  if (event.data?.type !== 'ALTERUS_CONNECT') return;

  const { name, email, userId, token } = event.data;

  chrome.storage.local.set({
    userConfig: {
      name,
      email,
      userId,
      token,
      connectedViaApp: true,
    }
  }, () => {
    window.postMessage({ type: 'ALTERUS_CONNECTED', success: true }, '*');
    console.log('✅ Alterus extension connected for', name);
  });
});
