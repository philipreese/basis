<script lang="ts">
  import type { PortfolioObservation } from './api';
  import { formatDollar, formatDate } from './formatters';

  let {
    observation,
    onClosePosition,
  }: {
    observation: PortfolioObservation;
    onClosePosition?: (positionId: string) => void;
  } = $props();
</script>

<section>
  <div class="flex justify-between items-center mb-6">
    <h2 class="text-xl font-bold dark:text-white tracking-tight flex items-center gap-2">
      <span>Active Position Scanner</span>
      <span class="px-2 py-0.5 text-xs bg-indigo-100 dark:bg-indigo-900/50 text-indigo-700 dark:text-indigo-400 rounded-full font-semibold">
        Layer A Observation
      </span>
    </h2>
  </div>

  {#if observation.scanned_positions.length === 0}
    <div class="carbon-card p-8 text-center">
      <p class="text-slate-500">No active positions loaded in scanner.</p>
    </div>
  {:else}
    <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
      {#each observation.scanned_positions as pos (pos.position_id)}
        {@const isP1 = pos.priority === 'P1 — CLOSE NOW'}
        {@const isP2 = pos.priority.startsWith('P2')}
        {@const isP3 = pos.priority === 'P3 — MONITOR'}

        {@const cardColorClass = isP1 ? 'border-rose-500 dark:border-rose-700 dark:glow-rose shadow-rose-500/5' : isP2 ? 'border-amber-500 dark:border-amber-700 dark:glow-amber shadow-amber-500/5' : isP3 ? 'border-yellow-500 dark:border-yellow-750 dark:glow-amber shadow-yellow-500/5' : ''}
        {@const headerColorClass = isP1 ? 'bg-rose-50/50 dark:bg-rose-950/15 border-rose-100 dark:border-rose-900/10' : isP2 ? 'bg-amber-50/50 dark:bg-amber-950/15 border-amber-100 dark:border-amber-900/10' : isP3 ? 'bg-yellow-50/50 dark:bg-yellow-950/15 border-yellow-100 dark:border-yellow-900/10' : 'bg-slate-50/50 dark:bg-slate-900/40 border-slate-100 dark:border-slate-850'}

        <article class="carbon-card overflow-hidden flex flex-col transition {cardColorClass}">
          <!-- Card Header -->
          <div class="p-6 border-b {headerColorClass}">
            <div class="flex justify-between items-start mb-2">
              <div>
                <span class="px-2.5 py-1 text-xs font-bold bg-indigo-100 dark:bg-indigo-950 text-indigo-700 dark:text-indigo-300 rounded uppercase">
                  {pos.underlying}
                </span>
                <span class="ml-2 text-sm font-semibold text-slate-600 dark:text-slate-400">
                  {pos.strategy_type.replace(/_/g, ' ')}
                </span>
              </div>
              <span class="px-2.5 py-1 text-[10px] font-black rounded uppercase tracking-wider {isP1 ? 'bg-rose-600 text-white animate-pulse' : isP2 ? 'bg-amber-500 text-white' : isP3 ? 'bg-yellow-500 text-slate-950' : 'bg-emerald-500 text-white'}">
                {pos.priority}
              </span>
            </div>
            <h3 class="text-base font-bold dark:text-white mt-1">{pos.action}</h3>
            <p class="text-xs font-semibold text-slate-600 dark:text-slate-400 leading-relaxed mt-1">{pos.reason}</p>
            <div class="mt-2.5 px-3 py-2 bg-slate-100 dark:bg-slate-950 rounded font-mono text-[10px] text-slate-500 dark:text-slate-400 border border-slate-200/50 dark:border-slate-900">
              {pos.math_detail}
            </div>
          </div>

          <!-- P1 Close Action -->
          {#if isP1 && onClosePosition}
            <div class="px-6 pb-4 pt-4">
              <button
                onclick={() => onClosePosition(pos.position_id)}
                class="w-full py-2.5 text-sm font-black rounded bg-rose-600 hover:bg-rose-700 text-white cursor-pointer transition shadow-sm uppercase tracking-wider animate-pulse"
              >
                Close Position Now →
              </button>
            </div>
          {/if}

          <!-- Option Value Mechanics / Math Display -->
          <div class="p-6 grow space-y-6">
            <!-- Option Legs Breakdown -->
            <div>
              <h4 class="text-[10px] font-bold text-slate-400 uppercase tracking-wider mb-2.5">Option Legs Structure</h4>
              <div class="overflow-x-auto border border-slate-100 dark:border-slate-800/60 rounded">
                <table class="w-full text-left border-collapse carbon-mono text-[11px] leading-normal">
                  <thead>
                    <tr class="border-b border-slate-200 dark:border-slate-800 bg-slate-50/50 dark:bg-slate-900/30 text-[10px] text-slate-400 uppercase font-bold tracking-wider">
                      <th class="py-2 px-3">Dir</th>
                      <th class="py-2 px-2 text-right">Strike</th>
                      <th class="py-2 px-2">Type</th>
                      <th class="py-2 px-2">Expiration</th>
                      <th class="py-2 px-2 text-right">Delta</th>
                      <th class="py-2 px-2 text-right">Theta</th>
                      <th class="py-2 px-2 text-right">Vega</th>
                      <th class="py-2 px-3 text-right">Gamma</th>
                    </tr>
                  </thead>
                  <tbody>
                    {#each pos.legs as leg}
                      <tr class="border-b border-slate-100 dark:border-slate-900/50 last:border-b-0 hover:bg-slate-50/50 dark:hover:bg-slate-900/10">
                        <td class="py-2 px-3">
                          <span class="font-black px-1.5 py-0.5 rounded text-[10px] {leg.direction === 'LONG' ? 'bg-sky-105 text-sky-700 dark:bg-sky-950 dark:text-sky-300' : 'bg-rose-105 text-rose-700 dark:bg-rose-950 dark:text-rose-300'}">
                            {leg.direction}
                          </span>
                        </td>
                        <td class="py-2 px-2 text-right font-bold text-slate-800 dark:text-slate-200">{leg.strike}</td>
                        <td class="py-2 px-2 font-semibold text-slate-650 dark:text-slate-450">{leg.option_type}</td>
                        <td class="py-2 px-2 text-slate-500">{formatDate(leg.expiration)}</td>
                        <td class="py-2 px-2 text-right text-slate-600 dark:text-slate-300">{leg.delta.toFixed(2)}</td>
                        <td class="py-2 px-2 text-right text-slate-600 dark:text-slate-300">{leg.theta.toFixed(2)}</td>
                        <td class="py-2 px-2 text-right text-slate-600 dark:text-slate-300">{leg.vega.toFixed(2)}</td>
                        <td class="py-2 px-3 text-right text-slate-600 dark:text-slate-300">{(leg.gamma || 0.0).toFixed(3)}</td>
                      </tr>
                    {/each}
                  </tbody>
                </table>
              </div>
            </div>

            <!-- Premium Math Grid -->
            <div class="grid grid-cols-3 gap-4 bg-slate-50 dark:bg-slate-950 p-4 rounded border border-slate-200/80 dark:border-slate-900">
              <div>
                <span class="text-[10px] font-bold text-slate-400 uppercase tracking-wider block mb-1">Premium / Share</span>
                <span class="text-sm font-bold carbon-mono dark:text-white">
                  {formatDollar(pos.entry_premium)}
                </span>
                <span class="text-[10px] text-slate-400 block uppercase">{pos.legs[0]?.direction === 'LONG' ? 'Debit' : 'Credit'}</span>
              </div>

              <div>
                <span class="text-[10px] font-bold text-slate-400 uppercase tracking-wider block mb-1">Multiplier Total</span>
                <span class="text-sm font-bold carbon-mono text-indigo-600 dark:text-indigo-400">
                  {formatDollar(pos.entry_premium * 100 * pos.contracts)}
                </span>
                <span class="text-[10px] text-slate-400 block">x100 x {pos.contracts} Contract</span>
              </div>

              <div>
                <span class="text-[10px] font-bold text-slate-400 uppercase tracking-wider block mb-1">Current Value</span>
                <span class="text-sm font-bold carbon-mono dark:text-white">
                  {formatDollar(pos.current_value_per_share * 100 * pos.contracts)}
                </span>
                <span class="text-[10px] text-slate-400 block">{formatDollar(pos.current_value_per_share)} / Share</span>
              </div>
            </div>

            <!-- Calculated Metrics -->
            <div class="grid grid-cols-2 gap-4">
              <div class="p-4 rounded border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900">
                <span class="text-[10px] font-bold text-slate-400 uppercase tracking-wider block mb-1">Max Profit</span>
                <span class="text-base font-bold carbon-mono text-emerald-600 dark:text-emerald-400">
                  {pos.max_profit === 999999 ? 'Unlimited' : formatDollar(pos.max_profit * 100 * pos.contracts)}
                </span>
                <span class="text-[10px] text-slate-400 block">
                  {pos.max_profit === 999999 ? 'Unlimited' : `${formatDollar(pos.max_profit)} / share`}
                </span>
              </div>

              <div class="p-4 rounded border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900">
                <span class="text-[10px] font-bold text-slate-400 uppercase tracking-wider block mb-1">Max Loss</span>
                <span class="text-base font-bold carbon-mono text-rose-600 dark:text-rose-400">
                  {formatDollar(pos.max_loss * 100 * pos.contracts)}
                </span>
                <span class="text-[10px] text-slate-400 block">
                  {formatDollar(pos.max_loss)} / share
                </span>
              </div>
            </div>
          </div>
        </article>
      {/each}
    </div>
  {/if}
</section>
