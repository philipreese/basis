<script lang="ts">
  import { REGIME_DISPLAY } from './api';
  import type { MarketState } from './api';

  let { marketState }: { marketState: MarketState } = $props();

  let showScoreBreakdown = $state(false);

  const colorMap: Record<string, string> = {
    emerald: 'bg-emerald-900/30 border-emerald-700/40 text-emerald-300',
    amber:   'bg-amber-900/30 border-amber-700/40 text-amber-300',
    rose:    'bg-rose-900/30 border-rose-700/40 text-rose-300',
    violet:  'bg-violet-900/30 border-violet-700/40 text-violet-300',
    slate:   'bg-slate-800 border-slate-700 text-slate-300',
  };
</script>

{@const regime = marketState.current_regime}
{@const info = REGIME_DISPLAY[regime] ?? { label: regime, color: 'slate', description: '' }}
{@const scores = marketState.regime_scores ?? {}}
{@const pillClass = colorMap[info.color] ?? colorMap.slate}

<div id="layer-b-ribbon" class="mb-6 rounded-2xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900/80 px-5 py-3 shadow-sm flex flex-wrap gap-4 items-center justify-between text-xs">
  <!-- Regime badge -->
  <div class="flex items-center gap-3">
    <span class="text-[10px] font-bold text-slate-400 uppercase tracking-widest">Layer B · Market Context</span>
    <span class="px-3 py-1 rounded-full border font-black tracking-wider uppercase {pillClass}">
      {info.label}
    </span>
    <span class="text-slate-500 dark:text-slate-400 hidden sm:inline">{info.description}</span>
  </div>

  <!-- Telemetry pills -->
  <div class="flex flex-wrap gap-3 items-center">
    <span class="font-mono text-slate-600 dark:text-slate-300">SPY <span class="font-bold">${marketState.spy_price.toFixed(2)}</span></span>
    <span class="text-slate-400">·</span>
    <span class="font-mono text-slate-600 dark:text-slate-300">SMA20 <span class="font-bold">${(marketState.spy_sma20 ?? 0).toFixed(2)}</span></span>
    <span class="text-slate-400">·</span>
    <span class="font-mono text-slate-600 dark:text-slate-300">VIX <span class="font-bold">{(marketState.vix_close ?? 0).toFixed(1)}</span></span>
    <span class="text-slate-400">·</span>
    <span class="font-mono {(marketState.spy_daily_return ?? 0) >= 0 ? 'text-emerald-500' : 'text-rose-500'}">
      Day {(marketState.spy_daily_return ?? 0) >= 0 ? '+' : ''}{((marketState.spy_daily_return ?? 0) * 100).toFixed(2)}%
    </span>

    <button
      id="regime-score-toggle"
      onclick={() => (showScoreBreakdown = !showScoreBreakdown)}
      class="ml-2 px-2 py-0.5 text-[10px] rounded border border-slate-300 dark:border-slate-700 text-slate-500 dark:text-slate-400 hover:border-slate-500 transition"
    >
      {showScoreBreakdown ? 'Hide' : 'Scores ▾'}
    </button>
  </div>

  <!-- Score breakdown panel -->
  {#if showScoreBreakdown}
    <div class="w-full mt-2 pt-3 border-t border-slate-100 dark:border-slate-800 grid grid-cols-2 sm:grid-cols-4 gap-3">
      {#each Object.entries(scores).sort((a, b) => b[1] - a[1]) as [r, s]}
        {@const ri = REGIME_DISPLAY[r] ?? { label: r, color: 'slate' }}
        {@const isWinner = r === regime}
        <div class="flex flex-col items-center p-2 rounded-xl border {isWinner ? `border-${ri.color}-500 bg-${ri.color}-950/20` : 'border-slate-200 dark:border-slate-800'} text-center">
          <span class="text-[10px] font-bold uppercase tracking-wider {isWinner ? `text-${ri.color}-400` : 'text-slate-500'}">{ri.label}</span>
          <span class="text-lg font-black font-mono {isWinner ? `text-${ri.color}-300` : 'text-slate-400'}">{s > 0 ? '+' : ''}{s.toFixed(0)}</span>
          {#if isWinner}<span class="text-[9px] text-slate-500 mt-0.5">▲ ACTIVE</span>{/if}
        </div>
      {/each}
    </div>
  {/if}
</div>
