<script lang="ts">
  import type { PortfolioObservation } from './api';
  import { formatDollar, formatDate } from './formatters';
  import Badge from './ui/Badge.svelte';
  import Button from './ui/Button.svelte';

  let {
    observation,
    onClosePosition,
  }: {
    observation: PortfolioObservation;
    onClosePosition?: (positionId: string) => void;
  } = $props();

  type BadgeVariant = 'danger' | 'warning' | 'success' | 'neutral';

  function priorityBadge(priority: string): { variant: BadgeVariant; pulse: boolean } {
    if (priority === 'P1 — CLOSE NOW') return { variant: 'danger',  pulse: true  };
    if (priority.startsWith('P2'))     return { variant: 'warning', pulse: false };
    if (priority === 'P3 — MONITOR')   return { variant: 'neutral', pulse: false };
    return { variant: 'success', pulse: false };
  }
</script>

<section>
  <div class="flex justify-between items-center mb-6">
    <h2 class="text-xl font-bold text-ctp-text tracking-tight flex items-center gap-2">
      Active Positions
    </h2>
  </div>

  {#if observation.scanned_positions.length === 0}
    <div class="carbon-card p-10 text-center">
      <p class="text-ctp-subtext0 font-medium">No open positions.</p>
      <p class="text-ctp-overlay0 text-xs mt-1">
        Go to <strong class="text-ctp-subtext0">Opportunities</strong> to scan for your first trade.
      </p>
    </div>
  {:else}
    <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
      {#each observation.scanned_positions as pos (pos.position_id)}
        {@const isP1 = pos.priority === 'P1 — CLOSE NOW'}
        {@const isP2 = pos.priority.startsWith('P2')}
        {@const isP3 = pos.priority === 'P3 — MONITOR'}
        {@const pb   = priorityBadge(pos.priority)}

        {@const cardBorder = isP1 ? 'border-ctp-red glow-red' :
                             isP2 ? 'border-ctp-yellow glow-yellow' :
                             isP3 ? 'border-ctp-peach' : ''}
        {@const headerBg = isP1 ? 'bg-ctp-red/10 border-ctp-red/20' :
                           isP2 ? 'bg-ctp-yellow/10 border-ctp-yellow/20' :
                           isP3 ? 'bg-ctp-peach/10 border-ctp-peach/20' :
                                  'bg-ctp-surface0/40 border-ctp-surface0'}

        <article class="carbon-card overflow-hidden flex flex-col transition {cardBorder}">
          <div class="p-5 border-b {headerBg}">
            <div class="flex justify-between items-start mb-2">
              <div class="flex items-center gap-2">
                <Badge label={pos.underlying} variant="indigo" />
                <span class="text-xs font-semibold text-ctp-subtext0">
                  {pos.strategy_type.replace(/_/g, ' ')}
                </span>
              </div>
              <span class="{isP1 ? 'animate-pulse' : ''}">
                <Badge label={pos.priority} variant={pb.variant} />
              </span>
            </div>
            <h3 class="text-sm font-bold text-ctp-text mt-1">{pos.action}</h3>
            <p class="text-sm font-medium text-ctp-subtext0 leading-relaxed mt-1">{pos.reason}</p>
            <div class="mt-2.5 px-3 py-2 bg-ctp-crust rounded carbon-mono text-xs text-ctp-overlay1 border border-ctp-surface0">
              {pos.math_detail}
            </div>
          </div>

          {#if isP1 && onClosePosition}
            <div class="px-5 pt-4 pb-2">
              <Button variant="danger" onclick={() => onClosePosition(pos.position_id)}>
                <span class="animate-pulse">Close Position Now →</span>
              </Button>
            </div>
          {/if}

          <div class="p-5 grow space-y-5">
            <div>
              <h4 class="text-xs font-bold text-ctp-overlay0 uppercase tracking-wider mb-2">Option Legs</h4>
              <div class="overflow-x-auto border border-ctp-surface0 rounded">
                <table class="w-full text-left border-collapse carbon-mono text-xs leading-normal">
                  <thead>
                    <tr class="border-b border-ctp-surface0 bg-ctp-crust text-xs text-ctp-overlay0 uppercase font-bold tracking-wider">
                      <th class="py-2 px-3">Dir</th>
                      <th class="py-2 px-2 text-right">Strike</th>
                      <th class="py-2 px-2">Type</th>
                      <th class="py-2 px-2">Expiry</th>
                      <th class="py-2 px-2 text-right">Δ</th>
                      <th class="py-2 px-2 text-right">Θ</th>
                      <th class="py-2 px-2 text-right">V</th>
                      <th class="py-2 px-3 text-right">Γ</th>
                    </tr>
                  </thead>
                  <tbody>
                    {#each pos.legs as leg}
                      <tr class="border-b border-ctp-surface0/50 last:border-b-0 hover:bg-ctp-surface0/30">
                        <td class="py-2 px-3">
                          <span class="font-bold px-1.5 py-0.5 rounded text-xs
                            {leg.direction === 'LONG'
                              ? 'bg-ctp-blue/15 text-ctp-blue border border-ctp-blue/30'
                              : 'bg-ctp-red/15 text-ctp-red border border-ctp-red/30'}">
                            {leg.direction}
                          </span>
                        </td>
                        <td class="py-2 px-2 text-right font-bold text-ctp-text">{leg.strike}</td>
                        <td class="py-2 px-2 font-semibold text-ctp-subtext0">{leg.option_type}</td>
                        <td class="py-2 px-2 text-ctp-overlay1">{formatDate(leg.expiration)}</td>
                        <td class="py-2 px-2 text-right text-ctp-subtext1">{leg.delta.toFixed(2)}</td>
                        <td class="py-2 px-2 text-right text-ctp-subtext1">{leg.theta.toFixed(2)}</td>
                        <td class="py-2 px-2 text-right text-ctp-subtext1">{leg.vega.toFixed(2)}</td>
                        <td class="py-2 px-3 text-right text-ctp-subtext1">{(leg.gamma || 0.0).toFixed(3)}</td>
                      </tr>
                    {/each}
                  </tbody>
                </table>
              </div>
            </div>

            <div class="grid grid-cols-3 gap-3 bg-ctp-crust p-4 rounded border border-ctp-surface0">
              <div>
                <span class="text-xs font-bold text-ctp-overlay0 uppercase tracking-wider block mb-1">Premium / Share</span>
                <span class="text-sm font-bold carbon-mono text-ctp-text">{formatDollar(pos.entry_premium)}</span>
                <span class="text-xs text-ctp-overlay0 block uppercase">
                  {pos.legs[0]?.direction === 'LONG' ? 'Debit' : 'Credit'}
                </span>
              </div>
              <div>
                <span class="text-xs font-bold text-ctp-overlay0 uppercase tracking-wider block mb-1">Total Cost</span>
                <span class="text-sm font-bold carbon-mono text-ctp-mauve">
                  {formatDollar(pos.entry_premium * 100 * pos.contracts)}
                </span>
                <span class="text-xs text-ctp-overlay0 block">×100 × {pos.contracts} contracts</span>
              </div>
              <div>
                <span class="text-xs font-bold text-ctp-overlay0 uppercase tracking-wider block mb-1">Current Value</span>
                <span class="text-sm font-bold carbon-mono text-ctp-text">
                  {formatDollar(pos.current_value_per_share * 100 * pos.contracts)}
                </span>
                <span class="text-xs text-ctp-overlay0 block">{formatDollar(pos.current_value_per_share)} / share</span>
              </div>
            </div>

            <div class="grid grid-cols-2 gap-4">
              <div class="p-4 rounded border border-ctp-surface0 bg-ctp-mantle">
                <span class="text-xs font-bold text-ctp-overlay0 uppercase tracking-wider block mb-1">Max Profit</span>
                <span class="text-base font-bold carbon-mono text-ctp-green">
                  {pos.max_profit === 999999 ? 'Unlimited' : formatDollar(pos.max_profit * 100 * pos.contracts)}
                </span>
                <span class="text-xs text-ctp-overlay0 block">
                  {pos.max_profit === 999999 ? 'Unlimited' : `${formatDollar(pos.max_profit)} / share`}
                </span>
              </div>
              <div class="p-4 rounded border border-ctp-surface0 bg-ctp-mantle">
                <span class="text-xs font-bold text-ctp-overlay0 uppercase tracking-wider block mb-1">Max Loss</span>
                <span class="text-base font-bold carbon-mono text-ctp-red">
                  {formatDollar(pos.max_loss * 100 * pos.contracts)}
                </span>
                <span class="text-xs text-ctp-overlay0 block">{formatDollar(pos.max_loss)} / share</span>
              </div>
            </div>
          </div>
        </article>
      {/each}
    </div>
  {/if}
</section>
