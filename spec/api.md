# API Reference

> Part of the [modular specification](README.md). This table is built from the actual routes in [backend/main.py](../backend/main.py) and the Pydantic schemas in [backend/models.py](../backend/models.py) — it reflects the implemented surface, not just intent. Base path: `/api`. The live contract is always `GET /openapi.json`; the frontend regenerates types from it via `pixi run sync-types`.

## Endpoints

### Portfolio
| Method | Path | Purpose | Response model |
|---|---|---|---|
| GET | `/api/portfolio/config` | Read portfolio + risk + Greek-limit config | `PortfolioConfigSchema` |
| POST | `/api/portfolio/config` | Update portfolio config | `PortfolioConfigSchema` |
| GET | `/api/portfolio/observation` | **Layer A** — lifecycle scan + Greeks + safeguards + market state | (composite JSON) |

### Positions
| Method | Path | Purpose | Response model |
|---|---|---|---|
| GET | `/api/positions` | List all positions (OPEN/CLOSED/EXPIRED) | `List[PositionSchema]` |
| POST | `/api/positions` | Create position; **422 if intent journal incomplete** | `PositionSchema` |
| POST | `/api/positions/refresh` | Fetch live option quotes, update `current_value_per_share` | `List[PositionSchema]` |
| GET | `/api/positions/{position_id}` | Read one position | `PositionSchema` |
| POST | `/api/positions/{position_id}/close` | Close + create immutable post-mortem | `ClosurePostMortemSchema` |
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

## Schemas

Request/response shapes are defined as Pydantic models in [backend/models.py](../backend/models.py). The domain shapes (`PlaybookDefinitionSchema`, `OptionLegSchema`, `OperationalJournalEntrySchema`, `PositionSchema`, `ClosurePostMortemSchema`, `OpportunityRecordSchema`) mirror the canonical interfaces in [data-models.md](data-models.md).

Endpoint-specific models:
- `OpportunityScanResult` — wraps eligible + suppressed candidate cards (each with `eligible: bool` and a suppression reason).
- `TradeSpecResult` — the generated spec plus `hard_blocks[]` (uncircumventable) and `warnings[]` (acknowledgeable). Validation rules in [domain-rules.md → Validation](domain-rules.md#validation--common-sense-kill-switch).
- `ClosePositionRequest` — body for closing a position (current value, exit trigger, actual move %, lesson tags).
- `UpdateOutcomeRequest` — body for the ledger PATCH (`outcome_if_taken`).
- `PerformanceDiagnosticsSchema` — per-playbook metrics + benchmarks. Annualized figures (CAGR on capital at risk, Sharpe) are **sample-gated** in [backend/performance.py](../backend/performance.py): `null` below 10 trades or a 30-day span — never fabricated. Max drawdown is a dollar figure over the actual trade sequence. The SPY benchmark CAGR annualizes the stored `index_history` closes (≥180-day span required); BXM stays `null` (no free data source).

**Source of truth:** [backend/main.py](../backend/main.py) (routes), [backend/models.py](../backend/models.py) (schemas).
