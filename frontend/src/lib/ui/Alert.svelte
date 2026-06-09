<script lang="ts">
  import type { Snippet } from 'svelte';
  import { IconCritical, IconWarning, IconInfo, IconSuccess } from './icons';

  type Level = 'critical' | 'warning' | 'info' | 'success';

  let {
    level,
    title,
    message,
    action,
    children,
  }: {
    level:     Level;
    title:     string;
    message?:  string;
    action?:   { label: string; onclick: () => void };
    children?: Snippet;
  } = $props();

  const wrapClass: Record<Level, string> = {
    critical: 'border-ctp-red/40 bg-ctp-red/10 text-ctp-red',
    warning:  'border-ctp-yellow/40 bg-ctp-yellow/10 text-ctp-yellow',
    info:     'border-ctp-blue/40 bg-ctp-blue/10 text-ctp-blue',
    success:  'border-ctp-green/40 bg-ctp-green/10 text-ctp-green',
  };
</script>

<div class="rounded-lg border-l-2 border p-3.5 {wrapClass[level]}">
  <div class="flex items-start justify-between gap-3">
    <div class="flex-1 min-w-0">
      <p class="text-xs font-bold uppercase tracking-wide flex items-center gap-1.5">
        {#if level === 'critical'}
          <IconCritical size={13} strokeWidth={2.5} />
        {:else if level === 'warning'}
          <IconWarning size={13} strokeWidth={2.5} />
        {:else if level === 'info'}
          <IconInfo size={13} strokeWidth={2.5} />
        {:else}
          <IconSuccess size={13} strokeWidth={2.5} />
        {/if}
        {title}
      </p>
      {#if message}
        <p class="text-xs mt-1 opacity-75">{message}</p>
      {/if}
      {#if children}
        <div class="mt-2 text-xs opacity-80">
          {@render children()}
        </div>
      {/if}
    </div>
    {#if action}
      <button
        onclick={action.onclick}
        class="shrink-0 px-3 py-1.5 text-xs font-bold rounded-lg bg-current/10 hover:bg-current/20 transition-colors"
      >
        {action.label}
      </button>
    {/if}
  </div>
</div>
