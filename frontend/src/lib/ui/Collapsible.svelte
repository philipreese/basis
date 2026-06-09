<script lang="ts">
  import type { Snippet } from 'svelte';
  import { ChevronDown, ChevronUp } from 'lucide-svelte';

  let {
    title,
    count,
    defaultOpen = false,
    children,
  }: {
    title:        string;
    count?:       number;
    defaultOpen?: boolean;
    children?:    Snippet;
  } = $props();

  let open = $state(defaultOpen);
</script>

<div>
  <button
    onclick={() => (open = !open)}
    class="w-full flex items-center justify-between px-4 py-3 text-xs font-semibold text-slate-500 dark:text-slate-400
      hover:bg-slate-50 dark:hover:bg-slate-900/50 transition-colors rounded-lg"
  >
    <span class="flex items-center gap-2">
      {title}
      {#if count !== undefined}
        <span class="inline-flex items-center px-1.5 py-0.5 rounded-full text-[10px] font-bold bg-slate-200 text-slate-600 dark:bg-slate-700 dark:text-slate-300">
          {count}
        </span>
      {/if}
    </span>
    <span class="opacity-50">
      {#if open}<ChevronUp size={12} />{:else}<ChevronDown size={12} />{/if}
    </span>
  </button>
  {#if open}
    {@render children?.()}
  {/if}
</div>
