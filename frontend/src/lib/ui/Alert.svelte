<script lang="ts">
  import type { Snippet } from 'svelte';
  import { AlertCircle, AlertTriangle, Info, CheckCircle2 } from 'lucide-svelte';

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
    critical: 'border-rose-300 bg-rose-50 dark:border-rose-900/60 dark:bg-rose-950/30 text-rose-800 dark:text-rose-300',
    warning:  'border-amber-300 bg-amber-50 dark:border-amber-900/60 dark:bg-amber-950/30 text-amber-800 dark:text-amber-300',
    info:     'border-sky-200 bg-sky-50 dark:border-sky-900/60 dark:bg-sky-950/30 text-sky-800 dark:text-sky-300',
    success:  'border-emerald-200 bg-emerald-50 dark:border-emerald-900/60 dark:bg-emerald-950/30 text-emerald-800 dark:text-emerald-300',
  };
</script>

<div class="rounded-xl border p-4 {wrapClass[level]}">
  <div class="flex items-start justify-between gap-3">
    <div class="flex-1 min-w-0">
      <p class="text-xs font-black uppercase tracking-wide flex items-center gap-1.5">
        {#if level === 'critical'}
          <AlertCircle size={14} strokeWidth={2.5} />
        {:else if level === 'warning'}
          <AlertTriangle size={14} strokeWidth={2.5} />
        {:else if level === 'info'}
          <Info size={14} strokeWidth={2.5} />
        {:else}
          <CheckCircle2 size={14} strokeWidth={2.5} />
        {/if}
        {title}
      </p>
      {#if message}
        <p class="text-xs mt-1 opacity-80">{message}</p>
      {/if}
      {#if children}
        <div class="mt-2 text-xs">
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
