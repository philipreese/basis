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
    getBooks,
  } from './lib/api';
  import type {
    PortfolioConfig, PortfolioOverview, Position, MarketState, PortfolioObservation,
    OpportunityScanResult, TradeSpecResult,
    ClosurePostMortem, OpportunityRecord, PerformanceDiagnostics,
    ClosePositionRequest, RollPositionRequest, ScannedPosition,
    BookSummary, ExecutorStatus,
  } from './lib/api';
  import { TABS, normalizeTab, filterBooks, SCORECARD_URL, type TabId, type LabSection } from './lib/consoleNav';
  import ShareBookSummary      from './lib/ShareBookSummary.svelte';
  import PracticeReviewsPanel  from './lib/PracticeReviewsPanel.svelte';
  import KillSwitchCard        from './lib/KillSwitchCard.svelte';
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
  import FormField             from './lib/ui/FormField.svelte';
  import Snackbar              from './lib/ui/Snackbar.svelte';
  import { toast }             from './lib/ui/snackbar.svelte.ts';
  import { formatDollar }      from './lib/formatters';
  import {
    type ColorMode, THEME_CHROME_COLOR, THEME_STORAGE_KEY,
    colorModeLabel, nextColorMode, parseColorMode, resolveDark,
  } from './lib/theme';
  import {
    IconHome, IconResearch, IconLab, IconBooks, IconSettings,
    IconLightMode, IconDarkMode, IconAutoMode, IconRefresh, IconBack,
  } from './lib/ui/icons';

  let config               = $state<PortfolioConfig | null>(null);
  // #860: the overview headline — fleet ledger NAV + broker's last-seen NAV,
  // two labeled provenances; the editable config's total_nav is the manual
  // lane's (B00's) capital and no longer appears as the headline.
  let portfolioOverview    = $state<PortfolioOverview | null>(null);
  let positions            = $state<Position[]>([]);
  let marketState          = $state<MarketState | null>(null);
  let observation          = $state<PortfolioObservation | null>(null);
  let colorMode            = $state<ColorMode>('auto');
  let themeMedia: MediaQueryList | null = null;
  // #1133: Home · Books · Research · Options lab · Settings. Every options-
  // only surface (Scan, Analysis, Greek limits, telemetry, the position list)
  // is a section of the lab.
  let activeTab            = $state<TabId>('home');
  let labSection           = $state<LabSection>('overview');
  // Lab books and executor status, for Home's share-book cards, the money
  // check, the lab summary and Settings' read-only status. Null until a
  // fetch succeeds (#861: never a fabricated value).
  let books                = $state<BookSummary[] | null>(null);
  let executorStatus       = $state<ExecutorStatus | null>(null);
  const shareBooks         = $derived(books ? filterBooks(books, 'active') : []);
  const practiceBooks      = $derived(books ? filterBooks(books, 'practice') : []);

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

  onMount(() => {
    const media = matchMedia('(prefers-color-scheme: dark)');
    themeMedia = media;
    // Storage can throw (private window, blocked site data): the console
    // still works, it just won't remember the choice.
    try { colorMode = parseColorMode(localStorage.getItem(THEME_STORAGE_KEY)); } catch { /* keep auto */ }
    applyTheme();
    media.addEventListener('change', applyTheme);
    void loadData();
    return () => media.removeEventListener('change', applyTheme);
  });

  function applyTheme() {
    const dark = resolveDark(colorMode, themeMedia?.matches ?? true);
    document.documentElement.classList.toggle('dark', dark);
    document.documentElement.style.colorScheme = dark ? 'dark' : 'light';
    const meta = document.querySelector<HTMLMetaElement>('meta[name="theme-color"]');
    if (meta) meta.content = dark ? THEME_CHROME_COLOR.dark : THEME_CHROME_COLOR.light;
  }

  function cycleColorMode() {
    colorMode = nextColorMode(colorMode);
    try { localStorage.setItem(THEME_STORAGE_KEY, colorMode); } catch { /* not remembered */ }
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
    await attempt(async () => { books              = await getBooks(); });
    // Never fabricate PAPER on fetch failure (#475) — a live backend
    // whose status endpoint 500s must read as unknown, not falsely safe.
    try {
      executorStatus = await getExecutorStatus();
      tradingMode = executorStatus.trading_mode ?? 'paper';
    } catch {
      executorStatus = null;
      tradingMode = 'unknown';
    }

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
        // The server IGNORES a posted spy_rv20 (#1035) — it is computed from
        // index_history, never hand-typed — but the schema requires the field.
        spy_rv20: marketState?.spy_rv20 ?? 0,
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

  // Any tab name, current or pre-#1133 ('overview', 'scan', 'analysis'),
  // lands on a tab that exists — an unmatched name used to render nothing.
  function navigate(name: string) {
    const target = normalizeTab(name);
    activeTab = target.tab;
    if (target.lab) labSection = target.lab;
  }

  function goLab(section: LabSection) {
    activeTab = 'lab';
    labSection = section;
  }

  // GreeksPanel's breach alert (B00's BookCard on the Books tab) sends the
  // operator to the position list, which lives in the Options lab (#1133) —
  // the tab switch has to render before the anchor exists, hence the tick().
  async function goToPositions() {
    goLab('overview');
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
                onclick={() => { activeTab = 'home'; }}>
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
                <p class="text-xs text-ctp-subtext0 leading-none">markets lab</p>
            </div>
        </button>

        <!-- Desktop tab bar -->
        <nav class="hidden md:flex items-center gap-1 border-l border-ctp-surface0 ml-5 pl-5">
          {#each TABS as tab (tab.id)}
            <button
              onclick={() => { activeTab = tab.id; }}
              aria-current={activeTab === tab.id ? 'page' : undefined}
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
          onclick={cycleColorMode}
          class="flex items-center gap-1.5 p-2 rounded bg-ctp-surface0 text-ctp-subtext1 text-xs font-medium hover:ring-2 hover:ring-ctp-surface1 transition"
          aria-label={`Color theme: ${colorModeLabel(colorMode)}. Change color theme`}
          title="Color theme: Auto follows your device"
        >
          {#if colorMode === 'light'}<IconLightMode size={15} strokeWidth={2} />
          {:else if colorMode === 'dark'}<IconDarkMode size={15} strokeWidth={2} />
          {:else}<IconAutoMode size={15} strokeWidth={2} />{/if}
          <span>{colorModeLabel(colorMode)}</span>
        </button>
      </div>
    </div>
  </header>

  <!-- ── Supervision Status Strip (all tabs, #73) ─────────────────────── -->
  <StatusStrip />

  <!-- ── Main ─────────────────────────────────────────────────────────── -->
  <main class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6 grow w-full pb-24 md:pb-8">

    <!-- ── Home Tab (#1133) ──────────────────────────────────────────── -->
    {#if activeTab === 'home'}
      <!-- Verdict block (#890): every ackable item is its own row with its
           own inline reason form. #1133: the practice-book review flags are
           one line here that links to the Options lab, where their rows live. -->
      <AttentionBlock
        onClosePosition={handleClosePosition}
        onNavigate={navigate}
        onShowPractice={() => goLab('overview')}
      />

      <h2 class="text-xs font-bold uppercase tracking-wider text-ctp-overlay0 mb-2">Books that matter</h2>
      <section class="grid grid-cols-1 lg:grid-cols-2 gap-3 mb-6" data-testid="home-share-books">
        {#if books === null}
          <div class="carbon-card p-4 text-sm text-ctp-overlay0" class:animate-pulse={!loadFailed}>
            {loadFailed ? 'Books failed to load — check backend.' : 'Loading books…'}
          </div>
        {:else if shareBooks.length === 0}
          <div class="carbon-card p-4 text-sm text-ctp-overlay0">No active share books.</div>
        {:else}
          {#each shareBooks as book (book.id)}
            <ShareBookSummary {book} onOpen={() => { activeTab = 'books'; }} />
          {/each}
        {/if}
      </section>

      <!-- Money check: the ledger's NAV beside the broker's last-seen NAV
           (#860, two labeled provenances) and last night's reconciliation.
           No value renders before its fetch lands (#861). -->
      <h2 class="text-xs font-bold uppercase tracking-wider text-ctp-overlay0 mb-2">Money check</h2>
      <section class="carbon-card p-4 mb-6 grid grid-cols-2 gap-3" data-testid="home-money-check">
        {#if portfolioOverview}
          <div class="min-w-0">
            <div class="text-[11px] text-ctp-overlay0">Fleet NAV · basis says</div>
            <div class="carbon-mono text-ctp-text font-bold" data-testid="home-fleet-nav">{formatDollar(portfolioOverview.fleet_nav)}</div>
            <div class="text-[10px] text-ctp-overlay0">{portfolioOverview.managed_books} executor books ({portfolioOverview.active_books} active) · ledger</div>
          </div>
          <div class="min-w-0">
            <div class="text-[11px] text-ctp-overlay0">Broker NAV · broker says</div>
            <div class="carbon-mono text-ctp-text font-bold" data-testid="home-broker-nav">
              {portfolioOverview.broker_nav != null ? formatDollar(portfolioOverview.broker_nav) : '—'}
            </div>
            <div class="text-[10px] text-ctp-overlay0">
              {portfolioOverview.broker_nav_captured_at
                ? `as of ${new Date(portfolioOverview.broker_nav_captured_at).toLocaleString()} · ${portfolioOverview.broker}`
                : `no snapshot yet · ${portfolioOverview.broker}`}
            </div>
          </div>
        {:else}
          <div class="col-span-2 text-sm" class:animate-pulse={!loadFailed}>
            {#if loadFailed}
              <span class="font-bold text-ctp-red">NAV failed to load — check backend</span>
            {:else}
              <span class="text-ctp-overlay0">Loading NAV…</span>
            {/if}
          </div>
        {/if}
        {#if tradingMode === 'paper'}
          <!-- #1148: in PAPER the broker side is IBKR's play-money paper
               account, which is never expected to match the ledger — the
               side-by-side read as an alarm without this note. Reconciliation
               below still runs as-is, since it compares positions, not NAV. -->
          <div class="col-span-2 text-[11px] text-ctp-overlay0" data-testid="home-money-check-paper-note">
            Paper account — the broker's play-money balance isn't expected to match.
          </div>
        {/if}
        <div class="col-span-2 text-xs" data-testid="home-records">
          {#if executorStatus === null}
            <span class="text-ctp-yellow">Reconciliation status unknown</span>
          {:else if executorStatus.last_reconciliation_result === null}
            <span class="text-ctp-overlay0">No reconciliation has run yet</span>
          {:else if executorStatus.last_reconciliation_result === 'CLEAN'}
            <span class="text-ctp-green">Records match</span>
            <span class="text-ctp-overlay0">· checked {executorStatus.last_reconciliation_at ? new Date(executorStatus.last_reconciliation_at).toLocaleString() : '—'}</span>
          {:else}
            <button type="button" class="font-bold text-ctp-red hover:underline" onclick={() => { activeTab = 'books'; }}>
              Reconciliation {executorStatus.last_reconciliation_result}{executorStatus.last_reconciliation_resolved ? ' (resolved)' : ''} · see Books →
            </button>
          {/if}
        </div>
      </section>

      <h2 class="text-xs font-bold uppercase tracking-wider text-ctp-overlay0 mb-2">Research</h2>
      <button type="button" class="carbon-card p-4 mb-6 w-full text-left space-y-2 hover:bg-ctp-surface0/30 transition"
              onclick={() => { activeTab = 'research'; }} data-testid="home-research">
        <div class="flex justify-between gap-2 text-sm">
          <span class="text-ctp-text">Monthly brief</span>
          <span class="text-ctp-overlay0">coming with #1131</span>
        </div>
        <div class="flex justify-between gap-2 text-sm">
          <span class="text-ctp-text">Trackers</span>
          <span class="text-ctp-overlay0">run outside basis</span>
        </div>
      </button>
    {/if}

    <!-- ── Research Tab (#1133; the brief itself is #1131) ─────────────── -->
    {#if activeTab === 'research'}
      <div class="space-y-4 mt-2" data-testid="research-tab">
        <div>
          <h2 class="text-xl font-bold text-ctp-text tracking-tight">Research</h2>
          <p class="text-xs text-ctp-overlay0">Monthly brief · trackers · scorecard</p>
        </div>
        <section class="carbon-card p-4 space-y-2" data-testid="research-brief">
          <h3 class="font-bold text-ctp-text">Monthly brief and picks</h3>
          <p class="text-sm text-ctp-subtext0">
            Coming with #1131. The monthly research brief and your picks will land here; nothing is shown until it exists.
          </p>
        </section>
        <section class="carbon-card p-4 space-y-2" data-testid="research-trackers">
          <h3 class="font-bold text-ctp-text">Trackers</h3>
          <p class="text-sm text-ctp-subtext0">Trackers run outside basis; see Home app.</p>
        </section>
        <a href={SCORECARD_URL} target="_blank" rel="noopener noreferrer"
           class="carbon-card p-4 min-h-12 flex items-center justify-between text-sm text-ctp-text hover:bg-ctp-surface0/30 transition"
           data-testid="research-scorecard">
          Research scorecard <span class="text-ctp-mauve">open →</span>
        </a>
      </div>
    {/if}

    <!-- ── Options lab (#1133): practice summary, review flags, positions,
         and every options-only tool as a section ──────────────────────── -->
    {#if activeTab === 'lab'}
      <div class="mb-4 flex items-start justify-between gap-3">
        <div class="min-w-0">
          <h2 class="text-xl font-bold text-ctp-text tracking-tight">Options lab</h2>
          <p class="text-xs text-ctp-overlay0">Paper practice · no book here is headed for real money</p>
        </div>
        {#if labSection !== 'overview'}
          <button type="button" onclick={() => { labSection = 'overview'; }} data-testid="lab-back"
                  class="shrink-0 min-h-10 flex items-center gap-1 text-xs font-bold text-ctp-mauve hover:underline">
            <IconBack size={14} strokeWidth={2} /> Lab
          </button>
        {/if}
      </div>
    {/if}

    {#if activeTab === 'lab' && labSection === 'overview'}
      <div class="space-y-6" data-testid="lab-overview">
        <section class="carbon-card p-4 grid grid-cols-3 gap-2" data-testid="lab-summary">
          <div class="min-w-0">
            <div class="text-[11px] text-ctp-overlay0">Practice books</div>
            <div class="carbon-mono text-ctp-text font-bold">{books === null ? '—' : practiceBooks.length}</div>
          </div>
          <div class="min-w-0">
            <div class="text-[11px] text-ctp-overlay0">Open positions</div>
            <div class="carbon-mono text-ctp-text font-bold">{positionsLoaded ? openPositionCount : '—'}</div>
          </div>
          <div class="min-w-0">
            <div class="text-[11px] text-ctp-overlay0">Paper P&amp;L</div>
            <div class="carbon-mono text-ctp-text font-bold">
              {books === null ? '—' : formatDollar(practiceBooks.reduce((s, b) => s + b.pnl, 0))}
            </div>
          </div>
        </section>

        <PracticeReviewsPanel onClosePosition={handleClosePosition} />

        <div class="carbon-card divide-y divide-ctp-surface0" role="group" aria-label="Lab sections" data-testid="lab-sections">
          {#each ([
            ['scan', 'Scan (diagnostic)', "tonight's playbook scan"],
            ['analysis', 'Analysis', 'evidence, leaderboard, post-mortems'],
            ['limits', 'Greeks and risk limits', 'B00 capital and limits'],
            ['telemetry', 'Market telemetry', 'IVR, catalysts, live fetch'],
          ] as const) as [id, label, hint]}
            <button type="button" onclick={() => goLab(id)} data-testid="lab-open-{id}"
                    class="w-full min-h-13 px-4 py-3 flex items-center justify-between gap-3 text-left text-sm text-ctp-text hover:bg-ctp-surface0/30 transition">
              <span class="font-semibold">{label}</span>
              <span class="text-xs text-ctp-overlay0 text-right">{hint} →</span>
            </button>
          {/each}
        </div>

        {#if marketState}
          <MarketContextRibbon {marketState} />
        {/if}

        <!-- The options position list (close / roll), moved from Overview. -->
        {#if observation}
          <div id="position-scanner" style="scroll-margin-top: 5rem;">
            <PositionRow {observation} onClosePosition={handleClosePosition} onRollPosition={handleRollPosition} />
          </div>
        {:else}
          <div class="carbon-card p-10 text-center text-ctp-overlay0">
            Loading position data…
          </div>
        {/if}
      </div>
    {/if}

    <!-- ── Lab › Scan (diagnostic, #315; a tab of its own before #1133) ── -->
    {#if activeTab === 'lab' && labSection === 'scan'}
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

    <!-- ── Lab › Analysis (#315; reports #242-#244; a tab before #1133) ── -->
    {#if activeTab === 'lab' && labSection === 'analysis'}
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

    <!-- ── Settings Tab (#1133): safety first; book setup is read-only ── -->
    {#if activeTab === 'settings'}
      <div class="space-y-4 mt-2 max-w-3xl" data-testid="settings-tab">
        <div>
          <h2 class="text-xl font-bold text-ctp-text tracking-tight">Settings</h2>
          <p class="text-xs text-ctp-overlay0">Safety first · book setup is read-only here</p>
        </div>

        <KillSwitchCard onGoHome={() => { activeTab = 'home'; }} />

        <!-- Only what an endpoint serves (#475: never a fabricated safe badge). -->
        <section class="carbon-card p-4 space-y-2" data-testid="settings-live-trading">
          <h3 class="font-bold text-ctp-text">Live trading</h3>
          <dl class="grid grid-cols-[minmax(0,1fr)_auto] gap-x-3 gap-y-1.5 text-sm">
            <dt class="text-ctp-subtext0">Mode (this console's backend)</dt>
            <dd class="carbon-mono font-bold {tradingMode === 'paper' ? 'text-ctp-yellow' : 'text-ctp-red'}" data-testid="settings-mode">
              {tradingMode === 'unknown' ? 'UNKNOWN' : tradingMode.toUpperCase()}
            </dd>
            <dt class="text-ctp-subtext0">Last executor run</dt>
            <dd class="carbon-mono {executorStatus === null || executorStatus.stale ? 'text-ctp-red' : 'text-ctp-text'}">
              {executorStatus === null ? 'unknown' : executorStatus.heartbeat_at ? new Date(executorStatus.heartbeat_at).toLocaleString() : 'never'}
            </dd>
            <dt class="text-ctp-subtext0">Armed</dt>
            <dd class="text-ctp-overlay0">not shown here</dd>
          </dl>
          <p class="text-xs text-ctp-overlay0">
            The console cannot see the arm. A live run is a dry run unless that run's own environment arms it (README, Executor (Live)).
          </p>
        </section>

        <section class="carbon-card p-4 space-y-3" data-testid="settings-share-setup">
          <h3 class="font-bold text-ctp-text">Share-book setup</h3>
          {#if books === null}
            <p class="text-sm text-ctp-overlay0">{loadFailed ? 'Books failed to load — check backend.' : 'Loading…'}</p>
          {:else if shareBooks.length === 0}
            <p class="text-sm text-ctp-overlay0">No active share books.</p>
          {:else}
            {#each shareBooks as book (book.id)}
              <dl class="grid grid-cols-[minmax(0,1fr)_auto] gap-x-3 gap-y-1 text-sm" data-testid="settings-share-{book.id}">
                <dt class="font-bold text-ctp-text col-span-2">{book.id} · {book.name}</dt>
                <dt class="text-ctp-subtext0">Status</dt>
                <dd class="carbon-mono">{book.status}{book.control_state !== 'ACTIVE' ? ` · ${book.control_state}` : ''}</dd>
                <dt class="text-ctp-subtext0">Config</dt>
                <dd class="carbon-mono">v{book.config_version} · {book.config_hash.slice(0, 8)}</dd>
                <dt class="text-ctp-subtext0">Symbols held</dt>
                <dd class="carbon-mono text-right">{(book.share_holdings ?? []).map(h => h.symbol).join(' ') || 'none yet'}</dd>
              </dl>
            {/each}
          {/if}
          <p class="text-xs text-ctp-overlay0">
            Changed through code review, not from the phone: menu, signal and weights live in backend/etf_trend.py,
            backend/turn_of_month.py and backend/seeds.py.
          </p>
        </section>

        <section class="carbon-card p-4 space-y-2" data-testid="settings-notifications">
          <h3 class="font-bold text-ctp-text">Notifications</h3>
          <dl class="grid grid-cols-[minmax(0,1fr)_auto] gap-x-3 gap-y-1.5 text-sm">
            <dt class="text-ctp-subtext0">Nightly digest</dt>
            <dd class={executorStatus?.last_digest_pushed === false ? 'text-ctp-red font-bold' : 'text-ctp-text'} data-testid="settings-digest">
              {executorStatus === null ? 'unknown'
                : executorStatus.last_digest_pushed === null ? 'none sent yet'
                : executorStatus.last_digest_pushed ? 'last one delivered' : 'last one UNDELIVERED'}
            </dd>
            <dt class="text-ctp-subtext0">Urgent pushes</dt>
            <dd class={executorStatus?.last_urgent_pushed === false ? 'text-ctp-red font-bold' : 'text-ctp-text'} data-testid="settings-urgent">
              {executorStatus === null ? 'unknown'
                : executorStatus.last_urgent_pushed === null ? 'nothing to send last run'
                : executorStatus.last_urgent_pushed ? 'last one delivered' : 'last one UNDELIVERED'}
            </dd>
          </dl>
        </section>

        <p class="text-xs text-ctp-overlay0">
          Looking for Greek limits or market telemetry? They moved to the
          <button type="button" class="font-bold text-ctp-mauve hover:underline" onclick={() => goLab('limits')}>Options lab</button>.
        </p>
      </div>
    {/if}

    <!-- ── Lab › Greeks and risk limits (Settings before #1133) ─────────── -->
    {#if activeTab === 'lab' && labSection === 'limits'}
      <div class="max-w-3xl">
          <!-- Portfolio Config -->
          <section class="carbon-card p-4 sm:p-6">
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
      </div>
    {/if}

    <!-- ── Lab › Market telemetry (Settings before #1133) ──────────────── -->
    {#if activeTab === 'lab' && labSection === 'telemetry'}
      <div class="max-w-3xl">
          <!-- Market Telemetry -->
          <section class="carbon-card p-4 sm:p-6">
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
  <!-- Five equal columns that may shrink (minmax(0,1fr)): a fixed bar wider
       than the phone is what pushes the page sideways. -->
  <nav aria-label="Main" class="md:hidden fixed bottom-0 left-0 right-0 z-50 border-t border-ctp-surface0 bg-ctp-crust/95 backdrop-blur-md grid grid-cols-5 items-center px-1 py-1.5">
    {#each TABS as tab (tab.id)}
      {@const id = tab.id}
      {@const isActive = activeTab === id}
      <button
        onclick={() => { activeTab = id; }}
        aria-current={isActive ? 'page' : undefined}
        class="min-h-12 min-w-0 flex flex-col items-center justify-center gap-0.5 text-[11px] font-bold transition px-1
          {isActive ? 'text-ctp-mauve' : 'text-ctp-overlay0'}"
      >
        {#if id === 'home'}
          <IconHome size={18} strokeWidth={1.75} />
        {:else if id === 'books'}
          <IconBooks size={18} strokeWidth={1.75} />
        {:else if id === 'research'}
          <IconResearch size={18} strokeWidth={1.75} />
        {:else if id === 'lab'}
          <IconLab size={18} strokeWidth={1.75} />
        {:else}
          <IconSettings size={18} strokeWidth={1.75} />
        {/if}
        <span class="truncate max-w-full">{tab.short}</span>
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
