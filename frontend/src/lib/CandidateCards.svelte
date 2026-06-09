<script lang="ts">
  import type { OpportunityScanResult } from './api';

  let {
    scanResult,
    onSelectPlaybook,
  }: {
    scanResult: OpportunityScanResult;
    onSelectPlaybook: (playbookId: string) => void;
  } = $props();

  const strategyLabels: Record<string, string> = {
    IRON_CONDOR: 'Iron Condor',
    BULL_CALL_SPREAD: 'Bull Call Spread',
    BEAR_PUT_SPREAD: 'Bear Put Spread',
    LONG_STRADDLE: 'Long Straddle',
    LONG_STRANGLE: 'Long Strangle',
  };
</script>

<section class="mb-8">
  <div class="flex items-center gap-3 mb-5">
    <h2 class="text-xl font-bold dark:text-white tracking-tight">Layer C — Opportunity Engine</h2>
    <span class="px-2 py-0.5 text-xs bg-violet-100 dark:bg-violet-900/50 text-violet-700 dark:text-violet-400 rounded-full font-semibold">
      Eligible Playbooks
    </span>
  </div>

  {#if scanResult.portfolio_blocked}
    <div class="p-5 rounded-2xl border border-rose-200 bg-rose-50 dark:border-rose-950/30 dark:bg-rose-950/20 text-rose-800 dark:text-rose-400">
      <p class="text-sm font-bold mb-1">🛑 ALL CANDIDATES SUPPRESSED</p>
      <p class="text-xs leading-relaxed">{scanResult.block_reason}</p>
    </div>

  {:else if scanResult.candidates.length === 0}
    <div class="p-8 rounded-3xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 text-center">
      <p class="text-slate-500 text-sm">No eligible playbooks match current market conditions.</p>
      <p class="text-slate-400 text-xs mt-1">All playbooks suppressed by entry filters or exposure gates.</p>
    </div>

  {:else}
    <div class="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-3 gap-5">
      {#each scanResult.candidates as card (card.playbook.id)}
        <article class="bg-white dark:bg-slate-900 rounded-3xl border border-violet-200 dark:border-violet-900/40 shadow-sm overflow-hidden flex flex-col">
          <!-- Card Header -->
          <div class="p-5 border-b border-violet-100 dark:border-violet-900/20 bg-violet-50/50 dark:bg-violet-950/10">
            <div class="flex justify-between items-start mb-1">
              <span class="px-2.5 py-1 text-xs font-bold bg-indigo-100 dark:bg-indigo-950 text-indigo-700 dark:text-indigo-300 rounded-full uppercase">
                {card.playbook.underlying_ticker}
              </span>
              <span class="px-2 py-0.5 text-[10px] font-black rounded border border-emerald-400 text-emerald-600 dark:border-emerald-600 dark:text-emerald-400 uppercase tracking-wider">
                ELIGIBLE
              </span>
            </div>
            <h3 class="text-sm font-bold dark:text-white mt-2 leading-tight">{card.playbook.name}</h3>
            <p class="text-xs text-slate-500 dark:text-slate-400 mt-0.5">
              {strategyLabels[card.playbook.strategy_type] ?? card.playbook.strategy_type} · {card.playbook.execution_mode}
            </p>
          </div>

          <!-- Strike Derivation Parameters -->
          {#if card.strike_params}
            {@const p = card.strike_params}
            <div class="p-5 grow space-y-3 text-xs">
              <div>
                <span class="text-[10px] font-bold text-slate-400 uppercase tracking-wider block mb-1">Automated Order Specification</span>
                <div class="bg-slate-50 dark:bg-slate-950 rounded-xl p-3 border border-slate-200/80 dark:border-slate-900 font-mono text-[11px] text-slate-600 dark:text-slate-300 leading-relaxed">
                  {#if card.playbook.strategy_type === 'IRON_CONDOR'}
                    → Sell 1x {p.underlying} Put Spread (Δ {p.short_leg_delta?.toFixed(2)} short leg)<br>
                    → Sell 1x {p.underlying} Call Spread (Δ {p.short_leg_delta?.toFixed(2)} short leg)<br>
                    → Wing width: ${p.spread_width_dollars?.toFixed(0)}
                  {:else if card.playbook.strategy_type === 'BULL_CALL_SPREAD'}
                    → Buy 1x {p.underlying} Call (Δ {p.long_leg_delta?.toFixed(2)} — ATM)<br>
                    → Sell 1x {p.underlying} Call (Δ {p.short_leg_delta?.toFixed(2)} — target)<br>
                    → Spread width: ${p.spread_width_dollars?.toFixed(0)}
                  {:else if card.playbook.strategy_type === 'BEAR_PUT_SPREAD'}
                    → Buy 1x {p.underlying} Put (Δ -{p.long_leg_delta?.toFixed(2)} — ATM)<br>
                    → Sell 1x {p.underlying} Put (Δ -{p.short_leg_delta?.toFixed(2)} — target)<br>
                    → Spread width: ${p.spread_width_dollars?.toFixed(0)}
                  {:else if card.playbook.strategy_type === 'LONG_STRADDLE'}
                    → Buy 1x {p.underlying} ATM Call<br>
                    → Buy 1x {p.underlying} ATM Put<br>
                    → ATM strike: closest to ${p.current_price.toFixed(2)}
                  {:else if card.playbook.strategy_type === 'LONG_STRANGLE'}
                    → Buy 1x {p.underlying} Call (Δ {p.short_leg_delta?.toFixed(2)})<br>
                    → Buy 1x {p.underlying} Put (Δ -{p.short_leg_delta?.toFixed(2)})<br>
                    → OTM on both sides
                  {/if}
                </div>
              </div>

              <div class="text-[10px] text-slate-400 border-t border-slate-100 dark:border-slate-800 pt-2">
                <span class="font-bold text-slate-500">Derived from:</span> Target DTE={p.target_dte}
                {#if p.short_leg_delta} · Short Δ={p.short_leg_delta.toFixed(2)}{/if}
                {#if p.spread_width_dollars} · Wing ${p.spread_width_dollars.toFixed(0)}{/if}
                {#if p.one_sigma_move} · 1σ=${p.one_sigma_move.toFixed(2)}{/if}
              </div>
            </div>
          {/if}

          <!-- Action Button -->
          <div class="px-5 pb-5">
            <button
              onclick={() => onSelectPlaybook(card.playbook.id)}
              class="w-full py-2.5 text-sm font-bold rounded-xl bg-violet-600 hover:bg-violet-700 text-white cursor-pointer transition shadow-sm"
            >
              Generate Trade Spec →
            </button>
          </div>
        </article>
      {/each}
    </div>
  {/if}
</section>
