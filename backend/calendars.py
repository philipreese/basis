"""calendars.py — the operator-maintained static calendars (#187).

Three domain modules consume dates that only a human maintains: projected
ex-dividend dates (assignment defense, #130), FOMC/CPI schedules (catalyst
merging, #131), and NYSE full closures (the holiday guard, #68). The tables
and the one staleness rule live here; the domain logic stays with its
concern. One digest question — "is any calendar about to run out?" — has
one interface: stale_calendars().

All dates are projections until confirmed against the published schedules
(SSGA/iShares distributions, the Fed and BLS calendars, the NYSE holiday
page). The staleness horizon flags coverage in the digest before it lapses.
"""

import datetime

# Projected ex-dividend dates (ISO). SPY: third Friday of Mar/Jun/Sep/Dec.
# IWM: quarterly, mid-month (verified 2026-08-18: September is 09-15 per
# iShares — NOT month-end). TLT: monthly, first business day.
EX_DIV_CALENDAR: dict[str, tuple[str, ...]] = {
    # AAPL (#317): quarterly, historically the Feb/May/Aug/Nov second week.
    # Projections from the published pattern; confirm each against the
    # declared date when the earnings entry is typed in.
    "AAPL": (
        "2026-11-09",
        "2027-02-08",
        "2027-05-10",
        "2027-08-09",
        "2027-11-08",
    ),
    # SPY 2026-09-18 confirmed against SSGA's distribution schedule
    # (2026-08-18); June was actually 06-18.
    #
    # 2009-2023 are historical ACTUALS for the replay engine (#795/#796),
    # each date confirmed by two independent dividend-history sources
    # (Yahoo Finance dividend events + dividendhistory.org; 2012-09-21 by
    # Yahoo + digrin.com). They all land on third Fridays in this window,
    # but the list is from actuals, not third-Friday inference.
    #
    # HISTORICAL GAP (deliberate): IWM, TLT, and AAPL have no 2009-2023
    # entries. TLT aggregator histories disagreed with each other (missing
    # 2023-12-01, disputed 2023-12-14/15, a hole in Nov 2012) and the
    # authoritative iShares distribution CSV was unreachable; IWM had two
    # December dates (2019-12-16, 2020-12-14) verifiable from only one
    # source. Rather than seed a hard entry block with single-source
    # dates, the replay coverage declared below is SPY-only for ex-div —
    # replay of other tickers' ex-div behavior is out of coverage.
    "SPY": (
        "2009-03-20",
        "2009-06-19",
        "2009-09-18",
        "2009-12-18",
        "2010-03-19",
        "2010-06-18",
        "2010-09-17",
        "2010-12-17",
        "2011-03-18",
        "2011-06-17",
        "2011-09-16",
        "2011-12-16",
        "2012-03-16",
        "2012-06-15",
        "2012-09-21",
        "2012-12-21",
        "2013-03-15",
        "2013-06-21",
        "2013-09-20",
        "2013-12-20",
        "2014-03-21",
        "2014-06-20",
        "2014-09-19",
        "2014-12-19",
        "2015-03-20",
        "2015-06-19",
        "2015-09-18",
        "2015-12-18",
        "2016-03-18",
        "2016-06-17",
        "2016-09-16",
        "2016-12-16",
        "2017-03-17",
        "2017-06-16",
        "2017-09-15",
        "2017-12-15",
        "2018-03-16",
        "2018-06-15",
        "2018-09-21",
        "2018-12-21",
        "2019-03-15",
        "2019-06-21",
        "2019-09-20",
        "2019-12-20",
        "2020-03-20",
        "2020-06-19",
        "2020-09-18",
        "2020-12-18",
        "2021-03-19",
        "2021-06-18",
        "2021-09-17",
        "2021-12-17",
        "2022-03-18",
        "2022-06-17",
        "2022-09-16",
        "2022-12-16",
        "2023-03-17",
        "2023-06-16",
        "2023-09-15",
        "2023-12-15",
        "2026-03-20",
        "2026-06-18",
        "2026-09-18",
        "2026-12-18",
        "2027-03-19",
        "2027-06-18",
        "2027-09-17",
        "2027-12-17",
    ),
    "IWM": (
        "2026-03-17",
        "2026-06-16",
        "2026-09-15",
        "2026-12-15",
        "2027-03-16",
        "2027-06-15",
        "2027-09-14",
        "2027-12-14",
    ),
    "TLT": (
        "2026-09-01",
        "2026-10-01",
        "2026-11-02",
        "2026-12-01",
        "2027-01-04",
        "2027-02-01",
        "2027-03-01",
        "2027-04-01",
        "2027-05-03",
        "2027-06-01",
        "2027-07-01",
        "2027-08-02",
        "2027-09-01",
        "2027-10-01",
        "2027-11-01",
        "2027-12-01",
    ),
}

