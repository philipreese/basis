<script lang="ts">
  import type { Snippet } from 'svelte';

  let {
    text,
    position = 'top',
    children,
  }: {
    text:       string;
    position?:  'top' | 'bottom';
    children?:  Snippet;
  } = $props();

  let visible = $state(false);
</script>

<span
  role="group"
  class="relative inline-flex items-center"
  onmouseenter={() => (visible = true)}
  onmouseleave={() => (visible = false)}
  onfocus={() => (visible = true)}
  onblur={() => (visible = false)}
>
  {@render children?.()}
  {#if visible}
    <span
      class="absolute z-50 left-1/2 -translate-x-1/2 px-2.5 py-1.5 text-xs font-medium
        bg-ctp-crust text-ctp-text rounded-lg shadow-lg whitespace-nowrap pointer-events-none border border-ctp-surface0
        {position === 'bottom' ? 'top-full mt-2' : 'bottom-full mb-2'}"
      role="tooltip"
    >
      {text}
      {#if position === 'bottom'}
        <span class="absolute bottom-full left-1/2 -translate-x-1/2 border-4 border-transparent border-b-ctp-crust"></span>
      {:else}
        <span class="absolute top-full left-1/2 -translate-x-1/2 border-4 border-transparent border-t-ctp-crust"></span>
      {/if}
    </span>
  {/if}
</span>
