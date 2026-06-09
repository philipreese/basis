<script lang="ts">
  import type { TradeSpecResult, OperationalJournalEntry, Position } from './api';
  import { createPosition, createOpportunityRecord } from './api';
  import { formatDollar, formatDate, formatDte, formatPct } from './formatters';
  import Alert from './ui/Alert.svelte';
  import Badge from './ui/Badge.svelte';
  import Button from './ui/Button.svelte';
  import FormField from './ui/FormField.svelte';
  import { X, ArrowLeft, Check } from 'lucide-svelte';

  let {
    result,
    playbookName,
    onDismiss,
    onPositionSaved,
  }: {
    result: TradeSpecResult;
    playbookName: string;
    onDismiss: () => void;
    onPositionSaved?: (pos: Position) => void;
  } = $props();

  let confirmedWarnings = $state<Set<string>>(new Set());
  let showJournalForm   = $state(false);
  let isSaving          = $state(false);
  let saveError         = $state('');

  let coreThesisRationale   = $state('');
  let structuralInvalidation = $state('');
  let expectedMoveStr        = $state('');
  let emotionalState         = $state<'Calm' | 'Anxious' | 'Chasing' | 'Bored' | ''>('');
  let confidenceRating       = $state<1 | 2 | 3 | 4 | 5 | 0>(0);

  function confirmWarning(check: string) {
    confirmedWarnings = new Set([...confirmedWarnings, check]);
  }

  const allWarningsConfirmed = $derived(result.warnings.every(w => confirmedWarnings.has(w.check)));

  const canProceed = $derived(
    result.hard_blocks.length === 0 && allWarningsConfirmed && result.spec !== null
  );

  const journalValid = $derived(
    coreThesisRationale.trim().length > 0 &&
    structuralInvalidation.trim().length > 0 &&
    expectedMoveStr.trim().length > 0 &&
    !isNaN(parseFloat(expectedMoveStr)) &&
    emotionalState !== '' &&
    confidenceRating >= 1 && confidenceRating <= 5
  );

  const DEBIT_STRATEGIES = new Set(['BULL_CALL_SPREAD', 'BEAR_PUT_SPREAD', 'LONG_STRADDLE', 'LONG_STRANGLE']);

  async function handleConfirmSave() {
    if (!result.spec || !journalValid) return;
    const spec = result.spec;
    saveError = '';
    isSaving  = true;

    const journal: OperationalJournalEntry = {
      core_thesis_rationale:         coreThesisRationale.trim(),
      structural_invalidation:       structuralInvalidation.trim(),
      expected_underlying_move_pct:  parseFloat(expectedMoveStr),
      pre_trade_emotional_state:     emotionalState as OperationalJournalEntry['pre_trade_emotional_state'],
      pre_trade_confidence_rating:   confidenceRating as OperationalJournalEntry['pre_trade_confidence_rating'],
    };

    const premiumDirection = DEBIT_STRATEGIES.has(spec.strategy_type) ? 'DEBIT' : 'CREDIT';
    const maxLossPerShare  = spec.max_loss_dollars / 100;

    const position: Position = {
      id:                       `pos_${spec.playbook_id}_${Date.now()}`,
      underlying:               spec.underlying,
      strategy_type:            spec.strategy_type as Position['strategy_type'],
      execution_mode:           'PAPER',
      legs: spec.legs.map(leg => ({
        option_type: leg.option_type,
        direction:   leg.action === 'BUY' ? 'LONG' : 'SHORT',
        strike:      leg.strike,
        expiration:  leg.expiration_date,
        delta:  0.0, theta: 0.0, vega: 0.0, gamma: 0.0,
      })),
      entry_date:               new Date().toISOString().slice(0, 10),
      expiration_date:          spec.expiration_date,
      entry_premium:            spec.limit_price_per_share,
      premium_direction:        premiumDirection,
      current_value_per_share:  spec.limit_price_per_share,
      contracts:                1,
      max_profit:               spec.max_gain_dollars ?? 999999.0,
      max_loss:                 maxLossPerShare,
      profit_target_per_share:  spec.profit_target_dollars / 100,
      loss_limit_per_share:     spec.loss_limit_dollars / 100,
      notes:                    spec.closing_order_instructions,
      rolls:                    0,
      status:                   'OPEN',
      playbook_id:              spec.playbook_id,
      playbook_version:         undefined,
      playbook_snapshot:        undefined,
      journal,
      warnings_acknowledged:    [...confirmedWarnings],
    };

    try {
      const saved = await createPosition(position);
      await createOpportunityRecord({
        playbook_id:      spec.playbook_id,
        playbook_version: '1.0',
        generated_at:     new Date().toISOString(),
        accepted:         true,
        outcome_if_taken: null,
        bypass_reason:    null,
      });
      onPositionSaved?.(saved);
      onDismiss();
    } catch (e: unknown) {
      saveError = e instanceof Error ? e.message : 'Failed to save position';
    } finally {
      isSaving = false;
    }
  }

  const inputCls = 'w-full mt-1 px-3 py-2 border border-slate-200 dark:border-slate-700 rounded-lg bg-white dark:bg-slate-900 text-sm focus:outline-none focus:ring-2 focus:ring-emerald-400 dark:text-slate-100 resize-none';
