<script lang="ts">
  import { fly } from 'svelte/transition';
  import { toasts, dismiss, type ToastLevel } from './snackbar.svelte.ts';
  import { IconSuccess, IconCritical, IconInfo } from './icons';

  const colorMap: Record<ToastLevel, string> = {
    success: 'bg-ctp-green/15 border-ctp-green/40 text-ctp-green',
    error:   'bg-ctp-red/15 border-ctp-red/40 text-ctp-red',
    info:    'bg-ctp-blue/15 border-ctp-blue/40 text-ctp-blue',
  };
</script>

<div
  class="fixed bottom-6 right-6 z-50 flex flex-col gap-2 max-w-sm w-full sm:w-auto"
  aria-live="polite"
  aria-atomic="false"
>
  {#each toasts as t (t.id)}
    <div
      transition:fly={{ y: 16, duration: 200 }}
      class="flex items-start gap-3 px-4 py-3 rounded-lg border shadow-lg {colorMap[t.level]}"
      role="status"
    >
      <span class="shrink-0 mt-0.5">
        {#if t.level === 'success'}
          <IconSuccess size={15} strokeWidth={2.5} />
        {:else if t.level === 'error'}
          <IconCritical size={15} strokeWidth={2.5} />
        {:else}
          <IconInfo size={15} strokeWidth={2.5} />
        {/if}
      </span>
      <p class="text-sm font-medium flex-1 leading-snug">{t.message}</p>
      <button
        onclick={() => dismiss(t.id)}
        class="shrink-0 opacity-60 hover:opacity-100 transition-opacity ml-1"
        aria-label="Dismiss"
      >
        <svg viewBox="0 0 14 14" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
          <line x1="2" y1="2" x2="12" y2="12" />
          <line x1="12" y1="2" x2="2" y2="12" />
        </svg>
      </button>
    </div>
  {/each}
</div>
