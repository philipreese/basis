<script lang="ts">
  import { onMount } from 'svelte';
  import { getEvidenceVerdict, type EvidenceVerdict } from './api';
  import { formatDollar } from './formatters';
  import { toast } from './ui/snackbar.svelte.ts';

  let report = $state<EvidenceVerdict | null>(null);

  onMount(async () => {
    try {
      report = await getEvidenceVerdict();
    } catch (e: unknown) {
      toast('Failed to load evidence verdict: ' + (e instanceof Error ? e.message : String(e)), 'error');
    }
  });

  const fmt = (v: number | null | undefined) => (v === null || v === undefined ? '—' : formatDollar(v));
  const numCls = (v: number | null | undefined) =>
    v === null || v === undefined ? 'text-ctp-overlay0' : v >= 0 ? 'text-ctp-green' : 'text-ctp-red';

  // #716: 'insufficient' is the expected value for a long time, rendered
  // without apology — it gets the SAME neutral treatment as the other
  // non-terminal verdicts, not a warning color that implies something is
  // wrong.
  const verdictCls = (v: string) =>
    v === 'compelling' ? 'bg-ctp-green/15 text-ctp-green'
    : v === 'failed' ? 'bg-ctp-red/15 text-ctp-red'
    : v === 'promising' ? 'bg-ctp-yellow/15 text-ctp-yellow'
    : 'bg-ctp-surface0 text-ctp-overlay0';
</script>

<section>
  <div class="flex items-baseline justify-between mb-4">
    <h2 class="text-xl font-bold text-ctp-text tracking-tight">Why should I believe this?</h2>
    <p class="text-xs text-ctp-overlay0">
      One pure function over the evidence ledger — composes the Live Gate and null-drill's own judgments, invents nothing new
    </p>
  </div>

  {#if !report}
    <div class="carbon-card p-8 text-center text-ctp-overlay0">Loading evidence verdict…</div>
  {:else}
    <div class="carbon-card p-5" data-testid="evidence-verdict-card">
      <div class="flex items-center justify-between mb-1">
        <span class="px-3 py-1 rounded text-sm font-bold uppercase tracking-wide {verdictCls(report.verdict)}" data-testid="evidence-verdict-badge">
          {report.verdict}
        </span>
        <p class="text-xs text-ctp-overlay0 text-right">
          as of {report.as_of.slice(0, 10)} · evidence through {report.evidence_through.slice(0, 10)} · policy v{report.policy_version}
        </p>
      </div>
      <p class="text-xs text-ctp-subtext0 mt-2 mb-5">{report.verdict_basis}</p>

      <div class="grid grid-cols-2 md:grid-cols-4 gap-4 carbon-mono">
        <div>
          <div class="text-xs text-ctp-overlay0 uppercase tracking-wider">Closed trades</div>
          <div class="text-lg font-bold text-ctp-text">{report.closed_trades}</div>
        </div>
        <div>
          <div class="text-xs text-ctp-overlay0 uppercase tracking-wider">Elapsed months</div>
          <div class="text-lg font-bold text-ctp-text">{report.elapsed_months.toFixed(1)}</div>
        </div>
        <div>
          <div class="text-xs text-ctp-overlay0 uppercase tracking-wider">Books raced</div>
          <div class="text-lg font-bold text-ctp-text">{report.books_raced}</div>
        </div>
        <div>
          <div class="text-xs text-ctp-overlay0 uppercase tracking-wider">Variants tested / abandoned</div>
          <div class="text-lg font-bold text-ctp-text">{report.variants_tested} / {report.variants_abandoned}</div>
        </div>
        <div>
          <div class="text-xs text-ctp-overlay0 uppercase tracking-wider">Expected net profit</div>
          <div class="text-lg font-bold {numCls(report.expected_net_profit)}">{fmt(report.expected_net_profit)}</div>
          {#if report.expected_net_profit_ci_low !== null && report.expected_net_profit_ci_high !== null}
            <div class="text-xs text-ctp-overlay0">95% CI [{fmt(report.expected_net_profit_ci_low)}, {fmt(report.expected_net_profit_ci_high)}]</div>
          {/if}
        </div>
        <div>
          <div class="text-xs text-ctp-overlay0 uppercase tracking-wider">Max drawdown</div>
          <div class="text-lg font-bold text-ctp-red">{fmt(-Math.abs(report.max_drawdown))}</div>
        </div>
        <div>
          <div class="text-xs text-ctp-overlay0 uppercase tracking-wider">Worst observed loss</div>
          <div class="text-lg font-bold text-ctp-red">{fmt(report.worst_observed_loss)}</div>
        </div>
        <div>
          <div class="text-xs text-ctp-overlay0 uppercase tracking-wider">Envelope breaches / anomaly events</div>
          <div class="text-lg font-bold {report.envelope_breaches > 0 ? 'text-ctp-red' : 'text-ctp-text'}">
            {report.envelope_breaches} / {report.anomaly_events}
          </div>
        </div>
      </div>

      {#if report.spy_benchmark_line}
        <p class="text-xs text-ctp-overlay0 mt-5 pt-4 border-t border-ctp-surface0">{report.spy_benchmark_line}</p>
      {/if}
    </div>
  {/if}
</section>
