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
    refreshPositionPrices,
  } from './lib/api';
  import type {
    PortfolioConfig, Position, MarketState, PortfolioObservation,
    OpportunityScanResult, TradeSpecResult,
    ClosurePostMortem, OpportunityRecord, PerformanceDiagnostics,
    ClosePositionRequest,
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
  import Alert                 from './lib/ui/Alert.svelte';
  import Badge                 from './lib/ui/Badge.svelte';
  import Button                from './lib/ui/Button.svelte';
  import MetricCard            from './lib/ui/MetricCard.svelte';
  import FormField             from './lib/ui/FormField.svelte';
  import { formatDollar }      from './lib/formatters';
  import {
    LayoutDashboard, Zap, ChartColumn, SlidersHorizontal,
    Lock, Sun, Moon, RefreshCw,
  } from 'lucide-svelte';

  let config               = $state<PortfolioConfig | null>(null);
  let positions            = $state<Position[]>([]);
  let marketState          = $state<MarketState | null>(null);
  let observation          = $state<PortfolioObservation | null>(null);
  let darkMode             = $state(true);
  let errorMsg             = $state('');
  let successMsg           = $state('');
  let isAcknowledgeReviewed = $state(false);
  let activeTab            = $state<'scanner' | 'opportunities' | 'ledger' | 'settings'>('scanner');

  // Portfolio config form state
  let totalNav                      = $state(10000);
  let broker                        = $state('Charles Schwab');
  let accountType                   = $state('Roth IRA');
  let optionsApproval               = $state('Level 3 — Spreads');
  let executionMode                 = $state<'LIVE' | 'PAPER'>('PAPER');
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
  let selectedSpecResult   = $state<TradeSpecResult | null>(null);
  let selectedPlaybookName = $state('');
  let isLoadingSpec        = $state(false);

  // Sprint 5 state
  let postMortems        = $state<ClosurePostMortem[]>([]);
  let opportunityRecords = $state<OpportunityRecord[]>([]);
  let diagnostics        = $state<PerformanceDiagnostics | null>(null);
  let closingPositionId  = $state<string | null>(null);

  const openPositionCount = $derived(positions.filter(p => p.status === 'OPEN').length);
  const hasP1             = $derived(observation?.scanned_positions.some(p => p.priority === 'P1 — CLOSE NOW') ?? false);

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
      errorMsg = '';
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

      if (config) {
        totalNav                      = config.account.total_nav;
        broker                        = config.account.broker;
        accountType                   = config.account.account_type;
        optionsApproval               = config.account.options_approval;
        executionMode                 = config.account.execution_mode;
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
      errorMsg = 'Failed to load data: ' + (e instanceof Error ? e.message : String(e));
    }
  }

  async function handleSaveConfig(e: Event) {
    e.preventDefault();
    try {
      errorMsg = '';
      successMsg = '';
      const updated: PortfolioConfig = {
        account: { total_nav: totalNav, broker, account_type: accountType, options_approval: optionsApproval, execution_mode: executionMode },
        risk_profile: { max_trade_risk_pct: maxTradeRiskPct, max_trade_risk_dollars: maxTradeRiskDollars, max_underlying_concentration_pct: maxUnderlyingConcentrationPct, max_correlated_index_pct: maxCorrelatedIndexPct, minimum_cash_reserve_pct: minimumCashReservePct, max_simultaneous_positions: maxSimultaneousPositions, max_capital_deployed_pct: maxCapitalDeployedPct },
        portfolio_greek_limits: { max_net_delta: maxNetDelta, max_net_vega: maxNetVega, max_net_gamma: maxNetGamma },
      };
      config      = await updatePortfolioConfig(updated);
      observation = await getPortfolioObservation();
      successMsg  = 'Configuration saved.';
      setTimeout(() => (successMsg = ''), 3000);
    } catch (e: unknown) {
      errorMsg = 'Failed to save configuration: ' + (e instanceof Error ? e.message : String(e));
    }
  }

  async function handleSaveMarketState(e: Event) {
    e.preventDefault();
    try {
      errorMsg = '';
      successMsg = '';
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
      successMsg  = 'Market telemetry updated. Regime recomputed.';
      setTimeout(() => (successMsg = ''), 3000);
    } catch (e: unknown) {
      errorMsg = 'Failed to update market state: ' + (e instanceof Error ? e.message : String(e));
    }
  }

  async function handleFetchLive() {
    try {
      errorMsg   = '';
      successMsg = '';
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
      successMsg  = 'Live data fetched from Alpaca. Regime recomputed.';
      setTimeout(() => (successMsg = ''), 4000);
    } catch (e: unknown) {
      errorMsg = 'Live fetch failed: ' + (e instanceof Error ? e.message : String(e)) + '. Check Alpaca API credentials in .env';
    } finally {
      isFetchingLive = false;
    }
  }

  function handleAcknowledge() { isAcknowledgeReviewed = true; }

  async function handleScanOpportunities() {
    try {
      errorMsg       = '';
      opportunityScan = await scanOpportunities();
    } catch (e: unknown) {
      errorMsg = 'Failed to scan: ' + (e instanceof Error ? e.message : String(e));
    }
  }

  async function handleSelectPlaybook(playbookId: string) {
    try {
      errorMsg          = '';
      isLoadingSpec     = true;
      selectedSpecResult = null;
      const card = opportunityScan?.candidates.find(c => c.playbook.id === playbookId);
      selectedPlaybookName = card?.playbook.name ?? playbookId;
      selectedSpecResult   = await getTradeSpec(playbookId);
    } catch (e: unknown) {
      errorMsg = 'Failed to generate trade spec: ' + (e instanceof Error ? e.message : String(e));
    } finally {
      isLoadingSpec = false;
    }
  }

  function handleDismissSpec() {
    selectedSpecResult   = null;
    selectedPlaybookName = '';
  }

  async function handlePositionSaved(pos: Position) {
    positions            = await getPositions();
    observation          = await getPortfolioObservation();
    opportunityRecords   = await getOpportunityLedger();
    selectedSpecResult   = null;
    selectedPlaybookName = '';
    opportunityScan      = null;
    successMsg = `Position ${pos.id} saved.`;
    setTimeout(() => (successMsg = ''), 4000);
  }

  function handleClosePosition(positionId: string) { closingPositionId = positionId; }

  async function handleConfirmClose(positionId: string, req: ClosePositionRequest) {
    const pm         = await closePosition(positionId, req);
    closingPositionId = null;
    postMortems      = [...postMortems, pm];
    positions        = await getPositions();
    observation      = await getPortfolioObservation();
    diagnostics      = await getPerformanceDiagnostics();
    successMsg = `Position closed. Outcome: ${pm.outcome} · P&L: ${pm.realized_pnl >= 0 ? '+' : ''}$${pm.realized_pnl.toFixed(2)}`;
    setTimeout(() => (successMsg = ''), 5000);
  }

  const inputCls = 'w-full mt-1 px-3 py-2 border border-slate-200 dark:border-slate-700 rounded-lg bg-white dark:bg-slate-900 dark:text-slate-100 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-400';
