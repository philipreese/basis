<script lang="ts">
  import type { Snippet } from 'svelte';

  type Variant = 'primary' | 'danger' | 'secondary' | 'ghost';
  type Size    = 'sm' | 'md' | 'lg';

  let {
    variant  = 'primary',
    size     = 'md',
    disabled = false,
    loading  = false,
    type     = 'button',
    onclick,
    children,
  }: {
    variant?:  Variant;
    size?:     Size;
    disabled?: boolean;
    loading?:  boolean;
    type?:     'button' | 'submit' | 'reset';
    onclick?:  () => void;
    children?: Snippet;
  } = $props();

  const variantClasses: Record<Variant, string> = {
    primary:   'bg-ctp-mauve text-ctp-crust hover:bg-ctp-mauve/90',
    danger:    'bg-ctp-red text-ctp-crust hover:bg-ctp-red/90',
    secondary: 'bg-ctp-surface0 text-ctp-text hover:bg-ctp-surface1 border border-ctp-surface1',
    ghost:     'bg-transparent text-ctp-subtext0 hover:bg-ctp-surface0 hover:text-ctp-text',
  };

  const sizeClasses: Record<Size, string> = {
    sm: 'px-3 py-1.5 text-xs',
    md: 'px-4 py-2 text-sm',
    lg: 'px-5 py-2.5 text-sm',
  };
</script>

<button
  {type}
  {disabled}
  {onclick}
  class="inline-flex items-center justify-center gap-1.5 font-semibold rounded-lg transition-colors
    {variantClasses[variant]} {sizeClasses[size]}
    disabled:opacity-40 disabled:cursor-not-allowed"
>
  {#if loading}
    <svg class="animate-spin h-3.5 w-3.5 shrink-0" fill="none" viewBox="0 0 24 24">
      <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/>
      <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8z"/>
    </svg>
  {/if}
  {@render children?.()}
</button>
