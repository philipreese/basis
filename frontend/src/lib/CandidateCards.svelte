<script lang="ts">
  import type { OpportunityScanResult } from './api';
  import { createOpportunityRecord } from './api';
  import { formatDollar, formatDte } from './formatters';
  import Alert from './ui/Alert.svelte';
  import Badge from './ui/Badge.svelte';
  import Button from './ui/Button.svelte';
  import Collapsible from './ui/Collapsible.svelte';
  import Modal from './ui/Modal.svelte';
  import FormField from './ui/FormField.svelte';
  import { IconWarning } from './ui/icons';

  let {
    scanResult,
    onSelectPlaybook,
  }: {
    scanResult: OpportunityScanResult;
    onSelectPlaybook: (playbookId: string) => void;
  } = $props();

  let overrideCard   = $state<typeof scanResult.candidates[0] | null>(null);
  let overrideReason = $state('');

  function startOverride(card: typeof scanResult.candidates[0]) {
    overrideCard   = card;
    overrideReason = '';
  }

  async function confirmOverride() {
    const card = overrideCard;
    if (!card) return;
    const reason = overrideReason.trim();
    try {
      await createOpportunityRecord({
        playbook_id: card.playbook.id,
        playbook_version: card.playbook.version,
        generated_at: new Date().toISOString(),
        accepted: false,
        outcome_if_taken: null,
        bypass_reason: reason
          ? `${reason} (suppressed: ${card.suppressed_reason ?? 'n/a'})`
          : (card.suppressed_reason ?? 'User override'),
      });
    } catch {
      // Non-blocking
    }
    const id    = card.playbook.id;
    overrideCard = null;
    onSelectPlaybook(id);
  }

  const eligible   = $derived(scanResult.candidates.filter(c => c.eligible));
  const suppressed = $derived(scanResult.candidates.filter(c => !c.eligible));

  const strategyLabels: Record<string, string> = {
    IRON_CONDOR:      'Iron Condor',
    BROKEN_WING_BUTTERFLY: 'Broken-Wing Butterfly',
    CALENDAR_SPREAD:  'Calendar Spread',
    BULL_CALL_SPREAD: 'Bull Call Spread',
    BEAR_PUT_SPREAD:  'Bear Put Spread',
    BULL_PUT_SPREAD:  'Bull Put Spread',
    BEAR_CALL_SPREAD: 'Bear Call Spread',
    LONG_STRADDLE:    'Long Straddle',
    LONG_STRANGLE:    'Long Strangle',
  };
</script>

