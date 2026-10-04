<script lang="ts">
  import { onMount } from 'svelte';
  import {
    getLatestReconciliation, resolveReconciliation, recordExternalClose, adjustBookCash, resolvePartialOrder,
    correctShareHolding, settleShareOrder,
    type ReconciliationRun, type ShareDriftCause,
  } from './api';
  import { toast } from './ui/snackbar.svelte.ts';
  import { startPolling } from './poll';
  import { formatLocalDateTime } from './formatters';

  // Parent refreshes books/positions after a correction lands.
  let { onCorrectionApplied = () => {} }: { onCorrectionApplied?: () => void } = $props();

  let run = $state<ReconciliationRun | null>(null);
  let loaded = $state(false);

  // Correction forms — one open at a time.
  type FormKind = 'close' | 'cash' | 'partial' | 'share' | 'shareOrder' | 'resolve';
  let activeForm = $state<FormKind | null>(null);
  let busy = $state(false);

  // #1074: share drift. A holding correction (hand sale, reinvested dividend,
  // corporate action) and a held-order settlement (a missed fill night).
  const SHARE_CAUSES: { value: ShareDriftCause; label: string }[] = [
    { value: 'HAND_TRADE', label: 'Traded by hand at the broker' },
    { value: 'DIVIDEND_REINVESTED', label: 'Dividend reinvested' },
    { value: 'CORPORATE_ACTION', label: 'Corporate action (split, merger)' },
    { value: 'MISSED_FILL', label: 'Fill the books missed (no order left to settle)' },
    { value: 'OTHER', label: 'Other — say what in the reason' },
  ];
  let shareBookId = $state('B36');
  let shareSymbol = $state('');
  let shareCurrent = $state<number | null>(null);
  let shareCorrected = $state<number | null>(null);
  let shareCause = $state<ShareDriftCause>('HAND_TRADE');
  let shareReason = $state('');
  let shareClaim = $state(false);
  let shareCashDelta = $state<number | null>(null);
  const shareIsIncrease = $derived(shareCurrent !== null && shareCorrected !== null && shareCorrected > shareCurrent);

  let settleRef = $state('');
  let settleFilled = $state<number | null>(null);
  let settlePrice = $state<number | null>(null);
  let settleCommission = $state<number | null>(null);
  let settleReason = $state('');

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

  onMount(() => {
    load();
    // A run resolved yesterday, or a fresh DRIFT tonight, must not sit
    // hidden behind a page-load snapshot (#477).
    startPolling(() => {
      // Don't yank the run out from under an open correction form.
      if (activeForm === null) load({ silent: true });
    });
  });

  async function load(opts: { silent?: boolean } = {}) {
    try {
      run = await getLatestReconciliation();
    } catch (e: unknown) {
      if (!opts.silent) toast('Failed to load reconciliation: ' + (e instanceof Error ? e.message : String(e)), 'error');
    } finally {
      loaded = true;
    }
  }

  function openForm(form: FormKind) {
    activeForm = activeForm === form ? null : form;
  }

  async function submitShareHolding(e: SubmitEvent) {
    e.preventDefault();
    if (shareCurrent === null || shareCorrected === null) return;
    busy = true;
    try {
      const result = await correctShareHolding({
        book_id: shareBookId.trim().toUpperCase(),
        symbol: shareSymbol.trim().toUpperCase(),
        current_quantity: shareCurrent,
        corrected_quantity: shareCorrected,
        cause: shareCause,
        reason: shareReason.trim(),
        claim_increase: shareIsIncrease && shareClaim,
        cash_delta: shareCashDelta ?? 0,
      });
      toast(
        `${result.book_id} ${result.symbol}: ${result.quantity_before} → ${result.quantity_after} shares · cash $${result.cash_balance.toFixed(2)}`,
        'success',
        5000,
      );
      activeForm = null;
      shareSymbol = ''; shareCurrent = null; shareCorrected = null; shareReason = ''; shareClaim = false; shareCashDelta = null;
      onCorrectionApplied();
    } catch (err: unknown) {
      toast('Share holding correction failed: ' + (err instanceof Error ? err.message : String(err)), 'error');
    } finally {
      busy = false;
    }
  }

  async function submitShareSettle(e: SubmitEvent) {
    e.preventDefault();
    if (settleFilled === null) return;
    busy = true;
    try {
      const result = await settleShareOrder({
        order_ref: settleRef.trim(),
        filled_quantity: settleFilled,
        avg_fill_price: settlePrice,
        commission: settleCommission ?? 0,
        reason: settleReason.trim(),
      });
      toast(`${result.order_ref} → ${result.status}, ${result.filled_quantity} filled · holding ${result.holding_after}`, 'success', 5000);
      activeForm = null;
      settleRef = ''; settleFilled = null; settlePrice = null; settleCommission = null; settleReason = '';
      onCorrectionApplied();
    } catch (err: unknown) {
      toast('Share order settlement failed: ' + (err instanceof Error ? err.message : String(err)), 'error');
    } finally {
      busy = false;
    }
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
      const result = await resolvePartialOrder(partialRef.trim(), partialReason.trim());
      // The row's actual terminal status (#479), not an assumed CANCELLED.
      toast(`${result.order_ref} → ${result.status} — encumbrance released`, 'success', 5000);
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

  // Kind-specific one-liners instead of raw JSON.stringify (#474): the shape
  // is DriftItem { kind, key, sec_type, broker_qty, expected_qty,
  // unexpected_instrument } — spell out what actually diverged.
  function formatDrift(d: Record<string, unknown>): string {
    const kind = String(d.kind ?? 'DRIFT');
    const key = d.key ?? d.detail ?? '';
    const brokerQty = d.broker_qty;
    const expectedQty = d.expected_qty;
    const qty = typeof brokerQty === 'number' && typeof expectedQty === 'number' ? ` (broker=${brokerQty} vs expected=${expectedQty})` : '';
    const flag = d.unexpected_instrument ? ' — UNEXPECTED INSTRUMENT' : '';
    // #600: the server attaches a plain-English label for GHOST_ORDER items
    // ("B04 — SPY 745/742 bull put") — EXTERNAL_CLOSE/PARTIAL_DRIFT key on a
    // bare OCC symbol with no ref to resolve a label from, so those stay
    // unlabeled for now.
    const label = typeof d.label === 'string' ? d.label : null;
    switch (kind) {
      case 'GHOST_ORDER':
        return `GHOST_ORDER: ${key}${label ? ` (${label})` : ''} — live at the broker with no DB row${qty}${flag}`;
      case 'ORPHAN':
        return `ORPHAN: ${key} — the broker holds a position no book expects${qty}${flag}`;
      case 'EXTERNAL_CLOSE':
        return `EXTERNAL_CLOSE: ${key} — closed outside the console${qty}${flag}`;
      case 'PARTIAL_DRIFT':
        return `PARTIAL_DRIFT: ${key} — quantity mismatch${qty}${flag}`;
      case 'SHARE_DRIFT':
        // #1074: correct it with "Correct share holding" (or settle a held share order).
        return `SHARE_DRIFT: ${key} — broker shares differ from the share book's holding${qty}${flag}`;
      default:
        return `${kind}: ${key}${qty}${flag}`;
    }
  }
</script>

{#if loaded && run}
  {#if isUnresolvedDrift}
    <section class="carbon-card p-5 border border-ctp-red/40" data-testid="reconciliation-drift">
      <div class="flex items-baseline justify-between mb-3">
        <h2 class="text-base font-bold text-ctp-red tracking-tight">Reconciliation DRIFT — books ≠ broker</h2>
        <span class="text-xs text-ctp-overlay0 carbon-mono">{formatLocalDateTime(run.run_at)}</span>
      </div>
      <p class="text-xs text-ctp-subtext0 mb-3 max-w-2xl leading-relaxed">
        Entries are halted globally. Correct the books below (each correction is audited and demands a reason),
        then mark the run resolved. Resuming entries is a separate, explicit act on the status strip.
      </p>

      {#if run.drift_details?.length}
        <ul class="mb-4 space-y-1">
          {#each run.drift_details as d, i (i)}
            <li class="text-xs carbon-mono text-ctp-text bg-ctp-red/10 rounded px-2 py-1.5">
              {formatDrift(d)}
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
        <button onclick={() => openForm('share')} data-testid="recon-open-share"
                class="px-3 py-1.5 text-xs font-bold rounded transition {activeForm === 'share' ? 'bg-ctp-mauve text-ctp-crust' : 'bg-ctp-surface0 text-ctp-text hover:bg-ctp-surface1'}">
          Correct share holding
        </button>
        <button onclick={() => openForm('shareOrder')} data-testid="recon-open-share-order"
                class="px-3 py-1.5 text-xs font-bold rounded transition {activeForm === 'shareOrder' ? 'bg-ctp-mauve text-ctp-crust' : 'bg-ctp-surface0 text-ctp-text hover:bg-ctp-surface1'}">
          Settle held share order
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
      {:else if activeForm === 'share'}
        <form onsubmit={submitShareHolding} class="flex flex-wrap items-end gap-2 p-3 bg-ctp-crust rounded-lg border border-ctp-surface0">
          <p class="w-full text-xs text-ctp-yellow leading-snug">
            Sets a share book's holding to what the broker shows, for a symbol the book is designated to hold.
            Settle a held share order first if one is pending. Shares no book holds on purpose (an assignment)
            are closed at the broker, never adopted here.
          </p>
          <label class="flex flex-col gap-1 text-xs font-semibold text-ctp-subtext0">
            Book
            <input type="text" bind:value={shareBookId} placeholder="B36" class="{inputCls} w-20" data-testid="recon-share-book" />
          </label>
          <label class="flex flex-col gap-1 text-xs font-semibold text-ctp-subtext0">
            Symbol
            <input type="text" bind:value={shareSymbol} placeholder="VTI" class="{inputCls} w-20" data-testid="recon-share-symbol" />
          </label>
          <label class="flex flex-col gap-1 text-xs font-semibold text-ctp-subtext0">
            Books hold now
            <input type="number" step="any" min="0" bind:value={shareCurrent} placeholder="5" class="{inputCls} w-24" data-testid="recon-share-current" />
          </label>
          <label class="flex flex-col gap-1 text-xs font-semibold text-ctp-subtext0">
            Correct to
            <input type="number" step="any" min="0" bind:value={shareCorrected} placeholder="3" class="{inputCls} w-24" data-testid="recon-share-corrected" />
          </label>
          <label class="flex flex-col gap-1 text-xs font-semibold text-ctp-subtext0">
            What happened
            <select bind:value={shareCause} class="{inputCls}" data-testid="recon-share-cause">
              {#each SHARE_CAUSES as c (c.value)}
                <option value={c.value}>{c.label}</option>
              {/each}
            </select>
          </label>
          <label class="flex flex-col gap-1 text-xs font-semibold text-ctp-subtext0">
            Cash ($, signed, optional)
            <input type="number" step="0.01" bind:value={shareCashDelta} placeholder="598.00" class="{inputCls} w-28" data-testid="recon-share-cash" />
          </label>
          <label class="flex flex-col gap-1 text-xs font-semibold text-ctp-subtext0 grow">
            Reason
            <input type="text" bind:value={shareReason} placeholder="e.g. sold 2 VTI by hand at IBKR on 11/3" class="{inputCls} w-full" data-testid="recon-share-reason" />
          </label>
          {#if shareIsIncrease}
            <label class="flex items-center gap-1.5 text-xs font-semibold text-ctp-peach pb-1.5">
              <input type="checkbox" bind:checked={shareClaim} data-testid="recon-share-claim" class="accent-ctp-mauve" />
              These extra shares are the book's own — not an option assignment
            </label>
          {/if}
          <button type="submit"
                  disabled={busy || !shareBookId.trim() || !shareSymbol.trim() || shareCurrent === null || shareCorrected === null || shareReason.trim().length < 3 || (shareIsIncrease && !shareClaim)}
                  data-testid="recon-share-submit"
                  class="px-3 py-1.5 text-xs font-bold rounded bg-ctp-mauve text-ctp-crust disabled:opacity-40">
            Apply
          </button>
        </form>
      {:else if activeForm === 'shareOrder'}
        <form onsubmit={submitShareSettle} class="flex flex-wrap items-end gap-2 p-3 bg-ctp-crust rounded-lg border border-ctp-surface0">
          <p class="w-full text-xs text-ctp-yellow leading-snug">
            For a share order the sync is holding (filled at the broker, executions out of reach). Enter the
            order's TOTAL execution from the statement or the Flex audit; it is booked into the holding and the
            book's cash exactly as the nightly sync would.
          </p>
          <label class="flex flex-col gap-1 text-xs font-semibold text-ctp-subtext0 grow">
            Order ref
            <input type="text" bind:value={settleRef} placeholder="basis:B36:1a2b3c4d5e6f:share" class="{inputCls} w-full" data-testid="recon-settle-ref" />
          </label>
          <label class="flex flex-col gap-1 text-xs font-semibold text-ctp-subtext0">
            Shares filled
            <input type="number" step="any" min="0" bind:value={settleFilled} placeholder="4" class="{inputCls} w-24" data-testid="recon-settle-filled" />
          </label>
          <label class="flex flex-col gap-1 text-xs font-semibold text-ctp-subtext0">
            Avg price
            <input type="number" step="0.0001" min="0" bind:value={settlePrice} placeholder="331.00" class="{inputCls} w-28" data-testid="recon-settle-price" />
          </label>
          <label class="flex flex-col gap-1 text-xs font-semibold text-ctp-subtext0">
            Commission
            <input type="number" step="0.01" min="0" bind:value={settleCommission} placeholder="1.00" class="{inputCls} w-24" data-testid="recon-settle-commission" />
          </label>
          <label class="flex flex-col gap-1 text-xs font-semibold text-ctp-subtext0 grow">
            Reason
            <input type="text" bind:value={settleReason} placeholder="e.g. Flex: 4 GLD @ 331.00 on 11/2" class="{inputCls} w-full" data-testid="recon-settle-reason" />
          </label>
          <button type="submit" disabled={busy || !settleRef.trim() || settleFilled === null || settleReason.trim().length < 3}
                  data-testid="recon-settle-submit"
                  class="px-3 py-1.5 text-xs font-bold rounded bg-ctp-mauve text-ctp-crust disabled:opacity-40">
            Settle
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
      · {formatLocalDateTime(run.run_at)}
      {#if isDrift && run.resolved_at}· resolved: {run.resolution}{/if}
    </p>
  {/if}
{/if}
