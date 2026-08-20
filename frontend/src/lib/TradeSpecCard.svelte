<script lang="ts">
  import type { TradeSpecResult, OperationalJournalEntry, Position } from './api';
  import { createPosition, createOpportunityRecord } from './api';
  import { formatDollar, formatDate, formatDte, formatPct } from './formatters';
  import Alert from './ui/Alert.svelte';
  import Badge from './ui/Badge.svelte';
  import Button from './ui/Button.svelte';
  import FormField from './ui/FormField.svelte';
  import { IconClose, IconBack, IconConfirm } from './ui/icons';

  let {
    result,
    playbookName,
    onDismiss,
    onPositionSaved,
    diagnostic = false,
  }: {
    result: TradeSpecResult;
    playbookName: string;
    onDismiss: () => void;
    onPositionSaved?: (pos: Position) => void;
    /** #315: read-only view — the executor stages its own entries nightly. */
    diagnostic?: boolean;
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
      book_id:                  'B00', // manual-workbench entries stay in the manual book (#279)
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

  const inputCls = 'w-full mt-1 px-3 py-2 border border-ctp-surface1 rounded-lg bg-ctp-crust text-ctp-text text-sm focus:outline-none focus:ring-2 focus:ring-ctp-mauve resize-none carbon-mono';
</script>

<section class="mb-8">
  <div class="flex items-center justify-between mb-5">
    <div class="flex items-center gap-3">
      <h2 class="text-xl font-bold text-ctp-text">Trade Specification</h2>
      <span class="text-xs text-ctp-subtext0 font-medium">{playbookName}</span>
    </div>
    <button
      onclick={onDismiss}
      class="text-ctp-overlay0 hover:text-ctp-text transition"
    >
      <IconClose size={15} strokeWidth={2} />
    </button>
  </div>

  <!-- Hard Blocks -->
  {#if result.hard_blocks.length > 0}
    <div class="mb-5">
      <Alert level="critical" title="Trade blocked — resolve before proceeding">
        <div class="space-y-2 mt-2">
          {#each result.hard_blocks as block}
            <div class="flex items-start gap-2">
              <span class="carbon-mono font-bold text-xs px-1.5 py-0.5 bg-ctp-red/15 text-ctp-red rounded shrink-0">
                {block.check}
              </span>
              <span>{block.reason}</span>
            </div>
          {/each}
        </div>
        <p class="font-semibold uppercase tracking-wider mt-2 text-xs">Hard blocks cannot be bypassed.</p>
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
            ? 'border-ctp-surface0 opacity-50'
            : 'border-ctp-yellow/40 bg-ctp-yellow/5'}">
          <div>
            <span class="carbon-mono font-bold text-xs px-1.5 py-0.5 rounded mr-2
              {confirmed ? 'bg-ctp-surface0 text-ctp-overlay0' : 'bg-ctp-yellow/15 text-ctp-yellow'}">
              {warning.check}
            </span>
            <span class="{confirmed ? 'text-ctp-overlay0' : 'text-ctp-yellow'}">{warning.message}</span>
          </div>
          {#if !confirmed}
            <button
              onclick={() => confirmWarning(warning.check)}
              class="shrink-0 px-3 py-1.5 text-xs font-black rounded bg-ctp-yellow text-ctp-crust hover:bg-ctp-yellow/90 uppercase tracking-wider transition"
            >
              Acknowledge
            </button>
          {:else}
            <span class="shrink-0 flex items-center gap-1 text-xs font-bold text-ctp-green uppercase">
              <IconConfirm size={12} strokeWidth={2.5} /> Confirmed
            </span>
          {/if}
        </div>
      {/each}
    </div>
  {/if}

  <!-- Spec — shown when no hard blocks -->
  {#if result.spec && result.hard_blocks.length === 0}
    {@const spec = result.spec}
    <div class="carbon-card overflow-hidden">
      <!-- Header -->
      <div class="px-6 py-5 border-b border-ctp-surface0 bg-ctp-crust/50">
        <div class="flex flex-wrap gap-3 items-center justify-between">
          <div>
            <p class="text-xs font-bold text-ctp-overlay0 uppercase tracking-wider mb-1">Playbook Approved</p>
            <h3 class="text-base font-bold text-ctp-text">{spec.underlying} {spec.strategy_type.replace(/_/g, ' ')}</h3>
            <p class="text-xs text-ctp-subtext0 mt-0.5">
              Exp: {formatDate(spec.expiration_date)} · {formatDte(spec.dte_at_entry)} · {spec.order_type}
            </p>
          </div>
          <div class="text-right">
            <p class="text-xs font-bold text-ctp-overlay0 uppercase tracking-wider mb-1">Limit Price / Share</p>
            <p class="text-2xl font-black carbon-mono text-ctp-mauve">
              {formatDollar(spec.limit_price_per_share)}
            </p>
          </div>
        </div>
      </div>

      <div class="p-6 space-y-6">
        <!-- Order Legs -->
        <div>
          <h4 class="text-xs font-bold text-ctp-overlay0 uppercase tracking-wider mb-3">Order Legs</h4>
          <div class="space-y-2">
            {#each spec.legs as leg}
              <div class="flex justify-between items-center bg-ctp-crust p-3 rounded border border-ctp-surface0 text-xs">
                <div class="flex items-center gap-3">
                  <Badge
                    label={leg.action}
                    variant={leg.action === 'BUY' ? 'info' : 'danger'}
                  />
                  <span class="font-bold text-ctp-text">
                    {leg.quantity}× {spec.underlying} {leg.strike} {leg.option_type}
                  </span>
                  <span class="text-ctp-overlay0">{formatDate(leg.expiration_date)}</span>
                </div>
                {#if leg.delta_target !== null && leg.delta_target !== undefined}
                  <span class="carbon-mono text-ctp-subtext0 text-xs">Target Δ: {leg.delta_target.toFixed(2)}</span>
                {/if}
              </div>
            {/each}
          </div>
        </div>

        <!-- P&L Grid -->
        <div class="grid grid-cols-2 md:grid-cols-4 gap-3">
          <div class="p-3 rounded bg-ctp-crust border border-ctp-surface0">
            <span class="text-xs font-bold text-ctp-overlay0 uppercase block mb-1">Max Loss</span>
            <span class="text-base font-black carbon-mono text-ctp-red">{formatDollar(spec.max_loss_dollars)}</span>
          </div>
          <div class="p-3 rounded bg-ctp-crust border border-ctp-surface0">
            <span class="text-xs font-bold text-ctp-overlay0 uppercase block mb-1">Max Gain</span>
            <span class="text-base font-black carbon-mono text-ctp-green">{spec.max_gain_note}</span>
          </div>
          <div class="p-3 rounded bg-ctp-crust border border-ctp-surface0">
            <span class="text-xs font-bold text-ctp-overlay0 uppercase block mb-1">Profit Target</span>
            <span class="text-base font-black carbon-mono text-ctp-green">{formatDollar(spec.profit_target_dollars)}</span>
            <span class="text-xs text-ctp-overlay0 block carbon-mono">({formatPct(spec.profit_target_pct)})</span>
          </div>
          <div class="p-3 rounded bg-ctp-crust border border-ctp-surface0">
            <span class="text-xs font-bold text-ctp-overlay0 uppercase block mb-1">Loss Limit</span>
            <span class="text-base font-black carbon-mono text-ctp-red">{formatDollar(spec.loss_limit_dollars)}</span>
            <span class="text-xs text-ctp-overlay0 block carbon-mono">({formatPct(spec.loss_limit_pct)})</span>
          </div>
        </div>

        <!-- Break-evens -->
        <div>
          <span class="text-xs font-bold text-ctp-overlay0 uppercase tracking-wider block mb-2">
            Break-even Price{spec.break_even_prices.length > 1 ? 's' : ''}
          </span>
          <div class="flex gap-3 flex-wrap">
            {#each spec.break_even_prices as be}
              <span class="px-3 py-1 rounded bg-ctp-crust border border-ctp-surface0 carbon-mono text-sm font-bold text-ctp-text">
                {formatDollar(be)}
              </span>
            {/each}
          </div>
        </div>

        <!-- Derivation note -->
        <div class="text-xs text-ctp-overlay0 border-t border-ctp-surface0 pt-4 carbon-mono leading-relaxed">
          <span class="font-bold text-ctp-subtext0 block mb-1">Derivation Parameters:</span>
          {spec.derivation_params.derivation_note}
        </div>

        <!-- Closing order instructions -->
        <div class="rounded-xl border border-ctp-mauve/30 bg-ctp-mauve/5 p-4">
          <span class="text-xs font-bold text-ctp-mauve uppercase tracking-wider block mb-1.5">
            GTC Closing Order Instructions
          </span>
          <p class="text-sm text-ctp-subtext1 leading-relaxed">{spec.closing_order_instructions}</p>
        </div>

        <!-- Intent Journal -->
        {#if diagnostic}
          <p class="text-xs text-ctp-overlay0 text-center font-semibold border-t border-ctp-surface0 pt-4">
            Diagnostic view — the executor stages its own entries nightly. Nothing is saved from here.
          </p>
        {:else if canProceed}
          {#if !showJournalForm}
            <div class="flex justify-end pt-2">
              <Button variant="primary" onclick={() => (showJournalForm = true)}>
                Log Intent Journal & Save →
              </Button>
            </div>
          {:else}
            <div class="carbon-card p-5 space-y-4 border-ctp-green/30 bg-ctp-green/5">
              <h4 class="text-sm font-bold text-ctp-green uppercase tracking-wider">
                Intent Journal — Required Before Save
              </h4>
              <p class="text-sm text-ctp-subtext0">
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
                    class={inputCls}
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
                <span class="block text-xs font-semibold text-ctp-subtext0 mb-2">
                  Pre-Trade Confidence (1–5) <span class="text-ctp-red">*</span>
                </span>
                <div class="flex gap-2">
                  {#each [1, 2, 3, 4, 5] as n}
                    <button
                      type="button"
                      onclick={() => (confidenceRating = n as 1|2|3|4|5)}
                      class="w-9 h-9 rounded-lg text-sm font-black border transition
                        {confidenceRating === n
                          ? 'bg-ctp-green border-ctp-green text-ctp-crust'
                          : 'bg-ctp-crust border-ctp-surface1 text-ctp-subtext0 hover:border-ctp-green'}"
                    >
                      {n}
                    </button>
                  {/each}
                </div>
              </div>

              {#if saveError}
                <p class="text-xs text-ctp-red font-semibold">{saveError}</p>
              {/if}

              <div class="flex justify-between items-center pt-2">
                <button
                  onclick={() => (showJournalForm = false)}
                  class="text-xs text-ctp-overlay0 hover:text-ctp-text font-semibold transition"
                >
                  <IconBack size={13} strokeWidth={2} class="inline mr-1" /> Back to spec
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
          <p class="text-xs text-ctp-yellow text-center font-semibold">
            Acknowledge all warnings above to proceed.
          </p>
        {/if}
      </div>
    </div>
  {/if}
</section>
