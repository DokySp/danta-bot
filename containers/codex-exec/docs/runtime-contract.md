# External runtime contract

Status: `IMPLEMENTED` for the factory and injected transport normalization tests;
`EXTERNAL_INTEGRATION_UNVERIFIED` for actual KIS/DART/Codex deployment. The example
values in `tests/contract/test_runtime.py` are deliberately fake. They are not a
fee schedule, market calendar, model identity, credential, or live approval.

## Entry point and authority

`runtime.build_external_runtime(config, trusted_approval)` returns
`(MarketBundle, broker_port, decide_callback, refresh_callback)`.
`cli.make_application` passes these to the same `Application` and `Executor` used
by offline tests. CLI, service, protection and review therefore retain one
order-writing path. Factory imports never read authentication or make requests.

Before reading named environment variables or creating external adapters, the
factory requires an unexpired, configuration-bound trusted grant for
`account_read`, `market_read`, `disclosure_read`, and `model_call`. The runtime
manifest file SHA-256 must match
`approval.operational_evidence.runtime_manifest_sha256`. Real account order
writes still require the application's activation and `live_orders` grant;
broker demonstration orders require `demo_orders`; paper submissions require the
application's `paper_simulation` grant. Shadow rejects every mutation.

No automatic token creation, token refresh, model fallback or account adoption is
performed. Required values that remain null in the shipped configuration cause
`WAITING_FOR_HUMAN` when external startup is requested. They do not prevent the
authorized offline build and synthetic tests.

## Manifest fields

The `app.broker.capability_manifest` JSON file has exactly these top-level keys:

| Key | Required meaning |
| --- | --- |
| `schema_version` | Integer `1` |
| `source`, `verified` | Provenance and explicit verification; bound by trusted approval hash |
| `account_alias`, `environment` | Exact configured alias and KIS `real` or `demo` environment |
| `effective_at`, `expires_at` | Aware timestamps covering this use |
| `credentials` | `token_env`: name of the explicitly supplied KIS access-token environment variable |
| `calendar` | Verified source and complete ordered `sessions`; actual opens/closes/ordinals |
| `ticks` | Verified source, `bands` of lower bound/tick pairs, and validity timestamps |
| `costs` | Non-synthetic, verified `CostSchedule` for the exact account alias and KRX |
| `normalization` | Verified provider field/code/price-adjustment mappings described below |
| `bootstrap` | Approved cash, ownership boundaries, historical order range and optional settlement evidence |
| `model` | Isolated executable identity and an explicitly named authentication-home environment variable |
| `disclosures` | Successful-cursor starting date, corporation mapping, optional source-verified extraction |
| `rate_limit` | Verified source, positive minimum request interval and maximum queue wait no greater than five seconds |

`app.market.calendar_manifest` must point to the approved `calendar` object, or to
this capability manifest containing that object. The runtime compares the
referenced object to the approval-bound calendar.

The configured KIS account-reference environment variable contains
`account-component-product-component` in the exact `8 digits-2 digits` format.
Only the component names appear in configuration; actual values must be supplied
by the trusted deployment. Runtime errors do not print credentials.

## Provider normalization

`normalization.instruments` contains the provenance, explicit `true_codes`,
`false_codes`, `common_groups`, `issuer_by_symbol`, and `sector_by_industry`.
Both configured boards are collected completely. Unrecognized status codes,
missing issuer/sector mappings, excluded security kinds, and official risk states
remain excluded. The runtime never turns an unknown code into a normal stock.

`normalization.bars` identifies the stock and index price-return adjustment
bases, source, `consistent_ohlc_verified`, and `price_returns_only`. KIS daily
prices and the matching KOSPI/KOSDAQ index history are aligned with the approved
completed-session calendar. Missing sessions and incompatible OHLC bases fail
feature construction. The daily cache is session-based; refreshes in that same
session reuse the completed history.

`normalization.quote` names paths into the provider's `{asking, price}` record:
`bid`, `ask`, `session_date`, and `observed_time`, plus provenance. Optional
`bid_quantity` and `ask_quantity` paths support conservative paper fills.
An actual exchange observation date and time must be supported by the verified
provider contract. HTTP retrieval time is kept separately and is never copied
onto an old price. Missing date/time fields do not produce a verified quote.

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
actual cumulative fees, first-fill timestamp and observation timestamp. Mismatched
supplements are rejected. An exact later observation may correct previously
unknown costs without replaying fills.

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
The first available timestamp is immutable. Older date-only material first
collected after an outage is marked `UNCERTAIN`, preventing a fresh intraday
entry merely because the system rediscovered it today.

`disclosure_parser.parse_official_event` is called for newly fetched official
originals. Supported, explicitly labeled tables can produce earnings, guidance
or material-contract event facts automatically. Unrecognized templates,
unresolved corrections, absent units or comparison periods remain `PARTIAL` /
`WATCH`. Parser tests cover synthetic labeled originals; compatibility with
actual production DART templates has not been certified. An optional
`verified_events_path` must also match its approval-bound
`verified_events_sha256`, and its extraction must match the fetched original's
hash and instrument. It is not required for the automatic parser path.

The runtime passes the application-created frozen input through CodexAdapter.
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
transaction. `/usage` reads those same events. Missing provider usage stays
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
Actual account adoption, fees, source-field contracts, cold-start coverage,
quote latency, sustained rate limits, model isolation and protection performance
remain deployment evidence requirements, not simulated successes.