# DELIBERATELY ABSENT: election days (e.g. the 2026-11-03 midterms). Decided
# 2026-08-19 (#233): an election inside a Live Gate window is the stress
# episode ADR-0010 requires — adding it as a MAJOR catalyst would block
# entries across it and make the books hold their fewest positions exactly
# when the experiment most needs exposure. The regime engine and IVR gates
# are the general-purpose defense; do not "helpfully" add election dates.

# FOMC decision days (second day of each two-day meeting; the Fed posts
# them years ahead). 2027 dates are projections until the Fed publishes.
#
# 2009-2023 are historical actuals for the replay engine (#795/#796):
# scheduled meetings only, decision day (second day of two-day meetings;
# some 2009-2012 meetings were one-day — that day). Verified against
# federalreserve.gov/monetarypolicy/fomchistorical<year>.htm (2009-2020)
# and fomccalendars.htm (2021-2023). Unscheduled meetings, conference
# calls, and notation votes are excluded — notably the March 2020
# emergency actions (Mar 2 / Mar 15, both listed "unscheduled" by the
# Fed); the scheduled 2020-03-17/18 meeting was CANCELLED, so 2020 has
# seven scheduled decision days, not eight.
FOMC_DATES: tuple[str, ...] = (
    "2009-01-28",
    "2009-03-18",
    "2009-04-29",
    "2009-06-24",
    "2009-08-12",
    "2009-09-23",
    "2009-11-04",
    "2009-12-16",
    "2010-01-27",
    "2010-03-16",
    "2010-04-28",
    "2010-06-23",
    "2010-08-10",
    "2010-09-21",
    "2010-11-03",
    "2010-12-14",
    "2011-01-26",
    "2011-03-15",
    "2011-04-27",
    "2011-06-22",
    "2011-08-09",
    "2011-09-21",
    "2011-11-02",
    "2011-12-13",
    "2012-01-25",
    "2012-03-13",
    "2012-04-25",
    "2012-06-20",
    "2012-08-01",
    "2012-09-13",
    "2012-10-24",
    "2012-12-12",
    "2013-01-30",
    "2013-03-20",
    "2013-05-01",
    "2013-06-19",
    "2013-07-31",
    "2013-09-18",
    "2013-10-30",
    "2013-12-18",
    "2014-01-29",
    "2014-03-19",
    "2014-04-30",
    "2014-06-18",
    "2014-07-30",
    "2014-09-17",
    "2014-10-29",
    "2014-12-17",
    "2015-01-28",
    "2015-03-18",
    "2015-04-29",
    "2015-06-17",
    "2015-07-29",
    "2015-09-17",
    "2015-10-28",
    "2015-12-16",
    "2016-01-27",
    "2016-03-16",
    "2016-04-27",
    "2016-06-15",
    "2016-07-27",
    "2016-09-21",
    "2016-11-02",
    "2016-12-14",
    "2017-02-01",
    "2017-03-15",
    "2017-05-03",
    "2017-06-14",
    "2017-07-26",
    "2017-09-20",
    "2017-11-01",
    "2017-12-13",
    "2018-01-31",
    "2018-03-21",
    "2018-05-02",
    "2018-06-13",
    "2018-08-01",
    "2018-09-26",
    "2018-11-08",
    "2018-12-19",
    "2019-01-30",
    "2019-03-20",
    "2019-05-01",
    "2019-06-19",
    "2019-07-31",
    "2019-09-18",
    "2019-10-30",
    "2019-12-11",
    "2020-01-29",
    "2020-04-29",
    "2020-06-10",
    "2020-07-29",
    "2020-09-16",
    "2020-11-05",
    "2020-12-16",
    "2021-01-27",
    "2021-03-17",
    "2021-04-28",
    "2021-06-16",
    "2021-07-28",
    "2021-09-22",
    "2021-11-03",
    "2021-12-15",
    "2022-01-26",
    "2022-03-16",
    "2022-05-04",
    "2022-06-15",
    "2022-07-27",
    "2022-09-21",
    "2022-11-02",
    "2022-12-14",
    "2023-02-01",
    "2023-03-22",
    "2023-05-03",
    "2023-06-14",
    "2023-07-26",
    "2023-09-20",
    "2023-11-01",
    "2023-12-13",
    "2026-01-28",
    "2026-03-18",
    "2026-04-29",
    "2026-06-17",
    "2026-07-29",
    "2026-09-16",
    "2026-10-28",
    "2026-12-09",
    "2027-01-27",
    "2027-03-17",
    "2027-04-28",
    "2027-06-16",
    "2027-07-28",
    "2027-09-15",
    "2027-10-27",
    "2027-12-08",
)

