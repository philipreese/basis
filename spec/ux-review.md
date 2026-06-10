# UX Review — User Flow & Findings

> Part of the [modular specification](README.md). Reviews the implemented frontend ([frontend/src/](../frontend/src/)) for flow intuitiveness and rough edges. Findings here are **documented, not yet applied** — they feed [roadmap.md](roadmap.md). Effort estimates are rough (S = <½ day, M = ~1 day, L = multi-day).

## The app at a glance

Single-page app, four screens, gated by a session lock:

| Screen | Purpose |
|---|---|
| **Positions (Scanner)** | Layer A — market ribbon, account overview, Greeks, safeguards, position lifecycle cards |
| **Opportunities** | Layer C — scan → eligible/suppressed candidates → trade spec + intent journal |
| **Performance** | post-mortems, per-playbook diagnostics, opportunity ledger |
| **Settings** | portfolio/risk config, market telemetry (manual or "Fetch Live") |

## Primary flow

```
Load → session LOCKED → review Positions (P1/P2/P3) → "Acknowledge & Unlock"
     → Opportunities → Scan → pick candidate → Trade Spec → fill Intent Journal → Save
     → Performance (post-mortems / diagnostics) · Settings (risk + telemetry)
```

The lock enforces the spec's sequencing rule (position management before new entries) at the UX level — see [ADR-0005](decisions.md#adr-0005--session-lock-gating). P1 "CLOSE NOW" items are aggregated above the fold in red on load. This is a **strong, intentional flow**; the issues below are polish, not structure.

## Findings (prioritized)

### 1. Design-system drift — `M`
Three feature components use hardcoded slate/rose Tailwind classes instead of the Catppuccin tokens the rest of the app uses, so they don't theme correctly (notably in light mode):
- [SafeguardsPanel.svelte](../frontend/src/lib/SafeguardsPanel.svelte)
- [PerformanceDashboard.svelte](../frontend/src/lib/PerformanceDashboard.svelte)
- [OpportunityLedger.svelte](../frontend/src/lib/OpportunityLedger.svelte)

**Fix:** replace hardcoded colors with `--ctp-*` token classes (`text-ctp-red`, `bg-ctp-mantle`, `border-ctp-surface0`, …) to match the other components.

### 2. Missing intermediate states — `S–M`
- No loading skeleton while "Fetch Live" / position refresh is in flight — only a trailing toast. A brief skeleton under the Greeks/ribbon would prevent the "did it work?" gap.
- Form validation shows a red outline but thin inline guidance (e.g. malformed IVR input surfaces only in a toast). Add inline help text on invalid fields.

### 3. Flow gaps — `S–M`
- **Greeks limit exceeded** shows a red state but no call to action. Add a CTA / link toward the close flow when a limit is breached.
- **Opportunity override** lets the user bypass a suppressed playbook but records only "User override" — the ledger's audit value drops. Capture a short justification before override (the backend ledger already has a `bypass_reason` field to store it).
- **Session re-lock** control isn't discoverable — no tooltip or keyboard affordance. Add a tooltip; consider an Enter-to-acknowledge shortcut in review mode.

### 4. Accessibility — `M`
- Severity is conveyed by color + emoji only (🛑/⚠️). Add `aria-label`s with the full severity word.
- Data tables (position legs, ledger) lack `scope="col"` headers and row roles — add WAI-ARIA table semantics.
- Modals trap focus and close on Escape, but don't autofocus the first input on open. Add `autofocus`.

## What's already good
- Above-the-fold P1 alerting, snackbar notifications (no layout shift), mobile bottom-nav + desktop status bar, strict formatting (currency/pct/DTE/date), and the gating model are all solid and match the spec's intent.

These items are sequenced in [roadmap.md → Near-term](roadmap.md#near-term--ux--polish).
