<script lang="ts">
  import type { Snippet } from 'svelte';
  import { X } from 'lucide-svelte';

  let {
    title,
    onclose,
    body,
    footer,
  }: {
    title:    string;
    onclose:  () => void;
    body?:    Snippet;
    footer?:  Snippet;
  } = $props();

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') onclose();
  }
</script>

<svelte:window onkeydown={handleKeydown} />

<!-- Backdrop -->
<div
  class="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm flex items-center justify-center p-4"
  onclick={(e) => { if (e.target === e.currentTarget) onclose(); }}
  role="dialog"
  aria-modal="true"
  aria-label={title}
>
  <!-- Panel -->
  <div class="bg-white dark:bg-slate-900 rounded-2xl shadow-2xl w-full max-w-lg border border-slate-200 dark:border-slate-800 flex flex-col max-h-[90vh]">
    <!-- Header -->
    <div class="flex items-center justify-between px-6 py-4 border-b border-slate-200 dark:border-slate-800 shrink-0">
      <h2 class="text-sm font-bold text-slate-900 dark:text-slate-100">{title}</h2>
      <button
        onclick={onclose}
        class="p-1.5 rounded-lg text-slate-400 hover:text-slate-600 hover:bg-slate-100 dark:hover:bg-slate-800 transition-colors"
        aria-label="Close"
      >
        <X size={16} strokeWidth={2} />
      </button>
    </div>

    <!-- Body -->
    {#if body}
      <div class="flex-1 overflow-y-auto px-6 py-5">
        {@render body()}
      </div>
    {/if}

    <!-- Footer -->
    {#if footer}
      <div class="px-6 py-4 border-t border-slate-200 dark:border-slate-800 flex items-center justify-end gap-3 shrink-0">
        {@render footer()}
      </div>
    {/if}
  </div>
</div>
