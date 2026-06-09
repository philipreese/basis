<script lang="ts">
  import type { Snippet } from 'svelte';

  let {
    label,
    required = false,
    hint,
    children,
  }: {
    label:     string;
    required?: boolean;
    hint?:     string;
    children?: Snippet;
  } = $props();
</script>

<label class="block">
  <span class="block text-xs font-semibold text-slate-500 dark:text-slate-400 mb-1">
    {label}
    {#if required}<span class="text-rose-500 ml-0.5">*</span>{/if}
  </span>
  {@render children?.()}
  {#if hint}
    <span class="block text-[11px] text-slate-400 dark:text-slate-500 mt-1">{hint}</span>
  {/if}
</label>
