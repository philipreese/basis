<script lang="ts">
  import { onMount } from 'svelte';
  import { getPortfolioConfig, getPositions, updatePortfolioConfig } from './lib/api';
  import type { PortfolioConfig, Position } from './lib/api';

  // Svelte 5 Runes
  let config = $state<PortfolioConfig | null>(null);
  let positions = $state<Position[]>([]);
  let darkMode = $state(true);
  let errorMsg = $state('');
  let successMsg = $state('');
  let isEditingConfig = $state(false);

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
      successMsg = 'Configuration updated successfully.';
      isEditingConfig = false;
      setTimeout(() => (successMsg = ''), 3000);
    } catch (e: any) {
      errorMsg = 'Failed to save configuration: ' + e.message;
    }
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
        <button
          onclick={() => (isEditingConfig = !isEditingConfig)}
          class="mt-2 text-xs font-bold text-indigo-600 dark:text-indigo-400 hover:underline text-left block"
        >
          {isEditingConfig ? 'Close Settings' : 'Edit Risk Profile Settings →'}
        </button>
      </div>
    </section>

    <!-- Admin Configuration Panel -->
    {#if isEditingConfig}
      <section class="bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 rounded-3xl p-6 mb-8 shadow-sm transition-all">
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
          <!-- Grid config fields -->
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
              class="px-4 py-2 text-sm font-semibold rounded-lg bg-indigo-600 hover:bg-indigo-700 text-white"
            >
              Save Configuration
            </button>
          </div>
        </form>
      </section>
    {/if}

    <!-- Active Positions Section -->
    <section>
      <div class="flex justify-between items-center mb-6">
        <h2 class="text-xl font-bold dark:text-white tracking-tight flex items-center gap-2">
          <span>Active Positions</span>
          <span class="px-2 py-0.5 text-xs bg-indigo-100 dark:bg-indigo-900/50 text-indigo-700 dark:text-indigo-400 rounded-full font-semibold">
            Sprint 1 Seeded
          </span>
        </h2>
      </div>

      {#if positions.length === 0}
        <div class="bg-white dark:bg-slate-900 p-8 rounded-3xl border border-slate-200 dark:border-slate-800 text-center">
          <p class="text-slate-500">No positions loaded. Seeding could be running or failed.</p>
        </div>
      {:else}
        <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {#each positions as pos (pos.id)}
            <article class="bg-white dark:bg-slate-900 rounded-3xl border border-slate-200 dark:border-slate-800 shadow-sm overflow-hidden flex flex-col">
              <!-- Card Header -->
              <div class="p-6 border-b border-slate-200 dark:border-slate-800 bg-slate-50/50 dark:bg-slate-900/50">
                <div class="flex justify-between items-start mb-2">
                  <div>
                    <span class="px-2.5 py-1 text-xs font-bold bg-indigo-100 dark:bg-indigo-950 text-indigo-700 dark:text-indigo-300 rounded-full uppercase">
                      {pos.underlying}
                    </span>
                    <span class="ml-2 text-sm font-semibold text-slate-600 dark:text-slate-400">
                      {pos.strategy_type.replace('_', ' ')}
                    </span>
                  </div>
                  <span class="px-2 py-0.5 text-xs font-semibold bg-emerald-100 dark:bg-emerald-950 text-emerald-700 dark:text-emerald-300 rounded-lg">
                    {pos.status}
                  </span>
                </div>
                <h3 class="text-lg font-bold dark:text-white mt-1">{pos.notes}</h3>
              </div>

              <!-- Option Value Mechanics / Math Display -->
              <div class="p-6 flex-grow space-y-6">
                <!-- Option Legs Breakdown -->
                <div>
                  <h4 class="text-xs font-bold text-slate-400 uppercase tracking-wider mb-3">Option Legs Structure</h4>
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
                        <div class="flex gap-4 font-mono text-slate-500">
                          <span>Δ: {leg.delta}</span>
                          <span>Θ: {leg.theta}</span>
                          <span>Vega: {leg.vega}</span>
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
                    <span class="text-[10px] text-slate-400 block">{pos.premium_direction}</span>
                  </div>

                  <div>
                    <span class="text-[10px] font-bold text-slate-400 uppercase tracking-wider block mb-1">Multiplier Multiplied</span>
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

                <!-- Breakeven Points -->
                {#if pos.break_even_upside || pos.break_even_downside}
                  <div class="border-t border-slate-200 dark:border-slate-800 pt-4">
                    <h4 class="text-xs font-bold text-slate-400 uppercase tracking-wider mb-2">Breakeven Thresholds</h4>
                    <div class="flex justify-between text-xs font-mono">
                      {#if pos.break_even_downside}
                        <div>
                          <span class="text-slate-400 mr-2">Downside:</span>
                          <span class="font-bold dark:text-white">${pos.break_even_downside.toFixed(2)}</span>
                        </div>
                      {/if}
                      {#if pos.break_even_upside}
                        <div>
                          <span class="text-slate-400 mr-2">Upside:</span>
                          <span class="font-bold dark:text-white">${pos.break_even_upside.toFixed(2)}</span>
                        </div>
                      {/if}
                    </div>
                  </div>
                {/if}

                <!-- Subjective Operational Journal -->
                {#if pos.journal}
                  <div class="border-t border-slate-200 dark:border-slate-800 pt-4 space-y-2">
                    <h4 class="text-xs font-bold text-slate-400 uppercase tracking-wider">Operational Intent Journal</h4>
                    <div class="bg-slate-50 dark:bg-slate-950 p-4 rounded-2xl border border-slate-200 dark:border-slate-900 space-y-3">
                      <div>
                        <span class="text-[10px] font-semibold text-slate-400 block">Thesis Rationale</span>
                        <p class="text-xs font-medium dark:text-slate-300 leading-relaxed mt-0.5">{pos.journal.core_thesis_rationale}</p>
                      </div>
                      <div>
                        <span class="text-[10px] font-semibold text-slate-400 block">Structural Invalidation</span>
                        <p class="text-xs font-medium text-rose-600 dark:text-rose-400 leading-relaxed mt-0.5">{pos.journal.structural_invalidation}</p>
                      </div>
                      <div class="flex justify-between text-[10px] text-slate-400">
                        <span>Expected Move: <strong class="text-slate-700 dark:text-slate-300 font-mono">{pos.journal.expected_underlying_move_pct}%</strong></span>
                        <span>Mood: <strong class="text-slate-700 dark:text-slate-300 font-medium">{pos.journal.pre_trade_emotional_state}</strong></span>
                        <span>Confidence: <strong class="text-indigo-600 dark:text-indigo-400 font-mono">{pos.journal.pre_trade_confidence_rating}/5</strong></span>
                      </div>
                    </div>
                  </div>
                {/if}
              </div>
            </article>
          {/each}
        </div>
      {/if}
    </section>
  </main>
</div>
