<script lang="ts">
  import type { Snippet } from 'svelte';
  import { IconChevronDown, IconChevronUp } from './icons';

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
    class="w-full flex items-center justify-between px-4 py-3 text-xs font-semibold text-ctp-subtext0
      hover:bg-ctp-surface0 transition-colors rounded-lg"
  >
    <span class="flex items-center gap-2">
      {title}
      {#if count !== undefined}
        <span class="inline-flex items-center px-1.5 py-0.5 rounded-full text-[10px] font-bold bg-ctp-surface1 text-ctp-subtext0">
          {count}
        </span>
      {/if}
    </span>
    <span class="opacity-50">
      {#if open}<IconChevronUp size={12} />{:else}<IconChevronDown size={12} />{/if}
    </span>
  </button>
  {#if open}
    {@render children?.()}
  {/if}
</div>
