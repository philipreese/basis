<script lang="ts">
  import { onMount } from 'svelte';
  import { getAttention, type AttentionResponse, type AttentionRowItem } from './api';
  import { startPolling } from './poll';
  import AttentionItem from './AttentionItem.svelte';
  import Collapsible from './ui/Collapsible.svelte';

  // #1133: the practice-book review flags' home. Home shows them as one line
  // that links here; this panel lists every row with its own action (a close
  // stays one tap, never inert text). Same server bucket as Home's line
  // (attention.practice_reviews), so the two can never disagree on the count.
  let { onClosePosition }: { onClosePosition?: (positionId: string) => void } = $props();

  let attention  = $state<AttentionResponse | null>(null);
  let loadFailed = $state(false);

  onMount(() => {
    void load();
    startPolling(() => load());
  });

  async function load() {
    try {
      attention = await getAttention();
      loadFailed = false;
    } catch {
      if (attention === null) loadFailed = true;
    }
  }

  const rows = $derived<AttentionRowItem[]>((attention?.practice_reviews ?? []).map(p => ({
    id: `review:${p.position_id}`,
    title: `${p.underlying} ${p.strategy_type.replace(/_/g, ' ')} — ${p.priority}`,
    detail: p.reason,
    meta: p.book_id,
    action: p.action,
  })));
  const books = $derived([...new Set((attention?.practice_reviews ?? []).map(p => p.book_id))].sort());
</script>

<section class="carbon-card overflow-hidden" data-testid="lab-practice-reviews">
  <div class="p-4 flex items-baseline justify-between gap-2">
    <h3 class="font-bold text-ctp-text">Review flags</h3>
    <span class="text-xs text-ctp-overlay0">
      {#if loadFailed}failed to load{:else if !attention}loading…{:else}{rows.length}, advisory{/if}
    </span>
  </div>
  {#if attention && rows.length === 0}
    <p class="px-4 pb-4 text-xs text-ctp-overlay0">No review flags on the practice books.</p>
  {:else if rows.length > 0}
    <p class="px-4 pb-3 text-xs text-ctp-subtext0">
      Regime-conflict reviews on paper-only practice books ({books.join(', ')}). Shown here, never counted on Home.
    </p>
    <!-- Folded by default so the lab's sections stay on the first screen. -->
    <div class="border-t border-ctp-surface0">
      <Collapsible title="Show the flags" count={rows.length}>
        <div class="divide-y divide-ctp-surface0" data-testid="lab-practice-review-rows">
          {#each rows as item (item.id)}
            <AttentionItem {item} informational {onClosePosition} onResolved={load} />
          {/each}
        </div>
      </Collapsible>
    </div>
  {/if}
</section>
