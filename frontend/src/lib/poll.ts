import { onDestroy } from 'svelte';

const DEFAULT_INTERVAL_MS = 60_000;

/**
 * Polls `fn` on an interval while the component is mounted, and re-polls
 * immediately whenever the tab becomes visible again (#477).
 *
 * StatusStrip, ReconciliationPanel, and BooksTab used to fetch onMount only
 * — heartbeat_age_hours/stale, reconciliation results, and control state are
 * all computed server-side at request time, so a console left open forever
 * showed last night's snapshot and the staleness guard never fired
 * client-side. Skipping the interval tick while the tab is hidden avoids
 * hammering the backend from background tabs that will re-poll on focus
 * anyway.
 */
export function startPolling(fn: () => void | Promise<void>, intervalMs = DEFAULT_INTERVAL_MS): void {
  const interval = setInterval(() => {
    if (document.visibilityState === 'visible') void fn();
  }, intervalMs);

  function onVisibilityChange() {
    if (document.visibilityState === 'visible') void fn();
  }
  document.addEventListener('visibilitychange', onVisibilityChange);

  onDestroy(() => {
    clearInterval(interval);
    document.removeEventListener('visibilitychange', onVisibilityChange);
  });
}
