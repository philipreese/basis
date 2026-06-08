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
    REGIME_DISPLAY
  } from './lib/api';
  import type { PortfolioConfig, Position, MarketState, PortfolioObservation } from './lib/api';

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

  // Form states for dynamic configuration
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

  // Market telemetry form states
  let mockSpyPrice = $state(758.0);
  let mockSpySma20 = $state(750.0);
  let mockVixClose = $state(14.5);
  let mockDailyReturn = $state(0.5);   // displayed as %, stored as decimal
  let mockIvrs = $state('SPY:25');      // comma-separated KEY:VALUE pairs
  let mockCatalysts = $state('2026-06-08');
  let isFetchingLive = $state(false);
  let showScoreBreakdown = $state(false);

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
      positions = await getPositions();
      marketState = await getMarketState();
      observation = await getPortfolioObservation();

      if (config) {
        // Hydrate form states
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
        mockSpySma20 = marketState.spy_sma20 ?? 750.0;
        mockVixClose = marketState.vix_close ?? 14.5;
        mockDailyReturn = (marketState.spy_daily_return ?? 0.005) * 100;
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
        account: {
          total_nav: totalNav,
          broker,
          account_type: accountType,
          options_approval: optionsApproval,
          execution_mode: executionMode
        },
        risk_profile: {
          max_trade_risk_pct: maxTradeRiskPct,
          max_trade_risk_dollars: maxTradeRiskDollars,
          max_underlying_concentration_pct: maxUnderlyingConcentrationPct,
          max_correlated_index_pct: maxCorrelatedIndexPct,
          minimum_cash_reserve_pct: minimumCashReservePct,
          max_simultaneous_positions: maxSimultaneousPositions,
          max_capital_deployed_pct: maxCapitalDeployedPct
        },
        portfolio_greek_limits: {
          max_net_delta: maxNetDelta,
          max_net_vega: maxNetVega,
          max_net_gamma: maxNetGamma
        }
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
      // Parse IVR key:value pairs
      const ivrs: Record<string, number> = {};
      for (const pair of mockIvrs.split(',').map(s => s.trim()).filter(Boolean)) {
        const [k, v] = pair.split(':');
        if (k && v) ivrs[k.trim().toUpperCase()] = parseFloat(v.trim());
      }
      const updated = await updateMarketState({
        spy_price: mockSpyPrice,
        spy_sma20: mockSpySma20,
        vix_close: mockVixClose,
        underlying_ivrs: ivrs,
        spy_daily_return: mockDailyReturn / 100,
        catalyst_dates: cats,
        current_regime: 'CALM_BULL',  // placeholder — recomputed server-side
        regime_scores: {},
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
      // Sync form fields
      mockSpyPrice = marketState.spy_price;
      mockSpySma20 = marketState.spy_sma20 ?? 750.0;
      mockVixClose = marketState.vix_close ?? 14.5;
      mockDailyReturn = (marketState.spy_daily_return ?? 0.005) * 100;
      const ivrs = marketState.underlying_ivrs ?? {};
      mockIvrs = Object.entries(ivrs).map(([k, v]) => `${k}:${v}`).join(',') || 'SPY:25';
      mockCatalysts = (marketState.catalyst_dates || []).join(', ');
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
        <!-- Dark Mode Toggle -->
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

  <main class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8 flex-grow w-full">
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

    <!-- Layer B: Market Context Ribbon (subordinate — context only, no predictive claims) -->
    {#if marketState}
      {@const regime = marketState.current_regime}
      {@const info = REGIME_DISPLAY[regime] ?? { label: regime, color: 'slate', description: '' }}
      {@const scores = marketState.regime_scores ?? {}}
      {@const colorMap: Record<string, string> = {
        emerald: 'bg-emerald-900/30 border-emerald-700/40 text-emerald-300',
        amber:   'bg-amber-900/30 border-amber-700/40 text-amber-300',
        rose:    'bg-rose-900/30 border-rose-700/40 text-rose-300',
        violet:  'bg-violet-900/30 border-violet-700/40 text-violet-300',
        slate:   'bg-slate-800 border-slate-700 text-slate-300',
      }}
      {@const pillClass = colorMap[info.color] ?? colorMap.slate}

      <div id="layer-b-ribbon" class="mb-6 rounded-2xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900/80 px-5 py-3 shadow-sm flex flex-wrap gap-4 items-center justify-between text-xs">
        <!-- Regime badge -->
        <div class="flex items-center gap-3">
          <span class="text-[10px] font-bold text-slate-400 uppercase tracking-widest">Layer B · Market Context</span>
          <span class="px-3 py-1 rounded-full border font-black tracking-wider uppercase {pillClass}">
            {info.label}
          </span>
          <span class="text-slate-500 dark:text-slate-400 hidden sm:inline">{info.description}</span>
        </div>

        <!-- Telemetry pills -->
        <div class="flex flex-wrap gap-3 items-center">
          <span class="font-mono text-slate-600 dark:text-slate-300">SPY <span class="font-bold">${marketState.spy_price.toFixed(2)}</span></span>
          <span class="text-slate-400">·</span>
          <span class="font-mono text-slate-600 dark:text-slate-300">SMA20 <span class="font-bold">${(marketState.spy_sma20 ?? 0).toFixed(2)}</span></span>
          <span class="text-slate-400">·</span>
          <span class="font-mono text-slate-600 dark:text-slate-300">VIX <span class="font-bold">{(marketState.vix_close ?? 0).toFixed(1)}</span></span>
          <span class="text-slate-400">·</span>
          <span class="font-mono {(marketState.spy_daily_return ?? 0) >= 0 ? 'text-emerald-500' : 'text-rose-500'}">
            Day {(marketState.spy_daily_return ?? 0) >= 0 ? '+' : ''}{((marketState.spy_daily_return ?? 0) * 100).toFixed(2)}%
          </span>

          <!-- Score breakdown toggle -->
          <button
            id="regime-score-toggle"
            onclick={() => (showScoreBreakdown = !showScoreBreakdown)}
            class="ml-2 px-2 py-0.5 text-[10px] rounded border border-slate-300 dark:border-slate-700 text-slate-500 dark:text-slate-400 hover:border-slate-500 transition"
          >
            {showScoreBreakdown ? 'Hide' : 'Scores ▾'}
          </button>
        </div>

        <!-- Score breakdown panel -->
        {#if showScoreBreakdown}
          <div class="w-full mt-2 pt-3 border-t border-slate-100 dark:border-slate-800 grid grid-cols-2 sm:grid-cols-4 gap-3">
            {#each Object.entries(scores).sort((a, b) => b[1] - a[1]) as [r, s]}
              {@const ri = REGIME_DISPLAY[r] ?? { label: r, color: 'slate' }}
              {@const isWinner = r === regime}
              <div class="flex flex-col items-center p-2 rounded-xl border {isWinner ? `border-${ri.color}-500 bg-${ri.color}-950/20` : 'border-slate-200 dark:border-slate-800'} text-center">
                <span class="text-[10px] font-bold uppercase tracking-wider {isWinner ? `text-${ri.color}-400` : 'text-slate-500'}">{ri.label}</span>
                <span class="text-lg font-black font-mono {isWinner ? `text-${ri.color}-300` : 'text-slate-400'}">{s > 0 ? '+' : ''}{s.toFixed(0)}</span>
                {#if isWinner}<span class="text-[9px] text-slate-500 mt-0.5">▲ ACTIVE</span>{/if}
              </div>
            {/each}
          </div>
        {/if}
      </div>
    {/if}

    <!-- Layer A Lock Warning Overlay / Acknowledge Banner -->
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
        <span class="text-2xl font-bold dark:text-white">${totalNav.toLocaleString()}</span>
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
          <span class="text-2xl font-bold dark:text-white">{positions.length}</span>
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
          <button
            onclick={() => (isEditingConfig = false)}
            class="text-sm font-semibold text-slate-400 hover:text-slate-600 dark:hover:text-slate-200"
          >
            Cancel
          </button>
        </div>

        <form onsubmit={handleSaveConfig}>
          <div class="grid grid-cols-1 md:grid-cols-3 gap-6 mb-6">
            <!-- Account Settings -->
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

            <!-- Risk Thresholds -->
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

            <!-- Greek Limits -->
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
            <button
              type="button"
              onclick={() => (isEditingConfig = false)}
              class="px-4 py-2 text-sm font-semibold rounded-lg bg-slate-200 dark:bg-slate-800 text-slate-700 dark:text-slate-300"
            >
              Cancel
            </button>
            <button
              type="submit"
              class="px-4 py-2 text-sm font-semibold rounded-lg bg-indigo-600 hover:bg-indigo-700 text-white cursor-pointer"
            >
              Save Configuration
            </button>
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
          <div class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-4 mb-5">
            <label class="block text-xs font-semibold text-slate-500">
              SPY Price ($)
              <input id="input-spy-price" type="number" step="0.01" bind:value={mockSpyPrice}
                class="w-full mt-1 px-3 py-2 border rounded-lg dark:bg-slate-900 dark:border-slate-800 text-sm font-mono" />
            </label>
            <label class="block text-xs font-semibold text-slate-500">
              SPY SMA20 ($)
              <input id="input-spy-sma20" type="number" step="0.01" bind:value={mockSpySma20}
                class="w-full mt-1 px-3 py-2 border rounded-lg dark:bg-slate-900 dark:border-slate-800 text-sm font-mono" />
            </label>
            <label class="block text-xs font-semibold text-slate-500">
              VIX Close
              <input id="input-vix" type="number" step="0.01" bind:value={mockVixClose}
                class="w-full mt-1 px-3 py-2 border rounded-lg dark:bg-slate-900 dark:border-slate-800 text-sm font-mono" />
            </label>
            <label class="block text-xs font-semibold text-slate-500">
              Daily Return (%)
              <input id="input-daily-return" type="number" step="0.01" bind:value={mockDailyReturn}
                class="w-full mt-1 px-3 py-2 border rounded-lg dark:bg-slate-900 dark:border-slate-800 text-sm font-mono" />
            </label>
            <label class="block text-xs font-semibold text-slate-500 col-span-1">
              IVRs (TICKER:value, …)
              <input id="input-ivrs" type="text" bind:value={mockIvrs} placeholder="SPY:35,AAPL:60"
                class="w-full mt-1 px-3 py-2 border rounded-lg dark:bg-slate-900 dark:border-slate-800 text-sm font-mono" />
            </label>
            <label class="block text-xs font-semibold text-slate-500 col-span-1">
              Catalysts (dates or FOMC:YYYY-MM-DD)
              <input id="input-catalysts" type="text" bind:value={mockCatalysts} placeholder="FOMC:2026-06-18"
                class="w-full mt-1 px-3 py-2 border rounded-lg dark:bg-slate-900 dark:border-slate-800 text-sm font-mono" />
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

    <!-- Portfolio Net Greeks Panel (Flash Warnings if limit exceeded) -->
    {#if observation}
      {@const g = observation.greeks}
      {@const isDeltaExceeded = Math.abs(g.net_delta) > maxNetDelta}
      {@const isVegaExceeded = Math.abs(g.net_vega) > maxNetVega}
      {@const isGammaExceeded = Math.abs(g.net_gamma) > maxNetGamma}

      <section class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-8">
        <div class="bg-white dark:bg-slate-900 p-5 rounded-2xl border transition shadow-sm {isDeltaExceeded ? 'border-rose-500 bg-rose-50/10 dark:bg-rose-950/10 animate-pulse' : 'border-slate-200 dark:border-slate-800'}">
          <span class="text-xs font-semibold text-slate-400 uppercase tracking-wider block mb-1">Portfolio Net Delta (Δ)</span>
          <span class="text-2xl font-bold dark:text-white font-mono">{g.net_delta.toFixed(2)}</span>
          <span class="text-xs block mt-1 {isDeltaExceeded ? 'text-rose-500 font-bold' : 'text-slate-500'}">Limit: ±{maxNetDelta}</span>
        </div>

        <div class="bg-white dark:bg-slate-900 p-5 rounded-2xl border border-slate-200 dark:border-slate-800 shadow-sm">
          <span class="text-xs font-semibold text-slate-400 uppercase tracking-wider block mb-1">Portfolio Net Theta (Θ)</span>
          <span class="text-2xl font-bold dark:text-white font-mono">{g.net_theta.toFixed(2)}</span>
          <span class="text-xs text-slate-500 block mt-1">Daily Theta reward</span>
        </div>

        <div class="bg-white dark:bg-slate-900 p-5 rounded-2xl border transition shadow-sm {isVegaExceeded ? 'border-rose-500 bg-rose-50/10 dark:bg-rose-950/10 animate-pulse' : 'border-slate-200 dark:border-slate-800'}">
          <span class="text-xs font-semibold text-slate-400 uppercase tracking-wider block mb-1">Portfolio Net Vega</span>
          <span class="text-2xl font-bold dark:text-white font-mono">{g.net_vega.toFixed(2)}</span>
          <span class="text-xs block mt-1 {isVegaExceeded ? 'text-rose-500 font-bold' : 'text-slate-500'}">Limit: ±{maxNetVega}</span>
        </div>

        <div class="bg-white dark:bg-slate-900 p-5 rounded-2xl border transition shadow-sm {isGammaExceeded ? 'border-rose-500 bg-rose-50/10 dark:bg-rose-950/10 animate-pulse' : 'border-slate-200 dark:border-slate-800'}">
          <span class="text-xs font-semibold text-slate-400 uppercase tracking-wider block mb-1">Portfolio Net Gamma (Γ)</span>
          <span class="text-2xl font-bold dark:text-white font-mono">{g.net_gamma.toFixed(2)}</span>
          <span class="text-xs block mt-1 {isGammaExceeded ? 'text-rose-500 font-bold' : 'text-slate-500'}">Limit: ±{maxNetGamma}</span>
        </div>
      </section>
    {/if}

    <!-- Exposure Safeguards Warnings List -->
    {#if observation && observation.safeguards.length > 0}
      <section class="mb-8 space-y-3">
        <h3 class="text-xs font-bold text-slate-400 uppercase tracking-wider">Active Risk Safeguard Alerts</h3>
        {#each observation.safeguards as warn}
          <div class="p-4 rounded-xl border flex items-start gap-3 text-xs {warn.severity === 'CRITICAL' ? 'border-rose-200 bg-rose-50 text-rose-800 dark:border-rose-950/30 dark:bg-rose-950/20 dark:text-rose-400' : 'border-amber-200 bg-amber-50 text-amber-800 dark:border-amber-950/30 dark:bg-amber-950/20 dark:text-amber-400'}">
            <span class="font-bold">{warn.severity === 'CRITICAL' ? '🛑 CRITICAL' : '⚠️ WARNING'}:</span>
            <span>{warn.message}</span>
          </div>
        {/each}
      </section>
    {/if}

    <!-- Active Positions Section -->
    <section>
      <div class="flex justify-between items-center mb-6">
        <h2 class="text-xl font-bold dark:text-white tracking-tight flex items-center gap-2">
          <span>Active Position Scanner</span>
          <span class="px-2 py-0.5 text-xs bg-indigo-100 dark:bg-indigo-900/50 text-indigo-700 dark:text-indigo-400 rounded-full font-semibold">
            Layer A Observation
          </span>
        </h2>
      </div>

      {#if !observation || observation.scanned_positions.length === 0}
        <div class="bg-white dark:bg-slate-900 p-8 rounded-3xl border border-slate-200 dark:border-slate-800 text-center">
          <p class="text-slate-500">No active positions loaded in scanner.</p>
        </div>
      {:else}
        <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {#each observation.scanned_positions as pos (pos.position_id)}
            {@const isP1 = pos.priority === 'P1 — CLOSE NOW'}
            {@const isP2 = pos.priority.startsWith('P2')}
            {@const isP3 = pos.priority === 'P3 — MONITOR'}
            
            {@const cardColorClass = isP1 ? 'border-rose-500 dark:border-rose-900 shadow-rose-500/5' : isP2 ? 'border-amber-500 dark:border-amber-900 shadow-amber-500/5' : isP3 ? 'border-yellow-500 dark:border-yellow-900 shadow-yellow-500/5' : 'border-slate-200 dark:border-slate-800'}
            {@const headerColorClass = isP1 ? 'bg-rose-50/50 dark:bg-rose-950/20 border-rose-100 dark:border-rose-900/20' : isP2 ? 'bg-amber-50/50 dark:bg-amber-950/20 border-amber-100 dark:border-amber-900/20' : isP3 ? 'bg-yellow-50/50 dark:bg-yellow-950/20 border-yellow-100 dark:border-yellow-900/20' : 'bg-slate-50/50 dark:bg-slate-900/50 border-slate-100 dark:border-slate-850'}

            <article class="bg-white dark:bg-slate-900 rounded-3xl border shadow-sm overflow-hidden flex flex-col {cardColorClass}">
              <!-- Card Header -->
              <div class="p-6 border-b {headerColorClass}">
                <div class="flex justify-between items-start mb-2">
                  <div>
                    <span class="px-2.5 py-1 text-xs font-bold bg-indigo-100 dark:bg-indigo-950 text-indigo-700 dark:text-indigo-300 rounded-full uppercase">
                      {pos.underlying}
                    </span>
                    <span class="ml-2 text-sm font-semibold text-slate-600 dark:text-slate-400">
                      {pos.strategy_type.replace(/_/g, ' ')}
                    </span>
                  </div>
                  <span class="px-2.5 py-1 text-[10px] font-black rounded-lg uppercase tracking-wider {isP1 ? 'bg-rose-600 text-white animate-pulse' : isP2 ? 'bg-amber-500 text-white' : isP3 ? 'bg-yellow-500 text-slate-950' : 'bg-emerald-500 text-white'}">
                    {pos.priority}
                  </span>
                </div>
                <h3 class="text-base font-bold dark:text-white mt-1">{pos.action}</h3>
                <p class="text-xs font-semibold text-slate-600 dark:text-slate-400 leading-relaxed mt-1">{pos.reason}</p>
                <div class="mt-2.5 px-3 py-2 bg-slate-100 dark:bg-slate-950 rounded-xl font-mono text-[10px] text-slate-500 dark:text-slate-400 border border-slate-200/50 dark:border-slate-900">
                  {pos.math_detail}
                </div>
              </div>

              <!-- Option Value Mechanics / Math Display -->
              <div class="p-6 flex-grow space-y-6">
                <!-- Option Legs Breakdown -->
                <div>
                  <h4 class="text-[10px] font-bold text-slate-400 uppercase tracking-wider mb-3">Option Legs Structure</h4>
                  <div class="space-y-2">
                    {#each pos.legs as leg}
                      <div class="flex justify-between items-center bg-slate-50 dark:bg-slate-950/50 p-3 rounded-xl border border-slate-200/55 dark:border-slate-900/60 text-xs">
                        <div class="flex items-center gap-3">
                          <span class="font-black px-1.5 py-0.5 rounded text-[10px] {leg.direction === 'LONG' ? 'bg-sky-100 text-sky-700 dark:bg-sky-950 dark:text-sky-300' : 'bg-rose-100 text-rose-700 dark:bg-rose-950 dark:text-rose-300'}">
                            {leg.direction}
                          </span>
                          <span class="font-bold text-slate-800 dark:text-slate-200">
                            {leg.strike} {leg.option_type}
                          </span>
                          <span class="text-slate-400">{leg.expiration}</span>
                        </div>
                        <div class="flex gap-3 font-mono text-slate-500">
                          <span>Δ: {leg.delta}</span>
                          <span>Θ: {leg.theta}</span>
                          <span>Vega: {leg.vega}</span>
                          <span>Γ: {leg.gamma || 0.0}</span>
                        </div>
                      </div>
                    {/each}
                  </div>
                </div>

                <!-- Premium Math Grid -->
                <div class="grid grid-cols-3 gap-4 bg-slate-50 dark:bg-slate-950 p-4 rounded-2xl border border-slate-200/80 dark:border-slate-900">
                  <div>
                    <span class="text-[10px] font-bold text-slate-400 uppercase tracking-wider block mb-1">Premium / Share</span>
                    <span class="text-sm font-bold font-mono dark:text-white">
                      ${pos.entry_premium.toFixed(2)}
                    </span>
                    <span class="text-[10px] text-slate-400 block uppercase">{pos.legs[0]?.direction === 'LONG' ? 'Debit' : 'Credit'}</span>
                  </div>

                  <div>
                    <span class="text-[10px] font-bold text-slate-400 uppercase tracking-wider block mb-1">Multiplier Total</span>
                    <span class="text-sm font-bold font-mono text-indigo-600 dark:text-indigo-400">
                      ${(pos.entry_premium * 100 * pos.contracts).toFixed(2)}
                    </span>
                    <span class="text-[10px] text-slate-400 block">x100 x {pos.contracts} Contract</span>
                  </div>

                  <div>
                    <span class="text-[10px] font-bold text-slate-400 uppercase tracking-wider block mb-1">Current Value</span>
                    <span class="text-sm font-bold font-mono dark:text-white">
                      ${(pos.current_value_per_share * 100 * pos.contracts).toFixed(2)}
                    </span>
                    <span class="text-[10px] text-slate-400 block">${pos.current_value_per_share.toFixed(2)} / Share</span>
                  </div>
                </div>

                <!-- Calculated Metrics -->
                <div class="grid grid-cols-2 gap-4">
                  <div class="p-4 rounded-xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900">
                    <span class="text-[10px] font-bold text-slate-400 uppercase tracking-wider block mb-1">Max Profit</span>
                    <span class="text-base font-bold font-mono text-emerald-600 dark:text-emerald-400">
                      {pos.max_profit === 999999 ? 'Unlimited' : `$${(pos.max_profit * 100 * pos.contracts).toLocaleString(undefined, {minimumFractionDigits: 2})}`}
                    </span>
                    <span class="text-[10px] text-slate-400 block">
                      {pos.max_profit === 999999 ? 'Unlimited' : `$${pos.max_profit.toFixed(2)} / share`}
                    </span>
                  </div>

                  <div class="p-4 rounded-xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900">
                    <span class="text-[10px] font-bold text-slate-400 uppercase tracking-wider block mb-1">Max Loss</span>
                    <span class="text-base font-bold font-mono text-rose-600 dark:text-rose-400">
                      ${(pos.max_loss * 100 * pos.contracts).toLocaleString(undefined, {minimumFractionDigits: 2})}
                    </span>
                    <span class="text-[10px] text-slate-400 block">
                      ${pos.max_loss.toFixed(2)} / share
                    </span>
                  </div>
                </div>
              </div>
            </article>
          {/each}
        </div>
      {/if}
    </section>
  </main>
</div>