# CPI release days (BLS, ~8:30 ET mid-month). 2026 verified against the
# posted BLS schedule 2026-08-18; 2027 projects the second-week pattern.
#
# 2009-2023 are historical actuals for the replay engine (#795/#796):
# every date from the BLS archived-news-release pages (bls.gov/bls/
# news-release/cpi.htm, the modern home of the old schedule/archives/
# cpi_nr.htm; BLS keys each archive file to its release date), 2009-2016
# cross-checked against a 2016 snapshot of the original archive page.
# Twelve releases per year; note 2013-10-30 — the Sep 2013 release was
# delayed by the October 2013 government shutdown.
CPI_DATES: tuple[str, ...] = (
    "2009-01-16",
    "2009-02-20",
    "2009-03-18",
    "2009-04-15",
    "2009-05-15",
    "2009-06-17",
    "2009-07-15",
    "2009-08-14",
    "2009-09-16",
    "2009-10-15",
    "2009-11-18",
    "2009-12-16",
    "2010-01-15",
    "2010-02-19",
    "2010-03-18",
    "2010-04-14",
    "2010-05-19",
    "2010-06-17",
    "2010-07-16",
    "2010-08-13",
    "2010-09-17",
    "2010-10-15",
    "2010-11-17",
    "2010-12-15",
    "2011-01-14",
    "2011-02-17",
    "2011-03-17",
    "2011-04-15",
    "2011-05-13",
    "2011-06-15",
    "2011-07-15",
    "2011-08-18",
    "2011-09-15",
    "2011-10-19",
    "2011-11-16",
    "2011-12-16",
    "2012-01-19",
    "2012-02-17",
    "2012-03-16",
    "2012-04-13",
    "2012-05-15",
    "2012-06-14",
    "2012-07-17",
    "2012-08-15",
    "2012-09-14",
    "2012-10-16",
    "2012-11-15",
    "2012-12-14",
    "2013-01-16",
    "2013-02-21",
    "2013-03-15",
    "2013-04-16",
    "2013-05-16",
    "2013-06-18",
    "2013-07-16",
    "2013-08-15",
    "2013-09-17",
    "2013-10-30",
    "2013-11-20",
    "2013-12-17",
    "2014-01-16",
    "2014-02-20",
    "2014-03-18",
    "2014-04-15",
    "2014-05-15",
    "2014-06-17",
    "2014-07-22",
    "2014-08-19",
    "2014-09-17",
    "2014-10-22",
    "2014-11-20",
    "2014-12-17",
    "2015-01-16",
    "2015-02-26",
    "2015-03-24",
    "2015-04-17",
    "2015-05-22",
    "2015-06-18",
    "2015-07-17",
    "2015-08-19",
    "2015-09-16",
    "2015-10-15",
    "2015-11-17",
    "2015-12-15",
    "2016-01-20",
    "2016-02-19",
    "2016-03-16",
    "2016-04-14",
    "2016-05-17",
    "2016-06-16",
    "2016-07-15",
    "2016-08-16",
    "2016-09-16",
    "2016-10-18",
    "2016-11-17",
    "2016-12-15",
    "2017-01-18",
    "2017-02-15",
    "2017-03-15",
    "2017-04-14",
    "2017-05-12",
    "2017-06-14",
    "2017-07-14",
    "2017-08-11",
    "2017-09-14",
    "2017-10-13",
    "2017-11-15",
    "2017-12-13",
    "2018-01-12",
    "2018-02-14",
    "2018-03-13",
    "2018-04-11",
    "2018-05-10",
    "2018-06-12",
    "2018-07-12",
    "2018-08-10",
    "2018-09-13",
    "2018-10-11",
    "2018-11-14",
    "2018-12-12",
    "2019-01-11",
    "2019-02-13",
    "2019-03-12",
    "2019-04-10",
    "2019-05-10",
    "2019-06-12",
    "2019-07-11",
    "2019-08-13",
    "2019-09-12",
    "2019-10-10",
    "2019-11-13",
    "2019-12-11",
    "2020-01-14",
    "2020-02-13",
    "2020-03-11",
    "2020-04-10",
    "2020-05-12",
    "2020-06-10",
    "2020-07-14",
    "2020-08-12",
    "2020-09-11",
    "2020-10-13",
    "2020-11-12",
    "2020-12-10",
    "2021-01-13",
    "2021-02-10",
    "2021-03-10",
    "2021-04-13",
    "2021-05-12",
    "2021-06-10",
    "2021-07-13",
    "2021-08-11",
    "2021-09-14",
    "2021-10-13",
    "2021-11-10",
    "2021-12-10",
    "2022-01-12",
    "2022-02-10",
    "2022-03-10",
    "2022-04-12",
    "2022-05-11",
    "2022-06-10",
    "2022-07-13",
    "2022-08-10",
    "2022-09-13",
    "2022-10-13",
    "2022-11-10",
    "2022-12-13",
    "2023-01-12",
    "2023-02-14",
    "2023-03-14",
    "2023-04-12",
    "2023-05-10",
    "2023-06-13",
    "2023-07-12",
    "2023-08-10",
    "2023-09-13",
    "2023-10-12",
    "2023-11-14",
    "2023-12-12",
    "2026-01-13",
    "2026-02-13",
    "2026-03-11",
    "2026-04-10",
    "2026-05-12",
    "2026-06-10",
    "2026-07-14",
    "2026-08-12",
    "2026-09-11",
    "2026-10-14",
    "2026-11-10",
    "2026-12-10",
    "2027-01-13",
    "2027-02-10",
    "2027-03-10",
    "2027-04-13",
    "2027-05-12",
    "2027-06-10",
    "2027-07-13",
    "2027-08-11",
    "2027-09-14",
    "2027-10-13",
    "2027-11-10",
    "2027-12-10",
)