</script>

<div class="min-h-screen bg-slate-50 text-slate-900 dark:bg-slate-950 dark:text-slate-100 flex flex-col transition-colors duration-300">

  <!-- ── Header ───────────────────────────────────────────────────────── -->
  <header class="border-b border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 py-4 px-6 sticky top-0 z-50">
    <div class="max-w-7xl mx-auto flex justify-between items-center">
      <div class="flex items-center gap-3">
        <div class="w-8 h-8 rounded-lg bg-indigo-600 flex items-center justify-center text-white font-black text-base select-none">
          Α
        </div>
        <div>
          <h1 class="text-lg font-bold tracking-tight text-slate-800 dark:text-white">Alpaca Agent Bot</h1>
          <p class="text-xs text-slate-500 dark:text-slate-400">Options Playbook Automation</p>
        </div>

        <!-- Desktop tab bar -->
        <nav class="hidden md:flex items-center gap-1 border-l border-slate-200 dark:border-slate-800 ml-6 pl-6">
          {#each [
            { id: 'scanner',       label: 'Positions'     },
            { id: 'opportunities', label: 'Opportunities' },
            { id: 'ledger',        label: 'Performance'   },
            { id: 'settings',      label: 'Settings'      },
          ] as tab}
            {@const locked = tab.id !== 'scanner' && !isAcknowledgeReviewed}
            <button
              onclick={() => { if (!locked) activeTab = tab.id as typeof activeTab; }}
              disabled={locked}
              class="px-3 py-1.5 text-xs font-bold uppercase tracking-wider transition flex items-center gap-1
                {activeTab === tab.id
                  ? 'text-indigo-600 dark:text-cyan-400 border-b-2 border-indigo-600 dark:border-cyan-400'
                  : locked
                    ? 'text-slate-300 dark:text-slate-600 cursor-not-allowed'
                    : 'text-slate-400 hover:text-slate-900 dark:hover:text-white'}"
            >
              {#if locked}<Lock size={11} strokeWidth={2.5} />{/if}
              {tab.label}
            </button>
          {/each}
        </nav>
      </div>

      <div class="flex items-center gap-3">
        {#if isAcknowledgeReviewed}
          <button
            onclick={() => { isAcknowledgeReviewed = false; activeTab = 'scanner'; }}
            class="px-3 py-1.5 rounded-lg bg-rose-100 hover:bg-rose-200 dark:bg-rose-950 dark:hover:bg-rose-900 text-rose-700 dark:text-rose-300 text-xs font-bold transition flex items-center gap-1.5"
          >
            <Lock size={13} strokeWidth={2.5} /> <span class="hidden sm:inline">Re-lock</span>
          </button>
        {/if}
        <button
          onclick={toggleDarkMode}
          class="p-2 rounded-lg bg-slate-100 dark:bg-slate-800 text-slate-600 dark:text-slate-300 hover:ring-2 hover:ring-slate-300 dark:hover:ring-slate-600 transition"
          aria-label="Toggle theme"
        >
          {#if darkMode}<Sun size={16} strokeWidth={2} />{:else}<Moon size={16} strokeWidth={2} />{/if}
        </button>
      </div>
    </div>
  </header>

  <!-- ── Main ─────────────────────────────────────────────────────────── -->
  <main class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6 grow w-full pb-24 md:pb-8">

    <!-- System messages -->
    {#if errorMsg}
      <div class="mb-5">
        <Alert level="critical" title="Error" message={errorMsg} />
      </div>
    {/if}
    {#if successMsg}
      <div class="mb-5">
        <Alert level="success" title={successMsg} />
      </div>
    {/if}

    <!-- Market Context Ribbon (always visible) -->
    {#if marketState}
      <MarketContextRibbon {marketState} />
    {/if}

    <!-- P1 Critical Action (above the fold) -->
    {#if hasP1 && observation}
      <div class="mb-6">
        <Alert level="critical" title="Critical action required — close positions now">
          <div class="space-y-3 mt-2">
            {#each observation.scanned_positions.filter(p => p.priority === 'P1 — CLOSE NOW') as pos (pos.position_id)}
              <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3 p-3 bg-rose-100/50 dark:bg-rose-950/30 rounded-lg">
                <div>
                  <div class="flex items-center gap-2 mb-0.5">
                    <Badge label={pos.underlying} variant="danger" />
                    <span class="text-xs font-semibold">{pos.strategy_type.replace(/_/g, ' ')}</span>
                  </div>
                  <p class="text-xs font-bold">{pos.action}</p>
                  <p class="text-[11px] opacity-80 mt-0.5">{pos.reason}</p>
                </div>
                <Button variant="danger" onclick={() => handleClosePosition(pos.position_id)}>
                  <span class="animate-pulse">Close Now →</span>
                </Button>
              </div>
            {/each}
          </div>
        </Alert>
      </div>
    {/if}

    <!-- Session Lock Banner -->
    {#if !isAcknowledgeReviewed}
      <div class="mb-8 p-5 rounded-2xl border border-amber-300 bg-amber-50 dark:border-amber-900/40 dark:bg-amber-950/20 flex flex-col md:flex-row justify-between items-start md:items-center gap-4">
        <div>
          <p class="text-sm font-black text-amber-800 dark:text-amber-300 flex items-center gap-2">
            Review your positions before trading
          </p>
          <p class="text-xs text-amber-700/80 dark:text-amber-400/80 mt-1 leading-relaxed max-w-lg">
            Check active positions, Greek limits, and exposure safeguards below.
            Once you've reviewed, unlock the session to access Opportunities, Performance, and Settings.
          </p>
          <!-- Workflow breadcrumb -->
          <p class="text-[10px] text-amber-600/60 dark:text-amber-500/60 mt-2 font-semibold uppercase tracking-wider">
            Step 1 of 3: Review positions → Step 2: Scan opportunities → Step 3: Stage and save
          </p>
        </div>
        <Button variant="primary" onclick={handleAcknowledge}>
          Acknowledge & Unlock →
        </Button>
      </div>
    {/if}

    <!-- ── Scanner Tab ───────────────────────────────────────────────── -->
    {#if activeTab === 'scanner'}
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
          label="Execution Mode"
          value={executionMode}
          subtext="Manual sandbox"
          variant={executionMode === 'LIVE' ? 'danger' : 'warning'}
        />
        <div class="p-4 rounded-xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 flex flex-col justify-between">
          <div>
            <span class="block text-[10px] font-semibold uppercase tracking-wider text-slate-400 dark:text-slate-500 mb-1">
              Open Positions
            </span>
            <span class="block text-xl font-bold carbon-mono text-slate-900 dark:text-slate-100">
              {openPositionCount}
            </span>
          </div>
          {#if isAcknowledgeReviewed}
            <button
              onclick={() => { activeTab = 'settings'; }}
              class="mt-2 text-xs font-bold text-indigo-600 dark:text-indigo-400 hover:underline text-left"
            >
              Edit risk settings →
            </button>
          {:else}
            <span class="mt-2 text-[11px] text-slate-400 italic">Unlock to edit settings</span>
          {/if}
        </div>
      </section>

      <!-- Greeks Panel -->
      {#if observation}
        <GreeksPanel {observation} {maxNetDelta} {maxNetVega} {maxNetGamma} />
        <SafeguardsPanel {observation} />
        <PositionScanner {observation} onClosePosition={handleClosePosition} />
      {:else}
        <div class="carbon-card p-10 text-center text-slate-400">
          Loading position data…
        </div>
      {/if}
    {/if}

    <!-- ── Opportunities Tab ─────────────────────────────────────────── -->
    {#if activeTab === 'opportunities' && isAcknowledgeReviewed}
      <div class="mt-2">
        {#if !opportunityScan}
          <!-- Pre-scan state -->
          <div class="carbon-card p-8 text-center space-y-4">
            <div>
              <h2 class="text-lg font-bold dark:text-white">Find a Trade</h2>
              <p class="text-sm text-slate-500 dark:text-slate-400 mt-1 max-w-md mx-auto">
                Scan all active playbooks against current market conditions to see which strategies are eligible right now.
              </p>
            </div>
            <Button variant="primary" size="lg" onclick={handleScanOpportunities}>
              Scan for Opportunities →
            </Button>
            <p class="text-xs text-slate-400">
              Each playbook is checked against regime, IVR, concentration, and capital gates before appearing here.
            </p>
          </div>
        {:else if selectedSpecResult}
          <TradeSpecCard
            result={selectedSpecResult}
            playbookName={selectedPlaybookName}
            onDismiss={handleDismissSpec}
            onPositionSaved={handlePositionSaved}
          />
        {:else if isLoadingSpec}
          <!-- Spec loading skeleton -->
          <div class="carbon-card p-6 animate-pulse space-y-4">
            <div class="h-4 bg-slate-200 dark:bg-slate-700 rounded w-48"></div>
            <div class="h-24 bg-slate-100 dark:bg-slate-800 rounded"></div>
            <div class="grid grid-cols-4 gap-3">
              {#each [1, 2, 3, 4] as _}
                <div class="h-16 bg-slate-100 dark:bg-slate-800 rounded"></div>
              {/each}
            </div>
            <div class="h-4 bg-slate-200 dark:bg-slate-700 rounded w-32"></div>
          </div>
        {:else}
          <div class="flex justify-end mb-4">
            <button
              onclick={() => { opportunityScan = null; }}
              class="text-xs font-semibold text-slate-400 hover:text-slate-600 dark:hover:text-slate-200 transition"
            >
              ↺ Re-scan
            </button>
          </div>
          <CandidateCards scanResult={opportunityScan} onSelectPlaybook={handleSelectPlaybook} />
        {/if}
      </div>
    {/if}

    <!-- ── Performance Tab ──────────────────────────────────────────── -->
    {#if activeTab === 'ledger' && isAcknowledgeReviewed}
      <div class="space-y-8 mt-2">
        {#if diagnostics}
          <PerformanceDashboard {diagnostics} />
        {/if}

        {#if postMortems.length > 0}
          <div class="border-t border-slate-200 dark:border-slate-800 pt-8">
            <h2 class="text-xl font-bold dark:text-white tracking-tight mb-5">Closed Position Post-Mortems</h2>
            <div class="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-3 gap-5">
              {#each postMortems as pm (pm.id)}
                <PostMortemCard postMortem={pm} />
              {/each}
            </div>
          </div>
        {:else}
          <div class="carbon-card p-10 text-center">
            <p class="text-slate-500 font-medium">No closed positions yet.</p>
            <p class="text-slate-400 text-xs mt-1">
              Post-mortems appear here after you close a trade. Each one records outcome, P&L, and what you learned.
            </p>
          </div>
        {/if}

        <div class="border-t border-slate-200 dark:border-slate-800 pt-8">
          <OpportunityLedger records={opportunityRecords} />
        </div>
      </div>
    {/if}

    <!-- ── Settings Tab ──────────────────────────────────────────────── -->
    {#if activeTab === 'settings' && isAcknowledgeReviewed}
      <div class="space-y-6 mt-2">
        <!-- First-time callout (shown when NAV is still at default) -->
        {#if totalNav <= 10000 && broker === 'Charles Schwab'}
          <Alert
            level="info"
            title="First time? Set your account details here."
            message="Enter your real NAV and broker to calibrate the risk engine. Leave Execution Mode as PAPER until you're ready to trade live."
          />
        {/if}

        <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <!-- Portfolio Config -->
          <section class="bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 rounded-2xl p-6 shadow-sm">
            <h2 class="text-base font-bold dark:text-white mb-5">Portfolio Risk & Greek Limits</h2>
            <form onsubmit={handleSaveConfig}>
              <div class="space-y-5">
                <div class="bg-slate-50 dark:bg-slate-950 p-4 rounded-xl border border-slate-200 dark:border-slate-900">
                  <h3 class="font-bold text-xs text-indigo-600 dark:text-cyan-400 uppercase tracking-wider mb-3">Account Details</h3>
                  <div class="space-y-3">
                    <FormField label="Total NAV ($)">
                      <input type="number" bind:value={totalNav} class={inputCls} />
                    </FormField>
                    <FormField label="Broker Name">
                      <input type="text" bind:value={broker} class={inputCls} />
                    </FormField>
                    <FormField label="Execution Mode">
                      <select bind:value={executionMode} class={inputCls}>
                        <option value="PAPER">PAPER — Sandbox (no real funds)</option>
                        <option value="LIVE">LIVE — Real Funds</option>
                      </select>
                    </FormField>
                  </div>
                </div>

                <div class="bg-slate-50 dark:bg-slate-950 p-4 rounded-xl border border-slate-200 dark:border-slate-900">
                  <h3 class="font-bold text-xs text-indigo-600 dark:text-cyan-400 uppercase tracking-wider mb-3">Risk Thresholds</h3>
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

                <div class="bg-slate-50 dark:bg-slate-950 p-4 rounded-xl border border-slate-200 dark:border-slate-900">
                  <h3 class="font-bold text-xs text-indigo-600 dark:text-cyan-400 uppercase tracking-wider mb-3">Greek Limits</h3>
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
          <section class="bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 rounded-2xl p-6 shadow-sm">
            <div class="flex justify-between items-center mb-5">
              <div>
                <h2 class="text-base font-bold dark:text-white">Market Telemetry</h2>
                <p class="text-xs text-slate-400 mt-0.5">Used to compute market regime and playbook eligibility</p>
              </div>
              <Button
                variant="secondary"
                loading={isFetchingLive}
                disabled={isFetchingLive}
                onclick={handleFetchLive}
              >
                <RefreshCw size={13} strokeWidth={2} class={isFetchingLive ? 'animate-spin' : ''} />
                {isFetchingLive ? 'Fetching…' : 'Fetch Live'}
              </Button>
            </div>
            <form onsubmit={handleSaveMarketState} class="space-y-3">
              <div class="grid grid-cols-2 gap-3">
                <FormField label="SPY Price ($)">
                  <input id="input-spy-price" type="number" step="0.01" bind:value={mockSpyPrice} class="{inputCls} carbon-mono" />
                </FormField>
                <FormField label="SPY SMA20 ($)">
                  <input id="input-spy-sma20" type="number" step="0.01" bind:value={mockSpySma20} class="{inputCls} carbon-mono" />
                </FormField>
                <FormField label="VIX Close" hint="CBOE Volatility Index">
                  <input id="input-vix" type="number" step="0.01" bind:value={mockVixClose} class="{inputCls} carbon-mono" />
                </FormField>
                <FormField label="Daily Return (%)" hint="SPY daily return as a decimal">
                  <input id="input-daily-return" type="number" step="0.01" bind:value={mockDailyReturn} class="{inputCls} carbon-mono" />
                </FormField>
              </div>
              <FormField label="IVRs" hint="Format: TICKER:value, e.g. SPY:35,AAPL:60">
                <input id="input-ivrs" type="text" bind:value={mockIvrs} placeholder="SPY:35,AAPL:60" class="{inputCls} carbon-mono" />
              </FormField>
              <FormField label="Catalyst Dates" hint="Upcoming FOMC or earnings dates, e.g. 2026-06-18">
                <input id="input-catalysts" type="text" bind:value={mockCatalysts} placeholder="2026-06-18" class={inputCls} />
              </FormField>
              <div class="flex justify-end pt-2">
                <Button type="submit" variant="secondary">Apply Telemetry</Button>
              </div>
            </form>
          </section>
        </div>
      </div>
    {/if}
  </main>

  <!-- ── Mobile Bottom Tab Bar ────────────────────────────────────────── -->
  <nav class="md:hidden fixed bottom-0 left-0 right-0 z-50 border-t border-slate-200 dark:border-slate-800 bg-white/95 dark:bg-slate-900/95 backdrop-blur-md flex justify-around items-center px-2 py-2 shadow-lg">
    {#each ([
      ['scanner',       'Positions',   false],
      ['opportunities', 'Trade',       true],
      ['ledger',        'Performance', true],
      ['settings',      'Settings',    true],
    ] as const) as [id, label, gated]}
      {@const locked = gated && !isAcknowledgeReviewed}
      <button
        onclick={() => { if (!locked) activeTab = id; }}
        disabled={locked}
        class="flex flex-col items-center gap-0.5 text-[10px] font-bold uppercase transition min-w-0 px-3 py-1
          {activeTab === id ? 'text-indigo-600 dark:text-cyan-400' : locked ? 'text-slate-300 dark:text-slate-600 cursor-not-allowed' : 'text-slate-400'}"
      >
        {#if locked}
          <Lock size={18} strokeWidth={2} />
        {:else if id === 'scanner'}
          <LayoutDashboard size={18} strokeWidth={1.75} />
        {:else if id === 'opportunities'}
          <Zap size={18} strokeWidth={1.75} />
        {:else if id === 'ledger'}
          <ChartColumn size={18} strokeWidth={1.75} />
        {:else}
          <SlidersHorizontal size={18} strokeWidth={1.75} />
        {/if}
        <span>{label}</span>
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
</div>
