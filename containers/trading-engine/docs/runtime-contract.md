# External runtime contract

Status: the factory, automatic preparation and injected transport normalization
contracts are implemented. Deployment readiness requires observations on the actual
host; local tests do not prove real order execution or strategy returns. The example
values in `tests/contract/test_runtime.py` are deliberately fake. They are not a
fee schedule, market calendar, model identity, credential, or live approval.

## Automatic deployment: the production default

The approved production YAML selects `live`, `capability_manifest: automatic`,
`capital_krw: entire_account`, and `accepted_risk_policy: configured_profile`.
The operator transfers `app.yaml`, `strategy.yaml`, `schedules.yaml`, and private
`secrets.yaml`, logs into Codex in the shared Docker auth volume, and starts Compose.
No operator-authored `runtime.json`, `runtime-manifest.json`, SHA-256, or success flags
are required. `deployment.prepare_application` resolves authority from the protected
operator configuration and records observed preparation evidence in the existing DB.
The optional packaging script reports `PREPARED`, which means files were prepared;
it does not certify a remote host, provider connection, order, or investment outcome.

Preparation acquires the local writer lock, checks Codex login and executes the
local isolation probe, reads the real account and existing orders, fetches market
sources and the DART corporation mapping,
adopts approved inherited holdings once, reconciles, and checks backup/restore.
The source YAML remains read-only to the engine. Derived configuration, approval
references and observation hashes live under `var` in `state.sqlite`. Startup uses
the normal activation path; per-order policy, freshness and broker checks remain.
Missing credentials, unresolvable existing active/reserved orders or incomplete
observations prevent readiness. A restart preserves and reconciles the existing ledger.
Historical purchase prices/times and executions are never invented for adoption.
The initial collection defers disclosure originals until the review worker runs,
so approved existing-position protection can start first. Uncollected event coverage
blocks new entries. Only currently valid reservations block adoption; past one-day reservations and expired DAY orders are historical records. Reservation queries include the next 31 days and period-order expiry. Existing reservations are not automatically canceled.

The entire account's observed economic cash is separate from its stock valuation
and from the broker's orderable-cash limit. The approved pretrade sizing schedule
is `CONSERVATIVE_ESTIMATE`: 0.5% commission each side, 0.2% sell tax and 10 bp
slippage each side. These are estimates, not a verified customer commission agreement
or actual order-level costs. Broker account cash and reported daily fees/taxes are
reconciled separately; unknown order-level facts remain unknown. An unexplained cash change is never matched to a later fee solely by equal amount; exact performance stays unavailable until the cash-flow identity is established.

`deployment_sources` fully pages both KOSPI and KOSDAQ index histories and requires
the observed historical trading dates to agree. Current/future open dates use KIS
`CTCA0903R`, paging until every requested calendar day is observed, including closed
days. Pagination stops at that coverage even if further future pages exist; the
provider recommends daily caching.
The calendar's `verified` scope is observed open dates and a conservative policy window,
not actual special hours. Prepared sessions explicitly set `hours_verified: false`
and `special_hours_verified: false`. Orders are clipped to 09:00–15:20 Seoul time
and require the live regular-market stream classification. A delayed opening cannot
supply a valid regular-market quote before it actually opens; an extended close does
not extend this engine's trading window. The futures `/market-time` API is not used
as an equity session clock. NXT/SOR, closing-auction and aftermarket orders are not added.

Prepared sessions expose `daily_bar_available_at` at the following Seoul midnight.
Daily-history collection uses that boundary, separate from the continuous-order
cutoff, so the unfinished closing-auction/special-session daily bar is never cached
as complete. The production finalization/report schedule is the next day at 00:10
Seoul time. Observed adjusted stock HLC values and index HLC values must each pass
exact ordered session coverage and numeric consistency checks before feature use.
This establishes a current provider snapshot; `point_in_time_adjustment_verified`
remains false and does not become archival historical PIT evidence.

KIS master classifications use field-specific codes. For ST shares the actual public
master's blank ETP field is accepted as absent; other unknown classifications are
excluded. ETP codes and preferred-share codes are not generic booleans. The official
`idxcode.mst` byte layout supplies domestic KOSPI/KOSDAQ sector names; product-family
and placeholder rows are outside this mapping. Missing `0000` sectors stay excluded.
DART `corpCode.xml` supplies issuer and disclosure mappings. Source refresh retains
provenance and observed hashes. Telegram allowed chat/sender IDs are private settings;
a group chat ID never implies a person's sender identity.

