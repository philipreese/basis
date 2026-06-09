<script lang="ts">
  import type { ClosurePostMortem } from './api';
  import { formatDollar, formatDate, formatPct } from './formatters';
  import Badge from './ui/Badge.svelte';

  let { postMortem }: { postMortem: ClosurePostMortem } = $props();

  const EXIT_LABELS: Record<string, string> = {
    PROFIT_TARGET: 'Profit Target',
    LOSS_LIMIT: 'Loss Limit',
    TIME_RULE: 'Time Rule',
    CATALYST_RULE: 'Catalyst Rule',
    MANUAL: 'Manual',
  };

  const outcomeVariant = $derived(
    postMortem.outcome === 'WIN' ? 'success' :
    postMortem.outcome === 'LOSS' ? 'danger' : 'neutral'
  ) as 'success' | 'danger' | 'neutral';

  const cardGlow = $derived(
    postMortem.outcome === 'WIN' ? 'border-emerald-400 dark:border-emerald-600 dark:glow-emerald' :
    postMortem.outcome === 'LOSS' ? 'border-rose-400 dark:border-rose-600 dark:glow-rose' :
    'border-slate-200 dark:border-slate-800'
  );

  const headerBg = $derived(
    postMortem.outcome === 'WIN' ? 'bg-emerald-50/50 dark:bg-emerald-950/15 border-emerald-100 dark:border-emerald-900/20' :
    postMortem.outcome === 'LOSS' ? 'bg-rose-50/50 dark:bg-rose-950/15 border-rose-100 dark:border-rose-900/20' :
    'bg-slate-50/50 dark:bg-slate-900/40 border-slate-100 dark:border-slate-800'
  );

  const pnlClass = $derived(
    postMortem.realized_pnl >= 0
      ? 'text-emerald-600 dark:text-emerald-400'
      : 'text-rose-600 dark:text-rose-400'
  );
</script>

<article class="carbon-card overflow-hidden transition {cardGlow}">
  <div class="p-4 border-b {headerBg}">
    <div class="flex justify-between items-start gap-3">
      <div class="min-w-0">
        <p class="text-[10px] font-bold text-slate-400 uppercase tracking-wider mb-0.5 truncate">
          {postMortem.playbook_id ?? 'Unknown Playbook'}
          {#if postMortem.playbook_version}<span class="font-normal"> v{postMortem.playbook_version}</span>{/if}
        </p>
        <p class="text-xs text-slate-500">Closed {formatDate(postMortem.exit_date)}</p>
      </div>
      <Badge label={postMortem.outcome} variant={outcomeVariant} />
    </div>
  </div>

  <div class="p-4 space-y-3">
    <div class="grid grid-cols-3 gap-3">
      <div>
        <span class="text-[10px] font-semibold text-slate-400 uppercase block mb-0.5">P&L</span>
        <span class="text-sm font-black carbon-mono {pnlClass}">
          {postMortem.realized_pnl >= 0 ? '+' : ''}{formatDollar(postMortem.realized_pnl)}
        </span>
      </div>
      <div>
        <span class="text-[10px] font-semibold text-slate-400 uppercase block mb-0.5">Move</span>
        <span class="text-sm font-black carbon-mono dark:text-white">
          {postMortem.actual_underlying_move_pct >= 0 ? '+' : ''}{formatPct(postMortem.actual_underlying_move_pct)}
        </span>
      </div>
      <div>
        <span class="text-[10px] font-semibold text-slate-400 uppercase block mb-0.5">Exit</span>
        <span class="text-xs font-semibold dark:text-white leading-tight">
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