# Full-day US equity market closures. Verify against the NYSE published
# calendar as each year posts; observed dates included where the holiday
# falls on a weekend. Half days (day after Thanksgiving, Christmas Eve)
# still trade — the evening cadence runs after any close.
# 2009-2023 are historical actuals for the replay engine (#795/#796),
# cross-checked against the official NYSE Group holiday-calendar press
# releases (ir.theice.com, 2018-2023) and two independent historical
# NYSE-closure datasets for 2009-2017. One-off closures in range:
# Hurricane Sandy (2012-10-29/30) and the G.H.W. Bush day of mourning
# (2018-12-05). No COVID closure — markets stayed open through 2020.
# Saturday-holiday rule: when Jan 1 falls on a Saturday NYSE does NOT
# observe it (no closure for New Year's 2011 or 2022, per NYSE Rule 7.2);
# Saturday Jul 4 / Dec 25 are observed the Friday before. Juneteenth is a
# holiday only from 2022.
MARKET_HOLIDAYS: frozenset[str] = frozenset(
    {
        # 2009
        "2009-01-01",  # New Year's Day
        "2009-01-19",  # Martin Luther King Jr. Day
        "2009-02-16",  # Washington's Birthday
        "2009-04-10",  # Good Friday
        "2009-05-25",  # Memorial Day
        "2009-07-03",  # Independence Day (observed)
        "2009-09-07",  # Labor Day
        "2009-11-26",  # Thanksgiving
        "2009-12-25",  # Christmas
        # 2010
        "2010-01-01",  # New Year's Day
        "2010-01-18",  # Martin Luther King Jr. Day
        "2010-02-15",  # Washington's Birthday
        "2010-04-02",  # Good Friday
        "2010-05-31",  # Memorial Day
        "2010-07-05",  # Independence Day (observed)
        "2010-09-06",  # Labor Day
        "2010-11-25",  # Thanksgiving
        "2010-12-24",  # Christmas (observed)
        # 2011 — no New Year's closure (Jan 1 was a Saturday)
        "2011-01-17",  # Martin Luther King Jr. Day
        "2011-02-21",  # Washington's Birthday
        "2011-04-22",  # Good Friday
        "2011-05-30",  # Memorial Day
        "2011-07-04",  # Independence Day
        "2011-09-05",  # Labor Day
        "2011-11-24",  # Thanksgiving
        "2011-12-26",  # Christmas (observed)
        # 2012
        "2012-01-02",  # New Year's Day (observed)
        "2012-01-16",  # Martin Luther King Jr. Day
        "2012-02-20",  # Washington's Birthday
        "2012-04-06",  # Good Friday
        "2012-05-28",  # Memorial Day
        "2012-07-04",  # Independence Day
        "2012-09-03",  # Labor Day
        "2012-10-29",  # Hurricane Sandy
        "2012-10-30",  # Hurricane Sandy
        "2012-11-22",  # Thanksgiving
        "2012-12-25",  # Christmas
        # 2013
        "2013-01-01",  # New Year's Day
        "2013-01-21",  # Martin Luther King Jr. Day
        "2013-02-18",  # Washington's Birthday
        "2013-03-29",  # Good Friday
        "2013-05-27",  # Memorial Day
        "2013-07-04",  # Independence Day
        "2013-09-02",  # Labor Day
        "2013-11-28",  # Thanksgiving
        "2013-12-25",  # Christmas
        # 2014
        "2014-01-01",  # New Year's Day
        "2014-01-20",  # Martin Luther King Jr. Day
        "2014-02-17",  # Washington's Birthday
        "2014-04-18",  # Good Friday
        "2014-05-26",  # Memorial Day
        "2014-07-04",  # Independence Day
        "2014-09-01",  # Labor Day
        "2014-11-27",  # Thanksgiving
        "2014-12-25",  # Christmas
        # 2015
        "2015-01-01",  # New Year's Day
        "2015-01-19",  # Martin Luther King Jr. Day
        "2015-02-16",  # Washington's Birthday
        "2015-04-03",  # Good Friday
        "2015-05-25",  # Memorial Day
        "2015-07-03",  # Independence Day (observed)
        "2015-09-07",  # Labor Day
        "2015-11-26",  # Thanksgiving
        "2015-12-25",  # Christmas
        # 2016
        "2016-01-01",  # New Year's Day
        "2016-01-18",  # Martin Luther King Jr. Day
        "2016-02-15",  # Washington's Birthday
        "2016-03-25",  # Good Friday
        "2016-05-30",  # Memorial Day
        "2016-07-04",  # Independence Day
        "2016-09-05",  # Labor Day
        "2016-11-24",  # Thanksgiving
        "2016-12-26",  # Christmas (observed)
        # 2017
        "2017-01-02",  # New Year's Day (observed)
        "2017-01-16",  # Martin Luther King Jr. Day
        "2017-02-20",  # Washington's Birthday
        "2017-04-14",  # Good Friday
        "2017-05-29",  # Memorial Day
        "2017-07-04",  # Independence Day
        "2017-09-04",  # Labor Day
        "2017-11-23",  # Thanksgiving
        "2017-12-25",  # Christmas
        # 2018
        "2018-01-01",  # New Year's Day
        "2018-01-15",  # Martin Luther King Jr. Day
        "2018-02-19",  # Washington's Birthday
        "2018-03-30",  # Good Friday
        "2018-05-28",  # Memorial Day
        "2018-07-04",  # Independence Day
        "2018-09-03",  # Labor Day
        "2018-11-22",  # Thanksgiving
        "2018-12-05",  # National Day of Mourning (G.H.W. Bush)
        "2018-12-25",  # Christmas
        # 2019
        "2019-01-01",  # New Year's Day
        "2019-01-21",  # Martin Luther King Jr. Day
        "2019-02-18",  # Washington's Birthday
        "2019-04-19",  # Good Friday
        "2019-05-27",  # Memorial Day
        "2019-07-04",  # Independence Day
        "2019-09-02",  # Labor Day
        "2019-11-28",  # Thanksgiving
        "2019-12-25",  # Christmas
        # 2020
        "2020-01-01",  # New Year's Day
        "2020-01-20",  # Martin Luther King Jr. Day
        "2020-02-17",  # Washington's Birthday
        "2020-04-10",  # Good Friday
        "2020-05-25",  # Memorial Day
        "2020-07-03",  # Independence Day (observed)
        "2020-09-07",  # Labor Day
        "2020-11-26",  # Thanksgiving
        "2020-12-25",  # Christmas
        # 2021
        "2021-01-01",  # New Year's Day
        "2021-01-18",  # Martin Luther King Jr. Day
        "2021-02-15",  # Washington's Birthday
        "2021-04-02",  # Good Friday
        "2021-05-31",  # Memorial Day
        "2021-07-05",  # Independence Day (observed)
        "2021-09-06",  # Labor Day
        "2021-11-25",  # Thanksgiving
        "2021-12-24",  # Christmas (observed)
        # 2022 — no New Year's closure (Jan 1 was a Saturday)
        "2022-01-17",  # Martin Luther King Jr. Day
        "2022-02-21",  # Washington's Birthday
        "2022-04-15",  # Good Friday
        "2022-05-30",  # Memorial Day
        "2022-06-20",  # Juneteenth (observed)
        "2022-07-04",  # Independence Day
        "2022-09-05",  # Labor Day
        "2022-11-24",  # Thanksgiving
        "2022-12-26",  # Christmas (observed)
        # 2023
        "2023-01-02",  # New Year's Day (observed)
        "2023-01-16",  # Martin Luther King Jr. Day
        "2023-02-20",  # Washington's Birthday
        "2023-04-07",  # Good Friday
        "2023-05-29",  # Memorial Day
        "2023-06-19",  # Juneteenth
        "2023-07-04",  # Independence Day
        "2023-09-04",  # Labor Day
        "2023-11-23",  # Thanksgiving
        "2023-12-25",  # Christmas
        # 2026
        "2026-01-01",  # New Year's Day
        "2026-01-19",  # Martin Luther King Jr. Day
        "2026-02-16",  # Washington's Birthday
        "2026-04-03",  # Good Friday
        "2026-05-25",  # Memorial Day
        "2026-06-19",  # Juneteenth
        "2026-07-03",  # Independence Day (observed)
        "2026-09-07",  # Labor Day
        "2026-11-26",  # Thanksgiving
        "2026-12-25",  # Christmas
        # 2027
        "2027-01-01",  # New Year's Day
        "2027-01-18",  # Martin Luther King Jr. Day
        "2027-02-15",  # Washington's Birthday
        "2027-03-26",  # Good Friday
        "2027-05-31",  # Memorial Day
        "2027-06-18",  # Juneteenth (observed)
        "2027-07-05",  # Independence Day (observed)
        "2027-09-06",  # Labor Day
        "2027-11-25",  # Thanksgiving
        "2027-12-24",  # Christmas (observed)
    }
)