`/healthz` reports management HTTP liveness; `/readyz` reports actual trading-service
readiness. `/version` and `/status` remain available while preparation fails. Neither
HTTP readiness nor local/synthetic checks proves broker execution or strategy returns.
See [deployment commands](../deployment/README.md).

## Manual manifest compatibility: entry point and authority

The following file-envelope contract remains available for explicit manual deployments
and injected contract tests. It is not the default production setup procedure.

`runtime.build_external_runtime(config, trusted_approval)` returns
`(MarketBundle, broker_port, decide_callback, refresh_callback)`.
`cli.make_application` passes these to the same `Application` and `Executor` used
by offline tests. CLI, service, protection and review therefore retain one
order-writing path. Factory imports never read authentication or make requests.

Before reading private `config/secrets.yaml` values or creating external adapters, the
factory requires an unexpired, configuration-bound trusted grant for
`account_read`, `market_read`, `disclosure_read`, `model_call`, and `broker_auth`. The runtime
manifest file SHA-256 must match
`approval.operational_evidence.runtime_manifest_sha256`. Real account order
writes still require the application's activation and `live_orders` grant;
broker demonstration orders require `demo_orders`; paper submissions require the
application's `paper_simulation` grant. Shadow rejects every mutation.

KIS access tokens are issued and refreshed before read requests under `broker_auth`,
then cached privately in `state_dir/kis-token.json`. No broker request is retried
after authentication or transport failure. Model fallback is not automatic.
An approved bootstrap is adopted once into an empty ledger by `cli.make_application`;
the current complete account must match its quantities and have no unresolved orders.
Cash is distinct from stock value. Inherited holdings record adoption valuation/time,
not fictional BUY intents or historical fills. Existing ledgers are only reconciled on restart.
Live/demo service startup runs the same activation checks as the explicit CLI before workers start.
Unresolved required values in an explicitly selected manual configuration cause
`WAITING_FOR_HUMAN` when external startup is requested. They do not prevent the
authorized offline build and synthetic tests.

## Manual manifest fields

The `app.broker.capability_manifest` JSON file has exactly these top-level keys:

| Key | Required meaning |
| --- | --- |
| `schema_version` | Integer `1` |
| `source`, `verified` | Provenance and explicit verification; bound by trusted approval hash |
| `account_alias`, `environment` | Exact configured alias and KIS `real` or `demo` environment |
| `effective_at`, `expires_at` | Aware timestamps covering this use |
| `credentials` | Exactly `{"managed_token": true}`; access tokens are generated, not user-configured |
| `calendar` | Verified source and complete ordered `sessions`; actual opens/closes/ordinals |
| `ticks` | Verified source, `bands` of lower bound/tick pairs, and validity timestamps |
| `costs` | Non-synthetic, verified `CostSchedule` for the exact account alias and KRX |
| `normalization` | Verified provider field/code/price-adjustment mappings described below |
| `bootstrap` | Approved cash, ownership boundaries, historical order range and optional settlement evidence |
| `model` | Isolated executable identity and an explicitly named authentication-home entry in secrets.yaml |
| `disclosures` | Successful-cursor starting date, corporation mapping, optional source-verified extraction |
| `rate_limit` | Verified source, positive minimum request interval and maximum queue wait no greater than five seconds |

`app.market.calendar_manifest` must point to the approved `calendar` object, or to
this capability manifest containing that object. The runtime compares the
referenced object to the approval-bound calendar.

The configured KIS account-reference entry in `secrets.yaml` contains
`account-component-product-component` in the exact `8 digits-2 digits` format.
Only the component names appear in configuration; actual values must be supplied
by the trusted deployment. Existing `*_env` reference field names are retained,
but production resolves those names exclusively in `secrets.yaml`, not the process
environment. The optional factory `env` argument is an explicit contract-test
injection. Runtime errors do not print credentials.

The cache is keyed by environment and a digest of the app key/secret, reuses a
token with more than 60 seconds remaining, and serializes renewal with a file
lock. Cache and lock files require mode 0600; replacement is atomic and flushed.
Credentials are loaded once at startup; restart after rotating the private file.
Secrets are excluded from policy snapshots, config hashes, prompts and images.

