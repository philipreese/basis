<script lang="ts">
  import type { Snippet } from 'svelte';

  let {
    text,
    children,
  }: {
    text:      string;
    children?: Snippet;
  } = $props();

  let visible = $state(false);
</script>

<span
  class="relative inline-flex items-center"
  onmouseenter={() => (visible = true)}
  onmouseleave={() => (visible = false)}
  onfocus={() => (visible = true)}
  onblur={() => (visible = false)}
>
  {@render children?.()}
  {#if visible}
    <span
      class="absolute z-50 bottom-full left-1/2 -translate-x-1/2 mb-2 px-2.5 py-1.5 text-[11px] font-medium
        bg-slate-900 text-white dark:bg-slate-100 dark:text-slate-900
        rounded-lg shadow-lg whitespace-nowrap pointer-events-none"
      role="tooltip"
    >
      {text}
      <span class="absolute top-full left-1/2 -translate-x-1/2 border-4 border-transparent border-t-slate-900 dark:border-t-slate-100"></span>
    </span>
  {/if}
</span>
