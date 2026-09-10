/**
 * Service-worker registration (#1023).
 *
 * Deliberately narrow. It runs only in a production build, because the dev
 * server serves unbundled modules a shell-precaching worker would cache
 * wrongly and then keep serving. And it fails silently by design: a
 * service worker needs a SECURE CONTEXT, so a console reached over plain
 * http on a tailnet hostname simply does not get one and stays an ordinary
 * web page. That is a working console without offline support, not a broken
 * one, so it must not surface as an error to an operator mid-incident.
 */
export function registerServiceWorker(): void {
  if (!import.meta.env.PROD) return;
  if (!('serviceWorker' in navigator)) return;
  // Registration is not urgent and competes with the first paint of a page
  // someone may be opening to halt trading.
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch((err: unknown) => {
      // Includes the insecure-context refusal above. Logged, never thrown.
      console.info('basis: service worker not registered', err);
    });
  });
}