## Provider normalization

In the manual compatibility path, `normalization.instruments` contains provenance, explicit `true_codes`,
`false_codes`, `common_groups`, `issuer_by_symbol`, and `sector_by_industry`.
Both configured boards are collected completely. Unrecognized status codes,
missing issuer/sector mappings, excluded security kinds, and official risk states
remain excluded. The runtime never turns an unknown code into a normal stock.

`normalization.bars` identifies the stock and index price-return adjustment
bases, source, `consistent_ohlc_verified`, and `price_returns_only`. KIS daily
prices and the matching KOSPI/KOSDAQ index history are aligned with the approved
completed-session calendar. Missing sessions and incompatible OHLC bases fail
feature construction. The daily cache is keyed by the latest usable completed history; production automatic
preparation uses the following-midnight availability boundary described above.

`normalization.quote.transport=websocket` uses KIS `H0STCNT0`: `bid=BIDP1`,
`ask=ASKP1`, `session_date=BSOP_DATE`, `observed_time=STCK_CNTG_HOUR`,
`bid_quantity=BIDP_RSQN1`, and `ask_quantity=ASKP_RSQN1`. The factory requires
these exact mappings and source provenance. The default `rest` path retains
explicit paths into the provider's `{asking, price}` record for recorded contracts.
An actual exchange observation date and time must be supported by the verified
provider contract. Receipt time is kept separately and is never copied
onto an old price. Missing date/time fields do not produce a verified quote.
When a connected trade feed expires, a REST KRX book may supply `aspr_acpt_hour`,
bid/ask and quantities. Its date must remain verified by a regular-market packet
from the same live stream session, before and after the read. Disconnection,
halt/non-regular packets, missing dates and books older than five seconds still
fail closed. Receipt time never replaces the book's observation time.

`normalization.account` explicitly maps `symbol`, `quantity`,
`sellable_quantity`, `available_cash`, and the instrument/price used for the
broker's orderable-resource query. Whole-account balances are separated from the
strategy by the approved bootstrap and the SQLite order ledger. New unexplained
holdings or orders do not acquire strategy ownership.

`normalization.orders` maps symbol, namespace session date, broker order ID,
original quantity, cumulative quantity/notional, side/codes and canceled
quantity. `day_order_fill_session_verified` means a separately verified provider
contract establishes that the submitted KRX day order can fill only in its order
session; it does not assert an exact fill timestamp. Without that verification,
a precise session is not invented from an HTTP response.

## Confirmed fills and unknown costs

The official KIS cumulative-order fields inspected during development do not
establish an exact first-fill timestamp or actual order-level cumulative fees.
Order time and notification time are not relabeled as fill time. Known
cumulative quantity and notional still flow to the execution ledger immediately
for ownership and protection. Fees remain `null`, and fill-time quality is
`FIRST_OBSERVED`; the application can block new risk and exact cost certification
while keeping known holdings protected.

Optional `bootstrap.settled_observations_path` supplies independently verified
observations keyed by `environment:account_alias:YYYY-MM-DD:KRX:broker_id`. Each
record must identify its source/hash, matching cumulative quantity/notional,
and observation timestamp. Actual cumulative fees and the exact first-fill
timestamp are independent evidence: either may be supplied without the other.
Mismatched supplements are rejected. A later observation may correct previously
unknown costs or upgrade first-fill time quality without replaying fills. The
broker's actual first-fill timestamp stays separate from the time it was observed.

### KRX regular session and the September 2026 aftermarket

The active strategy remains `regular_continuous`. KRX introduced a separate
16:00–20:00 aftermarket on September 14, 2026; it is not an extension of the
regular continuous session in this runtime's calendar. Do not move the regular
session close to 20:00. The current KIS order path uses regular-session types
`00`/`01`; aftermarket types `41`–`47` and an aftermarket exit policy are not
implemented. In particular, the published aftermarket types do not include
the market sell used by this strategy.

The KIS notice also adds `MARKET_CLS_CODE` to real-time trade/quote messages:
`1` premarket, `2` regular, `3` aftermarket, `5` closing. The adapter implements
the portal's 47-field contract, with `MARKET_CLS_CODE` last, and requires `2`,
`HOUR_CLS_CODE=0`, `TRHT_YN=N`, and the verified regular calendar interval.
The upstream GitHub sample still lists 46 fields; that older layout is rejected.
Provider date/time and the original socket receipt must both be no older than
five seconds. Invalid, out-of-order or disconnected data cannot become a fresh quote.

