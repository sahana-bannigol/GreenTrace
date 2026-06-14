# Verdant — Tokenized Green Bonds on XRP Ledger

**Verdant** is a hackathon MVP that tokenizes green bonds as on-chain assets using XRPL's Multi-Purpose Token (MPT) standard. Investors must hold a KYC/ESG credential (XLS-70) before they can buy. Settlement is atomic via XRP escrow + crypto-condition — the entire bond purchase completes on-chain in ~4 seconds.

Built for the **Ripple Treasury Hackathon 2026** on XRPL Devnet.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Project Structure](#2-project-structure)
3. [XRPL Standards Used](#3-xrpl-standards-used)
4. [XRPL API Integration — Deep Dive](#4-xrpl-api-integration--deep-dive)
   - [Wallets](#41-wallets)
   - [Green Bond Issuance (MPT / XLS-33)](#42-green-bond-issuance-mpt--xls-33)
   - [Credential Gate (XLS-70)](#43-credential-gate-xls-70)
   - [Bond Purchase — 4-Step Settlement Flow](#44-bond-purchase--4-step-settlement-flow)
   - [Portfolio Query](#45-portfolio-query)
5. [Flask API Endpoints](#5-flask-api-endpoints)
6. [Frontend Architecture](#6-frontend-architecture)
7. [Key Workflows](#7-key-workflows)
   - [Issue a Green Bond](#71-issue-a-green-bond)
   - [Get a KPMG Credential](#72-get-a-kpmg-credential)
   - [Buy a Bond](#73-buy-a-bond)
   - [Credential Rejection (Wallet B)](#74-credential-rejection-wallet-b)
8. [Local Data Persistence](#8-local-data-persistence)
9. [Critical Technical Notes](#9-critical-technical-notes)
10. [Setup & Running](#10-setup--running)
11. [Environment Constants](#11-environment-constants)

---

## 1. Architecture Overview

```
Browser (Jinja2 templates + vanilla JS)
         │
         │  REST/JSON over HTTP
         ▼
Flask App (app.py)           ← bond DB, verifier wallet, route logic
         │
         │  xrpl-py SDK calls
         ▼
XRPL Devnet  (s.devnet.rippletest.net:51234)
   ├── MPTokenIssuanceCreate   → mint green bond tokens
   ├── MPTokenAuthorize        → opt buyer into receiving MPT
   ├── CredentialCreate/Accept → KYC/ESG credential (XLS-70)
   ├── EscrowCreate/Finish     → atomic XRP payment with crypto-condition
   └── Payment (MPTAmount)     → transfer bond tokens to buyer
```

Local state lives in two JSON files (`bonds_db.json`, `verifier_wallet.json`). All on-chain state is canonical; the local DB is a cache for metadata that does not fit on-chain.

---

## 2. Project Structure

```
verdant/
├── app.py                  # Flask backend — all routes and API endpoints
├── xrpl_service.py         # XRPL service layer — every ledger interaction
├── bonds_db.json           # Local bond registry (persisted across restarts)
├── verifier_wallet.json    # KPMG verifier wallet (created once, reused)
├── requirements.txt        # Python dependencies
├── static/
│   ├── style.css           # All CSS (dark green theme)
│   └── verdant.js          # Shared JS utilities (api(), toast(), etc.)
└── templates/
    ├── base.html           # Nav, toast, shared layout
    ├── dashboard.html      # Portfolio overview + trade history
    ├── issuer.html         # Bond issuance form
    ├── marketplace.html    # Bond listing + wallet panel + buy modal
    ├── bond_detail.html    # Per-bond detail + live XRPL data
    └── certificate.html    # Green investment certificate
```

---

## 3. XRPL Standards Used

| Standard | What it does in Verdant |
|----------|------------------------|
| **XLS-33 (MPT)** | Green bonds are minted as Multi-Purpose Tokens. `AssetScale=2` so 1 bond = 100 base units. Flags: `TF_MPT_CAN_ESCROW`, `TF_MPT_CAN_TRADE`, `TF_MPT_CAN_TRANSFER`. |
| **XLS-70 (Credentials)** | KYC/ESG gate. KPMG verifier issues `CredentialCreate`, investor signs `CredentialAccept`. The buy function checks `lsfAccepted` (flag `0x00010000`) on-chain before any transaction is submitted. |
| **Escrow + Crypto-Condition** | XRP payment from buyer → issuer uses a SHA-256 preimage condition. No time lock (`FinishAfter`) is used — condition alone eliminates clock-skew `tecNO_PERMISSION` errors. `CancelAfter` is anchored to live ledger `close_time + 300s`. |

---

## 4. XRPL API Integration — Deep Dive

All ledger logic is in `xrpl_service.py`. The module uses `xrpl-py`'s synchronous `JsonRpcClient`.

### 4.1 Wallets

```python
# Create + fund a fresh devnet wallet from the faucet
def create_wallet() -> dict:
    client = get_client()
    wallet = generate_faucet_wallet(client, debug=False)   # xrpl-py faucet helper
    resp = client.request(AccountInfo(account=wallet.classic_address,
                                      ledger_index="validated"))
    balance_xrp = float(drops_to_xrp(resp.result["account_data"]["Balance"]))
    return {
        "address": wallet.classic_address,
        "seed":    wallet.seed,
        "balance_xrp": balance_xrp,
        "explorer_url": f"{TESTNET_EXPLORER}/accounts/{wallet.classic_address}",
    }

# Read live XRP balance
def get_wallet_balance(address: str) -> float:
    resp = client.request(AccountInfo(account=address, ledger_index="validated"))
    return float(drops_to_xrp(resp.result["account_data"]["Balance"]))
```

**Restore from seed** — the app never stores seeds beyond `bonds_db.json` (issuer) and browser `localStorage` (buyer). Any wallet can be reconstructed with `Wallet.from_seed(seed)`.

---

### 4.2 Green Bond Issuance (MPT / XLS-33)

Function: `issue_green_bond(issuer_seed, bond_data)`

**Step-by-step:**

1. Build on-chain metadata JSON (max 9 top-level fields, XLS-89d guidance):
   ```python
   onchain_meta = {
       "ticker": ..., "name": ..., "asset_class": "rwa",
       "icon": "https://verdant.finance/icon.png",
       "issuer_name": ...,
       "green": { "isin", "coupon", "maturity", "use_of_proceeds",
                  "frameworks", "icma_pass", "eu_taxonomy_pass",
                  "climate_bonds_pass", "verify_score", "issued_date" }
   }
   ```

2. Hex-encode the JSON and embed it in `mptoken_metadata`:
   ```python
   metadata_hex = text_to_hex(json.dumps(onchain_meta, separators=(",", ":")))
   ```

3. Submit `MPTokenIssuanceCreate`:
   ```python
   tx = MPTokenIssuanceCreate(
       account=issuer_wallet.classic_address,
       asset_scale=2,                           # 1 bond = 100 base units
       maximum_amount=str(total_bonds * 100),
       transfer_fee=0,
       mptoken_metadata=metadata_hex,
       flags=(TF_MPT_CAN_ESCROW | TF_MPT_CAN_TRADE | TF_MPT_CAN_TRANSFER),
   )
   resp = submit_and_wait(tx, client, issuer_wallet)
   mpt_issuance_id = resp.result["meta"]["mpt_issuance_id"]
   ```

4. Return the `mpt_issuance_id` — this is the permanent on-chain ID for the bond.

**Verify Score** is computed locally (0–100):
- Base: 50
- +15 ICMA Green Bond Principles
- +15 EU Taxonomy
- +10 Climate Bonds Standard
- +5 use_of_proceeds filled
- +5 ISIN provided

**Live data query** — `get_mpt_info(mpt_issuance_id)` uses `LedgerEntry(mpt_issuance=...)` to fetch outstanding supply from the ledger and decode the hex metadata back to JSON.

---

### 4.3 Credential Gate (XLS-70)

Three functions implement the full credential lifecycle:

#### Issue + Accept (KPMG flow)

```python
def issue_and_accept_credential(verifier_seed, buyer_seed) -> dict:
    # 1. Verifier grants credential to buyer
    create_tx = CredentialCreate(
        account=verifier_wallet.classic_address,
        subject=buyer_wallet.classic_address,
        credential_type=CREDENTIAL_TYPE_HEX,   # hex("VERDANT_GREEN_BOND")
        uri=CREDENTIAL_URI_HEX,                # JSON metadata hex-encoded
    )
    submit_and_wait(create_tx, client, verifier_wallet)

    # 2. Buyer accepts the credential (sets lsfAccepted flag)
    accept_tx = CredentialAccept(
        account=buyer_wallet.classic_address,
        issuer=verifier_wallet.classic_address,
        credential_type=CREDENTIAL_TYPE_HEX,
    )
    submit_and_wait(accept_tx, client, buyer_wallet)
```

The `uri` hex encodes this JSON:
```json
{
  "Bond_Status": "Green_Verified",
  "Standard": "EU_Green_Bond_Standard",
  "Verified_By": "KPMG",
  "EU_Taxonomy": "Pass",
  "ICMA": "Pass"
}
```

#### On-chain Credential Check

```python
def check_has_credential(buyer_address, verifier_address) -> bool:
    resp = client.request(AccountObjects(
        account=buyer_address,
        ledger_index="validated",
        type="credential",          # filter to credential objects only
    ))
    for obj in resp.result.get("account_objects", []):
        if (obj.get("Issuer") == verifier_address
                and obj.get("CredentialType","").upper() == CREDENTIAL_TYPE_HEX.upper()
                and obj.get("Flags", 0) & 0x00010000):   # lsfAccepted
            return True
    return False
```

The `lsfAccepted` flag (`0x00010000`) is only set after `CredentialAccept` — a `CredentialCreate` alone does not pass this check. This prevents the verifier from self-granting credentials on behalf of investors.

**The KPMG verifier wallet** is created once on first server start and stored in `verifier_wallet.json`. It is reused across all credential operations.

---

### 4.4 Bond Purchase — 4-Step Settlement Flow

Function: `buy_bond(buyer_seed, issuer_seed, mpt_issuance_id, quantity, verifier_address=None)`

```
Step 1  MPTokenAuthorize     buyer opts-in to receive this MPT
Step 2  EscrowCreate         buyer locks XRP → issuer (crypto-condition)
Step 3  EscrowFinish         issuer reveals preimage, XRP releases (after ~4s)
Step 4  Payment (MPTAmount)  issuer sends bond tokens → buyer
```

#### Step 1 — MPTokenAuthorize

Before receiving any MPT, the buyer's account must opt in:
```python
auth_tx = MPTokenAuthorize(
    account=buyer_wallet.classic_address,
    mptoken_issuance_id=mpt_issuance_id,
)
submit_and_wait(auth_tx, client, buyer_wallet)
```

#### Step 2 — EscrowCreate with Crypto-Condition

```python
# SHA-256 preimage condition
preimage      = secrets.token_bytes(32)
condition_hash = hashlib.sha256(preimage).digest()

# Compact ASN.1 encoding (PREIMAGE-SHA-256, no time lock)
condition   = bytes([0xA0, 0x25, 0x80, 0x20]) + condition_hash + bytes([0x81, 0x01, 0x20])
fulfillment = bytes([0xA0, 0x22, 0x80, 0x20]) + preimage

# Anchor CancelAfter to live ledger time (avoids tecNO_PERMISSION)
ledger_resp        = client.request(Ledger(ledger_index="validated"))
ledger_close       = ledger_resp.result["ledger"]["close_time"]
cancel_after_ripple = ledger_close + 300          # 5-minute window

escrow_create_tx = EscrowCreate(
    account=buyer_wallet.classic_address,
    amount=xrp_to_drops(max(1, quantity)),         # 1 XRP per bond (symbolic)
    destination=issuer_wallet.classic_address,
    cancel_after=cancel_after_ripple,
    condition=condition_hex,
)
```

`escrow_sequence` (needed to finish the escrow) is read from `tx_json.Sequence` in the response.

#### Step 3 — EscrowFinish

```python
time.sleep(4)   # let EscrowCreate ledger close

escrow_finish_tx = EscrowFinish(
    account=issuer_wallet.classic_address,
    owner=buyer_wallet.classic_address,
    offer_sequence=escrow_sequence,
    condition=condition_hex,
    fulfillment=fulfillment_hex,      # reveals the preimage → unlocks escrow
)
submit_and_wait(escrow_finish_tx, client, issuer_wallet)
```

#### Step 4 — MPT Payment

```python
payment_tx = Payment(
    account=issuer_wallet.classic_address,
    destination=buyer_wallet.classic_address,
    amount=MPTAmount(
        mpt_issuance_id=mpt_issuance_id,
        value=str(quantity * 100),         # AssetScale=2: 1 bond = 100 units
    ),
)
submit_and_wait(payment_tx, client, issuer_wallet)
```

---

### 4.5 Portfolio Query

```python
def get_wallet_mpt_holdings(address: str) -> list:
    resp = client.request(AccountObjects(
        account=address,
        ledger_index="validated",
        type="mptoken",       # filter to MPToken objects only
    ))
    return resp.result.get("account_objects", [])
    # Each object: { "MPTokenIssuanceID": "...", "MPTAmount": "200", ... }
```

The Flask `/api/wallet/<address>/portfolio` endpoint enriches this raw data by joining against `bonds_db.json` to add human-readable bond names, coupon, maturity, and verify score.

---

## 5. Flask API Endpoints

### Wallets

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/wallet/create` | Fund a new devnet wallet via faucet. Returns `{ wallet: { address, seed, balance_xrp, explorer_url } }` |
| `POST` | `/api/wallet/info` | Balance + MPT holdings for an address. Body: `{ address }` |
| `GET`  | `/api/wallet/<address>/portfolio` | XRP balance + enriched bond holdings. Used for the portfolio panel. |

### Bonds

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/bond/issue` | Mint a green bond. Body: flat fields (name, ticker, issuer_seed, total_bonds, coupon, maturity, use_of_proceeds, frameworks, isin, issuer_name). |
| `GET`  | `/api/bonds` | List all bonds with live XRPL outstanding supply. |
| `GET`  | `/api/bond/<mpt_id>` | Single bond detail + live data. |
| `POST` | `/api/bond/buy` | Execute 4-step purchase. Body: `{ buyer_seed, mpt_issuance_id, quantity }`. Returns all 4 TX hashes. |
| `GET`  | `/api/bond/verify/<mpt_id>` | Live verification data from ledger + local metadata. |

### Credentials (XLS-70)

| Method | Path | Description |
|--------|------|-------------|
| `GET`  | `/api/credential/verifier` | Return KPMG verifier address + credential metadata fields. |
| `POST` | `/api/credential/issue` | Issue + auto-accept a KPMG credential to a wallet. Body: `{ buyer_seed }`. Returns both TX hashes. |
| `POST` | `/api/credential/check` | On-chain check: does this address hold an accepted credential? Body: `{ address }`. Returns `{ has_credential: bool }`. |

### Pages

| Path | Template | Description |
|------|----------|-------------|
| `/` | `dashboard.html` | Portfolio overview, bond table, trade history |
| `/issuer` | `issuer.html` | Issue a new green bond |
| `/marketplace` | `marketplace.html` | Browse + buy bonds |
| `/bond/<mpt_id>` | `bond_detail.html` | Per-bond detail with live XRPL data |
| `/certificate/<tx_hash>` | `certificate.html` | Green investment certificate |

---

## 6. Frontend Architecture

### Shared utilities (`verdant.js`)

```javascript
// All API calls go through this helper. Throws on success:false.
async function api(url, method = 'GET', body = null) { ... }

// Toast notification (top-right, 4.5s auto-dismiss)
function toast(msg, type = 'success') { ... }

// Auto-refresh outstanding bond counts every 30s (dashboard only)
function startLiveRefresh() { ... }
```

### Wallet state (browser `localStorage`)

| Key | Value |
|-----|-------|
| `walletA_seed` | Wallet A seed (verified investor) |
| `walletA_address` | Wallet A XRPL address |
| `walletA_initial_xrp` | XRP balance at creation (for drain bar) |
| `walletB_seed` | Wallet B seed (unverified) |
| `walletB_address` | Wallet B XRPL address |
| `issuer_seed` | Issuer wallet seed (set on Issuer page) |
| `issuer_address` | Issuer wallet address |

Wallet state persists across page reloads. On `marketplace.html` load, the wallet panel auto-restores both wallets and re-runs the on-chain credential check for Wallet A.

### Buy Modal flow (`marketplace.html`)

```
openBuyModal(mptId, name, coupon, verifyScore)
  └─ pre-selects Wallet A if generated, else Wallet B, else shows prompt

selectModalWallet('A' or 'B')
  └─ resets modal body
  └─ shows address
  └─ calls runCredentialCheck(wallet, address)

runCredentialCheck(wallet, address)
  └─ POST /api/credential/check
  └─ has_credential=true  → waits 900ms → shows buy controls
  └─ has_credential=false → waits 1000ms → shows rejection banner

confirmBuy()
  └─ POST /api/bond/buy
  └─ cycles step indicators (Step 1/4 → 4/4) via timer
  └─ on success → shows 4 TX hashes + certificate link
  └─ setTimeout(refreshPortfolio, 1500)  → updates portfolio panel
```

---

## 7. Key Workflows

### 7.1 Issue a Green Bond

1. Open `/issuer`
2. Click **Generate Issuer Wallet** → faucet funds 100 XRP → seed saved to `localStorage`
3. Fill bond form (name, ticker, coupon, maturity, frameworks)
4. Click **Issue Bond on XRPL** → `POST /api/bond/issue`
   - Backend calls `issue_green_bond(issuer_seed, bond_data)`
   - `MPTokenIssuanceCreate` submitted to devnet
   - Bond saved to `bonds_db.json`
5. `mpt_issuance_id` and XRPL Explorer link shown

### 7.2 Get a KPMG Credential

1. Open `/marketplace`
2. Click **Generate Wallet A** → new devnet wallet funded
3. Click **Get KPMG Credential (XLS-70)** → `POST /api/credential/issue`
   - Backend: `CredentialCreate` (verifier → buyer)
   - Backend: `CredentialAccept` (buyer accepts)
   - Both TX hashes returned and linked to Explorer
4. Badge updates to **✓ KPMG VERIFIED**, credential metadata shown

### 7.3 Buy a Bond

1. Wallet A credentialed (see above)
2. Click **Buy** on any bond in the table
3. Modal opens, auto-selects Wallet A
4. `runCredentialCheck` fires → `POST /api/credential/check`
5. Credential confirmed on-chain → buy controls appear
6. Click **Buy Bond (Escrow + MPT Transfer)** → `POST /api/bond/buy`
   - Step 1: `MPTokenAuthorize` (~2s)
   - Step 2: `EscrowCreate` with SHA-256 condition (~2s)
   - Step 3: `sleep(4)` → `EscrowFinish` (~2s)
   - Step 4: `Payment(MPTAmount)` (~2s)
7. All 4 TX hashes shown with Explorer links
8. Portfolio panel refreshes: XRP balance drops, bond token appears

### 7.4 Credential Rejection (Wallet B)

1. Click **Generate Wallet B (Unverified)** — no credential issued
2. Click **Buy** on any bond
3. Switch to **Wallet B** in the modal toggle
4. `runCredentialCheck` fires → `has_credential: false`
5. Rejection banner shown: **"No credential found — XLS-70 gate enforced on-chain"**
6. Even if `confirmBuy` is called directly, the server calls `check_has_credential` again and raises `CREDENTIAL_REQUIRED` before any transaction is submitted

---

## 8. Local Data Persistence

### `bonds_db.json`

Array of bond entries. Each entry:
```json
{
  "mpt_issuance_id": "000000...",
  "issuance_tx_hash": "ABC123...",
  "issuer": "rXXX...",
  "issuer_seed": "sXXX...",
  "metadata": {
    "ticker": "SOLAR1",
    "name": "UK Solar Bond Series A",
    "asset_class": "rwa",
    "issuer_name": "GreenEnergy plc",
    "coupon": "3.75",
    "maturity": "2034",
    "use_of_proceeds": "Solar Infrastructure",
    "frameworks": ["ICMA Green Bond Principles", "EU Taxonomy"],
    "verify_score": 90,
    ...
  },
  "verify_score": 90,
  "status": "verified",
  "total_bonds": 1000,
  "issued_at": "2026-06-14T10:00:00",
  "trades": [
    {
      "payment_tx_hash": "...",
      "auth_tx_hash": "...",
      "escrow_create_tx_hash": "...",
      "escrow_finish_tx_hash": "...",
      "buyer": "rYYY...",
      "quantity": 2,
      "timestamp": "...",
      "explorer_url": "..."
    }
  ]
}
```

### `verifier_wallet.json`

```json
{ "seed": "sXXX...", "address": "rZZZ..." }
```

Created automatically on first server start. The same wallet is reused across all credential operations, so credentials issued in previous sessions remain valid.

---

## 9. Critical Technical Notes

### Devnet Clock Offset (+3600 seconds)

XRPL Devnet ledger `close_time` runs ~3600 seconds ahead of local UTC (Ripple epoch = Unix epoch − 946684800). Any time-bound transaction field (`FinishAfter`, `CancelAfter`) that uses `datetime.utcnow()` will be in the ledger's past and fail with `tecNO_PERMISSION`.

**Fix**: Always query the live ledger close time first:
```python
ledger_resp  = client.request(Ledger(ledger_index="validated"))
ledger_close = ledger_resp.result["ledger"]["close_time"]
cancel_after = ledger_close + 300
```

### Why No `FinishAfter` on EscrowCreate

`FinishAfter` requires the escrow to remain locked until that ledger index closes. Combined with the clock offset, computing a safe value reliably from Python is fragile. Using only `CancelAfter` (a deadline) with a crypto-condition means the escrow can be finished immediately after the create ledger closes, while still having an expiry safety net. This is the correct pattern for near-instant settlement.

### httpx SSL Patch

XRPL Devnet serves an incomplete TLS certificate chain that fails validation even with the `certifi` CA bundle. `xrpl-py` uses `httpx` internally for async requests. The module patches `httpx.AsyncClient.__init__` at import time to force `verify=False`:

```python
_orig_async_init = _httpx.AsyncClient.__init__
def _no_verify_async_init(self, *args, verify=True, **kwargs):
    _orig_async_init(self, *args, verify=False, **kwargs)
_httpx.AsyncClient.__init__ = _no_verify_async_init
```

This is safe for a testnet demo. Production would use proper certificate pinning.

### MPT `AssetScale = 2`

`AssetScale=2` means all on-chain amounts are stored as integer base units where 1 human-readable bond = 100 base units. Every read divides by 100; every write multiplies by 100:
```python
# Write (buy 3 bonds)
value = str(3 * 100)   # "300"

# Read (display)
quantity = int(h.get("MPTAmount", "0")) / 100   # 3.0
```

### `escrow_sequence` Fallback

`EscrowFinish` requires `offer_sequence` — the sequence number of the `EscrowCreate` transaction. `xrpl-py` returns this in different places depending on the response shape:
```python
escrow_sequence = (
    escrow_resp.result.get("tx_json", {}).get("Sequence")
    or escrow_resp.result.get("Sequence")
)
```

---

## 10. Setup & Running

### Prerequisites

- Python 3.8+
- Internet access to XRPL Devnet

### Install

```bash
cd verdant
pip install -r requirements.txt
```

`requirements.txt`:
```
flask>=2.0.0
xrpl-py
requests>=2.25.0
```

### Run

```bash
python app.py
```

Server starts at `http://localhost:5000`

On first start, `verifier_wallet.json` is auto-created (one devnet faucet call). Subsequent starts reuse the same verifier.

### First-time walkthrough

1. `http://localhost:5000/issuer` → generate issuer wallet → issue a bond
2. `http://localhost:5000/marketplace` → generate Wallet A → get KPMG credential → buy bond
3. Optionally generate Wallet B → click Buy → watch credential rejection

---

## 11. Environment Constants

Defined at the top of `xrpl_service.py`:

```python
TESTNET_URL      = "https://s.devnet.rippletest.net:51234"
TESTNET_EXPLORER = "https://devnet.xrpl.org"

# XLS-70 credential identifier (hex-encoded UTF-8)
CREDENTIAL_TYPE_HEX = text_to_hex("VERDANT_GREEN_BOND")
# = "5645524441....." (ASCII hex)

# Credential URI metadata (hex-encoded JSON)
CREDENTIAL_URI_META = {
    "Bond_Status": "Green_Verified",
    "Standard":    "EU_Green_Bond_Standard",
    "Verified_By": "KPMG",
    "EU_Taxonomy": "Pass",
    "ICMA":        "Pass",
}
CREDENTIAL_URI_HEX = text_to_hex(json.dumps(CREDENTIAL_URI_META, separators=(",", ":")))
```

The `lsfAccepted` flag value for XLS-70 credential objects: `0x00010000` (decimal `65536`).

---

*Verdant — Ripple Treasury Hackathon 2026*
