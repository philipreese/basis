<script lang="ts">
  import type { BookSummary } from './api';
  import { checklistMet, checklistRows, shareVerdictLabel } from './bookMetrics';
  import { formatDollar } from './formatters';

  // #1133: Home's at-a-glance card for one share book — what it holds, its
  // control state and how far it is along the ADR-0006 stage-1 bar. Read-only:
  // halt/resume for the book lives on its Books card and in the attention
  // block above. Every number here is server data; a missing mark or an empty
  // holdings list says so rather than rendering a zero.
  let { book, onOpen }: { book: BookSummary; onOpen?: () => void } = $props();

  const holdings = $derived(book.share_holdings ?? []);
  const holdingsValue = $derived(
    holdings.length > 0 && holdings.every(h => h.value !== null)
      ? holdings.reduce((sum, h) => sum + (h.value ?? 0), 0)
      : null,
  );
  const stage1Rows = $derived(checklistRows(book.stage1_entry_bar.conditions));
  const stage1Met = $derived(checklistMet(book.stage1_entry_bar.conditions));
  const nextStep = $derived(stage1Rows.find(r => r.status !== 'ok') ?? null);
  const halted = $derived(book.control_state !== 'ACTIVE');
</script>

<article class="carbon-card p-4 space-y-3" data-testid="home-share-{book.id}">
  <div class="flex items-baseline justify-between gap-2">
    <button type="button" class="min-w-0 text-left font-bold text-ctp-text hover:underline"
            onclick={onOpen} data-testid="home-share-{book.id}-open">
      {book.id} · <span class="font-semibold">{book.name}</span>
    </button>
    <span class="shrink-0 text-[11px] font-bold carbon-mono {halted ? 'text-ctp-peach' : 'text-ctp-green'}"
          data-testid="home-share-{book.id}-state">
      {halted ? book.control_state.replace(/_/g, ' ') : 'ACTIVE'}
    </span>
  </div>

  <div class="grid grid-cols-2 gap-2 text-xs">
    <div class="min-w-0">
      <div class="text-[11px] text-ctp-overlay0">Holdings value</div>
      <div class="carbon-mono text-ctp-text" data-testid="home-share-{book.id}-value">
        {holdings.length === 0 ? 'none yet' : holdingsValue === null ? 'no mark yet' : formatDollar(holdingsValue)}
      </div>
    </div>
    <div class="min-w-0">
      <div class="text-[11px] text-ctp-overlay0">Yardstick</div>
      <div class="carbon-mono text-ctp-text">{shareVerdictLabel(book)}</div>
    </div>
  </div>

  {#if holdings.length > 0}
    <div class="flex flex-wrap gap-1.5" data-testid="home-share-{book.id}-holdings">
      {#each holdings as h (h.symbol)}
        <span class="px-1.5 py-0.5 rounded text-[11px] carbon-mono bg-ctp-green/15 text-ctp-green">
          {h.symbol} {h.quantity}
        </span>
      {/each}
    </div>
  {/if}

  <div class="space-y-1">
    <div class="flex justify-between text-xs">
      <span class="text-ctp-subtext0">Stage 1 (real money)</span>
      <span class="carbon-mono text-ctp-yellow" data-testid="home-share-{book.id}-stage1">
        {stage1Met} of {stage1Rows.length}
      </span>
    </div>
    <div class="h-1.5 rounded bg-ctp-surface0 overflow-hidden" aria-hidden="true">
      <div class="h-full rounded bg-ctp-yellow"
           style="width: {stage1Rows.length === 0 ? 0 : Math.round((stage1Met / stage1Rows.length) * 100)}%"></div>
    </div>
    {#if nextStep}
      <p class="text-[11px] text-ctp-overlay0">Next: {nextStep.label}</p>
    {/if}
  </div>
</article>
