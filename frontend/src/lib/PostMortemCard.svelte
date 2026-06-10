<script lang="ts">
  import type { ClosurePostMortem } from './api';
  import { formatDollar, formatDate, formatPct } from './formatters';
  import Badge from './ui/Badge.svelte';

  let { postMortem }: { postMortem: ClosurePostMortem } = $props();

  const EXIT_LABELS: Record<string, string> = {
    PROFIT_TARGET: 'Profit Target',
    LOSS_LIMIT:    'Loss Limit',
    TIME_RULE:     'Time Rule',
    CATALYST_RULE: 'Catalyst Rule',
    MANUAL:        'Manual',
  };

  const outcomeVariant = $derived(
    postMortem.outcome === 'WIN' ? 'success' :
    postMortem.outcome === 'LOSS' ? 'danger' : 'neutral'
  ) as 'success' | 'danger' | 'neutral';

  const cardBorder = $derived(
    postMortem.outcome === 'WIN'  ? 'border-ctp-green glow-green' :
    postMortem.outcome === 'LOSS' ? 'border-ctp-red glow-red' :
    'border-ctp-surface0'
  );

  const headerBg = $derived(
    postMortem.outcome === 'WIN'  ? 'bg-ctp-green/10 border-ctp-green/20' :
    postMortem.outcome === 'LOSS' ? 'bg-ctp-red/10 border-ctp-red/20' :
    'bg-ctp-surface0/50 border-ctp-surface0'
  );

  const pnlClass = $derived(
    postMortem.realized_pnl >= 0 ? 'text-ctp-green' : 'text-ctp-red'
  );
</script>

<article class="carbon-card overflow-hidden transition {cardBorder}">
  <div class="p-4 border-b {headerBg}">
    <div class="flex justify-between items-start gap-3">
      <div class="min-w-0">
        <p class="text-xs font-bold text-ctp-overlay0 uppercase tracking-wider mb-0.5 truncate">
          {postMortem.playbook_id ?? 'Unknown Playbook'}
          {#if postMortem.playbook_version}<span class="font-normal"> v{postMortem.playbook_version}</span>{/if}
        </p>
        <p class="text-xs text-ctp-subtext0">Closed {formatDate(postMortem.exit_date)}</p>
      </div>
      <Badge label={postMortem.outcome} variant={outcomeVariant} />
    </div>
  </div>

  <div class="p-4 space-y-3">
    <div class="grid grid-cols-3 gap-3">
      <div>
        <span class="text-xs font-semibold text-ctp-overlay0 uppercase block mb-0.5">P&L</span>
        <span class="text-sm font-black carbon-mono {pnlClass}">
          {postMortem.realized_pnl >= 0 ? '+' : ''}{formatDollar(postMortem.realized_pnl)}
        </span>
      </div>
      <div>
        <span class="text-xs font-semibold text-ctp-overlay0 uppercase block mb-0.5">Move</span>
        <span class="text-sm font-black carbon-mono text-ctp-text">
          {postMortem.actual_underlying_move_pct >= 0 ? '+' : ''}{formatPct(postMortem.actual_underlying_move_pct)}
        </span>
      </div>
      <div>
        <span class="text-xs font-semibold text-ctp-overlay0 uppercase block mb-0.5">Exit</span>
        <span class="text-xs font-semibold text-ctp-text leading-tight">
          {EXIT_LABELS[postMortem.exit_trigger] ?? postMortem.exit_trigger}
        </span>
      </div>
    </div>

    {#if postMortem.lesson_tags.length > 0}
      <div class="flex flex-wrap gap-1.5">
        {#each postMortem.lesson_tags as tag}
          <Badge label={tag} variant="indigo" />
        {/each}
      </div>
    {/if}

    {#if postMortem.user_override_logged}
      <Badge label="Override logged" variant="warning" />
    {/if}
  </div>
</article>
