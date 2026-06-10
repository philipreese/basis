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
  <span class="block text-xs font-semibold text-ctp-subtext0 mb-1">
    {label}
    {#if required}<span class="text-ctp-red ml-0.5">*</span>{/if}
  </span>
  {@render children?.()}
  {#if hint}
    <span class="block text-xs text-ctp-overlay0 mt-1">{hint}</span>
  {/if}
</label>
