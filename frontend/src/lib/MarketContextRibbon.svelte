<script lang="ts">
  import { REGIME_DISPLAY } from './api';
  import type { MarketState } from './api';
  import { formatDollar, formatPct } from './formatters';
  import Collapsible from './ui/Collapsible.svelte';

  let { marketState }: { marketState: MarketState } = $props();

  const pillMap: Record<string, string> = {
    emerald: 'bg-ctp-green/15 border-ctp-green/40 text-ctp-green',
    amber:   'bg-ctp-yellow/15 border-ctp-yellow/40 text-ctp-yellow',
    rose:    'bg-ctp-red/15 border-ctp-red/40 text-ctp-red',
    violet:  'bg-ctp-mauve/15 border-ctp-mauve/40 text-ctp-mauve',
    slate:   'bg-ctp-surface0 border-ctp-surface1 text-ctp-subtext0',
  };

  const scoreCardMap: Record<string, { border: string; bg: string; label: string; value: string }> = {
    emerald: { border: 'border-ctp-green/50',   bg: 'bg-ctp-green/10',   label: 'text-ctp-green',   value: 'text-ctp-green'   },
    amber:   { border: 'border-ctp-yellow/50',  bg: 'bg-ctp-yellow/10',  label: 'text-ctp-yellow',  value: 'text-ctp-yellow'  },
    rose:    { border: 'border-ctp-red/50',      bg: 'bg-ctp-red/10',     label: 'text-ctp-red',     value: 'text-ctp-red'     },
    violet:  { border: 'border-ctp-mauve/50',   bg: 'bg-ctp-mauve/10',   label: 'text-ctp-mauve',   value: 'text-ctp-mauve'   },
    slate:   { border: 'border-ctp-surface1',   bg: 'bg-ctp-surface0',   label: 'text-ctp-subtext0',value: 'text-ctp-subtext0' },
  };

  const regime   = $derived(marketState.current_regime);
  const info     = $derived(REGIME_DISPLAY[regime] ?? { label: regime, color: 'slate', description: '' });
  const scores   = $derived(marketState.regime_scores ?? {});
  const pillCls  = $derived(pillMap[info.color] ?? pillMap.slate);
</script>

<div id="layer-b-ribbon" class="mb-6 carbon-card glow-mauve">
  <div class="px-5 py-3 flex flex-wrap gap-4 items-center justify-between text-xs">
    <div class="flex items-center gap-3">
      <span class="text-[10px] font-bold text-ctp-overlay0 uppercase tracking-widest">Market Context</span>
      <span class="px-3 py-1 rounded border font-black tracking-wider uppercase {pillCls}">
        {info.label}
      </span>
      <span class="text-ctp-subtext0 hidden sm:inline">{info.description}</span>
    </div>

    <div class="flex flex-wrap gap-3 items-center carbon-mono">
      <span class="text-ctp-subtext1">SPY <span class="font-bold text-ctp-text">{formatDollar(marketState.spy_price)}</span></span>
      <span class="text-ctp-surface1">·</span>
      <span class="text-ctp-subtext1">SMA20 <span class="font-bold text-ctp-text">{formatDollar(marketState.spy_sma20 ?? 0)}</span></span>
      <span class="text-ctp-surface1">·</span>
      <span class="text-ctp-subtext1">VIX <span class="font-bold text-ctp-text">{(marketState.vix_close ?? 0).toFixed(1)}</span></span>
      <span class="text-ctp-surface1">·</span>
      <span class="{(marketState.spy_daily_return ?? 0) >= 0 ? 'text-ctp-green' : 'text-ctp-red'}">
        Day {(marketState.spy_daily_return ?? 0) >= 0 ? '+' : ''}{formatPct(marketState.spy_daily_return, true)}
      </span>
    </div>
  </div>

  {#if Object.keys(scores).length > 0}
    <div class="border-t border-ctp-surface0">
      <Collapsible title="Regime score breakdown">
        <div class="grid grid-cols-2 sm:grid-cols-4 gap-3 px-5 pb-4">
          {#each Object.entries(scores).sort((a, b) => b[1] - a[1]) as [r, s]}
            {@const ri = REGIME_DISPLAY[r] ?? { label: r, color: 'slate' }}
            {@const isWinner = r === regime}
            {@const sc = scoreCardMap[ri.color] ?? scoreCardMap.slate}
            <div class="flex flex-col items-center p-2 rounded border text-center
              {isWinner ? `${sc.border} ${sc.bg}` : 'border-ctp-surface0'}">
              <span class="text-[10px] font-bold uppercase tracking-wider {isWinner ? sc.label : 'text-ctp-overlay0'}">
                {ri.label}
              </span>
              <span class="text-lg font-black carbon-mono {isWinner ? sc.value : 'text-ctp-subtext0'}">
                {s > 0 ? '+' : ''}{s.toFixed(0)}
              </span>
              {#if isWinner}
                <span class="text-[9px] text-ctp-overlay0 mt-0.5">▲ ACTIVE</span>
              {/if}
            </div>
          {/each}
        </div>
      </Collapsible>
    </div>
  {/if}
</div>
