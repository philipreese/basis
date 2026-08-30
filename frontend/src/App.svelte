<script lang="ts">
  import { onMount, tick } from 'svelte';
  import {
    getPortfolioConfig,
    getPortfolioOverview,
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
    PortfolioConfig, PortfolioOverview, Position, MarketState, PortfolioObservation,
    OpportunityScanResult, TradeSpecResult,
    ClosurePostMortem, OpportunityRecord, PerformanceDiagnostics,
    ClosePositionRequest, RollPositionRequest, ScannedPosition,
  } from './lib/api';
  import MarketContextRibbon   from './lib/MarketContextRibbon.svelte';
  import AttentionBlock        from './lib/AttentionBlock.svelte';
  import PositionRow           from './lib/PositionRow.svelte';
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
  // #860: the overview headline — fleet ledger NAV + broker's last-seen NAV,
  // two labeled provenances; the editable config's total_nav is the manual
  // lane's (B00's) capital and no longer appears as the headline.
  let portfolioOverview    = $state<PortfolioOverview | null>(null);
  let positions            = $state<Position[]>([]);
  let marketState          = $state<MarketState | null>(null);
  let observation          = $state<PortfolioObservation | null>(null);
  let darkMode             = $state(true);
  let activeTab            = $state<'overview' | 'scan' | 'books' | 'analysis' | 'settings'>('overview');

  // Portfolio config form state — populated from /api/portfolio/config;
  // nothing that renders these may appear before `config` lands (#861: the
  // old fabricated initials flashed as a false headline while loading, the
  // same failure class #475 fixed for tradingMode).
  let totalNav                      = $state(0);
  let broker                        = $state('');
  let accountType                   = $state('');
  let optionsApproval               = $state('');
  // The REAL trading mode (#361): read from executor status (the backend's
  // IBKR_TRADING_MODE), never a form field — the old editable dropdown could
  // claim LIVE while the executor stayed paper. 'unknown' until a fetch
  // actually succeeds (#475) — falling back to 'paper' while loading or on
  // fetch failure would show a false "safe" badge for a live backend.
  let tradingMode                   = $state<'paper' | 'live' | 'unknown'>('unknown');
  let maxTradeRiskPct               = $state(0);
  let maxTradeRiskDollars           = $state(0);
  let maxUnderlyingConcentrationPct = $state(0);
  let maxCorrelatedIndexPct         = $state(0);
  let minimumCashReservePct         = $state(0);
  let maxSimultaneousPositions      = $state(0);
  let maxCapitalDeployedPct         = $state(0);
  let maxNetDelta                   = $state(0);
  let maxNetVega                    = $state(0);
  let maxNetGamma                   = $state(0);

  // Market telemetry form state — populated from /api/market/state; the
  // telemetry form is gated on `marketState` for the same reason as above.
  let mockSpyPrice    = $state(0);
  let mockSpySma20    = $state(0);
  let mockVixClose    = $state(0);
  let mockDailyReturn = $state(0);
  let mockIvrs        = $state('');
  let mockCatalysts   = $state('');
  let isFetchingLive  = $state(false);
  // True once loadData's first attempt failed — the skeletons switch to an
  // explicit error line instead of pulsing forever.
  let loadFailed      = $state(false);
  // The Open Positions count is only a claim once a positions fetch has
  // actually succeeded — 0-while-loading is a fabricated number (#866).
  let positionsLoaded = $state(false);

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
    // Per-resource isolation (#866): one endpoint hiccuping (e.g. a backend
    // restart mid-sequence) must not abort the rest of the load, and each
    // resource syncs its form state the moment it lands — never after
    // unrelated awaits, where a later failure would strand a truthy config
    // rendering zeroed values.
    let anyFailed = false;
    const attempt = async (fn: () => Promise<void>) => {
      try { await fn(); } catch { anyFailed = true; }
    };

    await attempt(async () => {
      const c = await getPortfolioConfig();
      config                        = c;
      totalNav                      = c.account.total_nav;
      broker                        = c.account.broker;
      accountType                   = c.account.account_type;
      optionsApproval               = c.account.options_approval;
      maxTradeRiskPct               = c.risk_profile.max_trade_risk_pct;
      maxTradeRiskDollars           = c.risk_profile.max_trade_risk_dollars;
      maxUnderlyingConcentrationPct = c.risk_profile.max_underlying_concentration_pct;
      maxCorrelatedIndexPct         = c.risk_profile.max_correlated_index_pct;
      minimumCashReservePct         = c.risk_profile.minimum_cash_reserve_pct;
      maxSimultaneousPositions      = c.risk_profile.max_simultaneous_positions;
      maxCapitalDeployedPct         = c.risk_profile.max_capital_deployed_pct;
      maxNetDelta                   = c.portfolio_greek_limits.max_net_delta;
      maxNetVega                    = c.portfolio_greek_limits.max_net_vega;
      maxNetGamma                   = c.portfolio_greek_limits.max_net_gamma;
    });

    await attempt(async () => { portfolioOverview = await getPortfolioOverview(); });

    await attempt(async () => {
      try {
        positions = await refreshPositionPrices();
      } catch {
        positions = await getPositions();
      }
      positionsLoaded = true;
    });

    await attempt(async () => {
      const m = await getMarketState();
      marketState     = m;
      mockSpyPrice    = m.spy_price;
      mockSpySma20    = Math.round((m.spy_sma20 ?? 750.0) * 100) / 100;
      mockVixClose    = m.vix_close ?? 14.5;
      mockDailyReturn = Math.round((m.spy_daily_return ?? 0.005) * 100 * 100) / 100;
      const ivrs      = m.underlying_ivrs ?? {};
      mockIvrs        = Object.entries(ivrs).map(([k, v]) => `${k}:${v}`).join(',') || 'SPY:25';
      mockCatalysts   = (m.catalyst_dates || []).join(', ');
    });

    await attempt(async () => { observation        = await getPortfolioObservation(); });
    await attempt(async () => { postMortems        = await getPostMortems(); });
    await attempt(async () => { opportunityRecords = await getOpportunityLedger(); });
    await attempt(async () => { diagnostics        = await getPerformanceDiagnostics(); });
    // Never fabricate PAPER on fetch failure (#475) — a live backend
    // whose status endpoint 500s must read as unknown, not falsely safe.
    try { tradingMode = (await getExecutorStatus()).trading_mode ?? 'paper'; } catch { tradingMode = 'unknown'; }

    loadFailed = anyFailed;
    if (anyFailed) toast('Some data failed to load — values shown may be incomplete.', 'error');
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

  // GreeksPanel's breach alert (now living in B00's BookCard on the Books tab)
  // sends the operator back to the Overview position list — the tab switch has
  // to render before the anchor exists to scroll to, hence the tick().
  async function goToPositions() {
    activeTab = 'overview';
    await tick();
    document.getElementById('position-scanner')?.scrollIntoView({ behavior: 'smooth' });
  }
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
            <!-- The basis mark: two legs of a spread; the gap is the basis
                 (matches frontend/public/favicon.svg). -->
            <svg class="w-7 h-7 select-none" viewBox="0 0 64 64" aria-hidden="true">
              <!-- #878: literal favicon colors (ink/gold/cream), deliberately
                   outside the theme tokens so the mark matches the tab icon
                   exactly in both themes. -->
              <rect width="64" height="64" rx="14" fill="#12141a" />
              <rect x="13" y="21" width="38" height="7" rx="3.5" fill="#d9a441" />
              <rect x="25" y="36" width="26" height="7" rx="3.5" fill="#e9e4d6" />
            </svg>
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

    <!-- ── Overview Tab ──────────────────────────────────────────────── -->
    {#if activeTab === 'overview'}
      <!-- Verdict block (#890): every ackable item is its own row with its
           own inline reason form — supersedes the old top-of-page P1 Alert
           block (its actionable half) and folds close-in-flight into its
           collapsed informational section (its other half). -->
      <AttentionBlock
        onClosePosition={handleClosePosition}
        onNavigate={(tab) => { activeTab = tab as typeof activeTab; }}
        fleetNav={portfolioOverview ? formatDollar(portfolioOverview.fleet_nav) : null}
        openPositionCount={positionsLoaded ? openPositionCount : null}
      />
      <!-- Account Overview — no card renders a value before its fetch lands
           (#861): a fabricated headline is a false claim, not a placeholder.
           DESIGN-890 §2: on mobile these COLLAPSE into AttentionBlock's header
           subtitle and the risk-settings link dies; full cards are desktop-only. -->
      <section class="hidden md:grid grid-cols-2 lg:grid-cols-4 gap-4 mb-8">
        {#if portfolioOverview}
          <MetricCard
            label="Fleet NAV"
            value={formatDollar(portfolioOverview.fleet_nav)}
            subtext="{portfolioOverview.active_books} executor books · ledger"
          />
          <MetricCard
            label="Broker NAV"
            value={portfolioOverview.broker_nav != null ? formatDollar(portfolioOverview.broker_nav) : '—'}
            subtext={portfolioOverview.broker_nav_captured_at
              ? `as of ${new Date(portfolioOverview.broker_nav_captured_at).toLocaleString()} · ${portfolioOverview.broker}`
              : `no snapshot yet · ${portfolioOverview.broker}`}
          />
        {:else}
          {#each ['Fleet NAV', 'Broker NAV'] as label}
            <div class="carbon-card p-4" class:animate-pulse={!loadFailed}>
              <span class="block text-xs font-semibold uppercase tracking-wider text-ctp-overlay0 mb-1">{label}</span>
              {#if loadFailed}
                <span class="block text-xs font-bold text-ctp-red">failed to load — check backend</span>
              {:else}
                <div class="h-6 bg-ctp-surface0 rounded w-24 mt-1"></div>
              {/if}
            </div>
          {/each}
        {/if}
        <div class="carbon-card p-4 flex flex-col justify-between">
          <div>
            <span class="block text-xs font-semibold uppercase tracking-wider text-ctp-overlay0 mb-1">
              Open Positions
            </span>
            <span class="block text-xl font-bold carbon-mono text-ctp-text">
              {positionsLoaded ? openPositionCount : '—'}
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

      <!-- Greeks/Safeguards moved to B00's BookCard workbench detail on the
           Books tab (#890 step 5) — both are scoped to B00 server-side
           (#889) and no longer belong on the executor-scope Overview. -->
      {#if observation}
        <div id="position-scanner" style="scroll-margin-top: 5rem;">
          <PositionRow {observation} onClosePosition={handleClosePosition} onRollPosition={handleRollPosition} />
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
      <BooksTab onDataChanged={loadData} onReducePositions={goToPositions} />
    {/if}

    <!-- ── Settings Tab ──────────────────────────────────────────────── -->
    {#if activeTab === 'settings'}
      <div class="space-y-6 mt-2">
        <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <!-- Portfolio Config -->
          <section class="carbon-card p-6">
            <h2 class="text-base font-bold text-ctp-text mb-5">Portfolio Risk & Greek Limits</h2>
            {#if !config}
              <p class="text-sm text-ctp-overlay0" class:animate-pulse={!loadFailed}>
                {loadFailed ? 'Configuration failed to load — check backend.' : 'Loading configuration…'}
              </p>
            {:else}
            <form onsubmit={handleSaveConfig}>
              <div class="space-y-5">
                <div class="bg-ctp-crust p-4 rounded-lg border border-ctp-surface0">
                  <h3 class="font-bold text-xs text-ctp-mauve uppercase tracking-wider mb-3">Manual Book (B00)</h3>
                  <div class="space-y-3">
                    <!-- #860: this number scopes the MANUAL lane's capital
                         gates only — executor books read their own envelope
                         basis and the overview headline reads the ledger. -->
                    <FormField label="B00 capital ($)" hint="Capital for the manual lane's risk gates — executor books are unaffected">
                      <input type="number" bind:value={totalNav} class={inputCls} />
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
            {/if}
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
            {#if !marketState}
              <p class="text-sm text-ctp-overlay0" class:animate-pulse={!loadFailed}>
                {loadFailed ? 'Telemetry failed to load — check backend.' : 'Loading telemetry…'}
              </p>
            {:else}
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
            {/if}
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
