<script lang="ts">
  import { onMount } from 'svelte';
  import {
    getPortfolioConfig,
    getPositions,
    updatePortfolioConfig,
    getMarketState,
    updateMarketState,
    fetchLiveMarketData,
    getPortfolioObservation,
    scanOpportunities,
    getTradeSpec,
    getPostMortems,
    getOpportunityLedger,
    getPerformanceDiagnostics,
    closePosition,
    rollPosition,
    refreshPositionPrices,
    getExecutorStatus,
  } from './lib/api';
  import type {
    PortfolioConfig, Position, MarketState, PortfolioObservation,
    OpportunityScanResult, TradeSpecResult,
    ClosurePostMortem, OpportunityRecord, PerformanceDiagnostics,
    ClosePositionRequest, RollPositionRequest, ScannedPosition,
  } from './lib/api';
  import MarketContextRibbon   from './lib/MarketContextRibbon.svelte';
  import GreeksPanel           from './lib/GreeksPanel.svelte';
  import SafeguardsPanel       from './lib/SafeguardsPanel.svelte';
  import PositionScanner       from './lib/PositionScanner.svelte';
  import CandidateCards        from './lib/CandidateCards.svelte';
  import TradeSpecCard         from './lib/TradeSpecCard.svelte';
  import PostMortemCard        from './lib/PostMortemCard.svelte';
  import OpportunityLedger     from './lib/OpportunityLedger.svelte';
  import PerformanceDashboard  from './lib/PerformanceDashboard.svelte';
  import ClosePositionModal    from './lib/ClosePositionModal.svelte';
  import RollPositionModal     from './lib/RollPositionModal.svelte';
  import StatusStrip           from './lib/StatusStrip.svelte';
  import BooksTab              from './lib/BooksTab.svelte';
  import FillQualityCard       from './lib/FillQualityCard.svelte';
  import LeaderboardCard       from './lib/LeaderboardCard.svelte';
  import EvidenceVerdictCard   from './lib/EvidenceVerdictCard.svelte';
  import RegimeHitRateCard     from './lib/RegimeHitRateCard.svelte';
  import Alert                 from './lib/ui/Alert.svelte';
  import Badge                 from './lib/ui/Badge.svelte';
  import Button                from './lib/ui/Button.svelte';
  import MetricCard            from './lib/ui/MetricCard.svelte';
  import FormField             from './lib/ui/FormField.svelte';
  import Snackbar              from './lib/ui/Snackbar.svelte';
  import { toast }             from './lib/ui/snackbar.svelte.ts';
  import { formatDollar }      from './lib/formatters';
  import {
    IconPositions, IconOpportunities, IconPerformance, IconBooks, IconSettings,
    IconLightMode, IconDarkMode, IconRefresh,
  } from './lib/ui/icons';

  let config               = $state<PortfolioConfig | null>(null);
  let positions            = $state<Position[]>([]);
  let marketState          = $state<MarketState | null>(null);
  let observation          = $state<PortfolioObservation | null>(null);
  let darkMode             = $state(true);
  let activeTab            = $state<'overview' | 'scan' | 'books' | 'analysis' | 'settings'>('overview');

  // Portfolio config form state
  let totalNav                      = $state(10000);
  let broker                        = $state('Charles Schwab');
  let accountType                   = $state('Roth IRA');
  let optionsApproval               = $state('Level 3 — Spreads');
  // The REAL trading mode (#361): read from executor status (the backend's
  // IBKR_TRADING_MODE), never a form field — the old editable dropdown could
  // claim LIVE while the executor stayed paper. 'unknown' until a fetch
  // actually succeeds (#475) — falling back to 'paper' while loading or on
  // fetch failure would show a false "safe" badge for a live backend.
  let tradingMode                   = $state<'paper' | 'live' | 'unknown'>('unknown');
  let maxTradeRiskPct               = $state(15.0);
  let maxTradeRiskDollars           = $state(1500);
  let maxUnderlyingConcentrationPct = $state(35.0);
  let maxCorrelatedIndexPct         = $state(50.0);
  let minimumCashReservePct         = $state(15.0);
  let maxSimultaneousPositions      = $state(3);
  let maxCapitalDeployedPct         = $state(85.0);
  let maxNetDelta                   = $state(50.0);
  let maxNetVega                    = $state(100.0);
  let maxNetGamma                   = $state(10.0);

  // Market telemetry form state
  let mockSpyPrice    = $state(758.0);
  let mockSpySma20    = $state(750.0);
  let mockVixClose    = $state(14.5);
  let mockDailyReturn = $state(0.5);
  let mockIvrs        = $state('SPY:25');
  let mockCatalysts   = $state('2026-06-08');
  let isFetchingLive  = $state(false);

  // Layer C state
  let opportunityScan      = $state<OpportunityScanResult | null>(null);
  let scanRanAt            = $state<Date | null>(null);
  let selectedSpecResult   = $state<TradeSpecResult | null>(null);
  let selectedPlaybookName = $state('');
  let isLoadingSpec        = $state(false);

  // Sprint 5 state
  let postMortems        = $state<ClosurePostMortem[]>([]);
  let opportunityRecords = $state<OpportunityRecord[]>([]);
  let diagnostics        = $state<PerformanceDiagnostics | null>(null);
  let closingPositionId  = $state<string | null>(null);
  let rollingPosition    = $state<ScannedPosition | null>(null);

  const openPositionCount = $derived(positions.filter(p => p.status === 'OPEN').length);
  // #602: a P1 already carrying an in-flight close is being handled — it
  // shouldn't re-page the operator or hold the "action required" badge, but
  // staying silent about it entirely would be its own failure — it still
  // shows in the panel below, just without a redundant Close button.
  const p1Positions        = $derived(observation?.scanned_positions.filter(p => p.priority === 'P1 — CLOSE NOW') ?? []);
  const hasP1Actionable    = $derived(p1Positions.some(p => !p.close_in_flight));
  const hasP1              = $derived(hasP1Actionable);

  // Inline validation for free-text telemetry fields
  const ivrsError = $derived.by(() => {
    for (const it of mockIvrs.split(',').map(s => s.trim()).filter(Boolean)) {
      const [k, v] = it.split(':');
      if (!k || v === undefined || v.trim() === '' || isNaN(parseFloat(v))) {
        return `Use TICKER:value pairs, e.g. SPY:35. Check "${it}".`;
      }
    }
    return '';
  });
  const catalystsError = $derived.by(() => {
    // Entries may be bare dates, prefixed ("FOMC:2026-09-16", merged in by
    // the seeded calendar), or underlying-scoped ("EARNINGS:AAPL:2026-10-29",
    // #317) — each just needs a parseable date inside it.
    for (const it of mockCatalysts.split(',').map(s => s.trim()).filter(Boolean)) {
      if (!/\d{4}-\d{2}-\d{2}/.test(it)) return `Each entry needs a YYYY-MM-DD date. Check "${it}".`;
    }
    return '';
  });
  const telemetryValid = $derived(!ivrsError && !catalystsError);

  function scrollToPositions() {
    document.getElementById('position-scanner')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  onMount(async () => {
    applyTheme();
    await loadData();
  });

  function applyTheme() {
    document.documentElement.classList.toggle('dark', darkMode);
  }

  function toggleDarkMode() {
    darkMode = !darkMode;
    applyTheme();
  }

  async function loadData() {
    try {
      config = await getPortfolioConfig();
      try {
        positions = await refreshPositionPrices();
      } catch {
        positions = await getPositions();
      }
      marketState  = await getMarketState();
      observation  = await getPortfolioObservation();
      postMortems  = await getPostMortems();
      opportunityRecords = await getOpportunityLedger();
      diagnostics  = await getPerformanceDiagnostics();
      // Never fabricate PAPER on fetch failure (#475) — a live backend
      // whose status endpoint 500s must read as unknown, not falsely safe.
      try { tradingMode = (await getExecutorStatus()).trading_mode ?? 'paper'; } catch { tradingMode = 'unknown'; }

      if (config) {
        totalNav                      = config.account.total_nav;
        broker                        = config.account.broker;
        accountType                   = config.account.account_type;
        optionsApproval               = config.account.options_approval;
        maxTradeRiskPct               = config.risk_profile.max_trade_risk_pct;
        maxTradeRiskDollars           = config.risk_profile.max_trade_risk_dollars;
        maxUnderlyingConcentrationPct = config.risk_profile.max_underlying_concentration_pct;
        maxCorrelatedIndexPct         = config.risk_profile.max_correlated_index_pct;
        minimumCashReservePct         = config.risk_profile.minimum_cash_reserve_pct;
        maxSimultaneousPositions      = config.risk_profile.max_simultaneous_positions;
        maxCapitalDeployedPct         = config.risk_profile.max_capital_deployed_pct;
        maxNetDelta                   = config.portfolio_greek_limits.max_net_delta;
        maxNetVega                    = config.portfolio_greek_limits.max_net_vega;
        maxNetGamma                   = config.portfolio_greek_limits.max_net_gamma;
      }

      if (marketState) {
        mockSpyPrice    = marketState.spy_price;
        mockSpySma20    = Math.round((marketState.spy_sma20 ?? 750.0) * 100) / 100;
        mockVixClose    = marketState.vix_close ?? 14.5;
        mockDailyReturn = Math.round((marketState.spy_daily_return ?? 0.005) * 100 * 100) / 100;
        const ivrs      = marketState.underlying_ivrs ?? {};
        mockIvrs        = Object.entries(ivrs).map(([k, v]) => `${k}:${v}`).join(',') || 'SPY:25';
        mockCatalysts   = (marketState.catalyst_dates || []).join(', ');
      }
    } catch (e: unknown) {
      toast('Failed to load data: ' + (e instanceof Error ? e.message : String(e)), 'error');
    }
  }

  async function handleSaveConfig(e: Event) {
    e.preventDefault();
    try {
      const updated: PortfolioConfig = {
        account: { total_nav: totalNav, broker, account_type: accountType, options_approval: optionsApproval },
        risk_profile: { max_trade_risk_pct: maxTradeRiskPct, max_trade_risk_dollars: maxTradeRiskDollars, max_underlying_concentration_pct: maxUnderlyingConcentrationPct, max_correlated_index_pct: maxCorrelatedIndexPct, minimum_cash_reserve_pct: minimumCashReservePct, max_simultaneous_positions: maxSimultaneousPositions, max_capital_deployed_pct: maxCapitalDeployedPct },
        portfolio_greek_limits: { max_net_delta: maxNetDelta, max_net_vega: maxNetVega, max_net_gamma: maxNetGamma },
      };
      config      = await updatePortfolioConfig(updated);
      observation = await getPortfolioObservation();
      toast('Configuration saved.', 'success', 3000);
    } catch (e: unknown) {
      toast('Failed to save configuration: ' + (e instanceof Error ? e.message : String(e)), 'error');
    }
  }

  async function handleSaveMarketState(e: Event) {
    e.preventDefault();
    try {
      const cats  = mockCatalysts.split(',').map(s => s.trim()).filter(Boolean);
      const ivrs: Record<string, number> = {};
      for (const pair of mockIvrs.split(',').map(s => s.trim()).filter(Boolean)) {
        const [k, v] = pair.split(':');
        if (k && v) ivrs[k.trim().toUpperCase()] = parseFloat(v.trim());
      }
      const updated = await updateMarketState({
        spy_price: mockSpyPrice, spy_sma20: mockSpySma20, vix_close: mockVixClose,
        underlying_ivrs: ivrs, spy_daily_return: mockDailyReturn / 100, catalyst_dates: cats,
        current_regime: 'CALM_BULL', regime_scores: {},
      });
      marketState = updated;
      observation = await getPortfolioObservation();
      toast('Market telemetry updated. Regime recomputed.', 'success', 3000);
    } catch (e: unknown) {
      toast('Failed to update market state: ' + (e instanceof Error ? e.message : String(e)), 'error');
    }
  }

  async function handleFetchLive() {
    try {
      isFetchingLive = true;
      marketState    = await fetchLiveMarketData();
      mockSpyPrice    = marketState.spy_price;
      mockSpySma20    = Math.round((marketState.spy_sma20 ?? 750.0) * 100) / 100;
      mockVixClose    = marketState.vix_close ?? 14.5;
      mockDailyReturn = Math.round((marketState.spy_daily_return ?? 0.005) * 100 * 100) / 100;
      const ivrs = marketState.underlying_ivrs ?? {};
      mockIvrs   = Object.entries(ivrs).map(([k, v]) => `${k}:${v}`).join(',') || 'SPY:25';
      mockCatalysts = (marketState.catalyst_dates || []).join(', ');
      try { positions = await refreshPositionPrices(); } catch { /* non-critical */ }
      observation = await getPortfolioObservation();
      toast('Live data fetched from IB Gateway. Regime recomputed.', 'success', 4000);
    } catch (e: unknown) {
      toast('Live fetch failed: ' + (e instanceof Error ? e.message : String(e)) + '. Is IB Gateway running?', 'error');
    } finally {
      isFetchingLive = false;
    }
  }

  async function handleScanOpportunities() {
    try {
      opportunityScan = await scanOpportunities();
      scanRanAt = new Date();
    } catch (e: unknown) {
      toast('Failed to scan: ' + (e instanceof Error ? e.message : String(e)), 'error');
    }
  }

  async function handleSelectPlaybook(playbookId: string) {
    try {
      isLoadingSpec     = true;
      selectedSpecResult = null;
      const card = opportunityScan?.candidates.find(c => c.playbook.id === playbookId);
      selectedPlaybookName = card?.playbook.name ?? playbookId;
      selectedSpecResult   = await getTradeSpec(playbookId);
    } catch (e: unknown) {
      toast('Failed to generate trade spec: ' + (e instanceof Error ? e.message : String(e)), 'error');
    } finally {
      isLoadingSpec = false;
    }
  }

  function handleDismissSpec() {
    selectedSpecResult   = null;
    selectedPlaybookName = '';
  }

  function handleClosePosition(positionId: string) { closingPositionId = positionId; }
  function handleRollPosition(pos: ScannedPosition) { rollingPosition = pos; }

  async function handleConfirmRoll(positionId: string, req: RollPositionRequest) {
    // Executor-book positions have real legs at the broker; the backend 409s
    // unless the drift consequence is explicitly acknowledged (#741, mirrors #279).
    const pos = positions.find(p => p.id === positionId);
    if (pos && pos.book_id !== 'B00' && !req.acknowledge_broker_divergence) {
      const ok = window.confirm(
        `${positionId} belongs to executor book ${pos.book_id}. Its legs are REAL at the broker and this roll ` +
        'is bookkeeping-only: no broker order is placed, and reconciliation WILL drift and halt entries globally ' +
        'tonight. Force the bookkeeping roll anyway?'
      );
      if (!ok) return;
      req = { ...req, acknowledge_broker_divergence: true };
    }
    const rolled = await rollPosition(positionId, req);
    rollingPosition = null;
    positions   = await getPositions();
    observation = await getPortfolioObservation();
    toast(`Position rolled (${rolled.rolls}/2). New expiration ${rolled.expiration_date}.`, 'success', 5000);
  }

  async function handleConfirmClose(positionId: string, req: ClosePositionRequest) {
    // Executor-book positions have real legs at the broker; the backend 409s
    // unless the drift consequence is explicitly acknowledged (#279).
    const pos = positions.find(p => p.id === positionId);
    if (pos && pos.book_id !== 'B00' && !req.acknowledge_broker_divergence) {
      const ok = window.confirm(
        `${positionId} belongs to executor book ${pos.book_id}. Its legs are REAL at the broker and this close ` +
        'is bookkeeping-only: reconciliation WILL drift and halt entries globally tonight. ' +
        'The executor closes its own positions. Force the bookkeeping close anyway?'
      );
      if (!ok) return;
      req = { ...req, acknowledge_broker_divergence: true };
    }
    const pm         = await closePosition(positionId, req);
    closingPositionId = null;
    postMortems      = [...postMortems, pm];
    positions        = await getPositions();
    observation      = await getPortfolioObservation();
    diagnostics      = await getPerformanceDiagnostics();
    toast(`Position closed. Outcome: ${pm.outcome} · P&L: ${pm.realized_pnl >= 0 ? '+' : ''}$${pm.realized_pnl.toFixed(2)}`, 'success', 5000);
  }

  const inputCls = 'w-full mt-1 px-3 py-2 border border-ctp-surface1 rounded-lg bg-ctp-crust text-ctp-text text-sm focus:outline-none focus:ring-2 focus:ring-ctp-mauve carbon-mono';
</script>

<div class="min-h-screen bg-ctp-base text-ctp-text flex flex-col">

  <!-- ── Title Bar (VS Code crust style) ──────────────────────────────── -->
  <header class="border-b border-ctp-surface0 bg-ctp-crust py-3 px-6 sticky top-0 z-50">
    <div class="max-w-7xl mx-auto flex justify-between items-center">
      <div class="flex items-center gap-3">
        <button class="px-3 py-1.5 text-xs font-bold flex gap-1 items-center"
                onclick={() => { activeTab = 'overview'; }}>
            <div class="w-7 h-7 rounded bg-ctp-mauve flex items-center justify-center text-ctp-crust font-black text-sm select-none">
            Α
            </div>
            <div class="justify-items-start pl-1">
                <h1 class="text-sm font-bold tracking-tight text-ctp-text">basis</h1>
                <p class="text-xs text-ctp-subtext0 leading-none">autonomous options lab</p>
            </div>
        </button>

        <!-- Desktop tab bar -->
        <nav class="hidden md:flex items-center gap-1 border-l border-ctp-surface0 ml-5 pl-5">
          {#each [
            { id: 'overview', label: 'Overview' },
            { id: 'scan',     label: 'Scan'     },
            { id: 'books',    label: 'Books'    },
            { id: 'analysis', label: 'Analysis' },
            { id: 'settings', label: 'Settings' },
          ] as tab}
            <button
              onclick={() => { activeTab = tab.id as typeof activeTab; }}
              class="px-3 py-1.5 text-xs font-bold uppercase tracking-wider transition flex items-center gap-1
                {activeTab === tab.id
                  ? 'text-ctp-mauve border-b-2 border-ctp-mauve'
                  : 'text-ctp-subtext0 hover:text-ctp-text'}"
            >
              {tab.label}
            </button>
          {/each}
        </nav>
      </div>

      <div class="flex items-center gap-2">
        <button
          onclick={toggleDarkMode}
          class="p-2 rounded bg-ctp-surface0 text-ctp-subtext1 hover:ring-2 hover:ring-ctp-surface1 transition"
          aria-label="Toggle theme"
        >
          {#if darkMode}<IconLightMode size={15} strokeWidth={2} />{:else}<IconDarkMode size={15} strokeWidth={2} />{/if}
        </button>
      </div>
    </div>
  </header>

  <!-- ── Supervision Status Strip (all tabs, #73) ─────────────────────── -->
  <StatusStrip />

  <!-- ── Main ─────────────────────────────────────────────────────────── -->
  <main class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6 grow w-full pb-24 md:pb-8">

    <!-- Market Context Ribbon (always visible) -->
    {#if marketState}
      <MarketContextRibbon {marketState} />
    {/if}

    <!-- P1 Critical Action (above the fold) -->
    {#if p1Positions.length > 0 && observation}
      <div class="mb-6">
        <Alert
          level={hasP1Actionable ? 'critical' : 'warning'}
          title={hasP1Actionable ? 'Critical action required — close positions now' : 'Close already in flight — no action needed'}
        >
          <div class="space-y-3 mt-2">
            {#each p1Positions as pos (pos.position_id)}
              <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3 p-3 rounded-lg {pos.close_in_flight ? 'bg-ctp-surface0' : 'bg-ctp-red/10'}">
                <div>
                  <div class="flex items-center gap-2 mb-0.5">
                    <Badge label={pos.underlying} variant={pos.close_in_flight ? 'neutral' : 'danger'} />
                    <span class="text-xs font-semibold">{pos.strategy_type.replace(/_/g, ' ')}</span>
                  </div>
                  <p class="text-xs font-bold">{pos.action}</p>
                  <p class="text-xs opacity-80 mt-0.5">{pos.reason}</p>
                </div>
                {#if pos.close_in_flight}
                  <span class="text-xs font-bold text-ctp-overlay0 whitespace-nowrap" data-testid="close-in-flight-{pos.position_id}">
                    ⏳ close in flight
                  </span>
                {:else}
                  <Button variant="danger" onclick={() => handleClosePosition(pos.position_id)}>
                    <span class="animate-pulse">Close Now →</span>
                  </Button>
                {/if}
              </div>
            {/each}
          </div>
        </Alert>
      </div>
    {/if}

    <!-- ── Overview Tab ──────────────────────────────────────────────── -->
    {#if activeTab === 'overview'}
      <!-- Account Overview -->
      <section class="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-8">
        <MetricCard
          label="Total NAV"
          value={formatDollar(totalNav)}
          subtext={broker}
        />
        <MetricCard
          label="Account Type"
          value={accountType}
          subtext={optionsApproval}
        />
        <MetricCard
          label="Trading Mode"
          value={tradingMode === 'unknown' ? 'MODE UNKNOWN' : tradingMode.toUpperCase()}
          subtext={tradingMode === 'unknown' ? 'executor status fetch failed — mode unknown' : "from the backend's IBKR_TRADING_MODE"}
          variant={tradingMode === 'unknown' ? 'danger' : tradingMode === 'live' ? 'danger' : 'warning'}
        />
        <div class="carbon-card p-4 flex flex-col justify-between">
          <div>
            <span class="block text-xs font-semibold uppercase tracking-wider text-ctp-overlay0 mb-1">
              Open Positions
            </span>
            <span class="block text-xl font-bold carbon-mono text-ctp-text">
              {openPositionCount}
            </span>
          </div>
          <button
            onclick={() => { activeTab = 'settings'; }}
            class="mt-2 text-xs font-bold text-ctp-mauve hover:underline text-left"
          >
            Edit risk settings →
          </button>
        </div>
      </section>

      <!-- Greeks Panel -->
      {#if observation}
        <GreeksPanel {observation} {maxNetDelta} {maxNetVega} {maxNetGamma} onReducePositions={scrollToPositions} />
        <SafeguardsPanel {observation} />
        <div id="position-scanner" style="scroll-margin-top: 5rem;">
          <PositionScanner {observation} onClosePosition={handleClosePosition} onRollPosition={handleRollPosition} />
        </div>
      {:else}
        <div class="carbon-card p-10 text-center text-ctp-overlay0">
          Loading position data…
        </div>
      {/if}
    {/if}

    <!-- ── Scan Tab (diagnostic, #315) ───────────────────────────────── -->
    {#if activeTab === 'scan'}
      <div class="mt-2">
        {#if !opportunityScan}
          <!-- Pre-scan state -->
          <div class="carbon-card p-8 text-center space-y-4">
            <div>
              <h2 class="text-lg font-bold text-ctp-text">What would tonight's scan do?</h2>
              <p class="text-sm text-ctp-subtext0 mt-1 max-w-md mx-auto">
                Run the playbook eligibility scan against current market conditions — the same gates the
                executor applies nightly. Diagnostic only; the executor stages its own entries.
              </p>
            </div>
            <Button variant="primary" size="lg" onclick={handleScanOpportunities}>
              Run Diagnostic Scan →
            </Button>
            <p class="text-xs text-ctp-overlay0">
              Each playbook is checked against regime, IVR, concentration, and capital gates before appearing here.
            </p>
          </div>
        {:else if selectedSpecResult}
          <TradeSpecCard
            result={selectedSpecResult}
            playbookName={selectedPlaybookName}
            onDismiss={handleDismissSpec}
            diagnostic
          />
        {:else if isLoadingSpec}
          <!-- Spec loading skeleton -->
          <div class="carbon-card p-6 animate-pulse space-y-4">
            <div class="h-4 bg-ctp-surface0 rounded w-48"></div>
            <div class="h-24 bg-ctp-surface0/50 rounded"></div>
            <div class="grid grid-cols-4 gap-3">
              {#each [1, 2, 3, 4] as _}
                <div class="h-16 bg-ctp-surface0/50 rounded"></div>
              {/each}
            </div>
            <div class="h-4 bg-ctp-surface0 rounded w-32"></div>
          </div>
        {:else}
          <div class="flex justify-end items-baseline gap-3 mb-4">
            {#if scanRanAt}
              <!-- Candidates are a snapshot (#356): telemetry may have moved since. -->
              <span class="text-[11px] text-ctp-overlay0">scanned {scanRanAt.toLocaleTimeString()}</span>
            {/if}
            <button
              onclick={handleScanOpportunities}
              class="text-xs font-semibold text-ctp-overlay0 hover:text-ctp-text transition"
            >
              ↺ Re-scan
            </button>
          </div>
          <CandidateCards scanResult={opportunityScan} onSelectPlaybook={handleSelectPlaybook} />
        {/if}
      </div>
    {/if}

    <!-- ── Analysis Tab (#315; reports #242-#244) ─────────────────────── -->
    {#if activeTab === 'analysis'}
      <div class="space-y-8 mt-2">
        <EvidenceVerdictCard />
        <LeaderboardCard />
        <FillQualityCard />
        <RegimeHitRateCard />
        {#if diagnostics}
          <PerformanceDashboard {diagnostics} />
        {/if}

        {#if postMortems.length > 0}
          <div class="border-t border-ctp-surface0 pt-8">
            <h2 class="text-xl font-bold text-ctp-text tracking-tight mb-5">Closed Position Post-Mortems</h2>
            <div class="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-3 gap-5">
              {#each postMortems as pm (pm.id)}
                <PostMortemCard postMortem={pm} />
              {/each}
            </div>
          </div>
        {:else}
          <div class="carbon-card p-10 text-center">
            <p class="text-ctp-subtext0 font-medium">No closed positions yet.</p>
            <p class="text-ctp-overlay0 text-xs mt-1">
              Post-mortems appear here after you close a trade. Each one records outcome, P&L, and what you learned.
            </p>
          </div>
        {/if}

        <div class="border-t border-ctp-surface0 pt-8">
          <OpportunityLedger records={opportunityRecords} />
        </div>
      </div>
    {/if}

    <!-- ── Books Tab (supervision console, #73) ─────────────────────── -->
    {#if activeTab === 'books'}
      <BooksTab onDataChanged={loadData} />
    {/if}

    <!-- ── Settings Tab ──────────────────────────────────────────────── -->
    {#if activeTab === 'settings'}
      <div class="space-y-6 mt-2">
        <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <!-- Portfolio Config -->
          <section class="carbon-card p-6">
            <h2 class="text-base font-bold text-ctp-text mb-5">Portfolio Risk & Greek Limits</h2>
            <form onsubmit={handleSaveConfig}>
              <div class="space-y-5">
                <div class="bg-ctp-crust p-4 rounded-lg border border-ctp-surface0">
                  <h3 class="font-bold text-xs text-ctp-mauve uppercase tracking-wider mb-3">Account Details</h3>
                  <div class="space-y-3">
                    <FormField label="Total NAV ($)">
                      <input type="number" bind:value={totalNav} class={inputCls} />
                    </FormField>
                    <FormField label="Broker Name">
                      <input type="text" bind:value={broker} class={inputCls} />
                    </FormField>
                  </div>
                </div>

                <div class="bg-ctp-crust p-4 rounded-lg border border-ctp-surface0">
                  <h3 class="font-bold text-xs text-ctp-mauve uppercase tracking-wider mb-3">Risk Thresholds</h3>
                  <div class="space-y-3">
                    <div class="grid grid-cols-2 gap-3">
                      <FormField label="Max Risk %">
                        <input type="number" step="0.1" bind:value={maxTradeRiskPct} class={inputCls} />
                      </FormField>
                      <FormField label="Max Risk $">
                        <input type="number" bind:value={maxTradeRiskDollars} class={inputCls} />
                      </FormField>
                    </div>
                    <FormField label="Max Underlying Concentration %">
                      <input type="number" step="0.1" bind:value={maxUnderlyingConcentrationPct} class={inputCls} />
                    </FormField>
                    <FormField label="Min Cash Reserve %">
                      <input type="number" step="0.1" bind:value={minimumCashReservePct} class={inputCls} />
                    </FormField>
                  </div>
                </div>

                <div class="bg-ctp-crust p-4 rounded-lg border border-ctp-surface0">
                  <h3 class="font-bold text-xs text-ctp-mauve uppercase tracking-wider mb-3">Greek Limits</h3>
                  <div class="space-y-3">
                    <FormField label="Max Net Delta (Δ)" hint="Total directional exposure across all positions">
                      <input type="number" bind:value={maxNetDelta} class={inputCls} />
                    </FormField>
                    <FormField label="Max Net Vega (V)" hint="Total volatility sensitivity across all positions">
                      <input type="number" bind:value={maxNetVega} class={inputCls} />
                    </FormField>
                    <FormField label="Max Net Gamma (Γ)" hint="Rate at which delta changes — higher gamma = more risk">
                      <input type="number" bind:value={maxNetGamma} class={inputCls} />
                    </FormField>
                  </div>
                </div>
              </div>
              <div class="flex justify-end mt-5">
                <Button type="submit" variant="primary">Save Configuration</Button>
              </div>
            </form>
          </section>

          <!-- Market Telemetry -->
          <section class="carbon-card p-6">
            <div class="flex justify-between items-center mb-5">
              <div>
                <h2 class="text-base font-bold text-ctp-text">Market Telemetry</h2>
                <p class="text-xs text-ctp-overlay0 mt-0.5">Used to compute market regime and playbook eligibility</p>
              </div>
              <Button
                variant="secondary"
                loading={isFetchingLive}
                disabled={isFetchingLive}
                onclick={handleFetchLive}
              >
                <IconRefresh size={13} strokeWidth={2} class={isFetchingLive ? 'animate-spin' : ''} />
                {isFetchingLive ? 'Fetching…' : 'Fetch Live'}
              </Button>
            </div>
            {#if isFetchingLive}
              <p class="text-xs text-ctp-mauve font-semibold animate-pulse mb-3">Pulling SPY &amp; VIX from IB Gateway…</p>
            {/if}
            <form onsubmit={handleSaveMarketState} class="space-y-3 transition-opacity {isFetchingLive ? 'opacity-50 pointer-events-none' : ''}" aria-busy={isFetchingLive}>
              <div class="grid grid-cols-2 gap-3">
                <FormField label="SPY Price ($)">
                  <input id="input-spy-price" type="number" step="0.01" bind:value={mockSpyPrice} disabled={isFetchingLive} class={inputCls} />
                </FormField>
                <FormField label="SPY SMA20 ($)">
                  <input id="input-spy-sma20" type="number" step="0.01" bind:value={mockSpySma20} disabled={isFetchingLive} class={inputCls} />
                </FormField>
                <FormField label="VIX Close" hint="CBOE Volatility Index">
                  <input id="input-vix" type="number" step="0.01" bind:value={mockVixClose} disabled={isFetchingLive} class={inputCls} />
                </FormField>
                <FormField label="Daily Return (%)" hint="SPY daily return as a decimal">
                  <input id="input-daily-return" type="number" step="0.01" bind:value={mockDailyReturn} disabled={isFetchingLive} class={inputCls} />
                </FormField>
              </div>
              <FormField label="IVRs" hint="Format: TICKER:value, e.g. SPY:35,AAPL:60" error={ivrsError}>
                <input id="input-ivrs" type="text" bind:value={mockIvrs} disabled={isFetchingLive} placeholder="SPY:35,AAPL:60" class={inputCls} />
              </FormField>
              <FormField label="Catalyst Dates" hint="FOMC/CPI merge in automatically. Single-name earnings use EARNINGS:TICKER:date, e.g. EARNINGS:AAPL:2026-10-29 — scoped entries never blackout other books" error={catalystsError}>
                <input id="input-catalysts" type="text" bind:value={mockCatalysts} disabled={isFetchingLive} placeholder="2026-06-18" class={inputCls} />
              </FormField>
              <div class="flex justify-end pt-2">
                <Button type="submit" variant="secondary" disabled={!telemetryValid || isFetchingLive}>Apply Telemetry</Button>
              </div>
            </form>
          </section>
        </div>
      </div>
    {/if}
  </main>

  <!-- ── VS Code Status Bar ────────────────────────────────────────────── -->
  <div class="ctp-statusbar hidden md:flex fixed bottom-0 left-0 right-0 z-50 items-center px-4 gap-4 carbon-mono select-none">
    <span class="font-bold">basis</span>
    <span class="opacity-60">·</span>
    <span class="opacity-80 {tradingMode === 'unknown' ? 'text-ctp-red font-bold' : ''}">
      {tradingMode === 'unknown' ? 'MODE UNKNOWN' : tradingMode.toUpperCase()}
    </span>
    {#if hasP1}
      <span class="opacity-100 font-bold animate-pulse">⚠ P1 ACTION REQUIRED</span>
    {/if}
    <span class="ml-auto opacity-60">{new Date().toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })}</span>
  </div>

  <!-- ── Mobile Bottom Tab Bar ────────────────────────────────────────── -->
  <nav class="md:hidden fixed bottom-0 left-0 right-0 z-50 border-t border-ctp-surface0 bg-ctp-crust/95 backdrop-blur-md flex justify-around items-center px-2 py-2">
    {#each ([
      ['overview', 'Overview'],
      ['scan',     'Scan'],
      ['books',    'Books'],
      ['analysis', 'Analysis'],
      ['settings', 'Settings'],
    ] as const) as [id, label]}
      {@const isActive = activeTab === id}
      <button
        onclick={() => { activeTab = id; }}
        class="flex flex-col items-center gap-0.5 text-xs font-bold uppercase transition min-w-0 px-3 py-1
          {isActive ? 'text-ctp-mauve' : 'text-ctp-overlay0'}"
      >
        {#if id === 'overview'}
          <IconPositions size={18} strokeWidth={1.75} />
        {:else if id === 'scan'}
          <IconOpportunities size={18} strokeWidth={1.75} />
        {:else if id === 'books'}
          <IconBooks size={18} strokeWidth={1.75} />
        {:else if id === 'analysis'}
          <IconPerformance size={18} strokeWidth={1.75} />
        {:else}
          <IconSettings size={18} strokeWidth={1.75} />
        {/if}
        <span>{label}</span>
        {#if isActive}
          <span class="w-1 h-1 rounded-full bg-ctp-mauve"></span>
        {/if}
      </button>
    {/each}
  </nav>

  <!-- ── Close Position Modal ──────────────────────────────────────────── -->
  {#if closingPositionId}
    <ClosePositionModal
      positionId={closingPositionId}
      onConfirm={handleConfirmClose}
      onCancel={() => (closingPositionId = null)}
    />
  {/if}

  <!-- ── Roll Position Modal (#7) ──────────────────────────────────────── -->
  {#if rollingPosition?.roll}
    <RollPositionModal
      position={rollingPosition}
      onConfirm={handleConfirmRoll}
      onCancel={() => (rollingPosition = null)}
    />
  {/if}

  <Snackbar />
</div>