# Contiguous verified HISTORICAL coverage of every table above (#795):
# each of 2009-2023 has its full verified set of NYSE closures, scheduled
# FOMC decision days, BLS CPI releases, and SPY ex-div dates. Replay
# tools (#796) MUST refuse to run over any date outside this range —
# fail closed: outside it the tables silently report "no holiday, no
# catalyst, no ex-div", which is wrong in the flattering direction
# (ADR-0015). 2024-2025 are unverified (production only ever queried
# 2026+); the 2026-2027 entries are the live operator-maintained window,
# not part of this range.
CALENDAR_COVERAGE_START: datetime.date = datetime.date(2009, 1, 1)
CALENDAR_COVERAGE_END: datetime.date = datetime.date(2023, 12, 31)

# Days of remaining coverage below which a calendar is flagged stale.
CALENDAR_HORIZON_DAYS = 60


def is_trading_day(day: datetime.date) -> bool:
    """Weekday and not a full-day closure. On holidays the executor writes
    its heartbeat and exits, and the gateway lifecycle never launches (#68)."""
    return day.weekday() < 5 and day.isoformat() not in MARKET_HOLIDAYS


def trading_days_between(start: datetime.date, end: datetime.date) -> int:
    """Trading days in (start, end] — holiday-aware, via is_trading_day.

    #742: assignment_defense._trading_days_between and regime_variants'
    _trading_days_until each carried their own local weekday-only
    approximation (explicitly flagged as one in their comments), which
    over-counts trading days across a market holiday. That over-count is the
    dangerous direction for a `<= N trading days` window check: the true
    count is smaller than the weekday count, so a real "within N trading
    days" condition can evaluate false and a P1/catalyst alert fires late.
    Both now delegate here, so the holiday-aware fix can't be added to one
    and missed in the other again."""
    days = 0
    d = start
    while d < end:
        d += datetime.timedelta(days=1)
        if is_trading_day(d):
            days += 1
    return days


