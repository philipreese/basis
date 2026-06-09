<script lang="ts">
  import type { OpportunityScanResult } from './api';
  import { createOpportunityRecord } from './api';
  import { formatDollar, formatDte } from './formatters';
  import Alert from './ui/Alert.svelte';
  import Badge from './ui/Badge.svelte';
  import Button from './ui/Button.svelte';
  import Collapsible from './ui/Collapsible.svelte';
  import { AlertTriangle } from 'lucide-svelte';

  let {
    scanResult,
    onSelectPlaybook,
  }: {
    scanResult: OpportunityScanResult;
    onSelectPlaybook: (playbookId: string) => void;
  } = $props();

  async function handleOverride(card: typeof scanResult.candidates[0]) {
    try {
      await createOpportunityRecord({
        playbook_id: card.playbook.id,
        playbook_version: card.playbook.version,
        generated_at: new Date().toISOString(),
        accepted: false,
        outcome_if_taken: null,
        bypass_reason: card.suppressed_reason ?? 'User override',
      });
    } catch {
      // Non-blocking
    }
    onSelectPlaybook(card.playbook.id);
  }

  const eligible   = $derived(scanResult.candidates.filter(c => c.eligible));
  const suppressed = $derived(scanResult.candidates.filter(c => !c.eligible));

  const strategyLabels: Record<string, string> = {
    IRON_CONDOR:      'Iron Condor',
    BULL_CALL_SPREAD: 'Bull Call Spread',
    BEAR_PUT_SPREAD:  'Bear Put Spread',
    LONG_STRADDLE:    'Long Straddle',
    LONG_STRANGLE:    'Long Strangle',
  };
</script>

