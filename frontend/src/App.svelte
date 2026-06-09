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
  import MarketContextRibbon from './lib/MarketContextRibbon.svelte';
  import GreeksPanel from './lib/GreeksPanel.svelte';
  import SafeguardsPanel from './lib/SafeguardsPanel.svelte';
  import PositionScanner from './lib/PositionScanner.svelte';
  import CandidateCards from './lib/CandidateCards.svelte';
  import TradeSpecCard from './lib/TradeSpecCard.svelte';
  import PostMortemCard from './lib/PostMortemCard.svelte';
  import OpportunityLedger from './lib/OpportunityLedger.svelte';
  import PerformanceDashboard from './lib/PerformanceDashboard.svelte';
  import ClosePositionModal from './lib/ClosePositionModal.svelte';
  import { formatDollar } from './lib/formatters';

  // Svelte 5 Runes
  let config = $state<PortfolioConfig | null>(null);
  let positions = $state<Position[]>([]);
  let marketState = $state<MarketState | null>(null);
  let observation = $state<PortfolioObservation | null>(null);
  let darkMode = $state(true);
  let errorMsg = $state('');
  let successMsg = $state('');
  let isEditingConfig = $state(false);
  let isAcknowledgeReviewed = $state(false);

  // Portfolio config form state
  let totalNav = $state(10000);
  let broker = $state('Charles Schwab');
  let accountType = $state('Roth IRA');
  let optionsApproval = $state('Level 3 — Spreads');
  let executionMode = $state<'LIVE' | 'PAPER'>('PAPER');
  let maxTradeRiskPct = $state(15.0);
  let maxTradeRiskDollars = $state(1500);
  let maxUnderlyingConcentrationPct = $state(35.0);
  let maxCorrelatedIndexPct = $state(50.0);
  let minimumCashReservePct = $state(15.0);
  let maxSimultaneousPositions = $state(3);
  let maxCapitalDeployedPct = $state(85.0);
  let maxNetDelta = $state(50.0);
  let maxNetVega = $state(100.0);
  let maxNetGamma = $state(10.0);

  // Market telemetry form state
  let mockSpyPrice = $state(758.0);
  let mockSpySma20 = $state(750.0);
  let mockVixClose = $state(14.5);
  let mockDailyReturn = $state(0.5);
  let mockIvrs = $state('SPY:25');
  let mockCatalysts = $state('2026-06-08');
  let isFetchingLive = $state(false);

  // Layer C state
  let opportunityScan = $state<OpportunityScanResult | null>(null);
  let selectedSpecResult = $state<TradeSpecResult | null>(null);
  let selectedPlaybookName = $state('');
  let isLoadingSpec = $state(false);

  // Sprint 5 state
  let postMortems = $state<ClosurePostMortem[]>([]);
  let opportunityRecords = $state<OpportunityRecord[]>([]);
  let diagnostics = $state<PerformanceDiagnostics | null>(null);
  let closingPositionId = $state<string | null>(null);

  onMount(async () => {
    applyTheme();
    await loadData();
  });

  function applyTheme() {
    if (darkMode) {
      document.documentElement.classList.add('dark');
    } else {
      document.documentElement.classList.remove('dark');
    }
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
      } catch (e) {
        positions = await getPositions();
      }
      marketState = await getMarketState();
      observation = await getPortfolioObservation();
      postMortems = await getPostMortems();
      opportunityRecords = await getOpportunityLedger();
      diagnostics = await getPerformanceDiagnostics();

      if (config) {
        totalNav = config.account.total_nav;
        broker = config.account.broker;
        accountType = config.account.account_type;
        optionsApproval = config.account.options_approval;
        executionMode = config.account.execution_mode;
        maxTradeRiskPct = config.risk_profile.max_trade_risk_pct;
        maxTradeRiskDollars = config.risk_profile.max_trade_risk_dollars;
        maxUnderlyingConcentrationPct = config.risk_profile.max_underlying_concentration_pct;
        maxCorrelatedIndexPct = config.risk_profile.max_correlated_index_pct;
        minimumCashReservePct = config.risk_profile.minimum_cash_reserve_pct;
        maxSimultaneousPositions = config.risk_profile.max_simultaneous_positions;
        maxCapitalDeployedPct = config.risk_profile.max_capital_deployed_pct;
        maxNetDelta = config.portfolio_greek_limits.max_net_delta;
        maxNetVega = config.portfolio_greek_limits.max_net_vega;
        maxNetGamma = config.portfolio_greek_limits.max_net_gamma;
      }

      if (marketState) {
        mockSpyPrice = marketState.spy_price;
        mockSpySma20 = Math.round((marketState.spy_sma20 ?? 750.0) * 100) / 100;
        mockVixClose = marketState.vix_close ?? 14.5;
        mockDailyReturn = Math.round((marketState.spy_daily_return ?? 0.005) * 100 * 100) / 100;
        const ivrs = marketState.underlying_ivrs ?? {};
        mockIvrs = Object.entries(ivrs).map(([k, v]) => `${k}:${v}`).join(',') || 'SPY:25';
        mockCatalysts = (marketState.catalyst_dates || []).join(', ');
      }
    } catch (e: any) {
      errorMsg = 'Failed to load database: ' + e.message;
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
      config = await updatePortfolioConfig(updated);
      observation = await getPortfolioObservation();
      successMsg = 'Configuration updated successfully.';
      isEditingConfig = false;
      setTimeout(() => (successMsg = ''), 3000);
    } catch (e: any) {
      errorMsg = 'Failed to save configuration: ' + e.message;
    }
  }

  async function handleSaveMarketState(e: Event) {
    e.preventDefault();
    try {
      errorMsg = '';
      successMsg = '';
      const cats = mockCatalysts.split(',').map(s => s.trim()).filter(s => s !== '');
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
      successMsg = 'Market telemetry updated. Regime recomputed.';
      setTimeout(() => (successMsg = ''), 3000);
    } catch (e: any) {
      errorMsg = 'Failed to update market state: ' + e.message;
    }
  }

  async function handleFetchLive() {
    try {
      errorMsg = '';
      successMsg = '';
      isFetchingLive = true;
      marketState = await fetchLiveMarketData();
      mockSpyPrice = marketState.spy_price;
      mockSpySma20 = Math.round((marketState.spy_sma20 ?? 750.0) * 100) / 100;
      mockVixClose = marketState.vix_close ?? 14.5;
      mockDailyReturn = Math.round((marketState.spy_daily_return ?? 0.005) * 100 * 100) / 100;
      const ivrs = marketState.underlying_ivrs ?? {};
      mockIvrs = Object.entries(ivrs).map(([k, v]) => `${k}:${v}`).join(',') || 'SPY:25';
      mockCatalysts = (marketState.catalyst_dates || []).join(', ');
      
      try {
        positions = await refreshPositionPrices();
      } catch (e: any) {
        console.warn('Failed to refresh live position prices:', e.message);
      }

      observation = await getPortfolioObservation();
      successMsg = 'Live data fetched from Alpaca. Regime recomputed.';
      setTimeout(() => (successMsg = ''), 4000);
    } catch (e: any) {
      errorMsg = 'Live fetch failed: ' + e.message + '. Check Alpaca API credentials in .env';
    } finally {
      isFetchingLive = false;
    }
  }

  function handleAcknowledge() {
    isAcknowledgeReviewed = true;
  }

  async function handleScanOpportunities() {
    try {
      errorMsg = '';
      opportunityScan = await scanOpportunities();
    } catch (e: any) {
      errorMsg = 'Failed to scan opportunities: ' + e.message;
    }
  }

  async function handleSelectPlaybook(playbookId: string) {
    try {
      errorMsg = '';
      isLoadingSpec = true;
      selectedSpecResult = null;
      const card = opportunityScan?.candidates.find(c => c.playbook.id === playbookId);
      selectedPlaybookName = card?.playbook.name ?? playbookId;
      selectedSpecResult = await getTradeSpec(playbookId);
    } catch (e: any) {
      errorMsg = 'Failed to generate trade spec: ' + e.message;
    } finally {
      isLoadingSpec = false;
    }
  }

  function handleDismissSpec() {
    selectedSpecResult = null;
    selectedPlaybookName = '';
  }

  async function handlePositionSaved(pos: Position) {
    positions = await getPositions();
    observation = await getPortfolioObservation();
    opportunityRecords = await getOpportunityLedger();
    selectedSpecResult = null;
    selectedPlaybookName = '';
    opportunityScan = null;
    successMsg = `Position ${pos.id} saved successfully.`;
    setTimeout(() => (successMsg = ''), 4000);
  }

  function handleClosePosition(positionId: string) {
    closingPositionId = positionId;
  }

  async function handleConfirmClose(positionId: string, req: ClosePositionRequest) {
    const pm = await closePosition(positionId, req);
    closingPositionId = null;
    postMortems = [...postMortems, pm];
    positions = await getPositions();
    observation = await getPortfolioObservation();
    diagnostics = await getPerformanceDiagnostics();
    successMsg = `Position closed. Outcome: ${pm.outcome} · P&L: ${pm.realized_pnl >= 0 ? '+' : ''}$${pm.realized_pnl.toFixed(2)}`;
    setTimeout(() => (successMsg = ''), 5000);
  }
