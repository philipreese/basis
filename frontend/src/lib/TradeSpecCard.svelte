<script lang="ts">
  import type { TradeSpecResult } from './api';

  let {
    result,
    playbookName,
    onDismiss,
  }: {
    result: TradeSpecResult;
    playbookName: string;
    onDismiss: () => void;
  } = $props();

  let confirmedWarnings = $state<Set<string>>(new Set());

  function confirmWarning(check: string) {
    confirmedWarnings = new Set([...confirmedWarnings, check]);
  }

  const allWarningsConfirmed = $derived(
    result.warnings.every(w => confirmedWarnings.has(w.check))
  );

  const canProceed = $derived(
    result.hard_blocks.length === 0 && allWarningsConfirmed && result.spec !== null
  );
</script>

<section class="mb-8">
  <div class="flex items-center justify-between mb-5">
    <div class="flex items-center gap-3">
      <h2 class="text-xl font-bold dark:text-white">Trade Specification</h2>
      <span class="text-xs text-slate-500 dark:text-slate-400 font-medium">{playbookName}</span>
    </div>
    <button
      onclick={onDismiss}
      class="text-sm font-semibold text-slate-400 hover:text-slate-600 dark:hover:text-slate-200"
    >
      ✕ Close
    </button>
  </div>

  <!-- Hard Blocks — uncircumventable -->
  {#if result.hard_blocks.length > 0}
    <div class="mb-5 rounded-2xl border border-rose-400 bg-rose-50 dark:bg-rose-950/20 dark:border-rose-900/50 p-5">
      <h3 class="text-sm font-black text-rose-700 dark:text-rose-400 mb-3 uppercase tracking-wider">
        🛑 Trade Blocked — Resolve Before Proceeding
      </h3>
      <div class="space-y-2">
        {#each result.hard_blocks as block}
          <div class="flex items-start gap-3 text-xs text-rose-800 dark:text-rose-300">
            <span class="font-mono font-bold text-[10px] px-1.5 py-0.5 bg-rose-100 dark:bg-rose-950 rounded shrink-0">{block.check}</span>
            <span>{block.reason}</span>
          </div>
        {/each}
      </div>
      <p class="text-[10px] text-rose-500 mt-3 font-semibold uppercase tracking-wider">Hard blocks cannot be bypassed. No "proceed anyway" option.</p>
    </div>
  {/if}

  <!-- Warnings — require explicit confirmation -->
  {#if result.warnings.length > 0}
    <div class="mb-5 space-y-3">
      {#each result.warnings as warning}
        {@const confirmed = confirmedWarnings.has(warning.check)}
        <div class="rounded-xl border {confirmed ? 'border-slate-200 dark:border-slate-800 opacity-60' : 'border-amber-300 dark:border-amber-800/50 bg-amber-50 dark:bg-amber-950/10'} p-4 text-xs flex items-start justify-between gap-4">
          <div>
            <span class="font-mono font-bold text-[10px] px-1.5 py-0.5 rounded {confirmed ? 'bg-slate-100 dark:bg-slate-800 text-slate-400' : 'bg-amber-100 dark:bg-amber-950 text-amber-700 dark:text-amber-400'} mr-2">{warning.check}</span>
            <span class="{confirmed ? 'text-slate-400' : 'text-amber-800 dark:text-amber-300'}">{warning.message}</span>
          </div>
          {#if !confirmed}
            <button
              onclick={() => confirmWarning(warning.check)}
              class="shrink-0 px-3 py-1.5 text-[10px] font-black rounded-lg bg-amber-500 hover:bg-amber-600 text-white uppercase tracking-wider cursor-pointer transition"
            >
              Acknowledged
            </button>
          {:else}
            <span class="shrink-0 text-[10px] font-bold text-emerald-500 uppercase">✓ Confirmed</span>
          {/if}
        </div>
      {/each}
    </div>
  {/if}

  <!-- Trade Spec — shown when no hard blocks -->
  {#if result.spec && result.hard_blocks.length === 0}
    {@const spec = result.spec}

    <div class="bg-white dark:bg-slate-900 rounded-3xl border border-slate-200 dark:border-slate-800 overflow-hidden shadow-sm">
      <!-- Header -->
      <div class="px-6 py-5 border-b border-slate-200 dark:border-slate-800 bg-slate-50/50 dark:bg-slate-900/50">
        <div class="flex flex-wrap gap-3 items-center justify-between">
          <div>
            <p class="text-[10px] font-bold text-slate-400 uppercase tracking-wider mb-1">Playbook Approved</p>
            <h3 class="text-base font-bold dark:text-white">{spec.underlying} {spec.strategy_type.replace(/_/g, ' ')}</h3>
            <p class="text-xs text-slate-500 mt-0.5">Exp: {spec.expiration_date} · {spec.dte_at_entry} DTE · {spec.order_type}</p>
          </div>
          <div class="text-right">
            <p class="text-[10px] font-bold text-slate-400 uppercase tracking-wider mb-1">Limit Price / Share</p>
            <p class="text-2xl font-black font-mono text-indigo-600 dark:text-indigo-400">${spec.limit_price_per_share.toFixed(2)}</p>
          </div>
        </div>
      </div>

      <div class="p-6 space-y-6">
        <!-- Legs -->
        <div>
          <h4 class="text-[10px] font-bold text-slate-400 uppercase tracking-wider mb-3">Order Legs</h4>
          <div class="space-y-2">
            {#each spec.legs as leg}
              <div class="flex justify-between items-center bg-slate-50 dark:bg-slate-950 p-3 rounded-xl border border-slate-200/60 dark:border-slate-900 text-xs">
                <div class="flex items-center gap-3">
                  <span class="font-black px-1.5 py-0.5 rounded text-[10px] {leg.action === 'BUY' ? 'bg-sky-100 text-sky-700 dark:bg-sky-950 dark:text-sky-300' : 'bg-rose-100 text-rose-700 dark:bg-rose-950 dark:text-rose-300'}">
                    {leg.action}
                  </span>
                  <span class="font-bold text-slate-800 dark:text-slate-200">{leg.quantity}x {spec.underlying} {leg.strike} {leg.option_type}</span>
                  <span class="text-slate-400">{leg.expiration_date}</span>
                </div>
                {#if leg.delta_target !== null && leg.delta_target !== undefined}
                  <span class="font-mono text-slate-500 text-[10px]">Target Δ: {leg.delta_target.toFixed(2)}</span>
                {/if}
              </div>
            {/each}
          </div>
        </div>

        <!-- P&L Grid -->
        <div class="grid grid-cols-2 md:grid-cols-4 gap-4">
          <div class="p-3 rounded-xl bg-slate-50 dark:bg-slate-950 border border-slate-200/80 dark:border-slate-900">
            <span class="text-[10px] font-bold text-slate-400 uppercase block mb-1">Max Loss</span>
            <span class="text-base font-black font-mono text-rose-600 dark:text-rose-400">${spec.max_loss_dollars.toFixed(2)}</span>
          </div>
          <div class="p-3 rounded-xl bg-slate-50 dark:bg-slate-950 border border-slate-200/80 dark:border-slate-900">
            <span class="text-[10px] font-bold text-slate-400 uppercase block mb-1">Max Gain</span>
            <span class="text-base font-black font-mono text-emerald-600 dark:text-emerald-400">{spec.max_gain_note}</span>
          </div>
          <div class="p-3 rounded-xl bg-slate-50 dark:bg-slate-950 border border-slate-200/80 dark:border-slate-900">
            <span class="text-[10px] font-bold text-slate-400 uppercase block mb-1">Profit Target</span>
            <span class="text-base font-black font-mono text-emerald-600 dark:text-emerald-400">${spec.profit_target_dollars.toFixed(2)}</span>
            <span class="text-[10px] text-slate-400 block">({spec.profit_target_pct.toFixed(0)}%)</span>
          </div>
          <div class="p-3 rounded-xl bg-slate-50 dark:bg-slate-950 border border-slate-200/80 dark:border-slate-900">
            <span class="text-[10px] font-bold text-slate-400 uppercase block mb-1">Loss Limit</span>
            <span class="text-base font-black font-mono text-rose-600 dark:text-rose-400">${spec.loss_limit_dollars.toFixed(2)}</span>
            <span class="text-[10px] text-slate-400 block">({spec.loss_limit_pct.toFixed(0)}%)</span>
          </div>
        </div>

        <!-- Break-evens -->
        <div>
          <span class="text-[10px] font-bold text-slate-400 uppercase tracking-wider block mb-2">Break-even Price{spec.break_even_prices.length > 1 ? 's' : ''}</span>
          <div class="flex gap-3 flex-wrap">
            {#each spec.break_even_prices as be}
              <span class="px-3 py-1 rounded-lg bg-slate-100 dark:bg-slate-950 border border-slate-200 dark:border-slate-800 font-mono text-sm font-bold dark:text-white">${be.toFixed(2)}</span>
            {/each}
          </div>
        </div>

        <!-- Derivation params -->
        <div class="text-[10px] text-slate-400 border-t border-slate-100 dark:border-slate-800 pt-4 font-mono leading-relaxed">
          <span class="font-bold text-slate-500 block mb-1">Derivation Parameters:</span>
          {spec.derivation_params.derivation_note}
        </div>

        <!-- Closing order instructions -->
        <div class="rounded-xl border border-indigo-200 dark:border-indigo-900/40 bg-indigo-50/50 dark:bg-indigo-950/10 p-4">
          <span class="text-[10px] font-bold text-indigo-600 dark:text-indigo-400 uppercase tracking-wider block mb-1.5">GTC Closing Order Instructions</span>
          <p class="text-xs text-slate-600 dark:text-slate-300 leading-relaxed">{spec.closing_order_instructions}</p>
        </div>

        <!-- Proceed button — only shown when all warnings confirmed -->
        {#if canProceed}
          <div class="flex justify-end pt-2">
            <button
              class="px-6 py-3 text-sm font-black rounded-xl bg-emerald-600 hover:bg-emerald-700 text-white cursor-pointer transition shadow-md uppercase tracking-wider"
            >
              Save Trade Spec & Log Intent Journal →
            </button>
          </div>
        {:else if result.warnings.length > 0 && !allWarningsConfirmed}
          <p class="text-xs text-amber-600 dark:text-amber-400 text-center font-semibold">
            Acknowledge all warnings above to proceed.
          </p>
        {/if}
      </div>
    </div>
  {/if}
</section>