<section class="mb-8">
  <div class="flex items-center gap-3 mb-5">
    <h2 class="text-xl font-bold text-ctp-text tracking-tight">Opportunities</h2>
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
          <article class="carbon-card overflow-hidden flex flex-col border-ctp-mauve/30 glow-mauve">
            <div class="p-5 border-b border-ctp-mauve/20 bg-ctp-mauve/5">
              <div class="flex justify-between items-start mb-2">
                <Badge label={card.playbook.underlying_ticker} variant="indigo" />
                <span class="px-2 py-0.5 text-xs font-black rounded border border-ctp-green/50 text-ctp-green uppercase tracking-wider">
                  ELIGIBLE
                </span>
              </div>
              <h3 class="text-sm font-bold text-ctp-text mt-2 leading-tight">{card.playbook.name}</h3>
              <p class="text-sm text-ctp-subtext0 mt-0.5">
                {strategyLabels[card.playbook.strategy_type] ?? card.playbook.strategy_type}
                · <Badge label={card.playbook.execution_mode} variant="neutral" />
              </p>
            </div>

            {#if p}
              <div class="p-5 grow space-y-3 text-xs">
                <div>
                  <span class="text-xs font-bold text-ctp-overlay0 uppercase tracking-wider block mb-1">Automated Order Specification</span>
                  <div class="bg-ctp-crust rounded p-3 border border-ctp-surface0 carbon-mono text-xs text-ctp-subtext1 leading-relaxed">
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
                    {:else if card.playbook.strategy_type === 'BULL_PUT_SPREAD'}
                      → Sell 1× {p.underlying} Put (Δ −{p.short_leg_delta?.toFixed(2)} — target)<br>
                      → Buy 1× {p.underlying} Put (further OTM wing)<br>
                      → Spread width: {formatDollar(p.spread_width_dollars)}
                    {:else if card.playbook.strategy_type === 'BEAR_CALL_SPREAD'}
                      → Sell 1× {p.underlying} Call (Δ {p.short_leg_delta?.toFixed(2)} — target)<br>
                      → Buy 1× {p.underlying} Call (further OTM wing)<br>
                      → Spread width: {formatDollar(p.spread_width_dollars)}
                    {:else if card.playbook.strategy_type === 'BROKEN_WING_BUTTERFLY'}
                      → Buy 1× {p.underlying} Put (narrow wing above body)<br>
                      → Sell 2× {p.underlying} Put (Δ −{p.short_leg_delta?.toFixed(2)} body)<br>
                      → Buy 1× {p.underlying} Put (2× wing below — skip-strike)<br>
                      → Narrow wing: {formatDollar(p.spread_width_dollars)}
                    {:else if card.playbook.strategy_type === 'CALENDAR_SPREAD'}
                      → Sell 1× {p.underlying} ATM Call (front month)<br>
                      → Buy 1× {p.underlying} ATM Call (one cycle back, same strike)<br>
                      → ATM strike: closest to {formatDollar(p.current_price)}
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
                <div class="text-xs text-ctp-overlay0 border-t border-ctp-surface0 pt-2 carbon-mono">
                  <span class="font-bold text-ctp-subtext0">Derived from:</span>
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
        <p class="text-ctp-subtext1 text-sm font-semibold">No playbooks cleared all entry conditions.</p>
        <p class="text-ctp-overlay0 text-xs mt-1">
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
          <div class="divide-y divide-ctp-surface0">
            {#each suppressed as card (card.playbook.id)}
              <div class="px-5 py-4 flex items-start justify-between gap-4 bg-ctp-base">
                <div class="min-w-0 grow">
                  <div class="flex items-center gap-2 mb-1.5">
                    <span class="text-xs font-bold text-ctp-text leading-tight">{card.playbook.name}</span>
                    <Badge
                      label={strategyLabels[card.playbook.strategy_type] ?? card.playbook.strategy_type}
                      variant="neutral"
                    />
                  </div>
                  <p class="text-sm text-ctp-yellow font-medium leading-snug flex items-start gap-1">
                    <IconWarning size={12} class="shrink-0 mt-0.5" />{card.suppressed_reason}
                  </p>
                </div>
                <button
                  onclick={() => startOverride(card)}
                  class="shrink-0 px-3 py-1.5 text-xs font-bold rounded border border-ctp-surface1
                    text-ctp-subtext0 hover:border-ctp-mauve hover:text-ctp-mauve transition uppercase tracking-wider"
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

{#if overrideCard}
  <Modal title="Override filter" onclose={() => (overrideCard = null)}>
    {#snippet body()}
      <div class="space-y-4">
        <p class="text-sm text-ctp-subtext0">
          <span class="font-bold text-ctp-text">{overrideCard?.playbook.name}</span> was filtered out:
        </p>
        <p class="text-sm text-ctp-yellow flex items-start gap-1.5">
          <IconWarning size={13} class="shrink-0 mt-0.5" />{overrideCard?.suppressed_reason}
        </p>
        <FormField
          label="Why are you overriding this filter?"
          required
          hint="Recorded in the opportunity ledger so you can review the call later."
        >
          <textarea
            bind:value={overrideReason}
            rows="3"
            placeholder="e.g. Catalyst is already priced in — taking the directional view anyway."
            class="w-full mt-1 px-3 py-2 border border-ctp-surface1 rounded-lg bg-ctp-crust text-ctp-text text-sm focus:outline-none focus:ring-2 focus:ring-ctp-mauve"
          ></textarea>
        </FormField>
      </div>
    {/snippet}
    {#snippet footer()}
      <Button variant="secondary" onclick={() => (overrideCard = null)}>Cancel</Button>
      <Button variant="primary" disabled={!overrideReason.trim()} onclick={confirmOverride}>
        Override &amp; Continue →
      </Button>
    {/snippet}
  </Modal>
{/if}