<section class="mb-8">
  <div class="flex items-center gap-3 mb-5">
    <h2 class="text-xl font-bold dark:text-white tracking-tight">Opportunities</h2>
    {#if eligible.length > 0}
      <Badge label="{eligible.length} eligible" variant="violet" />
    {/if}
    {#if suppressed.length > 0}
      <Badge label="{suppressed.length} filtered" variant="neutral" />
    {/if}
  </div>

  {#if scanResult.portfolio_blocked}
    <Alert
      level="critical"
      title="All candidates suppressed — portfolio gate"
      message={scanResult.block_reason ?? undefined}
    >
      <p class="font-semibold uppercase tracking-wider mt-1">
        Resolve the portfolio-level condition before scanning for new entries.
      </p>
    </Alert>

  {:else}
    <!-- Eligible cards -->
    {#if eligible.length > 0}
      <div class="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-3 gap-5 mb-6">
        {#each eligible as card (card.playbook.id)}
          {@const p = card.strike_params}
          <article class="carbon-card dark:bg-slate-950/80 dark:backdrop-blur-md overflow-hidden flex flex-col
            border-violet-200 dark:border-violet-900/40 dark:glow-cyan">
            <div class="p-5 border-b border-violet-100 dark:border-violet-900/20 bg-violet-50/50 dark:bg-violet-950/10">
              <div class="flex justify-between items-start mb-2">
                <Badge label={card.playbook.underlying_ticker} variant="indigo" />
                <span class="px-2 py-0.5 text-[10px] font-black rounded border border-emerald-400 text-emerald-600 dark:border-emerald-600 dark:text-emerald-400 uppercase tracking-wider">
                  ELIGIBLE
                </span>
              </div>
              <h3 class="text-sm font-bold dark:text-white mt-2 leading-tight">{card.playbook.name}</h3>
              <p class="text-xs text-slate-500 dark:text-slate-400 mt-0.5">
                {strategyLabels[card.playbook.strategy_type] ?? card.playbook.strategy_type}
                · <Badge label={card.playbook.execution_mode} variant="neutral" />
              </p>
            </div>

            {#if p}
              <div class="p-5 grow space-y-3 text-xs">
                <div>
                  <span class="text-[10px] font-bold text-slate-400 uppercase tracking-wider block mb-1">Automated Order Specification</span>
                  <div class="bg-slate-50 dark:bg-slate-950 rounded p-3 border border-slate-200/80 dark:border-slate-900 carbon-mono text-[11px] text-slate-600 dark:text-slate-300 leading-relaxed">
                    {#if card.playbook.strategy_type === 'IRON_CONDOR'}
                      → Sell 1× {p.underlying} Put Spread (Δ {p.short_leg_delta?.toFixed(2)} short leg)<br>
                      → Sell 1× {p.underlying} Call Spread (Δ {p.short_leg_delta?.toFixed(2)} short leg)<br>
                      → Wing width: {formatDollar(p.spread_width_dollars)}
                    {:else if card.playbook.strategy_type === 'BULL_CALL_SPREAD'}
                      → Buy 1× {p.underlying} Call (Δ {p.long_leg_delta?.toFixed(2)} — ATM)<br>
                      → Sell 1× {p.underlying} Call (Δ {p.short_leg_delta?.toFixed(2)} — target)<br>
                      → Spread width: {formatDollar(p.spread_width_dollars)}
                    {:else if card.playbook.strategy_type === 'BEAR_PUT_SPREAD'}
                      → Buy 1× {p.underlying} Put (Δ −{p.long_leg_delta?.toFixed(2)} — ATM)<br>
                      → Sell 1× {p.underlying} Put (Δ −{p.short_leg_delta?.toFixed(2)} — target)<br>
                      → Spread width: {formatDollar(p.spread_width_dollars)}
                    {:else if card.playbook.strategy_type === 'LONG_STRADDLE'}
                      → Buy 1× {p.underlying} ATM Call<br>
                      → Buy 1× {p.underlying} ATM Put<br>
                      → ATM strike: closest to {formatDollar(p.current_price)}
                    {:else if card.playbook.strategy_type === 'LONG_STRANGLE'}
                      → Buy 1× {p.underlying} Call (Δ {p.short_leg_delta?.toFixed(2)})<br>
                      → Buy 1× {p.underlying} Put (Δ −{p.short_leg_delta?.toFixed(2)})<br>
                      → OTM on both sides
                    {/if}
                  </div>
                </div>
                <div class="text-[10px] text-slate-400 border-t border-slate-100 dark:border-slate-800 pt-2 carbon-mono">
                  <span class="font-bold text-slate-500">Derived from:</span>
                  Target {formatDte(p.target_dte)}
                  {#if p.short_leg_delta} · Short Δ={p.short_leg_delta.toFixed(2)}{/if}
                  {#if p.spread_width_dollars} · Wing {formatDollar(p.spread_width_dollars)}{/if}
                  {#if p.one_sigma_move} · 1σ={formatDollar(p.one_sigma_move)}{/if}
                </div>
              </div>
            {/if}

            <div class="px-5 pb-5">
              <Button variant="primary" onclick={() => onSelectPlaybook(card.playbook.id)}>
                Generate Trade Spec →
              </Button>
            </div>
          </article>
        {/each}
      </div>
    {:else}
      <div class="carbon-card p-6 mb-6">
        <p class="text-slate-600 dark:text-slate-300 text-sm font-semibold">No playbooks cleared all entry conditions.</p>
        <p class="text-slate-400 text-xs mt-1">
          Review filtered playbooks below — you can override individual filters if you have conviction.
        </p>
      </div>
    {/if}

    <!-- Suppressed playbooks -->
    {#if suppressed.length > 0}
      <div class="carbon-card overflow-hidden">
        <Collapsible
          title="{suppressed.length} filtered playbook{suppressed.length > 1 ? 's' : ''} — entry conditions not met"
          count={suppressed.length}
        >
          <div class="divide-y divide-slate-100 dark:divide-slate-800">
            {#each suppressed as card (card.playbook.id)}
              <div class="px-5 py-4 flex items-start justify-between gap-4 bg-white dark:bg-slate-900">
                <div class="min-w-0 grow">
                  <div class="flex items-center gap-2 mb-1.5">
                    <span class="text-xs font-bold text-slate-700 dark:text-slate-200 leading-tight">{card.playbook.name}</span>
                    <Badge
                      label={strategyLabels[card.playbook.strategy_type] ?? card.playbook.strategy_type}
                      variant="neutral"
                    />
                  </div>
                  <p class="text-xs text-amber-700 dark:text-amber-400 font-medium leading-snug flex items-start gap-1">
                    <AlertTriangle size={12} class="shrink-0 mt-0.5" />{card.suppressed_reason}
                  </p>
                </div>
                <button
                  onclick={() => handleOverride(card)}
                  class="shrink-0 px-3 py-1.5 text-[10px] font-bold rounded border border-slate-300 dark:border-slate-700
                    text-slate-500 dark:text-slate-400 hover:border-violet-400 hover:text-violet-600
                    dark:hover:border-violet-600 dark:hover:text-violet-400 transition uppercase tracking-wider"
                >
                  Override →
                </button>
              </div>
            {/each}
          </div>
        </Collapsible>
      </div>
    {/if}
  {/if}
</section>
