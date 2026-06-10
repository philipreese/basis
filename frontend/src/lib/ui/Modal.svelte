<script lang="ts">
  import type { Snippet } from 'svelte';
  import { IconClose } from './icons';

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

<div
  class="fixed inset-0 z-50 bg-ctp-crust/80 backdrop-blur-sm flex items-center justify-center p-4"
  onclick={(e) => { if (e.target === e.currentTarget) onclose(); }}
  role="dialog"
  aria-modal="true"
  aria-label={title}
>
  <div class="bg-ctp-mantle rounded-xl shadow-2xl w-full max-w-lg border border-ctp-surface0 flex flex-col max-h-[90vh]">
    <div class="flex items-center justify-between px-6 py-4 border-b border-ctp-surface0 shrink-0">
      <h2 class="text-sm font-bold text-ctp-text">{title}</h2>
      <button
        onclick={onclose}
        class="p-1.5 rounded-lg text-ctp-overlay0 hover:text-ctp-text hover:bg-ctp-surface0 transition-colors"
        aria-label="Close"
      >
        <IconClose size={16} strokeWidth={2} />
      </button>
    </div>

    {#if body}
      <div class="flex-1 overflow-y-auto px-6 py-5">
        {@render body()}
      </div>
    {/if}

    {#if footer}
      <div class="px-6 py-4 border-t border-ctp-surface0 flex items-center justify-end gap-3 shrink-0">
        {@render footer()}
      </div>
    {/if}
  </div>
</div>
