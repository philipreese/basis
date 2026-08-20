<script lang="ts">
  import { onMount } from 'svelte';
  import {
    getLatestReconciliation, resolveReconciliation, recordExternalClose, adjustBookCash, resolvePartialOrder,
    type ReconciliationRun,
  } from './api';
  import { toast } from './ui/snackbar.svelte.ts';

  // Parent refreshes books/positions after a correction lands.
  let { onCorrectionApplied = () => {} }: { onCorrectionApplied?: () => void } = $props();

  let run = $state<ReconciliationRun | null>(null);
  let loaded = $state(false);

  // Correction forms — one open at a time.
  let activeForm = $state<'close' | 'cash' | 'partial' | 'resolve' | null>(null);
  let busy = $state(false);

  let closePositionId = $state('');
  let closeExitValue = $state<number | null>(null);
  let closeReason = $state('');
  let closeAckCancelled = $state(false);

  let cashBookId = $state('');
  let cashDelta = $state<number | null>(null);
  let cashReason = $state('');

  let partialRef = $state('');
  let partialReason = $state('');

  let resolutionText = $state('');

  onMount(load);

  async function load() {
    try {
      run = await getLatestReconciliation();
    } catch (e: unknown) {
      toast('Failed to load reconciliation: ' + (e instanceof Error ? e.message : String(e)), 'error');
    } finally {
      loaded = true;
    }
  }

  function openForm(form: 'close' | 'cash' | 'partial' | 'resolve') {
    activeForm = activeForm === form ? null : form;
  }

  async function submitExternalClose(e: SubmitEvent) {
    e.preventDefault();
    if (closeExitValue === null) return;
    busy = true;
    try {
      const pm = await recordExternalClose(closePositionId.trim(), closeExitValue, closeReason.trim(), closeAckCancelled);
      toast(`External close recorded: ${pm.outcome} · P&L ${pm.realized_pnl >= 0 ? '+' : ''}$${pm.realized_pnl.toFixed(2)}`, 'success', 5000);
      activeForm = null;
      closePositionId = ''; closeExitValue = null; closeReason = ''; closeAckCancelled = false;
      onCorrectionApplied();
    } catch (err: unknown) {
      toast('External close failed: ' + (err instanceof Error ? err.message : String(err)), 'error');
    } finally {
      busy = false;
    }
  }

  async function submitCashAdjustment(e: SubmitEvent) {
    e.preventDefault();
    if (cashDelta === null) return;
    busy = true;
    try {
      const result = await adjustBookCash(cashBookId.trim().toUpperCase(), cashDelta, cashReason.trim());
      toast(`${result.book_id} cash adjusted → $${result.cash_balance.toFixed(2)}`, 'success', 5000);
      activeForm = null;
      cashBookId = ''; cashDelta = null; cashReason = '';
      onCorrectionApplied();
    } catch (err: unknown) {
      toast('Cash adjustment failed: ' + (err instanceof Error ? err.message : String(err)), 'error');
    } finally {
      busy = false;
    }
  }

  async function submitPartialResolve(e: SubmitEvent) {
    e.preventDefault();
    busy = true;
    try {
      await resolvePartialOrder(partialRef.trim(), partialReason.trim());
      toast(`PARTIAL ${partialRef.trim()} terminalized — encumbrance released`, 'success', 5000);
      activeForm = null;
      partialRef = ''; partialReason = '';
      onCorrectionApplied();
    } catch (err: unknown) {
      toast('Partial resolve failed: ' + (err instanceof Error ? err.message : String(err)), 'error');
    } finally {
      busy = false;
    }
  }

  async function submitResolve(e: SubmitEvent) {
    e.preventDefault();
    if (!run) return;
    busy = true;
    try {
      run = await resolveReconciliation(run.id, resolutionText.trim());
      toast('Drift marked resolved. Entries stay halted until you RESUME explicitly.', 'success', 6000);
      activeForm = null;
      resolutionText = '';
    } catch (err: unknown) {
      toast('Resolve failed: ' + (err instanceof Error ? err.message : String(err)), 'error');
    } finally {
      busy = false;
    }
  }

  const inputCls = 'px-2 py-1 text-xs border border-ctp-surface1 rounded bg-ctp-crust text-ctp-text focus:outline-none focus:ring-1 focus:ring-ctp-mauve carbon-mono';
  const isDrift = $derived(run?.result === 'DRIFT');
  const isUnresolvedDrift = $derived(isDrift && !run?.resolved_at);
