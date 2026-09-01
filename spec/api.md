# API Reference

> Part of the [modular specification](README.md). This table is built from the actual routes in [backend/main.py](../backend/main.py) and the Pydantic schemas in [backend/models.py](../backend/models.py) — it reflects the implemented surface, not just intent. Base path: `/api`. The live contract is always `GET /openapi.json`; the frontend regenerates types from it via `pixi run sync-types`.

## Endpoints

### Portfolio
| Method | Path | Purpose | Response model |
|---|---|---|---|
| GET | `/api/portfolio/config` | Read portfolio + risk + Greek-limit config | `PortfolioConfigSchema` |
| POST | `/api/portfolio/config` | Update portfolio config | `PortfolioConfigSchema` |
| GET | `/api/portfolio/overview` | Console headline (#860): fleet ledger NAV (active executor books, B00 excluded) + broker's last-captured NetLiquidation | `PortfolioOverviewSchema` |
| GET | `/api/portfolio/observation` | **Layer A** — lifecycle scan + Greeks + safeguards + market state | (composite JSON) |
| GET | `/api/attention` | Triage-first "what needs you" surface — halts, P1 actions, reconciliation drift, partial orders, Flex discrepancies, delivery gaps, broker errors, unresolved urgent events, composed from existing queries with no new persisted state | `AttentionResponse` |

### Positions
| Method | Path | Purpose | Response model |
|---|---|---|---|
| GET | `/api/positions` | List all positions (OPEN/CLOSED/EXPIRED) | `List[PositionSchema]` |
| POST | `/api/positions` | Create position; **422 if intent journal incomplete** | `PositionSchema` |
| POST | `/api/positions/refresh` | Fetch live option quotes, update `current_value_per_share` | `List[PositionSchema]` |
| GET | `/api/positions/{position_id}` | Read one position | `PositionSchema` |
| POST | `/api/positions/{position_id}/close` | Close + create immutable post-mortem; **409 on executor-book positions** unless `acknowledge_broker_divergence` (#279) | `ClosurePostMortemSchema` |
| POST | `/api/positions/{position_id}/roll` | Defensive roll (net-credit-only, ≤2 rolls, direction rules) | `PositionSchema` |
| GET | `/api/positions/post-mortems` | List all post-mortems | `List[ClosurePostMortemSchema]` |
| GET | `/api/positions/{position_id}/post-mortem` | Read one post-mortem | `ClosurePostMortemSchema` |

### Playbooks
| Method | Path | Purpose | Response model |
|---|---|---|---|
| GET | `/api/playbooks` | List all playbooks | `List[PlaybookDefinitionSchema]` |
| POST | `/api/playbooks` | Create playbook (data-injected, no code change) | `PlaybookDefinitionSchema` |

### Market (Layer B)
| Method | Path | Purpose | Response model |
|---|---|---|---|
| GET | `/api/market/state` | Current regime + telemetry | `MarketStateSchema` |
| POST | `/api/market/state` | Manually set telemetry; regime recomputed | `MarketStateSchema` |
| POST | `/api/market/fetch` | Fetch delayed SPY/VIX from IB Gateway; recompute regime | `MarketStateSchema` |

### Opportunities (Layer C)
| Method | Path | Purpose | Response model |
|---|---|---|---|
| GET | `/api/opportunity/scan` | Scan playbooks → eligible + suppressed candidates | `OpportunityScanResult` |
| POST | `/api/opportunity/spec/{playbook_id}` | Generate trade spec (hard blocks + warnings + spec) | `TradeSpecResult` |
| GET | `/api/opportunity/ledger` | List opportunity records | `List[OpportunityRecordSchema]` |
| POST | `/api/opportunity/ledger` | Log an opportunity (accepted/bypassed) | `OpportunityRecordSchema` |
| PATCH | `/api/opportunity/ledger/{record_id}` | Backfill `outcome_if_taken` | `OpportunityRecordSchema` |

### Performance
| Method | Path | Purpose | Response model |
|---|---|---|---|
| GET | `/api/performance/diagnostics` | Per-playbook win rate / profit factor / avg RoR / CAGR / Sharpe / max drawdown + SPY benchmark | `PerformanceDiagnosticsSchema` |

### Supervision console (kill switch, books, audit)
| Method | Path | Purpose | Response model |
|---|---|---|---|
| GET | `/api/trading-control` | All control scopes + sentinel-halt flag | `TradingControlView` |
| POST | `/api/trading-control` | Set a scope's state with a typed reason — the ONLY resume surface (ADR-0008). A RESUME may carry `ack: {"rule": "<rule id>"}` (#931) to acknowledge that rule's most recent finding — the identity/magnitude snapshot is resolved server-side from the audit ledger, never taken from the client; 400 if that rule has no current evidence for the scope, or if `ack` is sent alongside a non-ACTIVE state | `TradingControlView` |
| GET | `/api/books` | Per-book summaries with the Live Gate checklist | `BooksView` |
| GET | `/api/audit-events` | Filterable audit trail (book, date, event type, limit) | `List[AuditEventSchema]` |
| GET | `/api/executor/status` | Heartbeat age, last reconciliation, last digest delivery | `ExecutorStatusSchema` |
| GET | `/api/orders/live` | What the system currently believes is resting at the broker — ref, book, plain-English spread label, order type/TIF/status — for direct comparison against the IBKR app during an incident | `List[LiveOrderSchema]` |
| GET | `/api/reconciliation/latest` | Newest run, but an unresolved DRIFT run wins over a later CLEAN snapshot — the halt it caused persists until a human resolves it (ADR-0008) (404 before the first run) | `ReconciliationRunSchema` |
| POST | `/api/reconciliation/{run_id}/resolve` | Record the human explanation on a drift run — never auto-resumes (ADR-0008) | `ReconciliationRunSchema` |
| POST | `/api/resolution/external-close` | Book a broker-side close: CLOSED at stated value, cash moved, MANUAL post-mortem, audited | `ClosurePostMortemSchema` |
| POST | `/api/resolution/partial-order` | Terminalize a PARTIAL order row, releasing its encumbrance and slot — records only the resolution itself; the partial's cash/position consequences must be recorded first via external-close/cash | `PartialOrderResolveResult` |
| POST | `/api/resolution/cash` | Signed book-cash correction with a mandatory reason, audited | `CashAdjustmentResult` |
| POST | `/api/resolution/flex-ack` | Acknowledge a weekly Flex-audit discrepancy exec_id with a reason — stops it re-alerting at urgent priority, without correcting the books | `FlexAckResult` |
| GET | `/api/analysis/fill-quality` | Measured slippage vs decided mid (ladder concession + market movement) against the $5/contract haircut | `FillQualityReport` |
| GET | `/api/analysis/leaderboard` | Books ranked by expectancy after haircut + knob-sweep monotonicity verdicts (sample-gated) | `LeaderboardReport` |
| GET | `/api/analysis/evidence-verdict` | The project's single reproducible evidence-ledger summary — composes existing pre-registered judgments only, no live null-drill computation | `EvidenceVerdictSchema` |
| GET | `/api/analysis/regime-hit-rate` | Entry-day regime vs closed outcome, overall and per engine variant | `RegimeHitRateReport` |

## Schemas

Request/response shapes are defined as Pydantic models in [backend/models.py](../backend/models.py). The domain shapes (`PlaybookDefinitionSchema`, `OptionLegSchema`, `OperationalJournalEntrySchema`, `PositionSchema`, `ClosurePostMortemSchema`, `OpportunityRecordSchema`) mirror the canonical interfaces in [data-models.md](data-models.md).

Endpoint-specific models:
- `OpportunityScanResult` — wraps eligible + suppressed candidate cards (each with `eligible: bool` and a suppression reason).
- `TradeSpecResult` — the generated spec plus `hard_blocks[]` (uncircumventable) and `warnings[]` (acknowledgeable). Validation rules in [domain-rules.md → Validation](domain-rules.md#validation--common-sense-kill-switch).
- `ClosePositionRequest` — body for closing a position (current value, exit trigger, actual move %, lesson tags).
- `UpdateOutcomeRequest` — body for the ledger PATCH (`outcome_if_taken`).
- `PerformanceDiagnosticsSchema` — per-playbook metrics + benchmarks. Annualized figures (CAGR on capital at risk, Sharpe) are **sample-gated** in [backend/performance.py](../backend/performance.py): `null` below 10 trades or a 30-day span — never fabricated. Max drawdown is a dollar figure over the actual trade sequence. The SPY benchmark CAGR annualizes the stored `index_history` closes (≥180-day span required); BXM stays `null` (no free data source).

**Source of truth:** [backend/main.py](../backend/main.py) (routes), [backend/models.py](../backend/models.py) (schemas).
