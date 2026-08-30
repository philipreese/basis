<script lang="ts">
  import type { PortfolioObservation } from './api';
  import { formatDollar, formatDate, formatDte } from './formatters';
  import Badge from './ui/Badge.svelte';
  import Button from './ui/Button.svelte';
  import { IconChevronDown, IconChevronUp } from './ui/icons';

  let {
    observation,
    onClosePosition,
    onRollPosition,
  }: {
    observation: PortfolioObservation;
    onClosePosition?: (positionId: string) => void;
    onRollPosition?: (position: PortfolioObservation['scanned_positions'][number]) => void;
  } = $props();

  type BadgeVariant = 'danger' | 'warning' | 'success' | 'neutral';

  function priorityBadge(priority: string): { variant: BadgeVariant; pulse: boolean } {
    if (priority === 'P1 — CLOSE NOW') return { variant: 'danger',  pulse: true  };
    if (priority.startsWith('P2'))     return { variant: 'warning', pulse: false };
    if (priority === 'P3 — MONITOR')   return { variant: 'neutral', pulse: false };
    return { variant: 'success', pulse: false };
  }

  // Server sends only the expiration date, not a precomputed DTE — every
  // other place that shows DTE (TradeSpecCard, CandidateCards) is handed
  // one by the backend already; this is the one collapsed-row field that
  // has to be derived client-side from a date the row already has.
  function dte(expirationDate: string): number {
    const ms = new Date(expirationDate).getTime() - Date.now();
    return Math.ceil(ms / 86_400_000);
  }

  // Same credit/debit math as observation.py's own P1 profit/loss checks
  // (backend/observation.py:156-192) — CREDIT profit is entry minus
  // current, DEBIT profit is current minus entry.
  function pnl(pos: PortfolioObservation['scanned_positions'][number]): number {
    const perShare = pos.premium_direction === 'CREDIT'
      ? pos.entry_premium - pos.current_value_per_share
      : pos.current_value_per_share - pos.entry_premium;
    return perShare * 100 * pos.contracts;
  }

  // Collapsed-by-default (#890 §4) — the verdict block already surfaces
  // every actionable P1 with its own Close Now button, so this list doesn't
  // need to duplicate that prominence by pre-expanding P1 rows.
  let expanded = $state<Set<string>>(new Set());

  function toggle(positionId: string) {
    const next = new Set(expanded);
    if (next.has(positionId)) next.delete(positionId); else next.add(positionId);
    expanded = next;
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
        Go to <strong class="text-ctp-subtext0">Scan</strong> to scan for your first trade.
      </p>
    </div>
  {:else}
    <div class="space-y-3">
      {#each observation.scanned_positions as pos (pos.position_id)}
        {@const isP1 = pos.priority === 'P1 — CLOSE NOW'}
        {@const isP2 = pos.priority.startsWith('P2')}
        {@const isP3 = pos.priority === 'P3 — MONITOR'}
        {@const pb   = priorityBadge(pos.priority)}
        {@const isOpen = expanded.has(pos.position_id)}

        {@const cardBorder = isP1 ? 'border-ctp-red glow-red' :
                             isP2 ? 'border-ctp-yellow glow-yellow' :
                             isP3 ? 'border-ctp-peach' : ''}
        {@const headerBg = isP1 ? 'bg-ctp-red/10 border-ctp-red/20' :
                           isP2 ? 'bg-ctp-yellow/10 border-ctp-yellow/20' :
                           isP3 ? 'bg-ctp-peach/10 border-ctp-peach/20' :
                                  'bg-ctp-surface0/40 border-ctp-surface0'}

        <article class="carbon-card overflow-hidden transition {cardBorder}">
          <!-- Collapsed row: underlying · strategy · DTE · P&L-or-status · priority
               (#890 §2/§4) — everything an operator needs at a glance, tap to
               expand into the full detail below. -->
          <button type="button" onclick={() => toggle(pos.position_id)}
                  data-testid="position-row-{pos.position_id}"
                  class="w-full flex items-center justify-between gap-2 p-3 text-left {headerBg} border-b {isOpen ? '' : 'border-b-0'}">
            <div class="flex items-center gap-2 min-w-0">
              <Badge label={pos.underlying} variant="indigo" />
              <span class="text-xs font-semibold text-ctp-subtext0 truncate">
                {pos.strategy_type.replace(/_/g, ' ')}
              </span>
              <span class="text-[11px] text-ctp-overlay0 shrink-0">{formatDte(dte(pos.expiration_date))}</span>
            </div>
            <div class="flex items-center gap-2 shrink-0">
              {#if pos.close_in_flight}
                <span class="text-[11px] font-bold text-ctp-overlay0" data-testid="position-row-{pos.position_id}-status">
                  ⏳ in flight
                </span>
              {:else}
                {@const p = pnl(pos)}
                <span class="text-xs font-bold carbon-mono {p >= 0 ? 'text-ctp-green' : 'text-ctp-red'}"
                      data-testid="position-row-{pos.position_id}-pnl">
                  {formatDollar(p)}
                </span>
              {/if}
              <span class={isP1 ? 'animate-pulse' : ''}>
                <Badge label={pos.priority} variant={pb.variant} />
              </span>
              <span class="text-ctp-overlay0">
                {#if isOpen}<IconChevronUp size={12} />{:else}<IconChevronDown size={12} />{/if}
              </span>
            </div>
          </button>

          {#if isOpen}
            <div class="p-5 border-b {headerBg}">
              <h3 class="text-sm font-bold text-ctp-text">{pos.action}</h3>
              <p class="text-sm font-medium text-ctp-subtext0 leading-relaxed mt-1">{pos.reason}</p>
              <div class="mt-2.5 px-3 py-2 bg-ctp-crust rounded carbon-mono text-xs text-ctp-overlay1 border border-ctp-surface0">
                {pos.math_detail}
              </div>
            </div>

            {#if onClosePosition}
              <div class="px-5 pt-4 pb-2 flex flex-wrap items-center gap-2">
                {#if pos.close_in_flight}
                  <!-- #602: a close is already staged/submitted for this position —
                       re-offering the button risks a duplicate exit. -->
                  <span class="text-xs font-bold text-ctp-overlay0" data-testid="close-in-flight-{pos.position_id}">
                    ⏳ Close already in flight{pos.close_in_flight_since ? ` — submitted ${pos.close_in_flight_since}` : ' — staged'}
                  </span>
                {:else if isP1}
                  <Button variant="danger" onclick={() => onClosePosition(pos.position_id)}>
                    <span class="animate-pulse">Close Position Now →</span>
                  </Button>
                {:else}
                  <Button variant="secondary" onclick={() => onClosePosition(pos.position_id)}>
                    Close Position…
                  </Button>
                {/if}
                {#if pos.roll && onRollPosition}
                  {#if pos.roll.eligible}
                    <Button variant="secondary" onclick={() => onRollPosition(pos)}>
                      Roll ↻ ({pos.roll.rolls_used}/{pos.roll.rolls_max} used)
                    </Button>
                  {:else}
                    <span class="text-xs font-bold text-ctp-red" title={pos.roll.reason}>
                      ⛔ Roll cap reached — forced exit
                    </span>
                  {/if}
                {/if}
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
                  <!-- Server-computed premium_direction (#479), not legs[0].direction —
                       iron-condor/BWB leg orderings can put a SHORT leg first and
                       mislabel a credit spread as Debit. -->
                  <span class="text-xs text-ctp-overlay0 block uppercase">
                    {pos.premium_direction === 'DEBIT' ? 'Debit' : 'Credit'}
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
          {/if}
        </article>
      {/each}
    </div>
  {/if}
</section>