One app key opens one background quote session with at most 41 subscriptions.
Changed subscriptions are removed and acknowledged before additions; common
symbols retain their subscriptions. Disconnects clear all quotes and ACKs before
bounded reconnect attempts. Protection reads the cache without waiting for a
socket. Full collection waits at most five seconds for initial ticks. Managed
positions and working orders have priority: if adding entry candidates exceeds
41, all entry candidates are excluded with diagnostics. More than 41 protected
symbols blocks quote availability rather than silently omitting a holding.
Do not share the app key with another simultaneously connected quote client.
Injected contract tests verify these paths; actual provider receipt and sustained
protection latency still require external operational verification.

The balance query's `AFHR_FLPR_YN=N` now explicitly means KRX regular-session
closing prices. It is retained for regular-session valuation; it does not
represent a live aftermarket mark.

This quote/entry window does not filter out already executed broker fills.
`day_order_fill_session_verified` establishes the execution's exchange business
day. A source-verified exact fill time may follow the ordinary close (for example,
an extended closing auction); retain that observation for reconciliation while
still rejecting future times or a conflicting verified business day.

Source: [KIS announcement, September 9, 2026](https://apiportal.koreainvestment.com/community/10000000-0000-0011-0000-000000000001/post/26dfe350-eb72-48e5-8175-34eb27970f3e).
Field layout: [KIS H0STCNT0 portal contract](https://apiportal.koreainvestment.com/api/apis/guide/property/714d1437-8f62-43db-a73c-cf509d3f6aa7).
Limits: [KIS current subscription limits](https://apiportal.koreainvestment.com/community/10000000-0000-0011-0000-000000000001/post/d0d1a83f-6f8d-4437-9700-6d26702fd989).

Terminal order status and previously confirmed fees do not end reconciliation of
that order. When a new snapshot contains the same broker namespace/order ID,
Executor applies its later cumulative revision and explicit correction through
the same fill ledger. Duplicate/older revisions remain idempotent. Historical
terminal orders absent from the snapshot are not converted into new UNKNOWN
orders; missing working orders still require reconciliation. Regression checks
cover filled and partially canceled orders, later price/fee corrections, late
cumulative fills, zero remaining reservations, and bounded-history absence.

Order identity, intent and organization metadata are read from the authoritative
SQLite `intents` table. `submit` returns the broker organization under
`metadata.organization`; Executor persists it atomically with acknowledgement.
`cancel` uses that persisted metadata. A lost acknowledgement cannot be recovered
by inventing a broker client ID or automatically resubmitting the order.

Runtime cursors, canonical event availability, normalized evidence, observation
revision hashes, quota circuits and paper-broker simulation state are stored in
`state.sqlite` under `runtime_cache`. HTTP/model calls never run inside those
short cache transactions. No JSON side ledger is used for restoration of order
identity or ownership.

## Official events and model input

The cursor advances only after complete list/document collection. Long resumed
ranges are split into DART-supported date ranges. Canonical event/fact/document
records remain in SQLite when the daily list cursor advances; recent five-session
events and evidence for held instruments are selected for the current bundle.
The first available timestamp is immutable. A verified publication date without
an exact timestamp is `DATE_ONLY`: age starts at the earliest session on or after
that Seoul date, while actual collection time still bounds evidence availability.
Late collection never rejuvenates old events. Missing or contradictory publication
dates remain `UNCERTAIN`; date-only records are not precise intraday PIT evidence.

`disclosure_parser.parse_official_event` is called for newly fetched official
originals. Supported, explicitly labeled tables can produce earnings, guidance
or material-contract event facts automatically. Unrecognized templates,
unresolved corrections, absent units or comparison periods remain `PARTIAL` /
`WATCH` for relevant recent events. Observation-only families and expired events
do not taint a separate current event. Undisclosed contract payment terms remain
explicitly unknown for model review; amount, counterparty and period are required.
Corrections link only to a unique same-issuer filing matching the official cover
table's document title and filing date. Parser-version changes reparse cached
originals without downloading them again or changing their collection timestamp.
Parser tests cover synthetic labeled originals and the merged-cell
contract layout observed in receipt `20260911800002`. A read-only probe on
2026-09-14 verified that original's amounts, dates, payment terms and qualifiers;
other production layouts remain unverified. See [DART evidence](dart-read-verification.json).
An optional
`verified_events_path` must also match its approval-bound
`verified_events_sha256`, and its extraction must match the fetched original's
hash and instrument. It is not required for the automatic parser path.

The runtime passes the application-created frozen input through CodexAdapter.
The model sees a compact original-document index. `get_fact` pages long originals
with character offsets; `search_official_evidence` searches these frozen documents,
not the Internet. Raw document identifiers do not replace validated output fact IDs.
The limited tool scope includes candidates, reviewed positions, current theses
and portfolio holdings/pending entries. Raw documents keep their instrument and
source provenance; external instructions inside them remain untrusted text.
Actual model credentials, account components and broker mutation tools are not
included in that input or the child environment. Isolation evidence is bound to
the executable SHA-256. Quota state is durable in SQLite.

Application binds its existing Store to the broker before any model decision.
The runtime refuses an unbound model call instead of opening a second writer.
Each call owns a new directory under `model-attempts`; after the adapter returns,
all of its attempt result/status/usage records are written as `MODEL_ATTEMPT`
journal events and the returned ModelResult as `MODEL_OUTCOME` in one short
transaction. `/usage` reads those same events and separately queries subscription rate limits through the authenticated Codex app-server RPC. It never resets the quota circuit or changes trading authority. Missing provider usage stays
`null`, including quota-circuit calls with zero attempts. Outcome usage is labeled
`last_attempt`; it is not an additional billable attempt or a summed cost.

Semantic validation samples the clock when the response is actually validated.
It does not relabel model start time as completion time. A 150-second model
attempt is not stale merely because it ran longer than 120 seconds; the decision
age limit is measured from completion and the application separately rechecks
current account/facts/quotes before dispatch. Long model work runs outside the
journal transaction and does not replace independent protection.

## Refresh and paper execution

Daily histories are reused within their completed-session key. Disclosures use a
180-second cursor/cache interval. Live quote requests cover managed/working
positions and candidates passing the deterministic event/technical filters,
without an arbitrary top-N claim. A shared priority request budget puts trading
and quote requests before bulk history work. Quote age above the approved five
seconds remains observable and cannot authorize new risk.

The paper port is an independent simulated broker. It fills only on a later,
fresh quote observation, within verified displayed depth, the fixed buy limit,
approved cost/slippage assumptions and remaining cash/quantity. Missing depth
means no simulated fill. This model is labeled
`PAPER_QUOTE_CONSTRAINED_SIMULATION`; it is not proof of achievable execution or
strategy profitability. It never calls KIS submit/cancel. Shadow uses read-only
account observations and rejects mutation at the port as well as the application.

## Verified development scope

The contract tests use injected transports and an injected model runner. They
check full-universe feature collection, same-session history reuse, no-event
coverage, source/hash authority, actual quote timestamps, unknown-fee partial
fills, parser/cursor continuity, and separation of credentials from model input.
There were no real account, token, DART, model or order calls in those tests.
Actual account adoption, fees, cold-start coverage, quote latency, sustained rate limits,
model isolation and protection performance remain deployment observations, not simulated
successes. Automatic startup records only checks it performs; a packaging run or this
document does not certify a remote installation. Public master-file inspection on
2026-09-21 checked the sector wire layout and ST blank-ETP representation without
authentication or account calls.

## Final dispatch boundary

Order/cancel POSTs only attempt a nonblocking read of an already valid token
cache. A missing, expiring or busy cache produces `NOT_SENT`; it cannot initiate
authentication or wait for another renewal after preflight. The next approved
read/monitoring request handles renewal. There is no automatic order retry.

For new submissions, KisBrokerPort supplies the earliest of the decision expiry,
quote timestamp + 5 seconds and session close. The adapter rechecks this deadline
and authorization after the priority queue, immediately before transport. A local
`NOT_SENT` submission becomes `INVALIDATED`, releasing its reservation and
recording `ORDER_NOT_SENT`. An unsent cancellation restores the existing order
state/reservation and records `CANCEL_NOT_SENT`. Actual transport uncertainty
continues to require reconciliation.
