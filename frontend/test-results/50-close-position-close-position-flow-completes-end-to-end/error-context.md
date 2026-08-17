# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: 50-close-position.spec.ts >> close-position flow completes end to end
- Location: e2e\50-close-position.spec.ts:34:1

# Error details

```
Test timeout of 30000ms exceeded.
```

```
Error: locator.click: Test timeout of 30000ms exceeded.
Call log:
  - waiting for getByRole('button', { name: /Confirm Close/ })
    - locator resolved to <button disabled type="button" class="inline-flex items-center justify-center gap-1.5 font-semibold rounded-lg transition cursor-pointer↵    hover:scale-[1.02] active:scale-[0.98]↵    bg-ctp-red text-ctp-crust hover:bg-ctp-red/90 px-4 py-2 text-sm↵    disabled:opacity-40 disabled:cursor-not-allowed disabled:scale-100">…</button>
  - attempting click action
    2 × waiting for element to be visible, enabled and stable
      - element is not enabled
    - retrying click action
    - waiting 20ms
    2 × waiting for element to be visible, enabled and stable
      - element is not enabled
    - retrying click action
      - waiting 100ms
    53 × waiting for element to be visible, enabled and stable
       - element is not enabled
     - retrying click action
       - waiting 500ms

```

# Page snapshot