</script>

<div class="min-h-screen bg-slate-50 text-slate-900 dark:bg-slate-950 dark:text-slate-100 flex flex-col transition-colors duration-300">
  <!-- Top Navigation Ribbon -->
  <header class="border-b border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 py-4 px-6 sticky top-0 z-50">
    <div class="max-w-7xl mx-auto flex justify-between items-center">
      <div class="flex items-center gap-3">
        <div class="w-8 h-8 rounded-lg bg-indigo-600 flex items-center justify-center text-white font-black">
          Α
        </div>
        <div>
          <h1 class="text-lg font-bold tracking-tight text-slate-800 dark:text-white">Alpaca Agent Bot</h1>
          <p class="text-xs text-slate-500 dark:text-slate-400">Options Playbook Automation Engine</p>
        </div>
      </div>
      <div class="flex items-center gap-4">
        {#if isAcknowledgeReviewed}
          <button
            onclick={() => {
              isAcknowledgeReviewed = false;
              isEditingConfig = false;
            }}
            class="px-3 py-1.5 rounded-lg bg-rose-100 hover:bg-rose-200 dark:bg-rose-950 dark:hover:bg-rose-900 text-rose-700 dark:text-rose-300 text-xs font-bold transition flex items-center gap-1.5"
            aria-label="Re-lock Session"
          >
            🔒 <span class="hidden sm:inline">Re-lock Session</span><span class="sm:hidden">Lock</span>
          </button>
        {/if}
        <button
          onclick={toggleDarkMode}
          class="p-2 rounded-lg bg-slate-100 dark:bg-slate-800 text-slate-600 dark:text-slate-300 hover:ring-2 hover:ring-slate-300 dark:hover:ring-slate-600 transition"
          aria-label="Toggle Theme"
        >
          {#if darkMode}
            ☀️ <span class="text-xs ml-1 hidden sm:inline">Light</span>
          {:else}
            🌙 <span class="text-xs ml-1 hidden sm:inline">Dark</span>
          {/if}
        </button>
      </div>
    </div>
  </header>

  <main class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-4 sm:py-8 grow w-full">
    <!-- Messages -->
    {#if errorMsg}
      <div class="mb-6 p-4 rounded-xl border border-red-200 bg-red-50 text-red-700 dark:border-red-900/30 dark:bg-red-950/20 dark:text-red-400">
        <span class="font-bold">Error:</span> {errorMsg}
      </div>
    {/if}
    {#if successMsg}
      <div class="mb-6 p-4 rounded-xl border border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-900/30 dark:bg-emerald-950/20 dark:text-emerald-400">
        {successMsg}
      </div>
    {/if}

    <!-- Layer B: Market Context Ribbon -->
    {#if marketState}
      <MarketContextRibbon {marketState} />
    {/if}

    <!-- P1 Critical Actions Alert Section (Above the fold, red) -->
    {#if observation && observation.scanned_positions.some(pos => pos.priority === 'P1 — CLOSE NOW')}
      <div class="mb-6 p-5 rounded-2xl border-2 border-rose-500 bg-rose-50 dark:bg-rose-950/20 text-rose-800 dark:text-rose-300 shadow-md">
        <h3 class="text-sm font-black text-rose-700 dark:text-rose-400 uppercase tracking-wider flex items-center gap-2 animate-pulse mb-3">
          🚨 CRITICAL ACTION REQUIRED: CLOSE NOW
        </h3>
        <div class="space-y-4">
          {#each observation.scanned_positions.filter(pos => pos.priority === 'P1 — CLOSE NOW') as pos (pos.position_id)}
            <div class="flex flex-col md:flex-row md:items-center justify-between p-4 bg-white dark:bg-slate-900 rounded-xl border border-rose-200 dark:border-rose-950 gap-4">
              <div>
                <div class="flex items-center gap-2 mb-1">
                  <span class="px-2 py-0.5 text-[10px] font-bold bg-rose-600 text-white rounded uppercase">{pos.underlying}</span>
                  <span class="text-xs font-semibold text-slate-500 dark:text-slate-400">{pos.strategy_type.replace(/_/g, ' ')}</span>
                </div>
                <p class="text-xs font-black text-slate-800 dark:text-slate-100">{pos.action}</p>
                <p class="text-[11px] text-slate-500 mt-0.5">{pos.reason}</p>
              </div>
              <button
                onclick={() => handleClosePosition(pos.position_id)}
                class="px-5 py-2.5 bg-rose-600 hover:bg-rose-700 text-white rounded-xl text-xs font-black uppercase tracking-wider shadow hover:shadow-md transition cursor-pointer shrink-0 animate-pulse"
              >
                Close Position Now →
              </button>
            </div>
          {/each}
        </div>
      </div>
    {/if}

    <!-- Layer A Session Lock Banner -->
    {#if !isAcknowledgeReviewed}
      <div class="mb-8 p-6 rounded-3xl border border-rose-300 bg-rose-50 text-rose-800 dark:border-rose-950/30 dark:bg-rose-950/20 dark:text-rose-400 flex flex-col md:flex-row justify-between items-center gap-4 shadow-sm">
        <div>
          <h2 class="text-base font-black flex items-center gap-2">
            ⚠️ SESSION NAVIGATION LOCKED
          </h2>
          <p class="text-xs font-medium mt-1 leading-relaxed">
            You must review the active position scanner alerts, aggregated Greeks, and risk exposure safeguards for the current session. Click below to unlock settings and other operations.
          </p>
        </div>
        <button
          onclick={handleAcknowledge}
          class="px-5 py-2.5 bg-rose-600 hover:bg-rose-700 text-white rounded-xl text-xs font-black tracking-wider uppercase shadow-md hover:shadow-lg transition cursor-pointer"
        >
          Acknowledge & Unlock Session
        </button>
      </div>
    {/if}

    <!-- Portfolio Account Overview Banner -->
    <section class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-8">
      <div class="bg-white dark:bg-slate-900 p-5 rounded-2xl border border-slate-200 dark:border-slate-800 shadow-sm">
        <span class="text-xs font-semibold text-slate-400 uppercase tracking-wider block mb-1">Total NAV</span>
        <span class="text-2xl font-bold dark:text-white">{formatDollar(totalNav)}</span>
        <span class="text-xs text-indigo-500 font-medium block mt-1">{broker}</span>
      </div>
      <div class="bg-white dark:bg-slate-900 p-5 rounded-2xl border border-slate-200 dark:border-slate-800 shadow-sm">
        <span class="text-xs font-semibold text-slate-400 uppercase tracking-wider block mb-1">Account Type</span>
        <span class="text-2xl font-bold dark:text-white">{accountType}</span>
        <span class="text-xs text-slate-500 dark:text-slate-400 block mt-1">{optionsApproval}</span>
      </div>
      <div class="bg-white dark:bg-slate-900 p-5 rounded-2xl border border-slate-200 dark:border-slate-800 shadow-sm">
        <span class="text-xs font-semibold text-slate-400 uppercase tracking-wider block mb-1">Execution Mode</span>
        <span class="text-2xl font-bold uppercase tracking-wider block {executionMode === 'LIVE' ? 'text-rose-500' : 'text-amber-500'}">
          {executionMode}
        </span>
        <span class="text-xs text-slate-500 dark:text-slate-400 block mt-1">Manual Sandbox Enabled</span>
      </div>
      <div class="bg-white dark:bg-slate-900 p-5 rounded-2xl border border-slate-200 dark:border-slate-800 shadow-sm flex flex-col justify-between">
        <div>
          <span class="text-xs font-semibold text-slate-400 uppercase tracking-wider block mb-1">Active Positions</span>
          <span class="text-2xl font-bold dark:text-white">{positions.filter(p => p.status === 'OPEN').length}</span>
        </div>
        {#if isAcknowledgeReviewed}
          <button
            onclick={() => (isEditingConfig = !isEditingConfig)}
            class="mt-2 text-xs font-bold text-indigo-600 dark:text-indigo-400 hover:underline text-left block"
          >
            {isEditingConfig ? 'Close Settings' : 'Edit Risk Profile Settings →'}
          </button>
        {:else}
          <span class="mt-2 text-xs font-semibold text-slate-400 italic">Settings locked</span>
        {/if}
      </div>
    </section>

    <!-- Admin Configuration Panel -->
    {#if isEditingConfig && isAcknowledgeReviewed}
      <section class="bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 rounded-3xl p-6 mb-8 shadow-sm transition-all animate-fade-in">
        <div class="flex justify-between items-center mb-6">
          <h2 class="text-xl font-bold dark:text-white">Portfolio Risk & Greek Limits Configuration</h2>
          <button onclick={() => (isEditingConfig = false)} class="text-sm font-semibold text-slate-400 hover:text-slate-600 dark:hover:text-slate-200">Cancel</button>
        </div>
        <form onsubmit={handleSaveConfig}>
          <div class="grid grid-cols-1 md:grid-cols-3 gap-6 mb-6">
            <div class="bg-slate-50 dark:bg-slate-950 p-4 rounded-2xl border border-slate-200 dark:border-slate-900">
              <h3 class="font-bold text-sm mb-4 text-indigo-600 dark:text-indigo-400">Account Details</h3>
              <div class="space-y-3">
                <label class="block text-xs font-semibold text-slate-500">
                  Total NAV ($)
                  <input type="number" bind:value={totalNav} class="w-full mt-1 px-3 py-2 border rounded-lg dark:bg-slate-900 dark:border-slate-800 text-sm" />
                </label>
                <label class="block text-xs font-semibold text-slate-500">
                  Broker Name
                  <input type="text" bind:value={broker} class="w-full mt-1 px-3 py-2 border rounded-lg dark:bg-slate-900 dark:border-slate-800 text-sm" />
                </label>
                <label class="block text-xs font-semibold text-slate-500">
                  Execution Mode
                  <select bind:value={executionMode} class="w-full mt-1 px-3 py-2 border rounded-lg dark:bg-slate-900 dark:border-slate-800 text-sm">
                    <option value="PAPER">PAPER (Sandbox)</option>
                    <option value="LIVE">LIVE (Real Funds)</option>
                  </select>
                </label>
              </div>
            </div>
            <div class="bg-slate-50 dark:bg-slate-950 p-4 rounded-2xl border border-slate-200 dark:border-slate-900">
              <h3 class="font-bold text-sm mb-4 text-indigo-600 dark:text-indigo-400">Risk Thresholds</h3>
              <div class="space-y-3">
                <div class="grid grid-cols-2 gap-2">
                  <label class="block text-xs font-semibold text-slate-500">
                    Max Risk %
                    <input type="number" step="0.1" bind:value={maxTradeRiskPct} class="w-full mt-1 px-3 py-2 border rounded-lg dark:bg-slate-900 dark:border-slate-800 text-sm" />
                  </label>
                  <label class="block text-xs font-semibold text-slate-500">
                    Max Risk $
                    <input type="number" bind:value={maxTradeRiskDollars} class="w-full mt-1 px-3 py-2 border rounded-lg dark:bg-slate-900 dark:border-slate-800 text-sm" />
                  </label>
                </div>
                <label class="block text-xs font-semibold text-slate-500">
                  Max Underlying Concentration %
                  <input type="number" step="0.1" bind:value={maxUnderlyingConcentrationPct} class="w-full mt-1 px-3 py-2 border rounded-lg dark:bg-slate-900 dark:border-slate-800 text-sm" />
                </label>
                <label class="block text-xs font-semibold text-slate-500">
                  Min Cash Reserve %
                  <input type="number" step="0.1" bind:value={minimumCashReservePct} class="w-full mt-1 px-3 py-2 border rounded-lg dark:bg-slate-900 dark:border-slate-800 text-sm" />
                </label>
              </div>
            </div>
            <div class="bg-slate-50 dark:bg-slate-950 p-4 rounded-2xl border border-slate-200 dark:border-slate-900">
              <h3 class="font-bold text-sm mb-4 text-indigo-600 dark:text-indigo-400">Greek Limits</h3>
              <div class="space-y-3">
                <label class="block text-xs font-semibold text-slate-500">
                  Max Net Delta (Δ)
                  <input type="number" bind:value={maxNetDelta} class="w-full mt-1 px-3 py-2 border rounded-lg dark:bg-slate-900 dark:border-slate-800 text-sm" />
                </label>
                <label class="block text-xs font-semibold text-slate-500">
                  Max Net Vega
                  <input type="number" bind:value={maxNetVega} class="w-full mt-1 px-3 py-2 border rounded-lg dark:bg-slate-900 dark:border-slate-800 text-sm" />
                </label>
                <label class="block text-xs font-semibold text-slate-500">
                  Max Net Gamma (Γ)
                  <input type="number" bind:value={maxNetGamma} class="w-full mt-1 px-3 py-2 border rounded-lg dark:bg-slate-900 dark:border-slate-800 text-sm" />
                </label>
              </div>
            </div>
          </div>
          <div class="flex justify-end gap-3">
            <button type="button" onclick={() => (isEditingConfig = false)} class="px-4 py-2 text-sm font-semibold rounded-lg bg-slate-200 dark:bg-slate-800 text-slate-700 dark:text-slate-300">Cancel</button>
            <button type="submit" class="px-4 py-2 text-sm font-semibold rounded-lg bg-indigo-600 hover:bg-indigo-700 text-white cursor-pointer">Save Configuration</button>
          </div>
        </form>
      </section>
    {/if}

    <!-- Layer B: Market Telemetry Simulation Panel -->
    {#if isAcknowledgeReviewed}
      <section id="market-telemetry-panel" class="bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 rounded-3xl p-6 mb-8 shadow-sm">
        <div class="flex flex-wrap justify-between items-center mb-5 gap-3">
          <div>
            <h2 class="text-xl font-bold dark:text-white">Market Telemetry Inputs</h2>
            <p class="text-xs text-slate-500 mt-0.5">Regime is computed automatically from the values below — no manual override.</p>
          </div>
          <button
            id="fetch-live-btn"
            type="button"
            onclick={handleFetchLive}
            disabled={isFetchingLive}
            class="px-4 py-2 text-sm font-semibold rounded-xl bg-indigo-600 hover:bg-indigo-700 disabled:opacity-60 text-white cursor-pointer flex items-center gap-2 transition"
          >
            {#if isFetchingLive}
              <span class="animate-spin text-base">⟳</span> Fetching…
            {:else}
              ⟳ Fetch Live Data
            {/if}
          </button>
        </div>
        <form onsubmit={handleSaveMarketState}>
          <div class="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-4 mb-5">
            <label class="block text-xs font-semibold text-slate-500">
              SPY Price ($)
              <input id="input-spy-price" type="number" step="0.01" bind:value={mockSpyPrice} class="w-full mt-1 px-3 py-2 border rounded-lg dark:bg-slate-900 dark:border-slate-800 text-sm font-mono" />
            </label>
            <label class="block text-xs font-semibold text-slate-500">
              SPY SMA20 ($)
              <input id="input-spy-sma20" type="number" step="0.01" bind:value={mockSpySma20} class="w-full mt-1 px-3 py-2 border rounded-lg dark:bg-slate-900 dark:border-slate-800 text-sm font-mono" />
            </label>
            <label class="block text-xs font-semibold text-slate-500">
              VIX Close
              <input id="input-vix" type="number" step="0.01" bind:value={mockVixClose} class="w-full mt-1 px-3 py-2 border rounded-lg dark:bg-slate-900 dark:border-slate-800 text-sm font-mono" />
            </label>
            <label class="block text-xs font-semibold text-slate-500">
              Daily Return (%)
              <input id="input-daily-return" type="number" step="0.01" bind:value={mockDailyReturn} class="w-full mt-1 px-3 py-2 border rounded-lg dark:bg-slate-900 dark:border-slate-800 text-sm font-mono" />
            </label>
            <label class="block text-xs font-semibold text-slate-500 col-span-1">
              IVRs (TICKER:value, …)
              <input id="input-ivrs" type="text" bind:value={mockIvrs} placeholder="SPY:35,AAPL:60" class="w-full mt-1 px-3 py-2 border rounded-lg dark:bg-slate-900 dark:border-slate-800 text-sm font-mono" />
            </label>
            <label class="block text-xs font-semibold text-slate-500 col-span-1">
              Catalysts (dates or FOMC:YYYY-MM-DD)
              <input id="input-catalysts" type="text" bind:value={mockCatalysts} placeholder="FOMC:2026-06-18" class="w-full mt-1 px-3 py-2 border rounded-lg dark:bg-slate-900 dark:border-slate-800 text-sm font-mono" />
            </label>
          </div>
          <div class="flex justify-end">
            <button type="submit" class="px-5 py-2 text-sm font-semibold rounded-xl bg-slate-700 hover:bg-slate-600 text-white cursor-pointer transition">
              Apply Simulated Telemetry
            </button>
          </div>
        </form>
      </section>
    {/if}

    <!-- Portfolio Net Greeks Panel -->
    {#if observation}
      <GreeksPanel {observation} {maxNetDelta} {maxNetVega} {maxNetGamma} />
    {/if}

    <!-- Exposure Safeguards -->
    {#if observation}
      <SafeguardsPanel {observation} />
    {/if}

    <!-- Layer A: Active Position Scanner -->
    {#if observation}
      <PositionScanner {observation} onClosePosition={handleClosePosition} />
    {:else}
      <section>
        <div class="bg-white dark:bg-slate-900 p-8 rounded-3xl border border-slate-200 dark:border-slate-800 text-center">
          <p class="text-slate-500">No active positions loaded in scanner.</p>
        </div>
      </section>
    {/if}

    <!-- Layer C: Opportunity Engine — only shown after session is acknowledged -->
    {#if isAcknowledgeReviewed}
      <div class="mt-8 border-t border-slate-200 dark:border-slate-800 pt-8">
        {#if !opportunityScan}
          <div class="flex items-center justify-between mb-5">
            <div>
              <h2 class="text-xl font-bold dark:text-white tracking-tight">Layer C — Opportunity Engine</h2>
              <p class="text-xs text-slate-500 mt-0.5">Scan active playbooks against current market telemetry.</p>
            </div>
            <button
              onclick={handleScanOpportunities}
              class="px-5 py-2.5 text-sm font-bold rounded-xl bg-violet-600 hover:bg-violet-700 text-white cursor-pointer transition shadow-sm"
            >
              Scan for Opportunities →
            </button>
          </div>
        {:else}
          {#if selectedSpecResult}
            <TradeSpecCard
              result={selectedSpecResult}
              playbookName={selectedPlaybookName}
              onDismiss={handleDismissSpec}
              onPositionSaved={handlePositionSaved}
            />
          {:else}
            <div class="flex justify-end mb-4">
              <button
                onclick={() => { opportunityScan = null; }}
                class="text-xs font-semibold text-slate-400 hover:text-slate-600 dark:hover:text-slate-200"
              >
                ↺ Re-scan
              </button>
            </div>
            <CandidateCards
              scanResult={opportunityScan}
              onSelectPlaybook={handleSelectPlaybook}
            />
            {#if isLoadingSpec}
              <div class="text-center py-8 text-slate-500 text-sm">Generating trade spec…</div>
            {/if}
          {/if}
        {/if}
      </div>

      <!-- Sprint 5: Post-Mortems -->
      {#if postMortems.length > 0}
        <div class="mt-8 border-t border-slate-200 dark:border-slate-800 pt-8">
          <h2 class="text-xl font-bold dark:text-white tracking-tight mb-5">Closed Position Post-Mortems</h2>
          <div class="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-3 gap-5">
            {#each postMortems as pm (pm.id)}
              <PostMortemCard postMortem={pm} />
            {/each}
          </div>
        </div>
      {/if}

      <!-- Sprint 5: Opportunity Ledger -->
      <div class="mt-8 border-t border-slate-200 dark:border-slate-800 pt-8">
        <OpportunityLedger records={opportunityRecords} />
      </div>

      <!-- Sprint 5: Performance Diagnostics -->
      {#if diagnostics}
        <div class="mt-8 border-t border-slate-200 dark:border-slate-800 pt-8">
          <PerformanceDashboard {diagnostics} />
        </div>
      {/if}
    {/if}
  </main>

  <!-- Close Position Modal -->
  {#if closingPositionId}
    <ClosePositionModal
      positionId={closingPositionId}
      onConfirm={handleConfirmClose}
      onCancel={() => (closingPositionId = null)}
    />
  {/if}
</div>
