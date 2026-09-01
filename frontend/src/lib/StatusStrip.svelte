<script lang="ts">
  import { onMount } from 'svelte';
  import {
    getTradingControl, getExecutorStatus, updateTradingControl,
    type TradingControlView, type ExecutorStatus,
  } from './api';
  import { toast } from './ui/snackbar.svelte.ts';
  import { startPolling } from './poll';
  import { formatLocalDateTime } from './formatters';

  let control  = $state<TradingControlView | null>(null);
  let executor = $state<ExecutorStatus | null>(null);

  // The one write this otherwise read-only strip retains: INITIATING a GLOBAL
  // halt (owner ruling on #914 — the verdict block owns every RESUME, but
  // moving toward safety must stay one tap from anywhere). Same inline-form
  // idiom as AttentionItem: local state, submit disabled on an empty trimmed
  // reason, never a shared form.
  let haltFormOpen   = $state(false);
  let haltReason     = $state('');
  let haltSubmitting = $state(false);

  async function submitGlobalHalt() {
    if (haltReason.trim().length === 0 || haltSubmitting) return;
    haltSubmitting = true;
    try {
      control = await updateTradingControl('GLOBAL', 'HALT_ENTRIES', haltReason.trim());
      haltFormOpen = false;
      haltReason = '';
      toast('GLOBAL entries halted', 'success');
    } catch (e: unknown) {
      toast('Halt failed: ' + (e instanceof Error ? e.message : String(e)), 'error');
    } finally {
      haltSubmitting = false;
    }
  }

  const globalControl = $derived(control?.controls.find(c => c.scope === 'GLOBAL') ?? null);
  const haltedBooks   = $derived(control?.controls.filter(c => c.scope !== 'GLOBAL' && c.state !== 'ACTIVE') ?? []);
  // #931: an ACTIVE-but-acknowledged scope is not a halt (filtered out of
  // haltedBooks above) but must still say so — "the acknowledgment must not
  // outlive its evidence" only holds operationally if the operator can see
  // it's still there.
  const ackedBooks    = $derived(
    control?.controls.filter(c => c.scope !== 'GLOBAL' && c.state === 'ACTIVE' && c.ack_rule) ?? []
  );
  const anyHalt       = $derived(
    (control?.sentinel_halt ?? false) || (globalControl?.state ?? 'HALT_ENTRIES') !== 'ACTIVE' || haltedBooks.length > 0
  );

  // Two independent fetches, not Promise.all (#475): a trading-control
  // outage must not blank the executor status (and vice versa) — otherwise
  // a control-endpoint failure would ALSO force the mode badge into
  // "unknown" even though the executor fetch itself succeeded.
  onMount(() => {
    loadControl();
    loadExecutor();
    // Heartbeat age, control state, and the recon badge are all computed
    // server-side at request time (#477) — poll so a console left open
    // doesn't show a page-load snapshot forever.
    startPolling(() => {
      loadControl({ silent: true });
      loadExecutor({ silent: true });
    });
  });

  async function loadControl(opts: { silent?: boolean } = {}) {
    try {
      control = await getTradingControl();
    } catch (e: unknown) {
      // Background polls fail silently — a transient blip shouldn't spam
      // toasts every interval; the strip keeps showing the last-known state.
      if (!opts.silent) toast('Failed to load control state: ' + (e instanceof Error ? e.message : String(e)), 'error');
    }
  }

  async function loadExecutor(opts: { silent?: boolean } = {}) {
    try {
      executor = await getExecutorStatus();
    } catch (e: unknown) {
      // executor stays null (first load) or holds its last value (poll): the
      // badge and strip render an explicit unknown/error state (#475)
      // rather than fabricating PAPER.
      if (!opts.silent) toast('Failed to load executor status: ' + (e instanceof Error ? e.message : String(e)), 'error');
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

    <!-- The backend's real IBKR_TRADING_MODE (#361), not a hardcoded label.
         Fabricating PAPER while loading or on fetch failure hides a live
         backend behind a "safe" badge — render an explicit unknown/error
         state instead (#475) until a fetch actually succeeds. -->
    <span class="px-1.5 py-0.5 rounded font-black tracking-wider
                 {executor === null ? 'bg-ctp-red/20 text-ctp-red animate-pulse' : (executor.trading_mode ?? 'paper') === 'live' ? 'bg-ctp-red/20 text-ctp-red' : 'bg-ctp-yellow/20 text-ctp-yellow'}"
          data-testid="trading-mode-badge">
      {executor === null ? 'MODE UNKNOWN' : (executor.trading_mode ?? 'paper').toUpperCase()}
    </span>

    {#if control}
      {#if control.sentinel_halt}
        <span class="font-bold text-ctp-red" data-testid="sentinel-halt">⛔ SENTINEL HALT file</span>
      {/if}

      {#if globalControl}
        <span class="flex items-center gap-1.5" data-testid="global-state"
              title={globalControl.state === 'ACTIVE' ? '' : `by ${globalControl.actor} · ${formatLocalDateTime(globalControl.changed_at)}`}>
          <span class="w-2 h-2 rounded-full {globalControl.state === 'ACTIVE' ? 'bg-ctp-green' : 'bg-ctp-red animate-pulse'}"></span>
          <span class="font-bold {globalControl.state === 'ACTIVE' ? 'text-ctp-green' : 'text-ctp-red'}">
            GLOBAL {globalControl.state}
          </span>
          {#if globalControl.state !== 'ACTIVE'}
            <span class="text-ctp-subtext0 font-normal" data-testid="global-reason">
              — {globalControl.reason} ({globalControl.actor}, {formatLocalDateTime(globalControl.changed_at)})
            </span>
          {:else if globalControl.ack_rule}
            <span class="text-ctp-yellow font-normal" data-testid="global-ack">
              — acknowledged since {globalControl.ack_since}: {globalControl.ack_rule}
              ({(globalControl.ack_identity ?? []).join(', ')})
            </span>
          {/if}
          {#if globalControl.state === 'ACTIVE' && !haltFormOpen}
            <button
              onclick={() => { haltFormOpen = true; }}
              class="text-xs font-bold text-ctp-red hover:underline"
              data-testid="global-halt-action">
              Halt
            </button>
          {/if}
        </span>
        {#if haltFormOpen && globalControl.state === 'ACTIVE'}
          <span class="flex items-center gap-1.5" data-testid="global-halt-form">
            <input
              bind:value={haltReason}
              placeholder="Reason for GLOBAL halt"
              class="bg-ctp-surface0 text-ctp-text text-xs rounded px-2 py-1 w-48"
              data-testid="global-halt-reason" />
            <button
              onclick={submitGlobalHalt}
              disabled={haltReason.trim().length === 0 || haltSubmitting}
              class="text-xs font-bold text-ctp-red disabled:opacity-40"
              data-testid="global-halt-confirm">
              Halt entries
            </button>
            <button
              onclick={() => { haltFormOpen = false; haltReason = ''; }}
              class="text-xs text-ctp-overlay0 hover:underline">
              Cancel
            </button>
          </span>
        {/if}
      {/if}

      {#each haltedBooks as book (book.scope)}
        <span class="text-ctp-red font-semibold" data-testid="halted-book-{book.scope}"
              title={`${book.scope} · by ${book.actor} · ${formatLocalDateTime(book.changed_at)}`}>
          ⛔ {book.label ?? book.scope} — {book.reason}
        </span>
      {/each}

      {#each ackedBooks as book (book.scope)}
        <span class="text-ctp-yellow font-semibold" data-testid="acked-book-{book.scope}"
              title={`${book.scope} · ${(book.ack_identity ?? []).join(', ')}`}>
          ✓ {book.label ?? book.scope} — acknowledged since {book.ack_since}: {book.ack_rule}
        </span>
      {/each}
    {/if}

    {#if executor}
      <span class="ml-auto flex items-center gap-4">
        <!-- broker_ok=false means the run happened but the Gateway was
             unusable — the run-age must not read GREEN for that (#478). -->
        <span data-testid="executor-age"
              class={executor.stale || executor.broker_ok === false ? 'text-ctp-red font-bold' : 'text-ctp-green'}
              title={executor.broker_ok === false ? 'Gateway unusable on the last run' : ''}>
          run {ageLabel(executor.heartbeat_age_hours)}
          {#if executor.entries_placed !== null || executor.closes_placed !== null}
            · {executor.entries_placed ?? 0} entries · {executor.closes_placed ?? 0} closes
          {/if}
          {#if executor.broker_ok === false}
            <span data-testid="broker-status">· broker UNAVAILABLE</span>
          {/if}
        </span>
        {#if executor.last_reconciliation_result}
          <span data-testid="recon-status"
                class={executor.last_reconciliation_result === 'CLEAN'
                  ? 'text-ctp-subtext0'
                  : executor.last_reconciliation_resolved
                    ? 'text-ctp-yellow font-bold'
                    : 'text-ctp-red font-bold'}
                title={executor.last_reconciliation_result !== 'CLEAN' && executor.last_reconciliation_resolved
                  ? 'A human recorded a resolution — entries stay halted until explicit RESUME (ADR-0008)'
                  : ''}>
            recon {executor.last_reconciliation_result}{executor.last_reconciliation_result !== 'CLEAN' && executor.last_reconciliation_resolved ? ' (resolved)' : ''}
          </span>
        {:else}
          <span class="text-ctp-overlay0">recon —</span>
        {/if}
        {#if executor.last_digest_pushed === false}
          <!-- The last composed digest never reached the phone (#277) -->
          <span data-testid="digest-status" class="text-ctp-red font-bold">digest UNDELIVERED</span>
        {/if}
        {#if executor.last_urgent_pushed === false}
          <!-- An urgent push existed but delivery failed (#478) — as
               invisible an outage as digest UNDELIVERED unless surfaced. -->
          <span data-testid="urgent-push-status" class="text-ctp-red font-bold">urgent push UNDELIVERED</span>
        {/if}
      </span>
    {/if}
  </div>
</div>
