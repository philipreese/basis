<script lang="ts">
  import { onMount } from 'svelte';
  import {
    getTradingControl, updateTradingControl, getExecutorStatus,
    type TradingControlView, type ExecutorStatus, type ControlState,
  } from './api';
  import { toast } from './ui/snackbar.svelte.ts';

  let control       = $state<TradingControlView | null>(null);
  let executor      = $state<ExecutorStatus | null>(null);
  let actionScope   = $state<string | null>(null);     // scope with the reason form open
  let actionState   = $state<ControlState>('HALT_ENTRIES');
  let reason        = $state('');
  let isSubmitting  = $state(false);

  const globalControl = $derived(control?.controls.find(c => c.scope === 'GLOBAL') ?? null);
  const haltedBooks   = $derived(control?.controls.filter(c => c.scope !== 'GLOBAL' && c.state !== 'ACTIVE') ?? []);
  const anyHalt       = $derived(
    (control?.sentinel_halt ?? false) || (globalControl?.state ?? 'HALT_ENTRIES') !== 'ACTIVE' || haltedBooks.length > 0
  );

  onMount(load);

  async function load() {
    try {
      [control, executor] = await Promise.all([getTradingControl(), getExecutorStatus()]);
    } catch (e: unknown) {
      toast('Failed to load control state: ' + (e instanceof Error ? e.message : String(e)), 'error');
    }
  }

  function openAction(scope: string, state: ControlState) {
    actionScope = scope;
    actionState = state;
    reason = '';
  }

  async function submitAction(e: Event) {
    e.preventDefault();
    if (!actionScope || reason.trim().length < 3) return;
    try {
      isSubmitting = true;
      control = await updateTradingControl(actionScope, actionState, reason.trim());
      toast(`${actionScope} → ${actionState}`, 'success', 4000);
      actionScope = null;
    } catch (e: unknown) {
      toast(e instanceof Error ? e.message : String(e), 'error');
    } finally {
      isSubmitting = false;
    }
  }

  function ageLabel(hours: number | null): string {
    if (hours === null) return 'never';
    if (hours < 1) return `${Math.round(hours * 60)}m ago`;
    if (hours < 48) return `${hours.toFixed(1)}h ago`;
    return `${Math.round(hours / 24)}d ago`;
  }
</script>

<div class="border-b border-ctp-surface0 {anyHalt ? 'bg-ctp-red/10' : 'bg-ctp-mantle'} px-6 py-1.5 text-xs carbon-mono"
     data-testid="status-strip">
  <div class="max-w-7xl mx-auto flex flex-wrap items-center gap-x-4 gap-y-1">

    <span class="px-1.5 py-0.5 rounded font-black tracking-wider bg-ctp-yellow/20 text-ctp-yellow">PAPER</span>

    {#if control}
      {#if control.sentinel_halt}
        <span class="font-bold text-ctp-red" data-testid="sentinel-halt">⛔ SENTINEL HALT file</span>
      {/if}

      {#if globalControl}
        <span class="flex items-center gap-1.5" data-testid="global-state">
          <span class="w-2 h-2 rounded-full {globalControl.state === 'ACTIVE' ? 'bg-ctp-green' : 'bg-ctp-red animate-pulse'}"></span>
          <span class="font-bold {globalControl.state === 'ACTIVE' ? 'text-ctp-green' : 'text-ctp-red'}">
            GLOBAL {globalControl.state}
          </span>
        </span>
        {#if globalControl.state === 'ACTIVE'}
          <button class="text-ctp-red font-bold hover:underline" data-testid="halt-global"
                  onclick={() => openAction('GLOBAL', 'HALT_ENTRIES')}>HALT</button>
        {:else if !control.sentinel_halt}
          <button class="text-ctp-green font-bold hover:underline" data-testid="resume-global"
                  onclick={() => openAction('GLOBAL', 'ACTIVE')}>RESUME</button>
        {/if}
      {/if}

      {#each haltedBooks as book (book.scope)}
        <span class="text-ctp-red font-semibold">⛔ {book.scope}</span>
      {/each}
    {/if}

    {#if executor}
      <span class="ml-auto flex items-center gap-4">
        <span data-testid="executor-age"
              class={executor.stale ? 'text-ctp-red font-bold' : 'text-ctp-green'}>
          run {ageLabel(executor.heartbeat_age_hours)}
        </span>
        {#if executor.last_reconciliation_result}
          <span class={executor.last_reconciliation_result === 'CLEAN' ? 'text-ctp-subtext0' : 'text-ctp-red font-bold'}>
            recon {executor.last_reconciliation_result}
          </span>
        {:else}
          <span class="text-ctp-overlay0">recon —</span>
        {/if}
      </span>
    {/if}
  </div>

  {#if actionScope}
    <form onsubmit={submitAction} class="max-w-7xl mx-auto flex items-center gap-2 pt-1.5 pb-0.5">
      <span class="font-bold {actionState === 'ACTIVE' ? 'text-ctp-green' : 'text-ctp-red'}">
        {actionState === 'ACTIVE' ? 'RESUME' : 'HALT'} {actionScope} —
      </span>
      <!-- svelte-ignore a11y_autofocus -->
      <input type="text" bind:value={reason} autofocus data-testid="control-reason"
             placeholder="reason (required, min 3 chars)"
             class="flex-1 max-w-md px-2 py-1 border border-ctp-surface1 rounded bg-ctp-crust text-ctp-text focus:outline-none focus:ring-1 focus:ring-ctp-mauve" />
      <button type="submit" disabled={reason.trim().length < 3 || isSubmitting} data-testid="control-confirm"
              class="px-2 py-1 rounded font-bold {actionState === 'ACTIVE' ? 'bg-ctp-green/20 text-ctp-green' : 'bg-ctp-red/20 text-ctp-red'} disabled:opacity-40">
        Confirm
      </button>
      <button type="button" class="text-ctp-overlay0 hover:text-ctp-text" onclick={() => (actionScope = null)}>
        Cancel
      </button>
    </form>
  {/if}
</div>