</script>

<section class="mb-8">
  <div class="flex items-center justify-between mb-5">
    <div class="flex items-center gap-3">
      <h2 class="text-xl font-bold dark:text-white">Trade Specification</h2>
      <span class="text-xs text-slate-500 dark:text-slate-400 font-medium">{playbookName}</span>
    </div>
    <button
      onclick={onDismiss}
      class="text-sm font-semibold text-slate-400 hover:text-slate-600 dark:hover:text-slate-200 transition"
    >
      <X size={15} strokeWidth={2} />
    </button>
  </div>

  <!-- Hard Blocks -->
  {#if result.hard_blocks.length > 0}
    <div class="mb-5">
      <Alert level="critical" title="Trade blocked — resolve before proceeding">
        <div class="space-y-2 mt-2">
          {#each result.hard_blocks as block}
            <div class="flex items-start gap-2">
              <span class="carbon-mono font-bold text-[10px] px-1.5 py-0.5 bg-rose-100 dark:bg-rose-950 rounded shrink-0">
                {block.check}
              </span>
              <span>{block.reason}</span>
            </div>
          {/each}
        </div>
        <p class="font-semibold uppercase tracking-wider mt-2 text-[10px]">Hard blocks cannot be bypassed.</p>
      </Alert>
    </div>
  {/if}

  <!-- Warnings -->
  {#if result.warnings.length > 0}
    <div class="mb-5 space-y-3">
      {#each result.warnings as warning}
        {@const confirmed = confirmedWarnings.has(warning.check)}
        <div class="rounded-xl border p-4 text-xs flex items-start justify-between gap-4
          {confirmed
            ? 'border-slate-200 dark:border-slate-800 opacity-50'
            : 'border-amber-300 dark:border-amber-800/50 bg-amber-50 dark:bg-amber-950/10'}">
          <div>
            <span class="carbon-mono font-bold text-[10px] px-1.5 py-0.5 rounded mr-2
              {confirmed ? 'bg-slate-100 dark:bg-slate-800 text-slate-400' : 'bg-amber-100 dark:bg-amber-950 text-amber-700 dark:text-amber-400'}">
              {warning.check}
            </span>
            <span class="{confirmed ? 'text-slate-400' : 'text-amber-800 dark:text-amber-300'}">{warning.message}</span>
          </div>
          {#if !confirmed}
            <button
              onclick={() => confirmWarning(warning.check)}
              class="shrink-0 px-3 py-1.5 text-[10px] font-black rounded bg-amber-500 hover:bg-amber-600 text-white uppercase tracking-wider transition"
            >
              Acknowledge
            </button>
          {:else}
            <span class="shrink-0 flex items-center gap-1 text-[10px] font-bold text-emerald-500 uppercase">
              <Check size={12} strokeWidth={2.5} /> Confirmed
            </span>
          {/if}
        </div>
      {/each}
    </div>
  {/if}

  <!-- Spec — shown when no hard blocks -->
  {#if result.spec && result.hard_blocks.length === 0}
    {@const spec = result.spec}
    <div class="carbon-card dark:bg-slate-950/80 dark:backdrop-blur-md overflow-hidden shadow-sm">
      <!-- Header -->
      <div class="px-6 py-5 border-b border-slate-200 dark:border-slate-800 bg-slate-50/50 dark:bg-slate-900/50">
        <div class="flex flex-wrap gap-3 items-center justify-between">
          <div>
            <p class="text-[10px] font-bold text-slate-400 uppercase tracking-wider mb-1">Playbook Approved</p>
            <h3 class="text-base font-bold dark:text-white">{spec.underlying} {spec.strategy_type.replace(/_/g, ' ')}</h3>
            <p class="text-xs text-slate-500 mt-0.5">
              Exp: {formatDate(spec.expiration_date)} · {formatDte(spec.dte_at_entry)} · {spec.order_type}
            </p>
          </div>
          <div class="text-right">
            <p class="text-[10px] font-bold text-slate-400 uppercase tracking-wider mb-1">Limit Price / Share</p>
            <p class="text-2xl font-black carbon-mono text-indigo-600 dark:text-indigo-400">
              {formatDollar(spec.limit_price_per_share)}
            </p>
          </div>
        </div>
      </div>

      <div class="p-6 space-y-6">
        <!-- Order Legs -->
        <div>
          <h4 class="text-[10px] font-bold text-slate-400 uppercase tracking-wider mb-3">Order Legs</h4>
          <div class="space-y-2">
            {#each spec.legs as leg}
              <div class="flex justify-between items-center bg-slate-50 dark:bg-slate-950 p-3 rounded border border-slate-200/60 dark:border-slate-900 text-xs">
                <div class="flex items-center gap-3">
                  <Badge
                    label={leg.action}
                    variant={leg.action === 'BUY' ? 'info' : 'danger'}
                  />
                  <span class="font-bold text-slate-800 dark:text-slate-200">
                    {leg.quantity}× {spec.underlying} {leg.strike} {leg.option_type}
                  </span>
                  <span class="text-slate-400">{formatDate(leg.expiration_date)}</span>
                </div>
                {#if leg.delta_target !== null && leg.delta_target !== undefined}
                  <span class="carbon-mono text-slate-500 text-[10px]">Target Δ: {leg.delta_target.toFixed(2)}</span>
                {/if}
              </div>
            {/each}
          </div>
        </div>

        <!-- P&L Grid -->
        <div class="grid grid-cols-2 md:grid-cols-4 gap-3">
          <div class="p-3 rounded bg-slate-50 dark:bg-slate-950 border border-slate-200/80 dark:border-slate-900">
            <span class="text-[10px] font-bold text-slate-400 uppercase block mb-1">Max Loss</span>
            <span class="text-base font-black carbon-mono text-rose-600 dark:text-rose-400">{formatDollar(spec.max_loss_dollars)}</span>
          </div>
          <div class="p-3 rounded bg-slate-50 dark:bg-slate-950 border border-slate-200/80 dark:border-slate-900">
            <span class="text-[10px] font-bold text-slate-400 uppercase block mb-1">Max Gain</span>
            <span class="text-base font-black carbon-mono text-emerald-600 dark:text-emerald-400">{spec.max_gain_note}</span>
          </div>
          <div class="p-3 rounded bg-slate-50 dark:bg-slate-950 border border-slate-200/80 dark:border-slate-900">
            <span class="text-[10px] font-bold text-slate-400 uppercase block mb-1">Profit Target</span>
            <span class="text-base font-black carbon-mono text-emerald-600 dark:text-emerald-400">{formatDollar(spec.profit_target_dollars)}</span>
            <span class="text-[10px] text-slate-400 block carbon-mono">({formatPct(spec.profit_target_pct)})</span>
          </div>
          <div class="p-3 rounded bg-slate-50 dark:bg-slate-950 border border-slate-200/80 dark:border-slate-900">
            <span class="text-[10px] font-bold text-slate-400 uppercase block mb-1">Loss Limit</span>
            <span class="text-base font-black carbon-mono text-rose-600 dark:text-rose-400">{formatDollar(spec.loss_limit_dollars)}</span>
            <span class="text-[10px] text-slate-400 block carbon-mono">({formatPct(spec.loss_limit_pct)})</span>
          </div>
        </div>

        <!-- Break-evens -->
        <div>
          <span class="text-[10px] font-bold text-slate-400 uppercase tracking-wider block mb-2">
            Break-even Price{spec.break_even_prices.length > 1 ? 's' : ''}
          </span>
          <div class="flex gap-3 flex-wrap">
            {#each spec.break_even_prices as be}
              <span class="px-3 py-1 rounded bg-slate-100 dark:bg-slate-950 border border-slate-200 dark:border-slate-800 carbon-mono text-sm font-bold dark:text-white">
                {formatDollar(be)}
              </span>
            {/each}
          </div>
        </div>

        <!-- Derivation note -->
        <div class="text-[10px] text-slate-400 border-t border-slate-100 dark:border-slate-800 pt-4 carbon-mono leading-relaxed">
          <span class="font-bold text-slate-500 block mb-1">Derivation Parameters:</span>
          {spec.derivation_params.derivation_note}
        </div>

        <!-- Closing order instructions -->
        <div class="rounded-xl border border-indigo-200 dark:border-indigo-900/40 bg-indigo-50/50 dark:bg-indigo-950/10 p-4">
          <span class="text-[10px] font-bold text-indigo-600 dark:text-indigo-400 uppercase tracking-wider block mb-1.5">
            GTC Closing Order Instructions
          </span>
          <p class="text-xs text-slate-700 dark:text-slate-300 leading-relaxed">{spec.closing_order_instructions}</p>
        </div>

        <!-- Intent Journal -->
        {#if canProceed}
          {#if !showJournalForm}
            <div class="flex justify-end pt-2">
              <Button variant="primary" onclick={() => (showJournalForm = true)}>
                Log Intent Journal & Save →
              </Button>
            </div>
          {:else}
            <div class="carbon-card p-5 space-y-4 border-emerald-200 dark:border-emerald-900/40 dark:bg-emerald-950/10">
              <h4 class="text-sm font-bold text-emerald-700 dark:text-emerald-400 uppercase tracking-wider">
                Intent Journal — Required Before Save
              </h4>
              <p class="text-xs text-slate-500">
                This is your pre-trade record. It cannot be edited after saving.
              </p>

              <FormField label="Core Thesis Rationale" required>
                <textarea
                  bind:value={coreThesisRationale}
                  rows="2"
                  placeholder="Why does this trade make sense right now?"
                  class={inputCls}
                ></textarea>
              </FormField>

              <FormField label="Structural Invalidation" required>
                <textarea
                  bind:value={structuralInvalidation}
                  rows="2"
                  placeholder="What would prove this thesis wrong?"
                  class={inputCls}
                ></textarea>
              </FormField>

              <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                <FormField label="Expected Underlying Move (%)" required>
                  <input
                    type="number"
                    step="0.1"
                    bind:value={expectedMoveStr}
                    placeholder="e.g. 2.5"
                    class="{inputCls} carbon-mono"
                  />
                </FormField>

                <FormField label="Pre-Trade Emotional State" required>
                  <select bind:value={emotionalState} class={inputCls}>
                    <option value="">Select…</option>
                    <option value="Calm">Calm</option>
                    <option value="Anxious">Anxious</option>
                    <option value="Chasing">Chasing</option>
                    <option value="Bored">Bored</option>
                  </select>
                </FormField>
              </div>

              <div>
                <span class="block text-xs font-semibold text-slate-500 mb-2">
                  Pre-Trade Confidence (1–5) <span class="text-rose-500">*</span>
                </span>
                <div class="flex gap-2">
                  {#each [1, 2, 3, 4, 5] as n}
                    <button
                      type="button"
                      onclick={() => (confidenceRating = n as 1|2|3|4|5)}
                      class="w-9 h-9 rounded-lg text-sm font-black border transition
                        {confidenceRating === n
                          ? 'bg-emerald-600 border-emerald-600 text-white'
                          : 'bg-white dark:bg-slate-900 border-slate-300 dark:border-slate-700 text-slate-500 hover:border-emerald-400'}"
                    >
                      {n}
                    </button>
                  {/each}
                </div>
              </div>

              {#if saveError}
                <p class="text-xs text-rose-600 dark:text-rose-400 font-semibold">{saveError}</p>
              {/if}

              <div class="flex justify-between items-center pt-2">
                <button
                  onclick={() => (showJournalForm = false)}
                  class="text-xs text-slate-400 hover:text-slate-600 dark:hover:text-slate-200 font-semibold"
                >
                  <ArrowLeft size={13} strokeWidth={2} class="inline mr-1" /> Back to spec
                </button>
                <Button
                  variant="primary"
                  disabled={!journalValid || isSaving}
                  loading={isSaving}
                  onclick={handleConfirmSave}
                >
                  {isSaving ? 'Saving…' : 'Confirm & Save Position →'}
                </Button>
              </div>
            </div>
          {/if}
        {:else if result.warnings.length > 0 && !allWarningsConfirmed}
          <p class="text-xs text-amber-600 dark:text-amber-400 text-center font-semibold">
            Acknowledge all warnings above to proceed.
          </p>
        {/if}
      </div>
    </div>
  {/if}
</section>
