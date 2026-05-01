> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Migrating to CLOB V2

> A complete guide to upgrading your integration to Polymarket's CLOB V2 — new contracts, new backend, new collateral token, and a simpler builder program.

Polymarket is shipping a coordinated upgrade of its entire trading infrastructure: **new Exchange contracts**, a **rewritten CLOB backend**, and a **new collateral token** (Polymarket USD, or pUSD). This guide walks you through everything you need to migrate.

<Warning>
  **Go-live: April 28, 2026 (\~11:00 UTC)** — approximately 1 hour of downtime. All open orders will be wiped during the cutover. Make sure your integration is on the V2 SDK before the maintenance window starts.
</Warning>

<Note>
  **Test against V2 before go-live.** Point your client at `https://clob-v2.polymarket.com` to start integrating now. On April 28th (\~11:00 UTC), V2 takes over the production URL — `https://clob.polymarket.com` — so you don't need to change the base URL again after the cutover.
</Note>

***

## TL;DR

| What                                           | Before (V1)                                                                                                | After (V2)                                                   |
| ---------------------------------------------- | ---------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------ |
| SDK package                                    | `@polymarket/clob-client` / `py-clob-client`                                                               | `@polymarket/clob-client-v2` / `py-clob-client-v2`           |
| Constructor                                    | Positional args                                                                                            | Options object, `chainId` → `chain`                          |
| Order fields                                   | `nonce`, `feeRateBps`, `taker`                                                                             | `timestamp` (ms), `metadata`, `builder`                      |
| Fees                                           | Embedded in the signed order                                                                               | Operator-set at match time                                   |
| Collateral                                     | USDC.e                                                                                                     | **pUSD** (standard ERC-20, backed by USDC)                   |
| Builder auth                                   | `POLY_BUILDER_*` HMAC headers + [`builder-signing-sdk`](https://github.com/Polymarket/builder-signing-sdk) | A single `builderCode` field on the order                    |
| EIP-712 domain version                         | `"1"`                                                                                                      | `"2"` (exchange only — API auth unchanged)                   |
| Exchange `verifyingContract` (raw API signers) | V1 addresses                                                                                               | V2 addresses — see [Contracts](/resources/contracts)         |
| Raw API order signing                          | V1 Order type                                                                                              | Updated Order type — [see API users section](#for-api-users) |

**If you're on the latest SDK,** most of this is handled automatically. The hot-swap mechanism detects the cutover and refreshes the client without manual intervention.

***

## Before you start

<Steps>
  <Step title="Read this guide end-to-end">
    Even if a section doesn't look relevant, skim it. V2 touches more surface area than any previous release.
  </Step>

  <Step title="Pin the latest SDK">
    Install the V2 SDK: [`@polymarket/clob-client-v2`](https://www.npmjs.com/package/@polymarket/clob-client-v2) (TypeScript) or [`py-clob-client-v2`](https://pypi.org/project/py-clob-client-v2/) (Python). Don't keep using the old `clob-client` / `py-clob-client` packages — those only work against V1 and stop functioning after cutover.
  </Step>

  <Step title="Have a test wallet ready">
    The order book is wiped during the maintenance window. Test your integration before the cutover.
  </Step>
</Steps>

<Warning>
  **Order book wipe:** All open orders are cancelled as part of the cutover. Plan to re-place orders immediately after the window closes.
</Warning>

***

## What's changing

### 1. New Exchange contracts

The onchain exchange has been rewritten from the ground up.

* Solidity upgraded from **0.8.15 → 0.8.30**, with Solady replacing OpenZeppelin for gas savings.
* Order struct simplified: `nonce`, `feeRateBps`, and `taker` removed; `timestamp`, `metadata`, `builder` added.
* EIP-712 exchange domain version bumped from `"1"` to `"2"`.
* Fees collected onchain at match time (no longer embedded in the signed order).
* Onchain cancel replaced with operator-controlled `pauseUser` / `unpauseUser`.
* Batched mint/merge operations for gas efficiency.

See [Contracts](/resources/contracts) for V2 addresses.

### 2. Rewritten CLOB backend

The order manager, ledger, executor, balance checker, and tracker are all new services. From an integrator's perspective:

* **Nonce system removed.** Order uniqueness now comes from `timestamp` (milliseconds). You no longer track nonces.
* **New fee model.** Platform fees are dynamic per market and queryable via `getClobMarketInfo()`.
* **Builder codes** enable integrator attribution and revenue sharing, replacing the old HMAC-header flow.

### 3. New collateral token

Polymarket is migrating from **USDC.e** to **pUSD** (Polymarket USD), a standard ERC-20 on Polygon backed by USDC. Backing is enforced onchain by the smart contract.

* For users trading on polymarket.com, the frontend handles wrapping automatically with a one-time approval.
* Power users and API-only traders wrap their USDC.e into pUSD via the Collateral Onramp contract's `wrap()` function.

***

## For API users

If you sign and post orders directly (without the SDK), here's what changes in the wire protocol. SDK users can skip this section — the client handles it.

### EIP-712 domain

The Exchange domain version bumps to `"2"` and the `verifyingContract` moves to the V2 Exchange.

```ts theme={null}
{
  name: "Polymarket CTF Exchange",
  version: "1", // [!code --]
  version: "2", // [!code ++]
  chainId: 137,
  verifyingContract: "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E", // [!code --]
  verifyingContract: "0xE111180000d2663C0091e4f400237545B87B996B", // [!code ++]
}
```

For Neg Risk markets, the `verifyingContract` is different:

```ts theme={null}
{
  verifyingContract: "0xC5d563A36AE78145C45a50134d48A1215220f80a", // [!code --]
  verifyingContract: "0xe2222d279d744050d28e00520010520000310F59", // [!code ++]
}
```

See [Contracts](/resources/contracts) for the canonical V2 addresses.

<Note>
  **Only the Exchange domain changes.** The `ClobAuthDomain` used for L1 API authentication stays at version `"1"` — L1/L2 auth is identical in V2.
</Note>

### EIP-712 Order type

The signed struct drops `taker`, `expiration`, `nonce`, and `feeRateBps`, and adds `timestamp`, `metadata`, and `builder`.

```ts theme={null}
Order(
  uint256 salt,
  address maker,
  address signer,
  address taker, // [!code --]
  uint256 tokenId,
  uint256 makerAmount,
  uint256 takerAmount,
  uint256 expiration, // [!code --]
  uint256 nonce, // [!code --]
  uint256 feeRateBps, // [!code --]
  uint8 side,
  uint8 signatureType // [!code --]
  uint8 signatureType, // [!code ++]
  uint256 timestamp, // [!code ++]
  bytes32 metadata, // [!code ++]
  bytes32 builder // [!code ++]
)
```

### Order value to sign

```ts theme={null}
{
  salt: "12345",
  maker: "0x...",
  signer: "0x...",
  taker: "0x0000000000000000000000000000000000000000", // [!code --]
  tokenId: "102936...",
  makerAmount: "1000000",
  takerAmount: "2000000",
  expiration: "0", // [!code --]
  nonce: "0", // [!code --]
  feeRateBps: "0", // [!code --]
  side: 0,
  signatureType: 1 // [!code --]
  signatureType: 1, // [!code ++]
  timestamp: "1713398400000", // [!code ++]
  metadata: "0x0000000000000000000000000000000000000000000000000000000000000000", // [!code ++]
  builder: "0x0000000000000000000000000000000000000000000000000000000000000000" // [!code ++]
}
```

* **`timestamp`** — order creation time in milliseconds. Replaces `nonce` for per-address uniqueness (not an expiration).
* **`metadata`** — bytes32.
* **`builder`** — bytes32. Zero unless you're attaching a builder code.

`side` is encoded as `uint8` in the signing payload (`0` = BUY, `1` = SELL), even though the wire body uses the string `"BUY"` / `"SELL"`. No change from V1.

### POST /order body

```ts theme={null}
{
  "order": {
    "salt": "12345",
    "maker": "0x...",
    "signer": "0x...",
    "taker": "0x0000000000000000000000000000000000000000", // [!code --]
    "tokenId": "102936...",
    "makerAmount": "1000000",
    "takerAmount": "2000000",
    "expiration": "0", 
    "nonce": "0", // [!code --]
    "feeRateBps": "0", // [!code --]
    "side": "BUY",
    "signatureType": 1,
    "timestamp": "1713398400000", // [!code ++]
    "metadata": "0x0000000000000000000000000000000000000000000000000000000000000000", // [!code ++]
    "builder": "0x0000000000000000000000000000000000000000000000000000000000000000", // [!code ++]
    "signature": "0x..."
  },
  "owner": "<api-key>",
  "orderType": "GTC"
}
```

### Request headers

The API auth headers are unchanged. Builder attribution moves into the signed `builder` field on the order, so the `POLY_BUILDER_*` HMAC headers are gone.

```yaml theme={null}
POLY_ADDRESS: 0x...
POLY_SIGNATURE: 0x...
POLY_TIMESTAMP: 1713398400
POLY_API_KEY: ...
POLY_PASSPHRASE: ...
POLY_BUILDER_API_KEY: ...        # [!code --]
POLY_BUILDER_SECRET: ...         # [!code --]
POLY_BUILDER_PASSPHRASE: ...     # [!code --]
POLY_BUILDER_SIGNATURE: 0x...    # [!code --]
```

***

## SDK Migration

### Install

CLOB V2 ships under new package names. Install them directly — don't keep using the old `clob-client` / `py-clob-client` packages.

<CodeGroup>
  ```bash TypeScript theme={null}
  npm install @polymarket/clob-client-v2@1.0.0
  ```

  ```bash Python theme={null}
  pip install py-clob-client-v2==1.0.0
  ```
</CodeGroup>

<Warning>
  The legacy `@polymarket/clob-client` and `py-clob-client` packages only work against V1 and will stop functioning after the April 28 cutover.
</Warning>

<Note>
  **What's next.** We're planning a unified SDK that folds Gamma, Data, and CLOB into a single package. Future releases will converge there — for now, `clob-client-v2` / `py-clob-client-v2` are the V2 CLOB clients.
</Note>

### Constructor: positional args → options object

The single most visible change. `chainId` is renamed to `chain`. `tickSizeTtlMs` is no longer configurable.

<CodeGroup>
  ```typescript Before (V1) theme={null}
  const client = new ClobClient(
    host,
    chainId,
    signer,
    creds,
    signatureType,
    funderAddress,
    useServerTime,
    builderConfig,
    getSigner,
    retryOnError,
    tickSizeTtlMs,   // ← removed in V2
    throwOnError,
  );
  ```

  ```typescript After (V2) theme={null}
  const client = new ClobClient({
    host,
    chain: chainId,  // ← renamed from chainId
    signer,
    creds,
    signatureType,
    funderAddress,
    useServerTime,
    builderConfig,   // shape changed — see Builder Program below
    getSigner,
    retryOnError,
    throwOnError,
  });
  ```
</CodeGroup>

<Tip>
  The only mental shift: wrap args in `{}` and rename `chainId` → `chain`. Everything else is the same.
</Tip>

### Order creation

Three fields are no longer user-settable: `feeRateBps`, `nonce`, `taker`. One new optional field: `builderCode`.

<CodeGroup>
  ```typescript Before (V1) theme={null}
  const order: UserOrder = {
    tokenID: "0x123...",
    price: 0.55,
    size: 100,
    side: Side.BUY,
    feeRateBps: 100,        // ← removed
    nonce: 12345,           // ← removed
    taker: "0xabc...",      // ← removed
    expiration: 1714000000,
  };
  ```

  ```typescript After (V2) theme={null}
  const order: UserOrderV2 = {
    tokenID: "0x123...",
    price: 0.55,
    size: 100,
    side: Side.BUY,
    expiration: 1714000000,
    builderCode: "0x...",   // optional — your builder code
  };
  ```
</CodeGroup>

**Market orders** follow the same pattern and add an optional `userUSDCBalance` field so the SDK can calculate fee-adjusted fill amounts:

```typescript theme={null}
const marketOrder: UserMarketOrderV2 = {
  tokenID: "0x123...",
  amount: 500,
  side: Side.BUY,
  orderType: OrderType.FOK,
  userUSDCBalance: 1000,   // optional — enables fee-aware calculations
  builderCode: "0x...",    // optional
};
```

## Fee model

Fees are now **determined by the protocol at match time**, not embedded in your signed order.

* **Platform fees** are dynamic per market: `fee = C × feeRate × p × (1 - p)`
* **Makers are never charged fees** — only takers pay.
* You no longer set `feeRateBps` on orders. The SDK handles fee calculation automatically.

### Querying fee parameters

V2 introduces `getClobMarketInfo()`, which returns all CLOB-level market parameters in one call:

<CodeGroup>
  ```typescript TypeScript theme={null}
  const info = await client.getClobMarketInfo(conditionID);
  // info.mts  — minimum tick size
  // info.mos  — minimum order size
  // info.fd   — fee details { r: rate, e: exponent, to: takerOnly }
  // info.t    — tokens [{ t: tokenID, o: outcome }, ...]
  // info.rfqe — is RFQ enabled
  ```

  ```python Python theme={null}
  info = client.get_clob_market_info(condition_id)
  # info["mts"]  — minimum tick size
  # info["mos"]  — minimum order size
  # info["fd"]   — fee details { "r", "e", "to" }
  # info["t"]    — tokens list
  ```
</CodeGroup>

If you were calculating fees manually in your integration, you can now rely on the SDK. Pass `userUSDCBalance` on market buy orders to get accurate fill amounts after fees.

***

## Builder Program

V2 replaces the old builder authentication flow (HMAC headers + separate signing SDK) with a native **builder code** attached directly to each order.

### What's gone

* `@polymarket/builder-signing-sdk` — **no longer needed**
* `POLY_BUILDER_API_KEY`, `POLY_BUILDER_SECRET`, `POLY_BUILDER_PASSPHRASE`, `POLY_BUILDER_SIGNATURE` headers
* Remote vs. local signing distinction

### What's new

* A single `builderCode` (bytes32) from your [Builder Profile](https://polymarket.com/settings?tab=builder)
* Attach it per-order via the `builderCode` field, **or** pass it once at construction so every order inherits it

<Note>
  `BuilderConfig` still exists, but its shape changed. In V1 it wrapped HMAC credentials from `@polymarket/builder-signing-sdk`. In V2 it's just `{ builderCode: string }`.
</Note>

<Note>
  **Your builder API key isn't retired.** The HMAC-based builder API key is still used to authenticate with the [Relayer](/trading/gasless) for gasless transactions. Only the order-signing flow moves to the `builderCode` field — your relayer integration keeps the same credentials.
</Note>

<CodeGroup>
  ```typescript Before (V1) theme={null}
  import { BuilderConfig, BuilderApiKeyCreds } from "@polymarket/builder-signing-sdk";

  const builderConfig = new BuilderConfig({
    localBuilderCreds: new BuilderApiKeyCreds({
      key: process.env.POLY_BUILDER_API_KEY,
      secret: process.env.POLY_BUILDER_SECRET,
      passphrase: process.env.POLY_BUILDER_PASSPHRASE,
    }),
  });

  const client = new ClobClient(
    host,
    chainId,
    signer,
    creds,
    signatureType,
    funderAddress,
    undefined,
    false,
    builderConfig,
  );
  ```

  ```typescript After (V2) — per-order builder code theme={null}
  const client = new ClobClient({
    host,
    chain: chainId,
    signer,
    creds,
    signatureType,
    funderAddress,
  });

  await client.createAndPostOrder(
    {
      tokenID: "0x...",
      price: 0.55,
      size: 100,
      side: Side.BUY,
      builderCode: process.env.POLY_BUILDER_CODE,
    },
    { tickSize: "0.01", negRisk: false },
  );
  ```

  ```typescript After (V2) — pass once at construction theme={null}
  const client = new ClobClient({
    host,
    chain: chainId,
    signer,
    creds,
    signatureType,
    funderAddress,
    builderConfig: { builderCode: process.env.POLY_BUILDER_CODE },
  });

  // Every order posted by this client now carries your builder code.
  await client.createAndPostOrder(
    {
      tokenID: "0x...",
      price: 0.55,
      size: 100,
      side: Side.BUY,
    },
    { tickSize: "0.01", negRisk: false },
  );
  ```
</CodeGroup>

***

## Collateral token: USDC.e → pUSD

Polymarket USD (pUSD) replaces USDC.e as the collateral token. pUSD is a standard ERC-20 on Polygon backed by USDC, with backing enforced onchain by the smart contract. The permissionless Collateral Onramp accepts USDC.e.

* **For users on polymarket.com:** the UI handles wrapping automatically.
* **For API-only traders:** wrap USDC.e into pUSD via the Collateral Onramp's `wrap()` function. See the [pUSD page](/concepts/pusd) for full examples and [Contracts](/resources/contracts) for addresses.

***

## Test markets

The markets below have liquidity on `clob-v2.polymarket.com` — use them to dry-run your integration end-to-end before go-live. To resolve a condition ID into token IDs and metadata, query [`gamma-api.polymarket.com/markets?condition_ids=<id>`](https://gamma-api.polymarket.com/markets).

### Markets with fees enabled

Use these to verify fee handling end-to-end.

* `0xaf5e903876ad42de97e1cf02c2ef8484df69bcfc5541b96a400116557d1e504e`
* `0xe9955a31d76ea97457410f61c9b9f2d27ac6fbd8302ed1849d93133884f4fb3b`
* `0x8e6a263fc5f4dd9433b4ccbc9bb9e0d89f4de8a91d3a82d6b437fe73fd847ea5`
* `0xc7d462e462d8d369aee17c9e3ea8f113166cf9a342a6434a0b2a0f7588dc1bbf`
* `0x0440d08e4f534afa8ad9f616a239a19ebf7f2476bd802b1d54180435fa83463f`
* `0xd048b3e9ccad82c57a4eec953e75794aeb5e6d0e08bdb79b9e01b24e0049f16d`
* `0x88310713845c54e36f29791a1c2dc172b9819b645ba82d5f87560805cd2bb788`
* `0xffcab88076a28795281bb07a082c7003a4ff5671420086cb16ec09dfbf9aea68`
* `0x64a5bcfb0c76a75081c8be49c895a976d506fce8d21aa9d73f1099e502f2fa4e`
* `0x8babe4f4d0eff732d660fa02929a581cb8478ed2a1696c158290b2794d3d7ac0`

### Additional markets with liquidity

* `0xcf8a237df51a51511c4f96bb4390480ad72b898ca8ec7e9a3c16c47c8e5e468a`
* `0x242d251e7e804f79e1f237896b0f5e73caea72375dcb84e5a60d1cd0d2f80ef5`
* `0x9c47ba9e666983bd8d82bfab790509153bf7756c43913f6ef269e33c8955939c`
* `0x221aaa62fed17db56fbc7983f88110a9c34861c3262154ee3315425378e3ae12`
* `0x625d0091f4c647e5497bd9b03f8526bd486d6c339380b8046e4dd5b3373046b7`
* `0xc9b9f89ac915385c8d77edb73872c49e5bf76e510b74b9609e74e7f8d0339df2`
* `0xde9f827cb2d568db7801439693645a941a38fe6feaeb08b86087ad367d991704`
* `0xfd029ab3d6d27b6e1f3480dce858c97fb12e5bebd6fb50be7520102c56ba8ce1`
* `0x894a61a2baa777adf1d03a263a4b2d8faa7e1ebc7bf6694a37701fae8add01d9`
* `0xcd57f3ad3bbbdaa96aacabd35f50c2d6e30f777e4a33876a4ab2dcd9f0b8c170`
* `0xc4b07998e8f9bf6b95f079d6dc0529f3c6f59698d4e168817ad5f99304de6c57`
* `0xe4f4b614a6c2b4ecd8eb700d19c0e6533d3fbd1bc28193b2255394ef74006e6f`
* `0xec181db4470b152493b58229862af3f6335b77cc719f5a0e7ed58c9f9848b992`
* `0x6ec4fec4885df7f3ac46e5d0051beb6d8ac75de6a8481f13f245ff26dcb4b662`
* `0x8f6e71601903224dc29c69b886a63f248c8051be259e7db299850708f1f86dd6`
* `0xfa40b5612a905f16ee42a18979f23fa1bbfcfc365f11d168f2e22bd0159ada77`
* `0x182390641d3b1b47cc64274b9da290efd04221c586651ba190880713da6347d9`

***

## Migration checklist

Work through this list as you update your integration:

<AccordionGroup>
  <Accordion title="SDK and constructor">
    * [ ] Install `@polymarket/clob-client-v2` / `py-clob-client-v2` (remove the legacy `clob-client` / `py-clob-client` packages)
    * [ ] Convert constructor calls from positional args to options object
    * [ ] Rename `chainId` parameter to `chain`
    * [ ] Remove `tickSizeTtlMs` and `geoBlockToken` from constructor config
  </Accordion>

  <Accordion title="Order creation">
    * [ ] Remove `feeRateBps`, `nonce`, `taker` from order creation calls
    * [ ] Add `builderCode` to orders if you're a builder
    * [ ] Pass `userUSDCBalance` on market buy orders for fee-adjusted fill amounts (optional)
    * [ ] Remove any manual fee calculation logic — fees are protocol-handled
  </Accordion>

  <Accordion title="Builder program">
    * [ ] Remove `@polymarket/builder-signing-sdk` from dependencies
    * [ ] Remove `POLY_BUILDER_*` environment variables
    * [ ] Copy your builder code from [your Builder Profile](https://polymarket.com/settings?tab=builder)
    * [ ] Store it as `POLY_BUILDER_CODE` (or any env var you prefer)
    * [ ] Attach `builderCode` on every order to get attribution
  </Accordion>

  <Accordion title="Raw order struct / manual signing">
    * [ ] Update any code that inspects raw order structs (new fields: `timestamp`, `metadata`, `builder`)
    * [ ] Update manual EIP-712 signing code (Exchange domain version `"1"` → `"2"`)
    * [ ] Update onchain contract references to new V2 addresses
  </Accordion>

  <Accordion title="Collateral">
    * [ ] If you're API-only, plan for wrapping USDC.e into pUSD via the Collateral Onramp
    * [ ] Update any hardcoded references to the old USDC.e address
  </Accordion>

  <Accordion title="Testing">
    * [ ] Test full order lifecycle on preprod
    * [ ] Verify builder attribution appears on the [Builder Leaderboard](https://builders.polymarket.com)
    * [ ] Plan for order book wipe — all open orders must be re-placed after migration
  </Accordion>
</AccordionGroup>

***

## Cutover day

**Go-live: April 28, 2026 (\~11:00 UTC).** The migration involves approximately **1 hour of downtime** during which trading is paused.

**During the window:**

* All open orders are wiped. You must re-place orders after the migration completes.
* The SDK's **hot-swap mechanism** queries a version endpoint and auto-refreshes when V2 goes live.
* If you're on the latest V2 SDK, **no manual intervention is needed during the cutover.**

**If your integration is on the V1 SDK after migration, it will stop working.** There is no backward compatibility.

Follow [Discord](https://discord.gg/polymarket), Telegram, and [status.polymarket.com](https://status.polymarket.com) for the exact maintenance window start time. The window is scheduled to avoid large market resolutions.

***

## FAQ

<AccordionGroup>
  <Accordion title="Do I need to re-generate my API keys?">
    No. L1/L2 authentication is identical in V2. Your existing API key, secret, and passphrase continue to work.
  </Accordion>

  <Accordion title="Will my open orders migrate automatically?">
    No — all open orders are wiped during the maintenance window. Re-place them after the window closes.
  </Accordion>

  <Accordion title="What happens if I forget to update the SDK?">
    Your V1 client will fail against the V2 backend after cutover. There is no backward compatibility — upgrade before the maintenance window ends.
  </Accordion>

  <Accordion title="Do I need to migrate my USDC.e to pUSD manually?">
    If you're trading through polymarket.com, no — the UI handles wrapping automatically with a one-time approval. If you're API-only, you'll need to call `wrap()` on the Collateral Onramp contract.
  </Accordion>

  <Accordion title="Is the builder code a secret?">
    No. Builder codes are **public identifiers** — they appear onchain in the `builder` field of every attributed order. Only you control which orders include your code, so keep it scoped to apps you own.
  </Accordion>

  <Accordion title="I calculate fees manually. What do I change?">
    Remove the manual calculation. Use `getClobMarketInfo(conditionID)` to query fee parameters (`fd.r`, `fd.e`, `fd.to`), and rely on the SDK to handle fee-adjusted amounts. Pass `userUSDCBalance` on market buy orders for accurate fill math.
  </Accordion>

  <Accordion title="Are WebSocket URLs or payloads changing?">
    WebSocket URLs are unchanged. Most message payloads are unchanged. The `fee_rate_bps` field on `last_trade_price` events continues to reflect the fee actually charged on the trade.
  </Accordion>
</AccordionGroup>

***

## Getting help

<CardGroup cols={2}>
  <Card title="Discord" icon="discord" href="https://discord.gg/polymarket">
    Real-time help from the Polymarket team and community.
  </Card>

  <Card title="Status" icon="signal" href="https://status.polymarket.com">
    Live status, incidents, and maintenance windows.
  </Card>

  <Card title="Builder Profile" icon="hammer" href="https://polymarket.com/settings?tab=builder">
    Copy your builder code and manage your builder account.
  </Card>

  <Card title="API Reference" icon="code" href="/api-reference/introduction">
    Endpoint-level documentation for every API.
  </Card>
</CardGroup>
