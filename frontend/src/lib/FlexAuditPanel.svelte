<script lang="ts">
  import { onMount } from 'svelte';
  import { getAuditEvents, ackFlexDiscrepancies, type AuditEvent } from './api';
  import { toast } from './ui/snackbar.svelte.ts';
  import { startPolling } from './poll';

  // Weekly Flex audit (#544/#571): FLEX_AUDIT is a report-only audit event —
  // there is no dedicated report row, so the latest one's payload IS the
  // report. exec_ids explained here stop re-alerting on the next run but
  // stay listed (as "acknowledged") until a fresh FLEX_AUDIT event supersedes
  // this one — the payload itself is never mutated after the fact.
  interface FlexAuditPayload {
    trades_total: number;
    trades_ours: number;
    missing_order_ref: number;
    discrepancies: string[];
    acknowledged: number;
  }

  let latest = $state<AuditEvent | null>(null);
  let loaded = $state(false);
  let selected = $state<Set<string>>(new Set());
  let reason = $state('');
  let busy = $state(false);
  let ackedThisSession = $state<Set<string>>(new Set());
  let latestEventId = $state<number | null>(null);

  onMount(() => {
    load();
    startPolling(() => {
      if (selected.size === 0) load({ silent: true });
    });
  });

  async function load(opts: { silent?: boolean } = {}) {
    try {
      const events = await getAuditEvents({ event_type: 'FLEX_AUDIT', limit: 1 });
      latest = events[0] ?? null;
      // A fresh run supersedes anything acked against a prior payload — but
      // "fresh" means a NEW event id. Clearing on every load (as before)
      // wiped an operator's just-submitted ack on the next ~30s poll tick,
      // since the poll re-fetches the same still-latest event: the exact
      // re-alert-fatigue failure #544 exists to prevent.
      if (latest && latest.id !== latestEventId) {
        ackedThisSession = new Set();
        latestEventId = latest.id;
      }
    } catch (e: unknown) {
      if (!opts.silent) toast('Failed to load Flex-audit report: ' + (e instanceof Error ? e.message : String(e)), 'error');
    } finally {
      loaded = true;
    }
  }

  const payload = $derived((latest?.payload as unknown as FlexAuditPayload | undefined) ?? null);

  // "UNKNOWN_ORDER_REF <ref> (exec <id>)" | "MISSING_FROM_LEDGER exec <id> ref <ref>" |
  // "FILL_MISMATCH exec <id>: ..." | "NO_ORDER_REFS_IN_EXPORT: ..." (no exec id — not ackable).
  function execIdOf(line: string): string | null {
    const m = line.match(/\bexec\s+([^\s):]+)/);
    return m ? m[1] : null;
  }

  const rows = $derived(
    (payload?.discrepancies ?? []).map(line => ({ line, execId: execIdOf(line) })),
  );
  const openRows = $derived(rows.filter(r => !ackedThisSession.has(r.execId ?? '')));

  function toggle(execId: string) {
    const next = new Set(selected);
    if (next.has(execId)) next.delete(execId); else next.add(execId);
    selected = next;
  }

  async function submitAck(e: SubmitEvent) {
    e.preventDefault();
    if (selected.size === 0 || reason.trim().length < 3) return;
    busy = true;
    try {
      const ids = [...selected];
      const result = await ackFlexDiscrepancies(ids, reason.trim());
      toast(`Acknowledged ${result.acked.length} discrepancy exec_id(s)`, 'success', 5000);
      ackedThisSession = new Set([...ackedThisSession, ...ids]);
      selected = new Set();
      reason = '';
    } catch (err: unknown) {
      toast('Flex-ack failed: ' + (err instanceof Error ? err.message : String(err)), 'error');
    } finally {
      busy = false;
    }
  }
</script>

{#if loaded && payload && openRows.length > 0}
  <section class="carbon-card p-5 border border-ctp-yellow/40" data-testid="flex-audit-panel">
    <div class="flex items-baseline justify-between mb-3">
      <h2 class="text-base font-bold text-ctp-yellow tracking-tight">Flex Audit — open discrepancies</h2>
      <span class="text-xs text-ctp-overlay0 carbon-mono">{latest?.run_at}</span>
    </div>
    <p class="text-xs text-ctp-subtext0 mb-3 max-w-2xl leading-relaxed">
      From the latest weekly Flex audit against the broker fills export. {payload.trades_ours} of {payload.trades_total} trades
      were ours; {payload.acknowledged} already acknowledged. Explaining an exec_id here stops it re-alerting on the next run —
      it does not correct the books (use the reconciliation tools above for that).
    </p>

    <ul class="mb-4 space-y-1" data-testid="flex-audit-discrepancies">
      {#each openRows as row (row.line)}
        <li class="text-xs carbon-mono text-ctp-text bg-ctp-yellow/10 rounded px-2 py-1.5 flex items-center gap-2">
          {#if row.execId}
            <input type="checkbox" checked={selected.has(row.execId)} onchange={() => toggle(row.execId as string)}
                   data-testid="flex-audit-select-{row.execId}" class="accent-ctp-mauve" />
          {:else}
            <span class="w-3.5"></span>
          {/if}
          <span>{row.line}</span>
        </li>
      {/each}
    </ul>

    <form onsubmit={submitAck} class="flex flex-wrap items-end gap-2 p-3 bg-ctp-crust rounded-lg border border-ctp-surface0">
      <label class="flex flex-col gap-1 text-xs font-semibold text-ctp-subtext0 grow">
        Reason ({selected.size} selected)
        <input type="text" bind:value={reason} placeholder="e.g. corrected via cash adjust on 8/19"
               class="px-2 py-1 text-xs border border-ctp-surface1 rounded bg-ctp-crust text-ctp-text focus:outline-none focus:ring-1 focus:ring-ctp-mauve carbon-mono w-full"
               data-testid="flex-audit-reason" />
      </label>
      <button type="submit" disabled={busy || selected.size === 0 || reason.trim().length < 3}
              data-testid="flex-audit-ack-submit"
              class="px-3 py-1.5 text-xs font-bold rounded bg-ctp-mauve text-ctp-crust disabled:opacity-40">
        Acknowledge
      </button>
    </form>
  </section>
{/if}