```yaml
- generic [ref=e3]:
  - banner [ref=e4]:
    - generic [ref=e5]:
      - generic [ref=e6]:
        - button "Α basis Options Playbook Automation" [ref=e7] [cursor=pointer]:
          - generic [ref=e8]: Α
          - generic [ref=e9]:
            - heading "basis" [level=1] [ref=e10]
            - paragraph [ref=e11]: Options Playbook Automation
        - navigation [ref=e12]:
          - button "Positions" [ref=e13] [cursor=pointer]
          - button "Opportunities" [disabled] [ref=e14]
          - button "Performance" [disabled] [ref=e16]
          - button "Books" [disabled] [ref=e18]
          - button "Settings" [disabled] [ref=e20]
      - button "Toggle theme" [ref=e23] [cursor=pointer]
  - generic [ref=e26]:
    - generic [ref=e27]: PAPER
    - generic [ref=e28]: GLOBAL ACTIVE
    - button "HALT" [ref=e31] [cursor=pointer]
    - generic [ref=e32]:
      - generic [ref=e33]: run never
      - generic [ref=e34]: recon —
  - main [ref=e35]:
    - generic [ref=e36]:
      - generic [ref=e37]:
        - generic [ref=e38]:
          - generic [ref=e39]: Market Context
          - generic [ref=e40]: CALM BULL
          - generic [ref=e41]: Bullish trend, low volatility — income strategies favoured
        - generic [ref=e42]:
          - generic [ref=e43]: SPY $758.00
          - generic [ref=e44]: ·
          - generic [ref=e45]: SMA20 $750.00
          - generic [ref=e46]: ·
          - generic [ref=e47]: VIX 14.5
          - generic [ref=e48]: ·
          - generic [ref=e49]: Day +0.5%
      - button "Regime score breakdown" [ref=e52] [cursor=pointer]
    - generic [ref=e59]:
      - paragraph [ref=e60]: Critical action required — close positions now
      - generic [ref=e64]:
        - generic [ref=e65]:
          - generic [ref=e66]:
            - generic [ref=e67]: SPY
            - generic [ref=e68]: BULL PUT SPREAD
          - paragraph [ref=e69]: CLOSE NOW
          - paragraph [ref=e70]: "Profit target reached: income trade profit of $60.00 meets 50% threshold of $50.00."
        - button "Close Now →" [ref=e71] [cursor=pointer]
    - generic [ref=e73]:
      - generic [ref=e74]:
        - paragraph [ref=e75]: Review your positions before trading
        - paragraph [ref=e76]: Check active positions, Greek limits, and exposure safeguards below. Once you've reviewed, unlock the session to access Opportunities, Performance, and Settings.
        - paragraph [ref=e77]: "Step 1 of 3: Review positions → Step 2: Scan opportunities → Step 3: Stage and save"
      - button "Acknowledge & Unlock →" [ref=e78] [cursor=pointer]
    - generic [ref=e79]:
      - generic [ref=e80]:
        - generic [ref=e81]: Total NAV
        - generic [ref=e82]: $10,000.00
        - generic [ref=e83]: Charles Schwab
      - generic [ref=e84]:
        - generic [ref=e85]: Account Type
        - generic [ref=e86]: Roth IRA
        - generic [ref=e87]: Level 3 — Spreads
      - generic [ref=e88]:
        - generic [ref=e89]: Execution Mode
        - generic [ref=e90]: PAPER
        - generic [ref=e91]: Manual sandbox
      - generic [ref=e92]:
        - generic [ref=e93]:
          - generic [ref=e94]: Open Positions
          - generic [ref=e95]: "1"
        - generic [ref=e96]: Unlock to edit settings
    - generic [ref=e97]:
      - generic [ref=e98]:
        - group [ref=e99]:
          - generic [ref=e100]: Net Delta (Δ)
        - generic [ref=e102]: "0.10"
        - generic [ref=e103]: "Limit: ±50"
      - generic [ref=e104]:
        - group [ref=e105]:
          - generic [ref=e106]: Net Theta (Θ)
        - generic [ref=e108]: "-0.02"
        - generic [ref=e109]: Daily decay reward
      - generic [ref=e110]:
        - group [ref=e111]:
          - generic [ref=e112]: Net Vega (V)
        - generic [ref=e114]: "-0.02"
        - generic [ref=e115]: "Limit: ±100"
      - generic [ref=e116]:
        - group [ref=e117]:
          - generic [ref=e118]: Net Gamma (Γ)
        - generic [ref=e120]: "0.00"
        - generic [ref=e121]: "Limit: ±10"
    - generic [ref=e123]:
      - heading "Active Positions" [level=2] [ref=e125]
      - article [ref=e127]:
        - generic [ref=e128]:
          - generic [ref=e129]:
            - generic [ref=e130]:
              - generic [ref=e131]: SPY
              - generic [ref=e132]: BULL PUT SPREAD
            - generic [ref=e133]: P1 — CLOSE NOW
          - heading "CLOSE NOW" [level=3] [ref=e135]
          - paragraph [ref=e136]: "Profit target reached: income trade profit of $60.00 meets 50% threshold of $50.00."
          - generic [ref=e137]: Profit per share $0.60 >= 50% of entry premium ($0.50)
        - button "Close Position Now →" [ref=e139] [cursor=pointer]
        - generic [ref=e141]:
          - generic [ref=e142]:
            - heading "Option Legs" [level=4] [ref=e143]
            - table [ref=e145]:
              - rowgroup [ref=e146]:
                - row [ref=e147]:
                  - columnheader "Dir" [ref=e148]
                  - columnheader "Strike" [ref=e149]
                  - columnheader "Type" [ref=e150]
                  - columnheader "Expiry" [ref=e151]
                  - columnheader "Δ" [ref=e152]
                  - columnheader "Θ" [ref=e153]
                  - columnheader "V" [ref=e154]
                  - columnheader "Γ" [ref=e155]
              - rowgroup [ref=e156]:
                - row [ref=e157]:
                  - cell "SHORT" [ref=e158]
                  - cell "700" [ref=e159]
                  - cell "PUT" [ref=e160]
                  - cell "June 18 2027" [ref=e161]
                  - cell "-0.30" [ref=e162]
                  - cell "0.05" [ref=e163]
                  - cell "0.10" [ref=e164]
                  - cell "0.010" [ref=e165]
                - row [ref=e166]:
                  - cell "LONG" [ref=e167]
                  - cell "695" [ref=e168]
                  - cell "PUT" [ref=e169]
                  - cell "June 18 2027" [ref=e170]
                  - cell "-0.20" [ref=e171]
                  - cell "0.03" [ref=e172]
                  - cell "0.08" [ref=e173]
                  - cell "0.010" [ref=e174]
          - generic [ref=e175]:
            - generic [ref=e176]:
              - generic [ref=e177]: Premium / Share
              - text: $1.00
              - generic [ref=e178]: Credit
            - generic [ref=e179]:
              - generic [ref=e180]: Total Cost
              - text: $100.00
              - generic [ref=e181]: ×100 × 1 contracts
            - generic [ref=e182]:
              - generic [ref=e183]: Current Value
              - text: $40.00
              - generic [ref=e184]: $0.40 / share
          - generic [ref=e185]:
            - generic [ref=e186]:
              - generic [ref=e187]: Max Profit
              - text: $100.00
              - generic [ref=e188]: $1.00 / share
            - generic [ref=e189]:
              - generic [ref=e190]: Max Loss
              - text: $400.00
              - generic [ref=e191]: $4.00 / share
  - generic [ref=e192]:
    - generic [ref=e193]: basis
    - generic [ref=e194]: ·
    - generic [ref=e195]: PAPER
    - generic [ref=e196]: ⚠ P1 ACTION REQUIRED
    - generic [ref=e197]: Aug 17, 2026
  - dialog "Close Position" [ref=e198]:
    - generic [ref=e199]:
      - generic [ref=e200]:
        - heading "Close Position" [level=2] [ref=e201]
        - button "Close" [ref=e202] [cursor=pointer]
      - generic [ref=e205]:
        - paragraph [ref=e206]: "Position: e2e-pos-1"
        - generic [ref=e207]:
          - generic [ref=e208]: Current Value / Share ($) *
          - spinbutton "Current Value / Share ($) *" [ref=e209]: "0.40"
        - generic [ref=e210]:
          - generic [ref=e211]: Exit Trigger *
          - combobox "Exit Trigger *" [ref=e212]:
            - option "Select a reason…"
            - option "Profit Target hit" [selected]
            - option "Loss Limit hit"
            - option "Time Rule (≤21 DTE)"
            - option "Catalyst Rule"
            - option "Manual decision"
        - generic [ref=e213]:
          - generic [ref=e214]: Actual Underlying Move (%) *
          - spinbutton "Actual Underlying Move (%) * Enter as a decimal, e.g. -1.5 for −1.5%" [active] [ref=e215]: "1.0"
          - generic [ref=e216]: Enter as a decimal, e.g. -1.5 for −1.5%
        - generic [ref=e217]:
          - generic [ref=e218]: Lesson Tags
          - textbox "Lesson Tags Comma-separated, optional. e.g. held-too-long, iv-crush" [ref=e219]:
            - /placeholder: held-too-long, iv-crush
          - generic [ref=e220]: Comma-separated, optional. e.g. held-too-long, iv-crush
      - generic [ref=e221]:
        - button "Cancel" [ref=e222] [cursor=pointer]
        - button "Confirm Close →" [disabled] [ref=e223]
```

