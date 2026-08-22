# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.68.2](https://github.com/philipreese/basis/compare/v0.68.1...v0.68.2) (2026-08-22)


### Bug Fixes

* **flex-audit:** Normalize exec ids before matching against the ledger ([#635](https://github.com/philipreese/basis/issues/635)) ([afebe67](https://github.com/philipreese/basis/commit/afebe67d760774b98ed84b6733cbff1ab650d464))

## [0.68.1](https://github.com/philipreese/basis/compare/v0.68.0...v0.68.1) (2026-08-22)


### Bug Fixes

* **tests:** Pin TestLayerACloses's market clock to a real trading day ([#633](https://github.com/philipreese/basis/issues/633)) ([9c185aa](https://github.com/philipreese/basis/commit/9c185aa5c7fd2093ac20692abd33ca381d0c0b4f))

## [0.68.0](https://github.com/philipreese/basis/compare/v0.67.1...v0.68.0) (2026-08-22)


### Features

* **broker:** Capture completedStatus so broker rejections are distinguishable from cancels ([#629](https://github.com/philipreese/basis/issues/629)) ([1a9031c](https://github.com/philipreese/basis/commit/1a9031ca863ce4810983e25518a9bdcc973138ab))


### Bug Fixes

* **broker:** Make the whatIfOrder preview gate a hard precondition ([#628](https://github.com/philipreese/basis/issues/628)) ([4ca0691](https://github.com/philipreese/basis/commit/4ca0691463ee6684617d5b3867175326e542493e))

## [0.67.1](https://github.com/philipreese/basis/compare/v0.67.0...v0.67.1) (2026-08-21)


### Bug Fixes

* **pricing:** Reject sign-inverted net_mid for defined-direction spreads ([#623](https://github.com/philipreese/basis/issues/623)) ([3a9da16](https://github.com/philipreese/basis/commit/3a9da16cb21a09c24f7c32a86c056ef1279d5372))


### Tests

* **e2e:** Cover the cash-moving correction forms and drift panel ([#624](https://github.com/philipreese/basis/issues/624)) ([1ccbf98](https://github.com/philipreese/basis/commit/1ccbf982eb306a671d3292c1e44efa04041cedaf))

## [0.67.0](https://github.com/philipreese/basis/compare/v0.66.1...v0.67.0) (2026-08-21)


### Features

* **console:** Wire plain-English labels into the StatusStrip halt banner ([#620](https://github.com/philipreese/basis/issues/620)) ([35970fa](https://github.com/philipreese/basis/commit/35970fa35bde322e08377fe9eef1082502d12318))

## [0.66.1](https://github.com/philipreese/basis/compare/v0.66.0...v0.66.1) (2026-08-21)


### Bug Fixes

* **flex:** Close the pre-main crash gap that let a run die silently ([#618](https://github.com/philipreese/basis/issues/618)) ([a3f26e5](https://github.com/philipreese/basis/commit/a3f26e5a7ef1b9b20a10c85ecf01fdef0ddfcbbb))

## [0.66.0](https://github.com/philipreese/basis/compare/v0.65.1...v0.66.0) (2026-08-21)


### Features

* **console:** Add audit-trail severity, grouping, and a routine-noise filter ([#616](https://github.com/philipreese/basis/issues/616)) ([0539e8b](https://github.com/philipreese/basis/commit/0539e8b48023bcce698d78d732681093e6d78619))

## [0.65.1](https://github.com/philipreese/basis/compare/v0.65.0...v0.65.1) (2026-08-21)


### Bug Fixes

* **console:** Make the CRITICAL ACTION panel close-aware ([#614](https://github.com/philipreese/basis/issues/614)) ([c29a491](https://github.com/philipreese/basis/commit/c29a491a793366ac210c76baddeb78890a466c66))

## [0.65.0](https://github.com/philipreese/basis/compare/v0.64.0...v0.65.0) (2026-08-21)


### Features

* **console:** Add live-at-broker orders panel ([#612](https://github.com/philipreese/basis/issues/612)) ([c124c28](https://github.com/philipreese/basis/commit/c124c282eb781025e44160e4b2fd5e470d35d7dc))

## [0.64.0](https://github.com/philipreese/basis/compare/v0.63.0...v0.64.0) (2026-08-21)


### Features

* **console:** Add plain-English book/instrument labels to operator surfaces ([#610](https://github.com/philipreese/basis/issues/610)) ([13bdcfa](https://github.com/philipreese/basis/commit/13bdcfa7c9ab510d27d5b51e25c103712fe08aa9))

## [0.63.0](https://github.com/philipreese/basis/compare/v0.62.1...v0.63.0) (2026-08-21)


### Features

* **console:** Render timestamps in operator local timezone ([#599](https://github.com/philipreese/basis/issues/599)) ([1310c62](https://github.com/philipreese/basis/commit/1310c6209d172371f2572835056f2b1ae5997e7b))


### Bug Fixes

* **console:** Stop Flex-audit ack panel from re-showing just-acked rows ([#596](https://github.com/philipreese/basis/issues/596)) ([3f3fc65](https://github.com/philipreese/basis/commit/3f3fc6535d6aa3567f9bfe7f6b2f88da8cf0c3b7))
* **operator:** Encode ntfy title as UTF-8 bytes to fix false UNDELIVERED status ([#598](https://github.com/philipreese/basis/issues/598)) ([35006c5](https://github.com/philipreese/basis/commit/35006c50187a9cfff211018556d5279743aee37d))
* **tests:** Isolate test DATABASE_URL and quarantine polluted audit rows ([#605](https://github.com/philipreese/basis/issues/605)) ([43c3fc6](https://github.com/philipreese/basis/commit/43c3fc6664d542fefc5e570e0501a31f8bd245ce))


### Miscellaneous

* **run-lock:** Re-stat lock file immediately before graveyard rename ([#606](https://github.com/philipreese/basis/issues/606)) ([c267c8d](https://github.com/philipreese/basis/commit/c267c8d9e1d4114c151695dc9b3936c8bc171247))

## [0.62.1](https://github.com/philipreese/basis/compare/v0.62.0...v0.62.1) (2026-08-21)


### Bug Fixes

* **executor:** Skip Layer A closes for positions with unresolved ghost-order drift ([#591](https://github.com/philipreese/basis/issues/591)) ([6b83423](https://github.com/philipreese/basis/commit/6b83423ee3be8157bda6cf8fd45cb704b7590474))

## [0.62.0](https://github.com/philipreese/basis/compare/v0.61.8...v0.62.0) (2026-08-21)


### Features

* **console:** Add Flex-audit acknowledgment panel ([#592](https://github.com/philipreese/basis/issues/592)) ([07ebc7b](https://github.com/philipreese/basis/commit/07ebc7bb266acb31d4b107ec66dc6931638cba2a))

## [0.61.8](https://github.com/philipreese/basis/compare/v0.61.7...v0.61.8) (2026-08-21)


### Bug Fixes

* **recovery:** Close the grouped recovery tail from Audit II R4 ([#586](https://github.com/philipreese/basis/issues/586)) ([29e336f](https://github.com/philipreese/basis/commit/29e336f67e340bc4911c8b9edc9daad8d9964c44))

## [0.61.7](https://github.com/philipreese/basis/compare/v0.61.6...v0.61.7) (2026-08-21)


### Bug Fixes

* **executor:** Hold restore-gap UNKNOWN verdicts instead of terminalizing ([#587](https://github.com/philipreese/basis/issues/587)) ([4a81260](https://github.com/philipreese/basis/commit/4a81260e3badab4c341767f4d6a4b2ec85ba4413))

## [0.61.6](https://github.com/philipreese/basis/compare/v0.61.5...v0.61.6) (2026-08-21)


### Bug Fixes

* **run_lock:** Make a stolen run lock fatal mid-run ([#584](https://github.com/philipreese/basis/issues/584)) ([c1bff24](https://github.com/philipreese/basis/commit/c1bff24794b4580a1bab46125fb6cd01a22e1774))

## [0.61.5](https://github.com/philipreese/basis/compare/v0.61.4...v0.61.5) (2026-08-21)


### Bug Fixes

* **executor:** Close the grouped executor/anomaly tail from Audit II R4 ([#582](https://github.com/philipreese/basis/issues/582)) ([487a9b5](https://github.com/philipreese/basis/commit/487a9b526631e06185b5afd399bde0a461d65035))

## [0.61.4](https://github.com/philipreese/basis/compare/v0.61.3...v0.61.4) (2026-08-21)


### Bug Fixes

* **executor:** Make expiry-settlement stale-mark guard session-aware ([#580](https://github.com/philipreese/basis/issues/580)) ([5ede605](https://github.com/philipreese/basis/commit/5ede605cc408b7f807f801c27de6c633edb77ba8))

## [0.61.3](https://github.com/philipreese/basis/compare/v0.61.2...v0.61.3) (2026-08-21)


### Bug Fixes

* **temporal:** Close the grouped low-priority temporal gaps from Audit II R4 ([#578](https://github.com/philipreese/basis/issues/578)) ([eb18e34](https://github.com/philipreese/basis/commit/eb18e34e09ab1bcf3bfcea74a57892b71ec60b7b))

## [0.61.2](https://github.com/philipreese/basis/compare/v0.61.1...v0.61.2) (2026-08-21)


### Bug Fixes

* **positions:** Stamp entry_date from the server clock, not the browser ([#576](https://github.com/philipreese/basis/issues/576)) ([ae8ffc3](https://github.com/philipreese/basis/commit/ae8ffc312fb8a01b6237eeab68eb945c51392ae2))

## [0.61.1](https://github.com/philipreese/basis/compare/v0.61.0...v0.61.1) (2026-08-21)


### Bug Fixes

* **locks:** Release gateway/fill_check/executor locks on pre-try crash paths ([#574](https://github.com/philipreese/basis/issues/574)) ([2f41341](https://github.com/philipreese/basis/commit/2f41341f3ca0cc599458d335dc0d6beebde7958e))

## [0.61.0](https://github.com/philipreese/basis/compare/v0.60.22...v0.61.0) (2026-08-21)


### Features

* **flex-audit:** Add acknowledgment ledger to stop urgent re-alerts on corrected discrepancies ([#572](https://github.com/philipreese/basis/issues/572)) ([96427a7](https://github.com/philipreese/basis/commit/96427a78c71a21862851cc8ca3b8100b494fb03d))

## [0.60.22](https://github.com/philipreese/basis/compare/v0.60.21...v0.60.22) (2026-08-21)


### Bug Fixes

* **database:** Use WAL-safe snapshot for pre-migration backup ([#569](https://github.com/philipreese/basis/issues/569)) ([b436ef0](https://github.com/philipreese/basis/commit/b436ef0700901cde972b5143604ef385a2e719c7))

## [0.60.21](https://github.com/philipreese/basis/compare/v0.60.20...v0.60.21) (2026-08-21)


### Bug Fixes

* **fills:** Anchor benchmark inception on broker execution time, not capture time ([#567](https://github.com/philipreese/basis/issues/567)) ([eae5707](https://github.com/philipreese/basis/commit/eae5707f1194b0b5bf9d869ac2b555f32632d38e))

## [0.60.20](https://github.com/philipreese/basis/compare/v0.60.19...v0.60.20) (2026-08-21)


### Bug Fixes

* **calendars:** Snap roll and calendar back-leg expiry off market holidays ([#565](https://github.com/philipreese/basis/issues/565)) ([69f6c45](https://github.com/philipreese/basis/commit/69f6c45e17d94311ca9c269db3b64dd8c2046383))

## [0.60.19](https://github.com/philipreese/basis/compare/v0.60.18...v0.60.19) (2026-08-21)


### Bug Fixes

* **anomaly:** Judge envelope breaches against the era that decided the position ([#563](https://github.com/philipreese/basis/issues/563)) ([1dfa3aa](https://github.com/philipreese/basis/commit/1dfa3aac91312356ba30c42df88b236ecef4ae19))

## [0.60.18](https://github.com/philipreese/basis/compare/v0.60.17...v0.60.18) (2026-08-20)


### Bug Fixes

* **dates:** Thread market_today through nightly decision-path fallbacks ([#556](https://github.com/philipreese/basis/issues/556)) ([92ba868](https://github.com/philipreese/basis/commit/92ba86862511c25c1e73802ccdb3e8888b08c507))

## [0.60.17](https://github.com/philipreese/basis/compare/v0.60.16...v0.60.17) (2026-08-20)


### Bug Fixes

* **books:** Attribute evidence to the config era that decided it ([#555](https://github.com/philipreese/basis/issues/555)) ([2dd10c2](https://github.com/philipreese/basis/commit/2dd10c2ab4f806363675fb96db1a3a6dd1780047))

## [0.60.16](https://github.com/philipreese/basis/compare/v0.60.15...v0.60.16) (2026-08-20)


### Bug Fixes

* **database:** Quarantine duplicate post-mortems instead of bricking every entrypoint ([#552](https://github.com/philipreese/basis/issues/552)) ([5a16244](https://github.com/philipreese/basis/commit/5a16244189f5e06b8b206d45577ad55fa64eb4d5))

## [0.60.15](https://github.com/philipreese/basis/compare/v0.60.14...v0.60.15) (2026-08-20)


### Bug Fixes

* **anomaly:** Bucket repeated-rejection trailing sessions by market date ([#550](https://github.com/philipreese/basis/issues/550)) ([1f761ca](https://github.com/philipreese/basis/commit/1f761ca3d38dea595a6f585d6914aabc8f5f0038))

## [0.60.14](https://github.com/philipreese/basis/compare/v0.60.13...v0.60.14) (2026-08-20)


### Bug Fixes

* **executor:** Route every fills-on-a-verdicted-row shape through the shared PARTIAL latch ([#549](https://github.com/philipreese/basis/issues/549)) ([73bf842](https://github.com/philipreese/basis/commit/73bf842c513d75551adc48b58f320ec9637b06f6))

## [0.60.13](https://github.com/philipreese/basis/compare/v0.60.12...v0.60.13) (2026-08-20)


### Bug Fixes

* **console:** Add acknowledge-cancelled checkbox to ClosePositionModal ([#529](https://github.com/philipreese/basis/issues/529)) ([33c8ccd](https://github.com/philipreese/basis/commit/33c8ccd3a08c2056c75e629f75e89ca4343f57cb))

## [0.60.12](https://github.com/philipreese/basis/compare/v0.60.11...v0.60.12) (2026-08-20)


### Bug Fixes

* **console:** Add LONG_PUT handling to TradeSpecCard and CandidateCards ([#527](https://github.com/philipreese/basis/issues/527)) ([573f8d5](https://github.com/philipreese/basis/commit/573f8d55c70a3f8e45f2b28bd5cc93ecf52cb4af))

## [0.60.11](https://github.com/philipreese/basis/compare/v0.60.10...v0.60.11) (2026-08-20)


### Bug Fixes

* **executor:** Close the state-machine tail — resting promotion, replay audit, mint restriction, zombie-fill rule ([#525](https://github.com/philipreese/basis/issues/525)) ([1c921f4](https://github.com/philipreese/basis/commit/1c921f4d7d0dfd7c285ca37b19745aa7ba9eb2e8))

## [0.60.10](https://github.com/philipreese/basis/compare/v0.60.9...v0.60.10) (2026-08-20)


### Bug Fixes

* **executor:** Stamp the roll latch from the sync too, not just the atomic commit ([#523](https://github.com/philipreese/basis/issues/523)) ([a13fcbb](https://github.com/philipreese/basis/commit/a13fcbb3ff8302bfbfe90e996f273b21f07bc8bd))

## [0.60.9](https://github.com/philipreese/basis/compare/v0.60.8...v0.60.9) (2026-08-20)


### Bug Fixes

* **database:** Make book-config sync diagnosable and document seeds.py as source of truth ([#520](https://github.com/philipreese/basis/issues/520)) ([059b871](https://github.com/philipreese/basis/commit/059b8718c074f377949dd97ed52c1f0f61b169a3))

## [0.60.8](https://github.com/philipreese/basis/compare/v0.60.7...v0.60.8) (2026-08-20)


### Bug Fixes

* **console:** Fix premium-direction guess, 422 toasts, close triggers, filters ([#519](https://github.com/philipreese/basis/issues/519)) ([1d92412](https://github.com/philipreese/basis/commit/1d92412d1552152a1f0fd572e6864e6aa15e75e9))

## [0.60.7](https://github.com/philipreese/basis/compare/v0.60.6...v0.60.7) (2026-08-20)


### Bug Fixes

* **reconciliation:** Respect broker order status and drop the TP parent-ref fallback ([#515](https://github.com/philipreese/basis/issues/515)) ([41d62ea](https://github.com/philipreese/basis/commit/41d62ea1258e558b9fd99130f6d3520ad65ec889))

## [0.60.6](https://github.com/philipreese/basis/compare/v0.60.5...v0.60.6) (2026-08-20)


### Bug Fixes

* **run-lock:** Break stale locks by graveyard rename and bracket Gateway tenancy ([#514](https://github.com/philipreese/basis/issues/514)) ([897a7c3](https://github.com/philipreese/basis/commit/897a7c37cfd165849fd23deb01d83fd86d55bee1))

## [0.60.5](https://github.com/philipreese/basis/compare/v0.60.4...v0.60.5) (2026-08-20)


### Bug Fixes

* **operator:** Harden alert_crash's sync engine and distinguish scheduler alerts ([#511](https://github.com/philipreese/basis/issues/511)) ([d0cd56f](https://github.com/philipreese/basis/commit/d0cd56fc530135be5da2f9461a37f98f46c57801))

## [0.60.4](https://github.com/philipreese/basis/compare/v0.60.3...v0.60.4) (2026-08-20)


### Bug Fixes

* **executor:** Close the PARTIAL-latch bypasses around acknowledge and UNKNOWN verdicts ([#509](https://github.com/philipreese/basis/issues/509)) ([e02bc40](https://github.com/philipreese/basis/commit/e02bc40f78ed561171fe79e518d9d8398eca2556))

## [0.60.3](https://github.com/philipreese/basis/compare/v0.60.2...v0.60.3) (2026-08-20)


### Bug Fixes

* **console:** Render broker health, urgent-push delivery, and recon resolution ([#508](https://github.com/philipreese/basis/issues/508)) ([6a38cfe](https://github.com/philipreese/basis/commit/6a38cfe49e1f889529dfa03e2a4f31a235a90661))

## [0.60.2](https://github.com/philipreese/basis/compare/v0.60.1...v0.60.2) (2026-08-20)


### Bug Fixes

* **executor:** Confirm TP cancel at the broker before stamping CANCELLED ([#505](https://github.com/philipreese/basis/issues/505)) ([f737ba2](https://github.com/philipreese/basis/commit/f737ba24ef0f3bd9610341b77add5cc4344a5af7))

## [0.60.1](https://github.com/philipreese/basis/compare/v0.60.0...v0.60.1) (2026-08-20)


### Bug Fixes

* **resolution:** Block partial-latch release and expiry settlement while true fill size is unknown ([#503](https://github.com/philipreese/basis/issues/503)) ([df91863](https://github.com/philipreese/basis/commit/df91863fac35b252cf32aad223b3e89bf7b3f326))

## [0.60.0](https://github.com/philipreese/basis/compare/v0.59.16...v0.60.0) (2026-08-20)


### Features

* **console:** Poll supervision panels instead of fetching once on mount ([#500](https://github.com/philipreese/basis/issues/500)) ([c00b6e0](https://github.com/philipreese/basis/commit/c00b6e0c2b5987c8c6158ccc1ede24bd554d10e9))

## [0.59.16](https://github.com/philipreese/basis/compare/v0.59.15...v0.59.16) (2026-08-20)


### Bug Fixes

* **executor:** Guard the order-state sync's status stamps with conditional UPDATEs ([#499](https://github.com/philipreese/basis/issues/499)) ([60fba93](https://github.com/philipreese/basis/commit/60fba934adf7ad565cf01c253ade3a6104787098))

## [0.59.15](https://github.com/philipreese/basis/compare/v0.59.14...v0.59.15) (2026-08-20)


### Bug Fixes

* **console:** Regenerate api-types.ts to match backend contract ([#496](https://github.com/philipreese/basis/issues/496)) ([acd9172](https://github.com/philipreese/basis/commit/acd9172c5750e37bb2b75ed5171c7fd066ad9705))

## [0.59.14](https://github.com/philipreese/basis/compare/v0.59.13...v0.59.14) (2026-08-20)


### Bug Fixes

* **executor:** Re-read each Layer A close candidate fresh before staging ([#493](https://github.com/philipreese/basis/issues/493)) ([1e2d2b5](https://github.com/philipreese/basis/commit/1e2d2b59685ee1fc2c01af2eacce9c3f5fc1fa13))

## [0.59.13](https://github.com/philipreese/basis/compare/v0.59.12...v0.59.13) (2026-08-20)


### Bug Fixes

* **console:** Render trading-mode unknown state instead of fabricating paper ([#492](https://github.com/philipreese/basis/issues/492)) ([710fb96](https://github.com/philipreese/basis/commit/710fb96578ea9cc63d3db0fbc80ba040d4b84744))

## [0.59.12](https://github.com/philipreese/basis/compare/v0.59.11...v0.59.12) (2026-08-20)


### Bug Fixes

* **trading-control:** Force a fresh read at the choke point past the identity map ([#490](https://github.com/philipreese/basis/issues/490)) ([d1505ff](https://github.com/philipreese/basis/commit/d1505ffba7c6496bbf2378b30f72feeadd95fb41))

## [0.59.11](https://github.com/philipreese/basis/compare/v0.59.10...v0.59.11) (2026-08-20)


### Bug Fixes

* **console:** Render halt reasons and server-computed urgent audit events ([#487](https://github.com/philipreese/basis/issues/487)) ([6380ade](https://github.com/philipreese/basis/commit/6380adea33469378f80d82ed7b5ebbfa13207e88))

## [0.59.10](https://github.com/philipreese/basis/compare/v0.59.9...v0.59.10) (2026-08-20)


### Bug Fixes

* **resolution:** Guard OPEN to CLOSED transitions against double-submit races ([#486](https://github.com/philipreese/basis/issues/486)) ([16ada70](https://github.com/philipreese/basis/commit/16ada70c82a9843a22eb6a9ddfa93a7dc5d06953))

## [0.59.9](https://github.com/philipreese/basis/compare/v0.59.8...v0.59.9) (2026-08-20)


### Bug Fixes

* **books:** Make book cash mutations SQL-side increments ([#484](https://github.com/philipreese/basis/issues/484)) ([51d0579](https://github.com/philipreese/basis/commit/51d05796fed8e87e5e9a288b5e8306b210de4d4c))

## [0.59.8](https://github.com/philipreese/basis/compare/v0.59.7...v0.59.8) (2026-08-20)


### Bug Fixes

* **backup:** Remove failed snapshots and silence the legacy warning under the lock fallback ([#460](https://github.com/philipreese/basis/issues/460)) ([2bd47b3](https://github.com/philipreese/basis/commit/2bd47b3ad9380e5b8155657d61ce7b518783e36e))

## [0.59.7](https://github.com/philipreese/basis/compare/v0.59.6...v0.59.7) (2026-08-20)


### Bug Fixes

* **executor:** Harden roll staging against broker errors and the latch crash window ([#458](https://github.com/philipreese/basis/issues/458)) ([72758c5](https://github.com/philipreese/basis/commit/72758c5e1ebec34b411aaa9d87e0a5267a61af36))

## [0.59.6](https://github.com/philipreese/basis/compare/v0.59.5...v0.59.6) (2026-08-20)


### Bug Fixes

* **executor:** Count only genuine market attempts as concession rungs ([#456](https://github.com/philipreese/basis/issues/456)) ([f1d8fb9](https://github.com/philipreese/basis/commit/f1d8fb9d4320f9088cd1c1b4f15439a3a7978abc))

## [0.59.5](https://github.com/philipreese/basis/compare/v0.59.4...v0.59.5) (2026-08-20)


### Bug Fixes

* **anomaly:** Convert UTC run timestamps to market dates in gap counting ([#454](https://github.com/philipreese/basis/issues/454)) ([092ce6b](https://github.com/philipreese/basis/commit/092ce6b9255cabac760bf15fe3c70fc8153dd2ee))

## [0.59.4](https://github.com/philipreese/basis/compare/v0.59.3...v0.59.4) (2026-08-20)


### Bug Fixes

* **fill-check:** Leave the Gateway up while an executor run lock is held ([#452](https://github.com/philipreese/basis/issues/452)) ([8d196c7](https://github.com/philipreese/basis/commit/8d196c71279d3952419ef036fcc2f24b8a30383c))

## [0.59.3](https://github.com/philipreese/basis/compare/v0.59.2...v0.59.3) (2026-08-20)


### Bug Fixes

* **alerts:** Make crash alerts durable with an audit row and ntfy retry ([#450](https://github.com/philipreese/basis/issues/450)) ([ee8da8c](https://github.com/philipreese/basis/commit/ee8da8cb4cbcb0b1daea8b474844ffd2c6723be9))

## [0.59.2](https://github.com/philipreese/basis/compare/v0.59.1...v0.59.2) (2026-08-20)


### Bug Fixes

* **locks:** Add owner tokens and atomic stale-break to the run lock ([#448](https://github.com/philipreese/basis/issues/448)) ([9714134](https://github.com/philipreese/basis/commit/97141340bac0dffaabc410e9f7093700ecd6b1bb))

## [0.59.1](https://github.com/philipreese/basis/compare/v0.59.0...v0.59.1) (2026-08-20)


### Bug Fixes

* **executor:** Block expiry settlement on a stale mark ([#446](https://github.com/philipreese/basis/issues/446)) ([4cee9aa](https://github.com/philipreese/basis/commit/4cee9aae9d69f73aece7430ff5f28b106b559b31))

## [0.59.0](https://github.com/philipreese/basis/compare/v0.58.15...v0.59.0) (2026-08-20)


### Features

* **resolution:** Add audited terminal path for PARTIAL orders ([#444](https://github.com/philipreese/basis/issues/444)) ([582694f](https://github.com/philipreese/basis/commit/582694f706163dacb7e9b4c2d33de414e922dc4e))

## [0.58.15](https://github.com/philipreese/basis/compare/v0.58.14...v0.58.15) (2026-08-20)


### Bug Fixes

* **executor:** Make the close pipeline PARTIAL-aware ([#442](https://github.com/philipreese/basis/issues/442)) ([a57775e](https://github.com/philipreese/basis/commit/a57775e3db3ccf4f0aadeb099a3e4dd12d7fb7f8))

## [0.58.14](https://github.com/philipreese/basis/compare/v0.58.13...v0.58.14) (2026-08-20)


### Bug Fixes

* **api:** Book cash and update the mark on manual position close ([#440](https://github.com/philipreese/basis/issues/440)) ([b3f2fa7](https://github.com/philipreese/basis/commit/b3f2fa75eed0c05e237a4fccf8874e2336f3b060))

## [0.58.13](https://github.com/philipreese/basis/compare/v0.58.12...v0.58.13) (2026-08-20)


### Bug Fixes

* **database:** Sync changed seed configs into existing books with a version bump ([#438](https://github.com/philipreese/basis/issues/438)) ([d03886b](https://github.com/philipreese/basis/commit/d03886b83ad7b415e4937e10afede71c742cc430))

## [0.58.12](https://github.com/philipreese/basis/compare/v0.58.11...v0.58.12) (2026-08-20)


### Bug Fixes

* **books:** Add per-playbook dedup so B32 holds one tail put in steady state ([#435](https://github.com/philipreese/basis/issues/435)) ([9f200d8](https://github.com/philipreese/basis/commit/9f200d8602f711dfc4ee0b5edd19e60f97808a07))

## [0.58.11](https://github.com/philipreese/basis/compare/v0.58.10...v0.58.11) (2026-08-20)


### Bug Fixes

* **resolution:** Unlock external-close for cancelled orders and skip drifted legs in Layer A ([#427](https://github.com/philipreese/basis/issues/427)) ([819747c](https://github.com/philipreese/basis/commit/819747c72f0ef8829c45a0fd3f7f26413efe5992))

## [0.58.10](https://github.com/philipreese/basis/compare/v0.58.9...v0.58.10) (2026-08-20)


### Documentation

* **flex:** State that the Flex audit detects and never backfills ([#431](https://github.com/philipreese/basis/issues/431)) ([16e52d6](https://github.com/philipreese/basis/commit/16e52d653505ce34469d039b48ec0cae0bd8e4e6))

## [0.58.9](https://github.com/philipreese/basis/compare/v0.58.8...v0.58.9) (2026-08-20)


### Bug Fixes

* **executor:** Commit the GTC profit-taker intent row before order placement ([#430](https://github.com/philipreese/basis/issues/430)) ([b781586](https://github.com/philipreese/basis/commit/b7815863d427653fda1aabde29ee8711e2d314db))

## [0.58.8](https://github.com/philipreese/basis/compare/v0.58.7...v0.58.8) (2026-08-20)


### Bug Fixes

* **reconciliation:** Flag ghost basis: open orders at the broker as drift ([#428](https://github.com/philipreese/basis/issues/428)) ([4409e89](https://github.com/philipreese/basis/commit/4409e8933bd20d56a803cb7190a1aee7805a3d25))

## [0.58.7](https://github.com/philipreese/basis/compare/v0.58.6...v0.58.7) (2026-08-20)


### Bug Fixes

* **broker:** Only upgrade UNKNOWN refs to FILLED from execution evidence ([#424](https://github.com/philipreese/basis/issues/424)) ([6523054](https://github.com/philipreese/basis/commit/652305415141d8449d6a50c59eced2a0937c53ab))

## [0.58.6](https://github.com/philipreese/basis/compare/v0.58.5...v0.58.6) (2026-08-20)


### Bug Fixes

* **executor:** Skip Layer A close staging when a close order is already resting ([#423](https://github.com/philipreese/basis/issues/423)) ([b3bc0bf](https://github.com/philipreese/basis/commit/b3bc0bf5d31ae3e66f2831f255002b541c88124f))

## [0.58.5](https://github.com/philipreese/basis/compare/v0.58.4...v0.58.5) (2026-08-20)


### Tests

* Rename sprint-era test files to concern-based names ([#401](https://github.com/philipreese/basis/issues/401)) ([3cbd733](https://github.com/philipreese/basis/commit/3cbd7339a9234beabf89f5b73bbd9636ba4ce747))

## [0.58.4](https://github.com/philipreese/basis/compare/v0.58.3...v0.58.4) (2026-08-20)


### Miscellaneous

* **models:** Remove the vestigial execution_mode label and surface the real trading mode ([#402](https://github.com/philipreese/basis/issues/402)) ([70641dc](https://github.com/philipreese/basis/commit/70641dc45d65c4715b71bba940cd6eb3ccfe0355))

## [0.58.3](https://github.com/philipreese/basis/compare/v0.58.2...v0.58.3) (2026-08-20)


### Miscellaneous

* **branding:** Rename the legacy Options Playbook name to basis everywhere ([#396](https://github.com/philipreese/basis/issues/396)) ([2f6c4fc](https://github.com/philipreese/basis/commit/2f6c4fc611d0b5b01fc7170787dbd07dd8122bc4))
* **models:** Remove the dead catalyst_exit_days_after exit rule ([#399](https://github.com/philipreese/basis/issues/399)) ([bca9e9b](https://github.com/philipreese/basis/commit/bca9e9bc835b15276048afbd588e63f2030fe8f5))

## [0.58.2](https://github.com/philipreese/basis/compare/v0.58.1...v0.58.2) (2026-08-20)


### Documentation

* **spec:** Update the operational state to Executor (Paper) and retire the sprint branch allowance ([#397](https://github.com/philipreese/basis/issues/397)) ([8c28f6f](https://github.com/philipreese/basis/commit/8c28f6f347f5189b627bead87689c103fe92c256))

## [0.58.1](https://github.com/philipreese/basis/compare/v0.58.0...v0.58.1) (2026-08-20)


### Miscellaneous

* **executor:** Sweep the small Audit II remainders ([#394](https://github.com/philipreese/basis/issues/394)) ([17d11df](https://github.com/philipreese/basis/commit/17d11df9af0ca14faed1fc469d921e37ffcd8800))

## [0.58.0](https://github.com/philipreese/basis/compare/v0.57.15...v0.58.0) (2026-08-20)


### Features

* **analysis:** Add pairwise arm panels and clean up sweep hygiene ([#392](https://github.com/philipreese/basis/issues/392)) ([b980480](https://github.com/philipreese/basis/commit/b980480dce007b6162a9c22ed02b3c233018eedf))

## [0.57.15](https://github.com/philipreese/basis/compare/v0.57.14...v0.57.15) (2026-08-20)


### Bug Fixes

* **console:** Refresh app state after resolution corrections and reject catalyst near-misses ([#390](https://github.com/philipreese/basis/issues/390)) ([c391c86](https://github.com/philipreese/basis/commit/c391c866b872df0f4b01e47be10a80883eefbdab))

## [0.57.14](https://github.com/philipreese/basis/compare/v0.57.13...v0.57.14) (2026-08-20)


### Bug Fixes

* **backup:** Date-shaped prune glob and WAL-safe snapshots ([#388](https://github.com/philipreese/basis/issues/388)) ([63b80f0](https://github.com/philipreese/basis/commit/63b80f09422d9aab3a0c2b745ae3eceff8c5cbb6))

## [0.57.13](https://github.com/philipreese/basis/compare/v0.57.12...v0.57.13) (2026-08-20)


### Bug Fixes

* **flex:** Skip BAG rows in the Flex trade parse ([#386](https://github.com/philipreese/basis/issues/386)) ([e4b424a](https://github.com/philipreese/basis/commit/e4b424a990d82a1823025cea5af228d50602e640))

## [0.57.12](https://github.com/philipreese/basis/compare/v0.57.11...v0.57.12) (2026-08-20)


### Bug Fixes

* **seeds:** Give the tail-hedge sleeve two slots so coverage survives the roll ([#384](https://github.com/philipreese/basis/issues/384)) ([8805acb](https://github.com/philipreese/basis/commit/8805acbe569264613e9bcb969283c5591ec6a20a))

## [0.57.11](https://github.com/philipreese/basis/compare/v0.57.10...v0.57.11) (2026-08-20)


### Bug Fixes

* **executor:** Hold the roll to Layer C entry discipline on stale nights ([#382](https://github.com/philipreese/basis/issues/382)) ([b7ff358](https://github.com/philipreese/basis/commit/b7ff3580f6bbcc98ce8f0fe425499bc508db1663))

## [0.57.10](https://github.com/philipreese/basis/compare/v0.57.9...v0.57.10) (2026-08-20)


### Bug Fixes

* **seeds:** Land the earnings-condor exit after the event for every report weekday ([#380](https://github.com/philipreese/basis/issues/380)) ([474fa1e](https://github.com/philipreese/basis/commit/474fa1e997690269508c0b542dd2ff78bbc58d89))

## [0.57.9](https://github.com/philipreese/basis/compare/v0.57.8...v0.57.9) (2026-08-20)


### Bug Fixes

* **resolution:** Reject NaN and infinite values on resolution inputs ([#374](https://github.com/philipreese/basis/issues/374)) ([fcf178b](https://github.com/philipreese/basis/commit/fcf178b6495f2f068816020e7bb6c4c2b778d53a))

## [0.57.8](https://github.com/philipreese/basis/compare/v0.57.7...v0.57.8) (2026-08-20)


### Bug Fixes

* **executor:** Block expiry settlement while a partial fill is latched ([#377](https://github.com/philipreese/basis/issues/377)) ([525f41e](https://github.com/philipreese/basis/commit/525f41e3cece84bc1a6296a8fdf5212d2f610e59))

## [0.57.7](https://github.com/philipreese/basis/compare/v0.57.6...v0.57.7) (2026-08-20)


### Bug Fixes

* **analysis:** Correct fill-quality sign convention for close fills and exclude partials ([#375](https://github.com/philipreese/basis/issues/375)) ([d68c1b1](https://github.com/philipreese/basis/commit/d68c1b1a8f5746f3b55ffe111fe357027bad2d85))

## [0.57.6](https://github.com/philipreese/basis/compare/v0.57.5...v0.57.6) (2026-08-20)


### Bug Fixes

* **resolution:** Refuse external close while live orders reference the position ([#372](https://github.com/philipreese/basis/issues/372)) ([ff01929](https://github.com/philipreese/basis/commit/ff01929955d3f1230b1ebaabe2e5059644e0fbbe))

## [0.57.5](https://github.com/philipreese/basis/compare/v0.57.4...v0.57.5) (2026-08-20)


### Bug Fixes

* **executor:** Latch the roll to one attempt per position ([#370](https://github.com/philipreese/basis/issues/370)) ([9f90ec6](https://github.com/philipreese/basis/commit/9f90ec6c2ff93ff51b685d60cf09749956c86270))

## [0.57.4](https://github.com/philipreese/basis/compare/v0.57.3...v0.57.4) (2026-08-20)


### Bug Fixes

* **executor:** Stop rounding spec strikes to integers before placement ([#368](https://github.com/philipreese/basis/issues/368)) ([3cff5f3](https://github.com/philipreese/basis/commit/3cff5f3aa26880cbf78c0ebe4e2a98dc480794fd))

## [0.57.3](https://github.com/philipreese/basis/compare/v0.57.2...v0.57.3) (2026-08-20)


### Bug Fixes

* **executor:** Apply close-fill cash only on the OPEN-to-CLOSED transition ([#366](https://github.com/philipreese/basis/issues/366)) ([affbc7e](https://github.com/philipreese/basis/commit/affbc7e34799cf648aea1142c1d7e0788d2086ce))

## [0.57.2](https://github.com/philipreese/basis/compare/v0.57.1...v0.57.2) (2026-08-20)


### Bug Fixes

* **executor:** Alert urgently on executor crash and withhold the heartbeat ([#364](https://github.com/philipreese/basis/issues/364)) ([e88781a](https://github.com/philipreese/basis/commit/e88781aeb3be25c98f048afcd4e11fa21cb0a6e3))

## [0.57.1](https://github.com/philipreese/basis/compare/v0.57.0...v0.57.1) (2026-08-20)


### Bug Fixes

* **database:** Survive a locked legacy file during the DB rename ([#362](https://github.com/philipreese/basis/issues/362)) ([b3565fb](https://github.com/philipreese/basis/commit/b3565fb78fd3e13a5ee70688c445b570e6432d13))

## [0.57.0](https://github.com/philipreese/basis/compare/v0.56.0...v0.57.0) (2026-08-20)


### Features

* **books:** Add B32 tail-hedge sleeve with LONG_PUT strategy ([#338](https://github.com/philipreese/basis/issues/338)) ([9e7d055](https://github.com/philipreese/basis/commit/9e7d055af3bfaad354111c436b83f484259f7063))

## [0.56.0](https://github.com/philipreese/basis/compare/v0.55.0...v0.56.0) (2026-08-20)


### Features

* **executor:** Add B31 roll arm with executor-side roll path ([#336](https://github.com/philipreese/basis/issues/336)) ([6dd2bee](https://github.com/philipreese/basis/commit/6dd2bee0f7a82980b3579f68f907a9929a01c1f9))

## [0.55.0](https://github.com/philipreese/basis/compare/v0.54.1...v0.55.0) (2026-08-20)


### Features

* **books:** Add B30 AAPL earnings-crush arm with scoped catalysts ([#334](https://github.com/philipreese/basis/issues/334)) ([f6f00d9](https://github.com/philipreese/basis/commit/f6f00d9c238dcd7cd099ed1eec1446aad40db8ec))

## [0.54.1](https://github.com/philipreese/basis/compare/v0.54.0...v0.54.1) (2026-08-20)


### Bug Fixes

* **broker:** Exclude BAG-level executions from fills fetch ([#332](https://github.com/philipreese/basis/issues/332)) ([443573b](https://github.com/philipreese/basis/commit/443573b238cb1cf515e874dcab9854fa1b50875e))

## [0.54.0](https://github.com/philipreese/basis/compare/v0.53.0...v0.54.0) (2026-08-20)


### Features

* **books:** Add B29 ensemble-consensus arm ([#329](https://github.com/philipreese/basis/issues/329)) ([20f5615](https://github.com/philipreese/basis/commit/20f5615c9230a0dd84e9c270ab5c40c58ac492c1))

## [0.53.0](https://github.com/philipreese/basis/compare/v0.52.0...v0.53.0) (2026-08-20)


### Features

* **analysis:** Add regime hit-rate report ([#327](https://github.com/philipreese/basis/issues/327)) ([3a07a61](https://github.com/philipreese/basis/commit/3a07a61fe130a11720eb0a343c0e732c8931d494))

## [0.52.0](https://github.com/philipreese/basis/compare/v0.51.0...v0.52.0) (2026-08-20)


### Features

* **analysis:** Add leaderboard with knob-sweep monotonicity verdicts ([#325](https://github.com/philipreese/basis/issues/325)) ([f2aea16](https://github.com/philipreese/basis/commit/f2aea163ce8ed98d9ffb4b15ecd9f20c3efa554a))

## [0.51.0](https://github.com/philipreese/basis/compare/v0.50.2...v0.51.0) (2026-08-20)


### Features

* **analysis:** Add fill-quality report with slippage decomposition ([#323](https://github.com/philipreese/basis/issues/323)) ([dc79212](https://github.com/philipreese/basis/commit/dc79212a7959958951991b4b0dc9fe3d97d87cd0))

## [0.50.2](https://github.com/philipreese/basis/compare/v0.50.1...v0.50.2) (2026-08-20)


### Code Refactoring

* **console:** Restructure around the executor era ([#321](https://github.com/philipreese/basis/issues/321)) ([845e2b0](https://github.com/philipreese/basis/commit/845e2b089683c304f5aae8e6d84088d0d7a22390))

## [0.50.1](https://github.com/philipreese/basis/compare/v0.50.0...v0.50.1) (2026-08-20)


### Miscellaneous

* **db:** Rename database files to basis.db and basis.live.db ([#314](https://github.com/philipreese/basis/issues/314)) ([603269e](https://github.com/philipreese/basis/commit/603269e868914d6ed38cb0e76217a3668d9d541e))

## [0.50.0](https://github.com/philipreese/basis/compare/v0.49.0...v0.50.0) (2026-08-20)


### Features

* **console:** Add reconciliation resolution flow ([#311](https://github.com/philipreese/basis/issues/311)) ([db779cd](https://github.com/philipreese/basis/commit/db779cd2b0d720c18de84f176866d1d12d76662d))

## [0.49.0](https://github.com/philipreese/basis/compare/v0.48.3...v0.49.0) (2026-08-20)


### Features

* **database:** Implement ADR-0006 trading-mode isolation ([#308](https://github.com/philipreese/basis/issues/308)) ([0648058](https://github.com/philipreese/basis/commit/06480581292ea3dc42a8807b726e5df3a4a34d2c))

## [0.48.3](https://github.com/philipreese/basis/compare/v0.48.2...v0.48.3) (2026-08-20)


### Documentation

* **spec:** Truth-sync README, spec, and .env.example to the shipped system ([#306](https://github.com/philipreese/basis/issues/306)) ([5ed94e5](https://github.com/philipreese/basis/commit/5ed94e5bd9b86226db06470abdca8947d4c309db))

## [0.48.2](https://github.com/philipreese/basis/compare/v0.48.1...v0.48.2) (2026-08-20)


### Miscellaneous

* **models:** Stamp config_hash onto positions at creation ([#304](https://github.com/philipreese/basis/issues/304)) ([8c2bba2](https://github.com/philipreese/basis/commit/8c2bba2a01572e33c8188a4abcbefb942c602e9d))

## [0.48.1](https://github.com/philipreese/basis/compare/v0.48.0...v0.48.1) (2026-08-20)


### Bug Fixes

* **executor:** Detect partial fills and alert on missed nights ([#302](https://github.com/philipreese/basis/issues/302)) ([160cb63](https://github.com/philipreese/basis/commit/160cb6351e3544672f899b566b26f2b089c63fa6))

## [0.48.0](https://github.com/philipreese/basis/compare/v0.47.0...v0.48.0) (2026-08-20)


### Features

* **pricing:** Default $1 strike grid, holiday-aware expiry snap, quote sanity bound ([#300](https://github.com/philipreese/basis/issues/300)) ([9703464](https://github.com/philipreese/basis/commit/97034643392a94d8d8df6a63102fb38f4601dfc5))

## [0.47.0](https://github.com/philipreese/basis/compare/v0.46.1...v0.47.0) (2026-08-20)


### Features

* **control:** Implement FLATTEN_REQUESTED as close-all-in-scope ([#298](https://github.com/philipreese/basis/issues/298)) ([228f371](https://github.com/philipreese/basis/commit/228f3715a3e70bf29a635e82072dbeb99cfe5d86))

## [0.46.1](https://github.com/philipreese/basis/compare/v0.46.0...v0.46.1) (2026-08-20)


### Bug Fixes

* **pricing:** Track mark freshness, guard stale exits, cap the close ladder ([#296](https://github.com/philipreese/basis/issues/296)) ([274d359](https://github.com/philipreese/basis/commit/274d359bd5bb2b426ee731e9c1cf166a93fc1c0c))

## [0.46.0](https://github.com/philipreese/basis/compare/v0.45.0...v0.46.0) (2026-08-20)


### Features

* **console:** Guard manual close on executor books, add per-book halt ([#294](https://github.com/philipreese/basis/issues/294)) ([e70c4d4](https://github.com/philipreese/basis/commit/e70c4d4c8199bfa290f3564b38927c0f628cb242))

## [0.45.0](https://github.com/philipreese/basis/compare/v0.44.0...v0.45.0) (2026-08-20)


### Features

* **control:** Add ntfy command watermark, morning poll, and receipt push ([#292](https://github.com/philipreese/basis/issues/292)) ([9ef6a35](https://github.com/philipreese/basis/commit/9ef6a35b2bd36c2b749d30ad52b5c539ecacb41a))

## [0.44.0](https://github.com/philipreese/basis/compare/v0.43.3...v0.44.0) (2026-08-20)


### Features

* **digest:** Persist the digest, retry the push, surface delivery status ([#290](https://github.com/philipreese/basis/issues/290)) ([1986226](https://github.com/philipreese/basis/commit/1986226455a4cfd2938afe5e2c2d722ad199ae3a))

## [0.43.3](https://github.com/philipreese/basis/compare/v0.43.2...v0.43.3) (2026-08-20)


### Bug Fixes

* **performance:** Debit commissions from book cash and gate expectancy ([#288](https://github.com/philipreese/basis/issues/288)) ([dd40bde](https://github.com/philipreese/basis/commit/dd40bde4acac2e2a920999082df3f0d8ab57cf8e))

## [0.43.2](https://github.com/philipreese/basis/compare/v0.43.1...v0.43.2) (2026-08-20)


### Bug Fixes

* **executor:** Add a run lock and repair the dead duplicate-order check ([#286](https://github.com/philipreese/basis/issues/286)) ([67a3380](https://github.com/philipreese/basis/commit/67a33808135b0746c7b591bf8e6069437aea535d))

## [0.43.1](https://github.com/philipreese/basis/compare/v0.43.0...v0.43.1) (2026-08-20)


### Miscellaneous

* **ops:** Harden SQLite, file logging, battery flags, and crash alerts ([#273](https://github.com/philipreese/basis/issues/273)) ([9685670](https://github.com/philipreese/basis/commit/968567003c4c553bf0fefd7d7db1c91b5f8e7603))

## [0.43.0](https://github.com/philipreese/basis/compare/v0.42.4...v0.43.0) (2026-08-20)


### Features

* **executor:** Settle expired positions and write post-mortems on autonomous closes ([#270](https://github.com/philipreese/basis/issues/270)) ([3d6cf86](https://github.com/philipreese/basis/commit/3d6cf86afff1303bbc1bcdd78aac8863cf75d2f5))

## [0.42.4](https://github.com/philipreese/basis/compare/v0.42.3...v0.42.4) (2026-08-20)


### Bug Fixes

* **executor:** Track the GTC profit-taker child and settle its fill ([#268](https://github.com/philipreese/basis/issues/268)) ([1de005e](https://github.com/philipreese/basis/commit/1de005e38aa0ceb1353cc403590469a80d831ba2))

## [0.42.3](https://github.com/philipreese/basis/compare/v0.42.2...v0.42.3) (2026-08-20)


### Bug Fixes

* **executor:** Freeze playbook snapshot on positions and enforce mandatory time exit ([#266](https://github.com/philipreese/basis/issues/266)) ([0999083](https://github.com/philipreese/basis/commit/0999083fe1f6f62778b4115402ec8946f963a918))

## [0.42.2](https://github.com/philipreese/basis/compare/v0.42.1...v0.42.2) (2026-08-20)


### Bug Fixes

* **dates:** Pin run dates to the market clock, filter events by run start ([#264](https://github.com/philipreese/basis/issues/264)) ([48eb446](https://github.com/philipreese/basis/commit/48eb4467c8f0509cc732f74cedaab54e733a8b53))

## [0.42.1](https://github.com/philipreese/basis/compare/v0.42.0...v0.42.1) (2026-08-20)


### Bug Fixes

* **executor:** Close fills debit the buy-back cost instead of crediting it ([#262](https://github.com/philipreese/basis/issues/262)) ([ee241f7](https://github.com/philipreese/basis/commit/ee241f787f7cc200654e4ba80f9b01aed85fc24f))

## [0.42.0](https://github.com/philipreese/basis/compare/v0.41.0...v0.42.0) (2026-08-20)


### Features

* **books:** B28 regime-flip exit — close when the regime leaves the entry state ([#255](https://github.com/philipreese/basis/issues/255)) ([ee50d83](https://github.com/philipreese/basis/commit/ee50d834b58114cef42be6061ae7b86c06da5365))

## [0.41.0](https://github.com/philipreese/basis/compare/v0.40.0...v0.41.0) (2026-08-19)


### Features

* **regime:** Observation-only engines V4-V6 (short-end curve, credit, breadth) ([#252](https://github.com/philipreese/basis/issues/252)) ([fad8b4f](https://github.com/philipreese/basis/commit/fad8b4fdc8b86c7ef4465da900691cf81260dd0e))

## [0.40.0](https://github.com/philipreese/basis/compare/v0.39.1...v0.40.0) (2026-08-19)


### Features

* **digest:** Show nightly regime consensus and variant splits ([#249](https://github.com/philipreese/basis/issues/249)) ([f7d7a3b](https://github.com/philipreese/basis/commit/f7d7a3b7bee10954cd4f8f7ba3a8284174e5c8c0))

## [0.39.1](https://github.com/philipreese/basis/compare/v0.39.0...v0.39.1) (2026-08-19)


### Bug Fixes

* **regime:** V2 thin-VRP maps to neutral, never a directional bear call ([#246](https://github.com/philipreese/basis/issues/246)) ([9b3246d](https://github.com/philipreese/basis/commit/9b3246d2e5417714a789d39762f03a19c20a2d93))

## [0.39.0](https://github.com/philipreese/basis/compare/v0.38.0...v0.39.0) (2026-08-19)


### Features

* **analysis:** Persist the per-book MTM equity curve nightly ([#240](https://github.com/philipreese/basis/issues/240)) ([0fc7516](https://github.com/philipreese/basis/commit/0fc75164a6ad1553095d0f24b4b22c568813c621))

## [0.38.0](https://github.com/philipreese/basis/compare/v0.37.4...v0.38.0) (2026-08-19)


### Features

* **fills:** Read-only morning fill check with ntfy push ([#237](https://github.com/philipreese/basis/issues/237)) ([5267bcb](https://github.com/philipreese/basis/commit/5267bcb9a22324c5ae007c28ffa6c43866490a20))

## [0.37.4](https://github.com/philipreese/basis/compare/v0.37.3...v0.37.4) (2026-08-19)


### Documentation

* **calendars:** Record the election-day exclusion decision ([#234](https://github.com/philipreese/basis/issues/234)) ([1e6946b](https://github.com/philipreese/basis/commit/1e6946b98bbd038f07d9d75e080d747167146731))

## [0.37.3](https://github.com/philipreese/basis/compare/v0.37.2...v0.37.3) (2026-08-19)


### Bug Fixes

* **quotes:** Poll for delayed option ticks instead of a fixed 5s window ([#231](https://github.com/philipreese/basis/issues/231)) ([aa7c398](https://github.com/philipreese/basis/commit/aa7c398700281ae75c53d002016bdf0abc410deb))

## [0.37.2](https://github.com/philipreese/basis/compare/v0.37.1...v0.37.2) (2026-08-19)


### Bug Fixes

* **digest:** Books with resting orders are awaiting fill, not idle ([#228](https://github.com/philipreese/basis/issues/228)) ([34eee36](https://github.com/philipreese/basis/commit/34eee36ffe2d4e958e7534f544d6def7fb3d048b))

## [0.37.1](https://github.com/philipreese/basis/compare/v0.37.0...v0.37.1) (2026-08-19)


### Bug Fixes

* **gateway:** Always kill the Gateway tree and sweep orphaned java ([#226](https://github.com/philipreese/basis/issues/226)) ([6815f62](https://github.com/philipreese/basis/commit/6815f622a5e968298617fdec41d97d8027dda461))

## [0.37.0](https://github.com/philipreese/basis/compare/v0.36.2...v0.37.0) (2026-08-19)


### Features

* **seeds:** Complete 3-point knob sweeps with books B23-B27 ([#222](https://github.com/philipreese/basis/issues/222)) ([2714f45](https://github.com/philipreese/basis/commit/2714f4554649ccd5a9bea3a0d5cfeddecbba0145))

## [0.36.2](https://github.com/philipreese/basis/compare/v0.36.1...v0.36.2) (2026-08-19)


### Bug Fixes

* **seeds:** Give B13 the risk budget its $5 wings require ([#220](https://github.com/philipreese/basis/issues/220)) ([7d504c6](https://github.com/philipreese/basis/commit/7d504c6b8ac336f5170ffc34d600140c53a96a2c))

## [0.36.1](https://github.com/philipreese/basis/compare/v0.36.0...v0.36.1) (2026-08-19)


### Documentation

* **spec:** Pre-register the Live Gate promotion procedure (ADR-0010) ([#216](https://github.com/philipreese/basis/issues/216)) ([383c072](https://github.com/philipreese/basis/commit/383c07229b50aa6c0db78406f9da545e568923e6))

## [0.36.0](https://github.com/philipreese/basis/compare/v0.35.0...v0.36.0) (2026-08-19)


### Features

* **digest:** Add SPY buy-and-hold benchmark line ([#212](https://github.com/philipreese/basis/issues/212)) ([b5a53cd](https://github.com/philipreese/basis/commit/b5a53cdf84a1b043abf0854615fa557074ac7092))

## [0.35.0](https://github.com/philipreese/basis/compare/v0.34.8...v0.35.0) (2026-08-19)


### Features

* **backup:** Copy the database nightly after the executor run ([#209](https://github.com/philipreese/basis/issues/209)) ([f65f850](https://github.com/philipreese/basis/commit/f65f8503d195326c2a47e2828f5d87652005dd47))

## [0.34.8](https://github.com/philipreese/basis/compare/v0.34.7...v0.34.8) (2026-08-19)


### Miscellaneous

* **operator:** Remove standalone operator entrypoint superseded by executor-nightly ([#206](https://github.com/philipreese/basis/issues/206)) ([a775c35](https://github.com/philipreese/basis/commit/a775c354de01136191734b2d928d0238853b4fd1))

## [0.34.7](https://github.com/philipreese/basis/compare/v0.34.6...v0.34.7) (2026-08-19)


### Bug Fixes

* **market-data:** Stream delayed option quotes instead of snapshot requests ([#202](https://github.com/philipreese/basis/issues/202)) ([1009e1f](https://github.com/philipreese/basis/commit/1009e1f6e12fd92f44889959f66d6a76bd7a87d9))

## [0.34.6](https://github.com/philipreese/basis/compare/v0.34.5...v0.34.6) (2026-08-18)


### Bug Fixes

* **market-data:** Use a separate client id for transient telemetry fetches ([#199](https://github.com/philipreese/basis/issues/199)) ([0fe32ad](https://github.com/philipreese/basis/commit/0fe32ad0e35cf8cb85edd6063d9cd83eb0ee20b6))

## [0.34.5](https://github.com/philipreese/basis/compare/v0.34.4...v0.34.5) (2026-08-18)


### Bug Fixes

* **scripts:** Write IBC config to C:\IBC instead of OneDrive-redirected Documents ([#196](https://github.com/philipreese/basis/issues/196)) ([10d28a3](https://github.com/philipreese/basis/commit/10d28a3b29c80b0e46f20ed7a8066335626b1abf))

## [0.34.4](https://github.com/philipreese/basis/compare/v0.34.3...v0.34.4) (2026-08-18)


### Bug Fixes

* **api:** Return 404 instead of fabricating market state in the observation route ([#193](https://github.com/philipreese/basis/issues/193)) ([796eec2](https://github.com/philipreese/basis/commit/796eec29d4469509f1ad7d426fc5192f9fa494a1))

## [0.34.3](https://github.com/philipreese/basis/compare/v0.34.2...v0.34.3) (2026-08-18)


### Code Refactoring

* **opportunity:** Split eligibility and telemetry out of the opportunity engine ([#191](https://github.com/philipreese/basis/issues/191)) ([f3ee49f](https://github.com/philipreese/basis/commit/f3ee49f91e554813117ad6d0676dc2192638617a))

## [0.34.2](https://github.com/philipreese/basis/compare/v0.34.1...v0.34.2) (2026-08-18)


### Code Refactoring

* **calendars:** Consolidate the operator-maintained calendars into one module ([#188](https://github.com/philipreese/basis/issues/188)) ([4f15706](https://github.com/philipreese/basis/commit/4f157066a4cb29ef798be4899be5de913c3efad3))

## [0.34.1](https://github.com/philipreese/basis/compare/v0.34.0...v0.34.1) (2026-08-18)


### Miscellaneous

* **executor:** Apply ruff formatting missed by the [#157](https://github.com/philipreese/basis/issues/157) squash ([#185](https://github.com/philipreese/basis/issues/185)) ([8a51308](https://github.com/philipreese/basis/commit/8a51308043a6824bc366e8daecff40bf919116dd))

## [0.34.0](https://github.com/philipreese/basis/compare/v0.33.6...v0.34.0) (2026-08-18)


### Features

* **gateway:** Add the IBC Gateway lifecycle, holiday guard, and 162 policy ([#157](https://github.com/philipreese/basis/issues/157)) ([4a25682](https://github.com/philipreese/basis/commit/4a2568266165f11cb31ecf30bf2cc6e02acc16b8))

## [0.33.6](https://github.com/philipreese/basis/compare/v0.33.5...v0.33.6) (2026-08-18)


### Code Refactoring

* **api:** Extract domain composition out of the fat main.py routes ([#181](https://github.com/philipreese/basis/issues/181)) ([eddf809](https://github.com/philipreese/basis/commit/eddf809a7940e9aab5c5bdd9336b475906b0c946))

## [0.33.5](https://github.com/philipreese/basis/compare/v0.33.4...v0.33.5) (2026-08-18)


### Code Refactoring

* **digest:** Type the blocked-entry seam as BlockedEntry data ([#177](https://github.com/philipreese/basis/issues/177)) ([6485b45](https://github.com/philipreese/basis/commit/6485b454817c624544dd85d14c38c8ce78494e22))

## [0.33.4](https://github.com/philipreese/basis/compare/v0.33.3...v0.33.4) (2026-08-18)


### Code Refactoring

* **executor:** Replace occ_by_leg tuples with a ComboLeg dataclass ([#173](https://github.com/philipreese/basis/issues/173)) ([569f70f](https://github.com/philipreese/basis/commit/569f70f6f1cbf292185fb52108f6e886fb6b55ce))

## [0.33.3](https://github.com/philipreese/basis/compare/v0.33.2...v0.33.3) (2026-08-18)


### Code Refactoring

* **models:** Declare the strategy vocabulary once as StrategyType ([#171](https://github.com/philipreese/basis/issues/171)) ([a36be22](https://github.com/philipreese/basis/commit/a36be222a564ad6853ccd050944d62ba6f404b1a))

## [0.33.2](https://github.com/philipreese/basis/compare/v0.33.1...v0.33.2) (2026-08-18)


### Code Refactoring

* **books:** Resolve book config through a typed BookConfig ([#168](https://github.com/philipreese/basis/issues/168)) ([671ccb0](https://github.com/philipreese/basis/commit/671ccb0769043a765761030fd325c51dcc65fd29))

## [0.33.1](https://github.com/philipreese/basis/compare/v0.33.0...v0.33.1) (2026-08-18)


### Miscellaneous

* **agents:** Configure repo for Pocock engineering skills ([#165](https://github.com/philipreese/basis/issues/165)) ([69b3a81](https://github.com/philipreese/basis/commit/69b3a81c6296f381707f95b07cab7b90cc1c6c7e))

## [0.33.0](https://github.com/philipreese/basis/compare/v0.32.1...v0.33.0) (2026-08-18)


### Features

* **digest:** Keep the nightly digest legible at 22 books ([#162](https://github.com/philipreese/basis/issues/162)) ([ac10e25](https://github.com/philipreese/basis/commit/ac10e251d5b3f5019bca970ebaaa4bd4c80e0fb4))

## [0.32.1](https://github.com/philipreese/basis/compare/v0.32.0...v0.32.1) (2026-08-18)


### Bug Fixes

* **calendars:** Correct verified ex-div and CPI dates ([#159](https://github.com/philipreese/basis/issues/159)) ([c3c457a](https://github.com/philipreese/basis/commit/c3c457a2f36a304fb3c8fda340dc0f4af9e7e9e5))

## [0.32.0](https://github.com/philipreese/basis/compare/v0.31.0...v0.32.0) (2026-08-18)


### Features

* **matrix:** Seed the TLT book B22 and complete the 22-book matrix ([#155](https://github.com/philipreese/basis/issues/155)) ([321da86](https://github.com/philipreese/basis/commit/321da86dce563022fb7ca29dc9c9197641b78497))

## [0.31.0](https://github.com/philipreese/basis/compare/v0.30.1...v0.31.0) (2026-08-18)


### Features

* **strategy:** Add the calendar spread and seed book B21 ([#153](https://github.com/philipreese/basis/issues/153)) ([af704ea](https://github.com/philipreese/basis/commit/af704ea82e91d27207b7bbd631e258d6079dc20e))

## [0.30.1](https://github.com/philipreese/basis/compare/v0.30.0...v0.30.1) (2026-08-18)


### Code Refactoring

* **backend:** Extract strategy builders and seed data into modules ([#151](https://github.com/philipreese/basis/issues/151)) ([7bf9e5c](https://github.com/philipreese/basis/commit/7bf9e5cb7d9a70f4ca10bcc5ec42cfc049111bac))

## [0.30.0](https://github.com/philipreese/basis/compare/v0.29.0...v0.30.0) (2026-08-18)


### Features

* **strategy:** Add the broken-wing butterfly and seed book B18 ([#148](https://github.com/philipreese/basis/issues/148)) ([61f35cd](https://github.com/philipreese/basis/commit/61f35cd7357c9f775517fbb581ad855153a17b5a))

## [0.29.0](https://github.com/philipreese/basis/compare/v0.28.0...v0.29.0) (2026-08-18)


### Features

* **regime:** Add the V3 repaired-matrix variant and seed books B19/B20 ([#146](https://github.com/philipreese/basis/issues/146)) ([42204bc](https://github.com/philipreese/basis/commit/42204bcab8eecf231dfca3b053de4045248e938c))

## [0.28.0](https://github.com/philipreese/basis/compare/v0.27.0...v0.28.0) (2026-08-18)


### Features

* **catalyst:** Seed FOMC/CPI dates into the catalyst calendar ([#144](https://github.com/philipreese/basis/issues/144)) ([9618ee8](https://github.com/philipreese/basis/commit/9618ee8f533044672fde590e77550f3eaa18d777))

## [0.27.0](https://github.com/philipreese/basis/compare/v0.26.0...v0.27.0) (2026-08-18)


### Features

* **defense:** Prevent ex-dividend early assignment on short calls ([#142](https://github.com/philipreese/basis/issues/142)) ([abca3b1](https://github.com/philipreese/basis/commit/abca3b12093bcc35c8bd62f2cd27c79fe60e3bfb))

## [0.26.0](https://github.com/philipreese/basis/compare/v0.25.0...v0.26.0) (2026-08-18)


### Features

* **telemetry:** Add per-underlying telemetry and seed the B09/B10 books ([#140](https://github.com/philipreese/basis/issues/140)) ([253aa23](https://github.com/philipreese/basis/commit/253aa2302c05bbf25f3490cefdc7f2ec4e8a34bf))

## [0.25.0](https://github.com/philipreese/basis/compare/v0.24.2...v0.25.0) (2026-08-18)


### Features

* **matrix:** Enforce the regime gate and seed the ADR-0009 experiment matrix ([#137](https://github.com/philipreese/basis/issues/137)) ([feffde2](https://github.com/philipreese/basis/commit/feffde21572b8b0342173d78cf202ec166eb50aa))

## [0.24.2](https://github.com/philipreese/basis/compare/v0.24.1...v0.24.2) (2026-08-17)


### Documentation

* **readme:** Restructure around the current system instead of sprint history ([#125](https://github.com/philipreese/basis/issues/125)) ([62548ad](https://github.com/philipreese/basis/commit/62548adbbe5bac85c6a9efe8779dc6e08613e771))

## [0.24.1](https://github.com/philipreese/basis/compare/v0.24.0...v0.24.1) (2026-08-17)


### Miscellaneous

* **ci:** Cache the Playwright browser keyed on the locked version ([#127](https://github.com/philipreese/basis/issues/127)) ([d15dcaa](https://github.com/philipreese/basis/commit/d15dcaae862389619c7c151f50528db9f56a44f9))

## [0.24.0](https://github.com/philipreese/basis/compare/v0.23.0...v0.24.0) (2026-08-17)


### Features

* **performance:** Compute sample-gated CAGR, Sharpe, max drawdown, and SPY benchmark ([#122](https://github.com/philipreese/basis/issues/122)) ([769494c](https://github.com/philipreese/basis/commit/769494c6d9e911610ccdbff93934dce6deffe297))

## [0.23.0](https://github.com/philipreese/basis/compare/v0.22.0...v0.23.0) (2026-08-17)


### Features

* **ledger:** Add filtering, sorting, and override-value summary to the Opportunity Ledger ([#119](https://github.com/philipreese/basis/issues/119)) ([823a706](https://github.com/philipreese/basis/commit/823a70605a7d45361bfe11708556fa11e718db01))

## [0.22.0](https://github.com/philipreese/basis/compare/v0.21.0...v0.22.0) (2026-08-17)


### Features

* **positions:** Add roll workflow with net-credit and roll-cap enforcement ([#118](https://github.com/philipreese/basis/issues/118)) ([33fea45](https://github.com/philipreese/basis/commit/33fea457cd5150c601b309520bbbcb9da87bcca6))

## [0.21.0](https://github.com/philipreese/basis/compare/v0.20.4...v0.21.0) (2026-08-17)


### Features

* **audit:** Add weekly Flex Query audit of the fills ledger ([#116](https://github.com/philipreese/basis/issues/116)) ([7b8c72f](https://github.com/philipreese/basis/commit/7b8c72ffd68c1618e3da5f5e9d98344be4c8bff4))

## [0.20.4](https://github.com/philipreese/basis/compare/v0.20.3...v0.20.4) (2026-08-17)


### Tests

* **e2e:** Add Playwright smoke pack for critical console flows ([#114](https://github.com/philipreese/basis/issues/114)) ([e4cd6b1](https://github.com/philipreese/basis/commit/e4cd6b165ef5d832a13b7c5f55915c1f39e38ded))

## [0.20.3](https://github.com/philipreese/basis/compare/v0.20.2...v0.20.3) (2026-08-17)


### Bug Fixes

* **frontend:** Enable close-position confirm by validating numeric bindings as numbers ([#112](https://github.com/philipreese/basis/issues/112)) ([e75d5cb](https://github.com/philipreese/basis/commit/e75d5cb6e3be095d55080798c2618c8dbc83dd14))

## [0.20.2](https://github.com/philipreese/basis/compare/v0.20.1...v0.20.2) (2026-08-17)


### Code Refactoring

* **api:** Add response_model to the portfolio observation endpoint ([#109](https://github.com/philipreese/basis/issues/109)) ([7b24711](https://github.com/philipreese/basis/commit/7b24711ca67a609699b63dfb610adc26a6b3a7ec))

## [0.20.1](https://github.com/philipreese/basis/compare/v0.20.0...v0.20.1) (2026-08-17)


### Code Refactoring

* **frontend:** Generate the API client from the backend OpenAPI schema ([#106](https://github.com/philipreese/basis/issues/106)) ([7a03606](https://github.com/philipreese/basis/commit/7a03606dd7bbf085b07fa1fbb290d2a7a1739a4c))

## [0.20.0](https://github.com/philipreese/basis/compare/v0.19.0...v0.20.0) (2026-08-17)


### Features

* **console:** Add status strip, Books tab, and audit view ([#103](https://github.com/philipreese/basis/issues/103)) ([9d53933](https://github.com/philipreese/basis/commit/9d539330bce3719610fca7d605135a304d3c8f25))

## [0.19.0](https://github.com/philipreese/basis/compare/v0.18.0...v0.19.0) (2026-08-17)


### Features

* **executor:** Add evening digest with urgent-push tiering and dead-man watchdog ([#101](https://github.com/philipreese/basis/issues/101)) ([520a770](https://github.com/philipreese/basis/commit/520a770ca7e410bd30a5ea473a7a489eae614d11))

## [0.18.0](https://github.com/philipreese/basis/compare/v0.17.1...v0.18.0) (2026-08-17)


### Features

* **executor:** Wire deterministic anomaly auto-halt rules into the pipeline ([#99](https://github.com/philipreese/basis/issues/99)) ([6e31ab2](https://github.com/philipreese/basis/commit/6e31ab25e5d81a99da4f8e7436bca1a6a6bd2220))

## [0.17.1](https://github.com/philipreese/basis/compare/v0.17.0...v0.17.1) (2026-08-17)


### Bug Fixes

* **executor:** Fit seed spreads inside the risk envelope and drop pre-launch migrations ([#97](https://github.com/philipreese/basis/issues/97)) ([0379bb3](https://github.com/philipreese/basis/commit/0379bb33ad2bba86a08a83b132cc023651921a85))

## [0.17.0](https://github.com/philipreese/basis/compare/v0.16.0...v0.17.0) (2026-08-17)


### Features

* **executor:** Add nightly executor pipeline orchestrating reconcile, closes, and entries ([#95](https://github.com/philipreese/basis/issues/95)) ([b69c1c7](https://github.com/philipreese/basis/commit/b69c1c7648469c0dbe73c511c44ad4bfd8af0c9c))

## [0.16.0](https://github.com/philipreese/basis/compare/v0.15.0...v0.16.0) (2026-08-17)


### Features

* **executor:** Add V1 and V2 regime variants with nightly readings and lab books ([#92](https://github.com/philipreese/basis/issues/92)) ([e8db9b7](https://github.com/philipreese/basis/commit/e8db9b7d56dc8c4e33de9d95b328be42c7be9ede))

## [0.15.0](https://github.com/philipreese/basis/compare/v0.14.0...v0.15.0) (2026-08-17)


### Features

* **executor:** Add per-book risk gates with capital encumbrance and netting block ([#90](https://github.com/philipreese/basis/issues/90)) ([1d5d9c8](https://github.com/philipreese/basis/commit/1d5d9c896c428b1483d39fcbbd14d5afd3edbbb8))

## [0.14.0](https://github.com/philipreese/basis/compare/v0.13.0...v0.14.0) (2026-08-17)


### Features

* **executor:** Add reconciliation engine with drift classification and fill backfill ([#88](https://github.com/philipreese/basis/issues/88)) ([88eac0c](https://github.com/philipreese/basis/commit/88eac0cb758b3a8f338126e3e92765a12d9becac))

## [0.13.0](https://github.com/philipreese/basis/compare/v0.12.0...v0.13.0) (2026-08-17)


### Features

* **executor:** Add latched kill switch with fail-closed defaults and HALT-only remote ([#86](https://github.com/philipreese/basis/issues/86)) ([d586242](https://github.com/philipreese/basis/commit/d586242f21f82da5715bd3a4ef6e4f193629810c))

## [0.12.0](https://github.com/philipreese/basis/compare/v0.11.0...v0.12.0) (2026-08-17)


### Features

* **executor:** Add BrokerSession adapter with combo orders and orderRef idempotency ([#84](https://github.com/philipreese/basis/issues/84)) ([cb90217](https://github.com/philipreese/basis/commit/cb90217c2b4a7f2085e987231f80aef266e9b3d3))

## [0.11.0](https://github.com/philipreese/basis/compare/v0.10.0...v0.11.0) (2026-08-17)


### Features

* **executor:** Add IBKR paper smoke-test script for combo order mechanics ([#81](https://github.com/philipreese/basis/issues/81)) ([892a850](https://github.com/philipreese/basis/commit/892a8507561fede39786f6831e7a3203a58112d1))

## [0.10.0](https://github.com/philipreese/basis/compare/v0.9.0...v0.10.0) (2026-08-17)


### Features

* **executor:** Persist nightly VIX and VIX3M closes to index_history ([#79](https://github.com/philipreese/basis/issues/79)) ([a8629e3](https://github.com/philipreese/basis/commit/a8629e361171566d4e7e9fdcf00213f00906435d))

## [0.9.0](https://github.com/philipreese/basis/compare/v0.8.2...v0.9.0) (2026-08-17)


### Features

* **executor:** Add multi-book schema with append-only audit tables ([#77](https://github.com/philipreese/basis/issues/77)) ([8c80507](https://github.com/philipreese/basis/commit/8c80507cd30b861d2b68bbeef240b6ca5f2dfe6a))

## [0.8.2](https://github.com/philipreese/basis/compare/v0.8.1...v0.8.2) (2026-08-17)


### Documentation

* **spec:** Add supervision concern, kill-switch ADR, and record executor decisions ([#75](https://github.com/philipreese/basis/issues/75)) ([d3f6f1c](https://github.com/philipreese/basis/commit/d3f6f1c1c21a4c93cd88ecdc3dca6fae470b9ddd))

## [0.8.1](https://github.com/philipreese/basis/compare/v0.8.0...v0.8.1) (2026-08-17)


### Documentation

* **spec:** Add Executor-Paper design document ([#58](https://github.com/philipreese/basis/issues/58)) ([4456687](https://github.com/philipreese/basis/commit/445668775a8375d7ea082281fd8d082058ce3cf2))

## [0.8.0](https://github.com/philipreese/basis/compare/v0.7.8...v0.8.0) (2026-08-17)


### Features

* **data:** Replace Alpaca market data with IBKR delayed feed ([#51](https://github.com/philipreese/basis/issues/51)) ([eb64fef](https://github.com/philipreese/basis/commit/eb64fef1f53b2c1ddf3ab801f1686444256a8e7c))
* **operator:** Add nightly evening pipeline with ntfy push digest ([#49](https://github.com/philipreese/basis/issues/49)) ([796eb10](https://github.com/philipreese/basis/commit/796eb10be8847ad19630609165b254bff3149931))


### Bug Fixes

* **ui:** Allow closing any open position; stop seeding demo positions ([#54](https://github.com/philipreese/basis/issues/54)) ([540a9a4](https://github.com/philipreese/basis/commit/540a9a44e267b134c9ae0fcc06286fb859b3c125))


### Continuous Integration

* Merge Release PRs with --squash to match repo merge settings ([#56](https://github.com/philipreese/basis/issues/56)) ([5da896b](https://github.com/philipreese/basis/commit/5da896b325c70fb6a45b9901ca16c32fb88694b8))

## [0.7.8](https://github.com/philipreese/basis/compare/v0.7.7...v0.7.8) (2026-08-17)


### Bug Fixes

* **backend:** Restrict CORS to the local dev origin ([f6c3cc1](https://github.com/philipreese/basis/commit/f6c3cc140cef7ee91dd4378c19e19b26cc933d0e))

## [0.7.7](https://github.com/philipreese/basis/compare/v0.7.6...v0.7.7) (2026-08-17)


### Bug Fixes

* **backend:** Unify capital-at-risk calculation across gates and safeguards ([aafc1ba](https://github.com/philipreese/basis/commit/aafc1ba09eb73e81e850c1ac426b875bfcad2df2))

## [0.7.6](https://github.com/philipreese/basis/compare/v0.7.5...v0.7.6) (2026-08-17)


### Miscellaneous

* Add ruff and pytest-cov gates; fix pixi test task chaining ([e5660c0](https://github.com/philipreese/basis/commit/e5660c0fc284a38b0a483324024f46598008b109))

## [0.7.5](https://github.com/philipreese/basis/compare/v0.7.4...v0.7.5) (2026-08-17)


### Code Refactoring

* **backend:** Make pricing.py the single source of trade economics ([2cf37f6](https://github.com/philipreese/basis/commit/2cf37f6f52dfdf00b36e4d9aecee5ef99b1daba7))

## [0.7.4](https://github.com/philipreese/basis/compare/v0.7.3...v0.7.4) (2026-08-17)


### Bug Fixes

* **db:** Replace drop_all migration heuristic with Alembic migrations ([fd39483](https://github.com/philipreese/basis/commit/fd3948307fc183e89d0ec5070ea18f86c80617e0))

## [0.7.3](https://github.com/philipreese/basis/compare/v0.7.2...v0.7.3) (2026-08-17)


### Documentation

* Fix spec drift, retire snapshot spec files, README truthfulness pass ([c19e329](https://github.com/philipreese/basis/commit/c19e329e018f3d6a1a7d9f74efc81ccd1b43e54b))

## [0.7.2](https://github.com/philipreese/basis/compare/v0.7.1...v0.7.2) (2026-08-17)


### Documentation

* Consolidate standards into AGENTS.md; hand CHANGELOG to release-please ([0ac4769](https://github.com/philipreese/basis/commit/0ac4769e1e7c1f8dfbfddd1101b821deaa73e0b1))

## [0.7.1](https://github.com/philipreese/basis/compare/v0.7.0...v0.7.1) (2026-08-17)


### Documentation

* **spec:** Capture autonomy roadmap decisions (ADR-0006, ADR-0007) ([f2a9696](https://github.com/philipreese/basis/commit/f2a9696eb3e72c89cd255addbd406ecf5c90b5ca))

## [0.7.0](https://github.com/philipreese/alpaca-agent-bot/compare/v0.6.1...v0.7.0) (2026-07-22)


### Features

* **playbooks:** Add credit-spread playbooks and playbook enabled flag ([266f9ec](https://github.com/philipreese/alpaca-agent-bot/commit/266f9ec1bf579fca0c94ff74021f4226b41d937d)), closes [#20](https://github.com/philipreese/alpaca-agent-bot/issues/20)

## [0.6.1](https://github.com/philipreese/alpaca-agent-bot/compare/v0.6.0...v0.6.1) (2026-06-14)


### Bug Fixes

* **ci:** Use immediate gh pr merge instead of auto-merge ([114425a](https://github.com/philipreese/alpaca-agent-bot/commit/114425ae4a9c4fdc5f7d216536d184475b10af75))


### Documentation

* **spec:** Remove branch protection step — not available on free private repos ([43f9160](https://github.com/philipreese/alpaca-agent-bot/commit/43f91605d2f837ac45a42bfd64f6732f3ec5e65f))


### Continuous Integration

* Set up GitHub Actions CI and release-please automation ([a49af49](https://github.com/philipreese/alpaca-agent-bot/commit/a49af49d3c8b02b43aaeb4a47148de2b5c3ea710))


### Miscellaneous

* **ci:** Regenerate pixi.lock with linux-64 platform ([d96988b](https://github.com/philipreese/alpaca-agent-bot/commit/d96988ba6909e682382b3948f05ac97ec118c91b))

## [0.6.0] - 2026-06-11

### Added
- **Shared Component Library** (`frontend/src/lib/ui/`): 9 reusable Svelte 5 primitives — `Badge`, `Button`, `MetricCard`, `Alert`, `FormField`, `Collapsible`, `DataTable`, `Modal`, `Tooltip` — eliminating duplicated markup across all feature components.
- **Design Token Centralization** (`frontend/src/index.css`): Rewrote CSS with Tailwind v4 `@theme` block. Semantic `--c-*` custom properties for consistent light/dark theming. Added `.glow-indigo` and `.glow-violet` to the glow set.
- **Snackbar notifications** (`frontend/src/lib/ui/Snackbar.svelte`, `snackbar.svelte.ts`): Fixed-position toast system replaces inline `errorMsg`/`successMsg` alerts. Toasts slide in via Svelte `fly` transition, auto-dismiss, and support success/error/info levels without shifting page layout.
- **Interactive hover animations** (`frontend/src/lib/ui/Button.svelte`, `frontend/src/index.css`): Buttons scale up on hover and compress on click (`hover:scale-[1.02]`, `active:scale-[0.98]`). Global `cursor-pointer` rule covers all non-disabled buttons. New `.carbon-card-interactive` utility for hover-grow cards with mauve glow shadow.
- **Fixed desktop status bar** (`frontend/src/App.svelte`): VS Code-style status bar is now `position: fixed` at the bottom of the viewport on desktop.
- **Mobile-first responsive layout** (`App.svelte`, `PositionScanner`, `CandidateCards`, `TradeSpecCard`, `PostMortemCard`, `OpportunityLedger`, `MarketContextRibbon`): Redesigned grids, padding, and tap targets for evening phone usage. Prominent above-the-fold red banner for P1 "CLOSE NOW" alerts. Re-lock Session button in header. Centralized formatting utilities (`formatters.ts`) for dollars, percentages, DTE, and dates; unit tests in `formatters.test.ts`.
- **Live Options Pricing Refresh** (`backend/market_data.py`, `backend/main.py`, `frontend/src/App.svelte`): `format_occ_symbol` and `fetch_options_latest_quotes` fetch live mid-market quotes from Alpaca Options Market Data API. `POST /api/positions/refresh` updates `current_value_per_share` for all open positions; frontend refreshes on load and after a live fetch. Covered by unit and integration tests.
- **Navigation Unit Tests** (`frontend/src/tests/navigation.test.ts`): 10 tests covering tab state transitions and session-lock gating.
- **Session re-lock discoverability** (`App.svelte`): Tooltip on the Re-lock button explains its effect; Enter key acknowledges and unlocks the session while the lock banner is visible.

### Changed
- **UX Clarity** (`App.svelte`): Session lock banner explains the review requirement with a 3-step breadcrumb. Opportunities pre-scan state is descriptive. Loading skeleton replaces spinner text. Empty states for post-mortems and first-time settings callout. Mobile tab labels aligned with desktop.
- **Design-token consistency**: Converted `SafeguardsPanel`, `PerformanceDashboard`, and `OpportunityLedger` from hardcoded Tailwind slate/rose classes to Catppuccin tokens (`--ctp-*`) and shared `ui/` primitives so they theme correctly in light/dark mode.
- **Component refactors**: All feature components use shared `ui/` primitives — `Alert`, `FormField`, `Button`, `Badge`, `Collapsible`, `Tooltip` on Greek labels.
- **CSS bug fixes**: Fixed dynamic Tailwind class names in `MarketContextRibbon.svelte`, non-standard color values in `CandidateCards.svelte` and `PositionScanner.svelte`.
- **Typography legibility pass**: Removed all sub-12px inline sizes across every component. Minimum body text is `text-xs` (12px); description and reason copy bumped to `text-sm` (14px) in `PositionScanner`, `TradeSpecCard`, `CandidateCards`, `SafeguardsPanel`, `Alert`, and the session-lock banner.
- **Fetch Live feedback** (`App.svelte`): Market Telemetry form shows a "Pulling SPY & VIX from Alpaca…" indicator and disables inputs while a live fetch is in flight.
- **Inline telemetry validation** (`App.svelte`, `FormField`): IVRs and Catalyst Dates fields validate format as you type; "Apply Telemetry" is disabled until inputs parse. Added optional `error` prop to `FormField`.
- **Override justification** (`CandidateCards`): Overriding a suppressed playbook now requires a written reason, recorded to the opportunity ledger's `bypass_reason`.
- **Greek-limit CTA** (`GreeksPanel`): Exceeded Greek limit now shows an alert with a "Review positions →" action that scrolls to the position scanner.

### Fixed
- **Floating-point noise in telemetry form** (`App.svelte`): Rounded SMA20 and Daily Return values to prevent display of raw floating-point noise.
- **Close P&L floating-point imprecision** (`backend/main.py`): Rounded `realized_pnl` to 2 decimal places in the close position endpoint.

### Accessibility
- **svelte-check 0 warnings** (`ui/Tooltip.svelte`, `ui/Collapsible.svelte`): Added `role="group"` to the Tooltip wrapper span; used `untrack()` in Collapsible to silence the `state_referenced_locally` hint.
- **Modal** (`ui/Modal.svelte`): Autofocuses the first field on open; added `tabindex` and keyboard handling.
- **Tables**: Added `scope="col"` to headers in `DataTable`, `PerformanceDashboard`, and `OpportunityLedger`.
- **Severity**: Safeguard alerts convey severity by icon + text (via `Alert`), not color alone.

### Documentation
- **Modular specification** (`spec/`): Split the monolithic `spec/project_spec.md` into concern-based files indexed by `spec/README.md` — `product.md`, `architecture.md`, `domain-rules.md`, `data-models.md`, `api.md`, `decisions.md`, and `standards.md`. Original preserved at `spec/archive/project_spec_v8.md`.
- **Analysis docs** (`spec/`): Added `gap-analysis.md`, `ux-review.md`, and `roadmap.md`.
- **Issue-driven workflow**: Documented the full GitHub CLI loop in `spec/standards.md`, `CLAUDE.md`, and `GEMINI.md`. Board Auto-add and Item-closed→Done workflows wired up.

## [0.5.0] - 2026-06-09

### Added
- **Sprint 5: Intent Journal** (`backend/models.py`, `backend/main.py`): `OperationalJournalEntrySchema` is now mandatory on `POST /api/positions`; missing or partial journal returns 422. Positions store `warnings_acknowledged: List[str]` to track which warnings the user confirmed before saving.
- **Sprint 5: Close Position workflow** (`POST /api/positions/{id}/close`): Accepts `ClosePositionRequest` (current value, exit trigger, actual move %, lesson tags). Computes realized P&L (DEBIT: current−entry; CREDIT: entry−current), derives WIN/LOSS/BREAKEVEN outcome, sets `user_override_logged` from `warnings_acknowledged`, freezes position to CLOSED, and creates a `ClosurePostMortemModel` record — all in one atomic transaction.
- **Sprint 5: Post-mortem retrieval** (`GET /api/positions/post-mortems`, `GET /api/positions/{id}/post-mortem`): List all post-mortems or fetch by position ID; route ordering ensures `/post-mortems` resolves before `/{id}`.
- **Sprint 5: Opportunity Ledger** (`GET/POST /api/opportunity/ledger`, `PATCH /api/opportunity/ledger/{id}`): Logs every accepted and bypassed trade opportunity. PATCH endpoint updates `outcome_if_taken` for after-the-fact analysis.
- **Sprint 5: Performance Diagnostics** (`GET /api/performance/diagnostics`): Returns `PerformanceDiagnosticsSchema` with per-playbook win rate, profit factor, avg return-on-risk grouped by `(playbook_id, playbook_version)`. CAGR/Sharpe/max-drawdown stub as "N/A (insufficient data)". Benchmarks section is stubbed. Initializes empty — no fictional data.
- **Sprint 5 ORM models**: `ClosurePostMortemModel` (table `closure_post_mortems`) and `OpportunityRecordModel` (table `opportunity_records`).
- **Sprint 5 Pydantic schemas**: `ClosePositionRequest`, `ClosurePostMortemSchema`, `OpportunityRecordSchema`, `UpdateOutcomeRequest`, `PlaybookMetrics`, `BenchmarkData`, `PerformanceDiagnosticsSchema`.
- **Sprint 5 frontend** (`TradeSpecCard.svelte`): "Save Trade Spec & Log Intent Journal" now reveals a mandatory 5-field intent journal form (thesis, invalidation, expected move, emotional state, confidence rating); "Confirm & Save Position" button only enabled when all fields are valid. On save: creates the position via API and logs an accepted opportunity record.
- **Sprint 5 frontend** (`PositionScanner.svelte`): P1-priority cards now show a "Close Position Now →" button; triggers `ClosePositionModal` overlay.
- **Sprint 5 frontend** (`CandidateCards.svelte`): Override button on suppressed playbooks now logs a bypassed `OpportunityRecord` (with suppression reason) before generating the trade spec.
- **Sprint 5 frontend** (`ClosePositionModal.svelte`, `PostMortemCard.svelte`, `OpportunityLedger.svelte`, `PerformanceDashboard.svelte`): New display components for the full post-trade workflow.
- **Sprint 5 frontend** (`App.svelte`): Integrates all Sprint 5 state (post-mortems, opportunity records, diagnostics), modal close flow, and position-saved callback.
- **Sprint 5 Tests** (`backend/tests/test_sprint5.py`): 28 tests covering journal enforcement (422 variants), close position P&L logic (WIN/LOSS/BREAKEVEN/double-close/404), post-mortem retrieval, opportunity ledger CRUD, and diagnostics computation. Total test count: 170.

### Fixed
- **Pre-sprint-5 bug fixes** (`backend/opportunity.py`, `backend/database.py`): Five correctness issues fixed — dead `_spy_trend_label` branch, `run_lifecycle_scan` hardcoded spy_price/regime in `_run_hard_blocks`, PREMIUM_UNREASONABLE using BUY-leg strike instead of market price, Iron Condor profit/loss targets using max_loss instead of limit price, and `_needs_migration` early-return skipping Sprint 4 check.

## [0.4.0] - 2026-06-09

### Added
- **Layer C Opportunity Engine** (`backend/opportunity.py`): Full Section 4.3/5.1/5.2/5.5 implementation. Scans all active playbooks against current market telemetry; applies portfolio-level gates (MAX_POSITIONS, MAX_CAPITAL), per-playbook suppression gates (UNDERLYING_CONCENTRATION, DIRECTIONAL_CONCENTRATION, IVR_GATE_INCOME, IVR_GATE_DEBIT), and entry filters (IVR range, VIX range, trend, catalyst). Returns `OpportunityScanResult` with eligible candidate cards and derived strike parameters.
- **Trade Spec Generator** (`backend/opportunity.py`): `generate_trade_spec()` derives concrete legs, limit price, max loss, break-even prices, profit target, loss limit, and GTC closing instructions for Iron Condor, Bull Call Spread, Bear Put Spread, Long Straddle, and Long Strangle strategies. Uses VIX-based 1σ move and rational Φ⁻¹ approximation for strike derivation; all derivation inputs recorded in `StrikeDerivedParams` for full traceability.
- **Trade Spec Validation** (`backend/opportunity.py`): `_run_hard_blocks()` checks UNRESOLVED_P1 (per-position lifecycle scan), CAPITAL_EXCEEDED, MAX_LOSS_EXCEEDED, EXPIRATION_ARITHMETIC (< 14 DTE), PREMIUM_UNREASONABLE, POSITION_COUNT, and STRIKE_SANITY. `_run_warnings()` checks REGIME_CONSISTENCY, DUPLICATE_UNDERLYING, BREAKEVEN_REALISM (> 2σ), and STRATEGY_NOVELTY. Hard blocks set `spec=None`; warnings require explicit UI confirmation before proceeding.
- **Playbook Seeding** (`backend/database.py`): Five default playbooks seeded on `init_db()`: SPY Iron Condor, Bull Call Spread, Bear Put Spread, Long Straddle, Long Strangle.
- **Sprint 4 API models** (`backend/models.py`): `StrikeDerivedParams`, `CandidateCard`, `OpportunityScanResult`, `TradeSpecLeg`, `TradeSpec`, `HardBlock`, `TradeWarning`, `TradeSpecResult`.
- **`GET /api/opportunity/scan`**: Returns eligible candidate cards with automated strike derivation notes for all non-suppressed playbooks.
- **`POST /api/opportunity/spec/{playbook_id}`**: Generates a full `TradeSpecResult` with hard-block/warning validation for the given playbook.
- **`CandidateCards.svelte`** (frontend): Displays eligible playbooks with automated order specification and per-card "Generate Trade Spec →" button; shows portfolio-level suppression banner when blocked.
- **`TradeSpecCard.svelte`** (frontend): Full trade specification display with per-warning "Acknowledged" button, hard-block suppression (no bypass), P&L grid, order legs, break-evens, derivation parameters, and GTC closing instructions. Proceed button gated on all warnings confirmed.
- **App.svelte component extraction**: Split `MarketContextRibbon.svelte`, `GreeksPanel.svelte`, `SafeguardsPanel.svelte`, and `PositionScanner.svelte` out of App.svelte; App.svelte reduced from ~727 to ~530 lines as a thin orchestrator.
- **Sprint 4 Tests** (`backend/tests/test_sprint4.py`): 60 tests covering all gates, entry filters, hard blocks, warnings, strike derivation, spec generation, and API integration. Total test count: 142 across all four test files.
- **OpenAPI type sync**: Regenerated `frontend/src/lib/api-types.ts` to include all Sprint 4 schemas.

## [0.3.1] - 2026-06-09

### Fixed
- **Live market fetch** (`backend/market_data.py`): Alpaca API returns `null` bars when no date range is given and today's session is incomplete; added `start = today - 60 days` to guarantee historical bars are returned.
- **Alpaca feed** (`backend/market_data.py`): Changed feed from `sip` to `iex` for free-tier account compatibility; also fixed `payload.get("bars") or []` to safely handle `null` in the response.
- **Credential loading** (`backend/main.py`): Load `.env` with an explicit absolute path and `override=True` so credentials are always available regardless of working directory or pre-set shell environment variables.
- **Lazy credential reads** (`backend/market_data.py`): Read Alpaca credentials via `os.environ.get()` at call time instead of capturing module-level constants at import, preventing stale empty values.

### Added
- **`python-dotenv` dependency**: Added to `pixi.toml` and `pyproject.toml`.
- **`svelte.config.js`** (`frontend/`): Added to fix `svelte-check` failing to load config due to a Vite CJS/ESM incompatibility in `@sveltejs/load-config`.
- **Dev tooling tasks** (`pixi.toml`): Added `check-frontend` (svelte-check + TypeScript), `check-backend` (compileall), and `check` (both) tasks.
- **`PYTHONUNBUFFERED=1`** (`pixi.toml`): Set on the `server` task for reliable stdout output from the uvicorn worker process.

### Changed
- **Verification Script** (`scripts/verify-project.ps1`): Overhauled to auto-detect project type, validate conventional commits, scan for hardcoded secrets, and verify documentation sync.
- **Workspace Config**: Replaced `AGENTS.md` with `CLAUDE.md` (project-level Claude Code instructions), added `.claudeignore` to reduce token overhead, and updated `.gitignore` for AI agent artifacts.
- **SQLAlchemy engine** (`backend/database.py`): Removed `echo=True` to suppress verbose SQL INFO logs.

## [0.3.0] - 2026-06-08

### Added
- **Regime Scoring Matrix** (`backend/regime.py`): Implement the full Section 4.2 weighted scoring matrix classifying SPY/SMA20 trend, VIX level, per-underlying IVR, catalyst calendar, and daily return into four market regimes. Tie-breaking follows a risk-priority hierarchy: `EVENT_CATALYST > TRENDING_BEAR > HIGH_VOL_NEUTRAL > CALM_BULL`.
- **Market Data Client** (`backend/market_data.py`): Isolated Alpaca API client that fetches SPY daily bars (price, SMA20, daily return) and VIX closing price, with graceful `None` fallback when credentials are absent or requests fail.
- **Extended Market State**: `MarketStateSchema` and `MarketStateModel` now store `spy_sma20`, `vix_close`, `underlying_ivrs`, `spy_daily_return`, and `regime_scores` alongside the existing fields.
- **`POST /api/market/state`**: Now recomputes the regime server-side from the supplied telemetry inputs — `current_regime` in the request body is ignored and always recalculated.
- **`POST /api/market/fetch`**: New endpoint that triggers a live Alpaca data pull, updates the stored telemetry, and recomputes the active regime. Returns `503` when credentials are not configured.
- **Layer B Context Ribbon** (frontend): Subordinate ribbon below the header showing the active regime badge, live telemetry pills (SPY price, SMA20, VIX, daily return), and a collapsible score breakdown panel for all four regimes.
- **Expanded Telemetry Form** (frontend): Replaced the manual regime dropdown with six input fields (SPY price, SMA20, VIX, daily return %, IVRs, catalyst dates) and a "Fetch Live Data" button wired to the new endpoint.
- **Sprint 3 Tests** (`backend/tests/test_sprint3.py`): 57 tests covering all five classification functions and their boundary conditions, known scenario matrix outputs, all tie-breaking combinations, mocked Alpaca fetch calls, and API integration tests for the new endpoints.

## [0.2.0] - 2026-06-08

### Added
- **Observation Engine**: Implement position lifecycle scanner, portfolio Greeks aggregator, and exposure safeguards.
- **Simulated Telemetry**: Build mock market environment state APIs and UI controls to adjust simulated regimes, SPY index prices, and catalyst calendars.
- **Session Lock**: Lock navigation and settings access until the portfolio risk telemetry has been reviewed and acknowledged for the active session.
- **Tests**: Add 13 backend unit and integration tests covering priority transitions, DTE decay, short strike breaches, and safeguards.