</script>

{#if loaded && run}
  {#if isUnresolvedDrift}
    <section class="carbon-card p-5 border border-ctp-red/40" data-testid="reconciliation-drift">
      <div class="flex items-baseline justify-between mb-3">
        <h2 class="text-base font-bold text-ctp-red tracking-tight">Reconciliation DRIFT — books ≠ broker</h2>
        <span class="text-xs text-ctp-overlay0 carbon-mono">{run.run_at}</span>
      </div>
      <p class="text-xs text-ctp-subtext0 mb-3 max-w-2xl leading-relaxed">
        Entries are halted globally. Correct the books below (each correction is audited and demands a reason),
        then mark the run resolved. Resuming entries is a separate, explicit act on the status strip.
      </p>

      {#if run.drift_details?.length}
        <ul class="mb-4 space-y-1">
          {#each run.drift_details as d, i (i)}
            <li class="text-xs carbon-mono text-ctp-text bg-ctp-red/10 rounded px-2 py-1.5">
              {JSON.stringify(d)}
            </li>
          {/each}
        </ul>
      {/if}

      <div class="flex flex-wrap gap-2 mb-3">
        <button onclick={() => openForm('close')} data-testid="recon-open-external-close"
                class="px-3 py-1.5 text-xs font-bold rounded transition {activeForm === 'close' ? 'bg-ctp-mauve text-ctp-crust' : 'bg-ctp-surface0 text-ctp-text hover:bg-ctp-surface1'}">
          Record external close
        </button>
        <button onclick={() => openForm('cash')} data-testid="recon-open-cash"
                class="px-3 py-1.5 text-xs font-bold rounded transition {activeForm === 'cash' ? 'bg-ctp-mauve text-ctp-crust' : 'bg-ctp-surface0 text-ctp-text hover:bg-ctp-surface1'}">
          Adjust book cash
        </button>
        <button onclick={() => openForm('partial')} data-testid="recon-open-partial"
                class="px-3 py-1.5 text-xs font-bold rounded transition {activeForm === 'partial' ? 'bg-ctp-mauve text-ctp-crust' : 'bg-ctp-surface0 text-ctp-text hover:bg-ctp-surface1'}">
          Resolve partial order
        </button>
        <button onclick={() => openForm('resolve')} data-testid="recon-open-resolve"
                class="px-3 py-1.5 text-xs font-bold rounded transition {activeForm === 'resolve' ? 'bg-ctp-green text-ctp-crust' : 'bg-ctp-green/15 text-ctp-green hover:bg-ctp-green/25'}">
          Mark resolved
        </button>
      </div>

      {#if activeForm === 'close'}
        <form onsubmit={submitExternalClose} class="flex flex-wrap items-end gap-2 p-3 bg-ctp-crust rounded-lg border border-ctp-surface0">
          <label class="flex flex-col gap-1 text-xs font-semibold text-ctp-subtext0">
            Position ID
            <input type="text" bind:value={closePositionId} placeholder="pos_…" class="{inputCls} w-44" data-testid="recon-close-position" />
          </label>
          <label class="flex flex-col gap-1 text-xs font-semibold text-ctp-subtext0">
            Exit value / share
            <input type="number" step="0.01" min="0" bind:value={closeExitValue} placeholder="0.40" class="{inputCls} w-28" data-testid="recon-close-value" />
          </label>
          <label class="flex flex-col gap-1 text-xs font-semibold text-ctp-subtext0 grow">
            Reason
            <input type="text" bind:value={closeReason} placeholder="e.g. closed by hand at IBKR on 8/20" class="{inputCls} w-full" data-testid="recon-close-reason" />
          </label>
          <label class="flex items-center gap-1.5 text-xs font-semibold text-ctp-subtext0 pb-1.5">
            <input type="checkbox" bind:checked={closeAckCancelled} data-testid="recon-close-ack" class="accent-ctp-mauve" />
            <!-- #407: without this, pending DB order rows refuse the close forever -->
            Pending orders on this position are cancelled at the broker
          </label>
          <button type="submit" disabled={busy || !closePositionId.trim() || closeExitValue === null || closeReason.trim().length < 3}
                  data-testid="recon-close-submit"
                  class="px-3 py-1.5 text-xs font-bold rounded bg-ctp-mauve text-ctp-crust disabled:opacity-40">
            Apply
          </button>
        </form>
      {:else if activeForm === 'cash'}
        <form onsubmit={submitCashAdjustment} class="flex flex-wrap items-end gap-2 p-3 bg-ctp-crust rounded-lg border border-ctp-surface0">
          <label class="flex flex-col gap-1 text-xs font-semibold text-ctp-subtext0">
            Book
            <input type="text" bind:value={cashBookId} placeholder="B07" class="{inputCls} w-20" data-testid="recon-cash-book" />
          </label>
          <label class="flex flex-col gap-1 text-xs font-semibold text-ctp-subtext0">
            Delta ($, signed)
            <input type="number" step="0.01" bind:value={cashDelta} placeholder="-12.50" class="{inputCls} w-28" data-testid="recon-cash-delta" />
          </label>
          <label class="flex flex-col gap-1 text-xs font-semibold text-ctp-subtext0 grow">
            Reason
            <input type="text" bind:value={cashReason} placeholder="e.g. assignment fee missing from fills" class="{inputCls} w-full" data-testid="recon-cash-reason" />
          </label>
          <button type="submit" disabled={busy || !cashBookId.trim() || cashDelta === null || cashDelta === 0 || cashReason.trim().length < 3}
                  data-testid="recon-cash-submit"
                  class="px-3 py-1.5 text-xs font-bold rounded bg-ctp-mauve text-ctp-crust disabled:opacity-40">
            Apply
          </button>
        </form>
      {:else if activeForm === 'partial'}
        <form onsubmit={submitPartialResolve} class="flex flex-wrap items-end gap-2 p-3 bg-ctp-crust rounded-lg border border-ctp-surface0">
          <p class="w-full text-xs text-ctp-yellow leading-snug">
            Releases the PARTIAL latch's encumbrance and slot. Record the partial's cash/position
            consequences FIRST (external close / cash adjust) — this only clears the latch.
          </p>
          <label class="flex flex-col gap-1 text-xs font-semibold text-ctp-subtext0 grow">
            Order ref
            <input type="text" bind:value={partialRef} placeholder="basis:B07:o_ab12cd34:close" class="{inputCls} w-full" data-testid="recon-partial-ref" />
          </label>
          <label class="flex flex-col gap-1 text-xs font-semibold text-ctp-subtext0 grow">
            Reason
            <input type="text" bind:value={partialReason} placeholder="e.g. remainder cancelled at IBKR; cash adjusted" class="{inputCls} w-full" data-testid="recon-partial-reason" />
          </label>
          <button type="submit" disabled={busy || !partialRef.trim() || partialReason.trim().length < 3}
                  data-testid="recon-partial-submit"
                  class="px-3 py-1.5 text-xs font-bold rounded bg-ctp-mauve text-ctp-crust disabled:opacity-40">
            Release
          </button>
        </form>
      {:else if activeForm === 'resolve'}
        <form onsubmit={submitResolve} class="flex flex-wrap items-end gap-2 p-3 bg-ctp-crust rounded-lg border border-ctp-surface0">
          <label class="flex flex-col gap-1 text-xs font-semibold text-ctp-subtext0 grow">
            What explained the drift?
            <input type="text" bind:value={resolutionText} placeholder="e.g. external close recorded for p1; cash matched" class="{inputCls} w-full" data-testid="recon-resolve-text" />
          </label>
          <button type="submit" disabled={busy || resolutionText.trim().length < 3}
                  data-testid="recon-resolve-submit"
                  class="px-3 py-1.5 text-xs font-bold rounded bg-ctp-green text-ctp-crust disabled:opacity-40">
            Mark resolved
          </button>
        </form>
      {/if}
    </section>
  {:else}
    <p class="text-xs text-ctp-overlay0 carbon-mono" data-testid="reconciliation-summary">
      Reconciliation: <span class={isDrift ? 'text-ctp-yellow font-bold' : 'text-ctp-green font-bold'}>{run.result}</span>
      · {run.run_at}
      {#if isDrift && run.resolved_at}· resolved: {run.resolution}{/if}
    </p>
  {/if}
{/if}