# Test source

```ts
  1  | import { expect, test } from '@playwright/test';
  2  | 
  3  | // A credit spread marked at 60% profit trips the P1 profit-target rule, so
  4  | // the position deterministically surfaces in the "close now" panel.
  5  | const P1_POSITION = {
  6  |   id: 'e2e-pos-1',
  7  |   underlying: 'SPY',
  8  |   strategy_type: 'BULL_PUT_SPREAD',
  9  |   execution_mode: 'PAPER',
  10 |   legs: [
  11 |     { option_type: 'PUT', direction: 'SHORT', strike: 700, expiration: '2027-06-18', delta: -0.3, theta: 0.05, vega: 0.1, gamma: 0.01 },
  12 |     { option_type: 'PUT', direction: 'LONG', strike: 695, expiration: '2027-06-18', delta: -0.2, theta: 0.03, vega: 0.08, gamma: 0.01 },
  13 |   ],
  14 |   entry_date: '2026-08-01',
  15 |   expiration_date: '2027-06-18',
  16 |   entry_premium: 1.0,
  17 |   premium_direction: 'CREDIT',
  18 |   current_value_per_share: 0.4,
  19 |   contracts: 1,
  20 |   max_profit: 1.0,
  21 |   max_loss: 4.0,
  22 |   notes: 'e2e seed',
  23 |   rolls: 0,
  24 |   status: 'OPEN',
  25 |   journal: {
  26 |     core_thesis_rationale: 'e2e smoke seed position',
  27 |     structural_invalidation: 'n/a — synthetic test position',
  28 |     expected_underlying_move_pct: 1.0,
  29 |     pre_trade_emotional_state: 'Calm',
  30 |     pre_trade_confidence_rating: 3,
  31 |   },
  32 | };
  33 | 
  34 | test('close-position flow completes end to end', async ({ page, request }) => {
  35 |   page.on('pageerror', (err) => console.log('PAGEERROR:', err.message));
  36 |   page.on('console', (msg) => { if (msg.type() === 'error') console.log('CONSOLE:', msg.text()); });
  37 |   // Seed one OPEN position through the real API (journal is mandatory).
  38 |   const created = await request.post('/api/positions', { data: P1_POSITION });
  39 |   expect(created.ok()).toBeTruthy();
  40 | 
  41 |   await page.goto('/');
  42 | 
  43 |   // The profit-target P1 renders above the fold with a close action.
  44 |   await page.getByRole('button', { name: /Close.*Now/ }).first().click();
  45 | 
  46 |   // Close modal: value, trigger, and move are all required.
  47 |   await expect(page.getByRole('heading', { name: 'Close Position' })).toBeVisible();
  48 |   await page.getByPlaceholder('e.g. 12.50').fill('0.40');
  49 |   await page.locator('select').selectOption('PROFIT_TARGET');
  50 |   await page.getByPlaceholder('e.g. -1.5').fill('1.0');
> 51 |   await page.getByRole('button', { name: /Confirm Close/ }).click();
     |                                                             ^ Error: locator.click: Test timeout of 30000ms exceeded.
  52 | 
  53 |   // Post-mortem toast confirms the WIN and the realized P&L.
  54 |   await expect(page.getByText(/Position closed\. Outcome: WIN/)).toBeVisible();
  55 |   await expect(page.getByRole('button', { name: /Close.*Now/ })).toBeHidden();
  56 | });
  57 | 
```