def snap_to_trading_day(day: datetime.date) -> datetime.date:
    """Walk a date back to the nearest trading day (#282, #541): listed
    options expiring on a market holiday (Good Friday) actually expire the
    prior trading day — a naive Friday-of-week snap yields an expiration
    that doesn't exist and the leg fails to quote. Shared by every
    expiration-date derivation (new entries, rolls, calendar back legs) so
    the holiday adjustment can't be added to one and missed in another."""
    guard = 0
    while not is_trading_day(day) and guard < 7:
        day -= datetime.timedelta(days=1)
        guard += 1
    return day


def _ends_within_horizon(dates: tuple[str, ...] | frozenset[str], horizon: datetime.date) -> bool:
    return max(datetime.date.fromisoformat(d) for d in dates) < horizon


def stale_calendars(today: datetime.date) -> list[str]:
    """Labels of every calendar whose coverage ends within the horizon —
    the operator must extend its table before it silently runs out."""
    horizon = today + datetime.timedelta(days=CALENDAR_HORIZON_DAYS)
    stale = [
        f"ex-div {symbol}" for symbol, dates in sorted(EX_DIV_CALENDAR.items()) if _ends_within_horizon(dates, horizon)
    ]
    if _ends_within_horizon((*FOMC_DATES, *CPI_DATES), horizon):
        stale.append("FOMC/CPI catalysts")
    if _ends_within_horizon(MARKET_HOLIDAYS, horizon):
        stale.append("market holidays")
    return stale
