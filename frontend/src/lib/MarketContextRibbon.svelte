<script lang="ts">
  import { REGIME_DISPLAY } from './api';
  import type { MarketState } from './api';
  import { formatDollar, formatPct } from './formatters';
  import Collapsible from './ui/Collapsible.svelte';

  let { marketState }: { marketState: MarketState } = $props();

  const pillMap: Record<string, string> = {
    emerald: 'bg-emerald-900/30 border-emerald-700/40 text-emerald-300',
    amber:   'bg-amber-900/30 border-amber-700/40 text-amber-300',
    rose:    'bg-rose-900/30 border-rose-700/40 text-rose-300',
    violet:  'bg-violet-900/30 border-violet-700/40 text-violet-300',
    slate:   'bg-slate-800 border-slate-700 text-slate-300',
  };

  // Static maps for score breakdown — avoids dynamic Tailwind class names
  const scoreCardMap: Record<string, { border: string; bg: string; label: string; value: string }> = {
    emerald: { border: 'border-emerald-500/80', bg: 'bg-emerald-950/20', label: 'text-emerald-400', value: 'text-emerald-300' },
    amber:   { border: 'border-amber-500/80',   bg: 'bg-amber-950/20',   label: 'text-amber-400',   value: 'text-amber-300'   },
    rose:    { border: 'border-rose-500/80',     bg: 'bg-rose-950/20',     label: 'text-rose-400',    value: 'text-rose-300'    },
    violet:  { border: 'border-violet-500/80',   bg: 'bg-violet-950/20',   label: 'text-violet-400',  value: 'text-violet-300'  },
    slate:   { border: 'border-slate-600',       bg: 'bg-slate-800/30',   label: 'text-slate-400',   value: 'text-slate-300'   },
  };

  const regime   = $derived(marketState.current_regime);
  const info     = $derived(REGIME_DISPLAY[regime] ?? { label: regime, color: 'slate', description: '' });
  const scores   = $derived(marketState.regime_scores ?? {});
  const pillCls  = $derived(pillMap[info.color] ?? pillMap.slate);
</script>

<div id="layer-b-ribbon" class="mb-6 carbon-card dark:backdrop-blur-md dark:glow-cyan">
  <div class="px-5 py-3 flex flex-wrap gap-4 items-center justify-between text-xs">
    <!-- Regime badge -->
    <div class="flex items-center gap-3">
      <span class="text-[10px] font-bold text-slate-400 uppercase tracking-widest">Market Context</span>
      <span class="px-3 py-1 rounded border font-black tracking-wider uppercase {pillCls}">
        {info.label}
      </span>
      <span class="text-slate-500 dark:text-slate-400 hidden sm:inline">{info.description}</span>
    </div>

    <!-- Telemetry pills -->
    <div class="flex flex-wrap gap-3 items-center carbon-mono">
      <span class="text-slate-600 dark:text-slate-300">SPY <span class="font-bold">{formatDollar(marketState.spy_price)}</span></span>
      <span class="text-slate-400">·</span>
      <span class="text-slate-600 dark:text-slate-300">SMA20 <span class="font-bold">{formatDollar(marketState.spy_sma20 ?? 0)}</span></span>
      <span class="text-slate-400">·</span>
      <span class="text-slate-600 dark:text-slate-300">VIX <span class="font-bold">{(marketState.vix_close ?? 0).toFixed(1)}</span></span>
      <span class="text-slate-400">·</span>
      <span class="{(marketState.spy_daily_return ?? 0) >= 0 ? 'text-emerald-500' : 'text-rose-500'}">
        Day {(marketState.spy_daily_return ?? 0) >= 0 ? '+' : ''}{formatPct(marketState.spy_daily_return, true)}
      </span>
    </div>
  </div>

  <!-- Score breakdown — collapsible -->
  {#if Object.keys(scores).length > 0}
    <div class="border-t border-slate-100 dark:border-slate-800">
      <Collapsible title="Regime score breakdown">
        <div class="grid grid-cols-2 sm:grid-cols-4 gap-3 px-5 pb-4">
          {#each Object.entries(scores).sort((a, b) => b[1] - a[1]) as [r, s]}
            {@const ri = REGIME_DISPLAY[r] ?? { label: r, color: 'slate' }}
            {@const isWinner = r === regime}
            {@const sc = scoreCardMap[ri.color] ?? scoreCardMap.slate}
            <div class="flex flex-col items-center p-2 rounded border text-center
              {isWinner ? `${sc.border} ${sc.bg}` : 'border-slate-200 dark:border-slate-800'}">
              <span class="text-[10px] font-bold uppercase tracking-wider {isWinner ? sc.label : 'text-slate-500'}">
                {ri.label}
              </span>
              <span class="text-lg font-black carbon-mono {isWinner ? sc.value : 'text-slate-400'}">
                {s > 0 ? '+' : ''}{s.toFixed(0)}
              </span>
              {#if isWinner}
                <span class="text-[9px] text-slate-500 mt-0.5">▲ ACTIVE</span>
              {/if}
            </div>
          {/each}
        </div>
      </Collapsible>
    </div>
  {/if}
</div>
