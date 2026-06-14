# GreenTrace — Project Overview

**Tokenized Green Bonds on the XRP Ledger**
*Built for Ripple Treasury Hackathon 2026 · XRPL Devnet*

---

## Table of Contents

1. [What is GreenTrace?](#1-what-is-greentrace)
2. [How We've Used XRPL](#2-how-weve-used-xrpl)
3. [What We've Built](#3-what-weve-built)
4. [How It's Wired](#4-how-its-wired)
5. [Where the Project Stands Now](#5-where-the-project-stands-now)
6. [Technical Gaps](#6-technical-gaps)
7. [Version 2 — Compliance Escrow Oracle + AI Agents](#7-version-2--compliance-escrow-oracle--ai-agents)

---

## 1. What is GreenTrace?

GreenTrace is a proof-of-concept platform for issuing, verifying, and trading **tokenized green bonds** on the XRP Ledger. It demonstrates that the full lifecycle of a regulated green bond — issuance, investor credentialing, atomic settlement — can be executed entirely on-chain in seconds, not days.

### The Problem It Solves

Traditional green bond markets have three structural problems:

| Problem | Traditional Market | GreenTrace |
|---|---|---|
| **Settlement speed** | T+2 days (CSD/custodian chain) | ~4 seconds on-chain |
| **Investor verification** | Paper KYC, siloed across brokers | XLS-70 on-chain credential, reusable across platforms |
| **Green compliance** | Annual self-reporting, unverifiable | Metadata embedded in the token, verify score on-chain |

### The Core Idea

Green bonds are minted as **Multi-Purpose Tokens (MPTs)** on XRPL — each bond is a unit of an on-chain asset with its green credentials (ISIN, frameworks, use of proceeds, verify score) embedded in the token metadata. Investors must hold a **KPMG-issued XLS-70 credential** before they can buy. Settlement uses an **XRP escrow with a SHA-256 crypto-condition** — payment and token transfer are atomically linked so neither party can be cheated.

---

## 2. How We've Used XRPL

GreenTrace uses four distinct XRPL capabilities, each solving a different part of the green bond problem.

### 2.1 MPT — Multi-Purpose Token (XLS-33)

Green bonds are minted as MPTs rather than fungible tokens or NFTs because MPTs were designed specifically for real-world assets: they support fractional supply, transfer restrictions, and on-chain metadata.

```
MPTokenIssuanceCreate
  ├── AssetScale = 2          → 1 bond = 100 base units (supports fractional bonds)
  ├── MaximumAmount           → total bond supply cap
  ├── TF_MPT_CAN_TRADE        → bonds can be traded on DEX
  ├── TF_MPT_CAN_TRANSFER     → peer-to-peer transfer allowed
  ├── TF_MPT_CAN_ESCROW       → bonds can be placed in escrow (v2 feature)
  └── MPTokenMetadata (hex)   → JSON: ticker, name, ISIN, coupon, maturity,
                                 use_of_proceeds, frameworks, verify_score
```

The metadata is hex-encoded JSON embedded directly on-chain, following XLS-89d guidance (max 9 top-level fields for discoverability). This means the bond's green credentials are permanent, immutable, and auditable by anyone with access to the ledger — no reliance on off-chain databases.

**Verify Score** is computed at issuance (0–100):
- Base: 50 points
- +15 ICMA Green Bond Principles
- +15 EU Taxonomy alignment
- +10 Climate Bonds Standard
- +5 Use of proceeds documented
- +5 ISIN provided

### 2.2 XLS-70 Credentials — Investor Gating

Before an investor can buy any bond, they must hold an accepted green investor credential. This is implemented using XRPL's native XLS-70 credential standard — not a database flag, not a JWT, but a cryptographically verifiable on-chain object.

```
CredentialCreate  (verifier → investor)
  ├── CredentialType = hex("GREENTRACE_GREEN_BOND")
  └── URI = hex(JSON { Bond_Status, Standard, Verified_By, EU_Taxonomy, ICMA })

CredentialAccept  (investor signs to accept)
  └── Sets lsfAccepted flag (0x00010000) on the credential object

On purchase: AccountObjects(type="credential") checked for lsfAccepted
```

The KPMG verifier wallet issues credentials. The `lsfAccepted` flag only exists after the investor explicitly accepts — preventing the verifier from self-granting credentials on behalf of investors. This dual-signature pattern mirrors the real-world KYC relationship between verifier and investor.

### 2.3 Escrow with Crypto-Condition — Atomic Settlement

The bond purchase is not a simple payment. It's a four-step atomic sequence where XRP payment and MPT token delivery are linked via a cryptographic commitment:

```
Step 1  MPTokenAuthorize     → buyer opts in to receive this specific MPT
Step 2  EscrowCreate         → buyer locks XRP → issuer under SHA-256 condition
Step 3  EscrowFinish         → issuer reveals preimage, XRP releases (~4s later)
Step 4  Payment(MPTAmount)   → issuer delivers bond tokens to buyer
```

The crypto-condition is a **PREIMAGE-SHA-256** condition (IETF draft-thomas-crypto-conditions):

```python
preimage       = secrets.token_bytes(32)          # random secret
condition_hash = hashlib.sha256(preimage).digest()
condition      = ASN1_DER_HEADER + condition_hash  # locked on-chain
fulfillment    = ASN1_DER_HEADER + preimage        # revealed to unlock
```

The `CancelAfter` is anchored to the live ledger `close_time` (not `datetime.utcnow()`) to avoid clock-skew errors on devnet. No `FinishAfter` is used — the condition alone gates release, enabling near-instant settlement.

### 2.4 AccountObjects Queries — Live Portfolio

Holdings and credentials are read directly from the XRPL ledger rather than inferred from local records:

```python
AccountObjects(account=address, type="mptoken")     # live bond holdings
AccountObjects(account=address, type="credential")  # active credentials
LedgerEntry(mpt_issuance=mpt_id)                    # live supply data
AccountInfo(account=address)                        # XRP balance
```

This means the portfolio panel always reflects true on-chain state — it cannot diverge from what the ledger actually holds.

---

## 3. What We've Built

### Backend — Flask + xrpl-py

`app.py` is the Flask application layer. It handles routing, request validation, local persistence, and orchestrates calls to `xrpl_service.py`.

`xrpl_service.py` is the XRPL service layer — every transaction, every ledger query goes through here. It is deliberately separate from Flask so it can be tested independently and reused in other contexts (CLI, background jobs, v2 oracle).

**API surface:**

| Endpoint | Purpose |
|---|---|
| `POST /api/wallet/create` | Fund a new devnet wallet via faucet |
| `POST /api/wallet/info` | XRP balance + MPT holdings |
| `GET /api/wallet/<address>/portfolio` | Enriched holdings with bond metadata |
| `POST /api/bond/issue` | Mint a green bond as MPT |
| `GET /api/bonds` | List all bonds with live supply |
| `POST /api/bond/buy` | Execute 4-step purchase |
| `GET /api/bond/verify/<mpt_id>` | Live verification data from ledger |
| `GET /api/credential/verifier` | KPMG verifier address |
| `POST /api/credential/issue` | Issue + accept XLS-70 credential |
| `POST /api/credential/check` | On-chain credential verification |

### Frontend — Jinja2 Templates + Vanilla JS

Five pages, each with a distinct role:

| Page | What it does |
|---|---|
| `/` Dashboard | Portfolio overview, bond supply stats, recent trades, verify score breakdown |
| `/issuer` | Generate issuer wallet, fill bond form, mint MPT on XRPL |
| `/marketplace` | Two-wallet demo (Wallet A credentialed / Wallet B rejected), bond table, buy modal with 4-step progress, portfolio panel |
| `/bond/<mpt_id>` | Per-bond detail page with live supply, on-chain metadata, trade history |
| `/certificate/<tx_hash>` | Green investment certificate with all four transaction hashes |

Wallet state (seeds, addresses) is stored in browser `localStorage` and auto-restored on page load. The marketplace auto-runs an on-chain credential check when a wallet is selected in the buy modal — there is no trust-on-open shortcut.

### Local Persistence

Two JSON files act as an off-chain cache for data that doesn't fit on-chain:

- `bonds_db.json` — bond registry: MPT ID, issuer seed (demo only), metadata, trade history
- `verifier_wallet.json` — KPMG verifier wallet, auto-heals after devnet resets by checking on-chain balance before use

---

## 4. How It's Wired

```
Browser
  │
  │  REST/JSON
  ▼
Flask (app.py)
  ├── bonds_db.json          ← local metadata cache
  ├── verifier_wallet.json   ← KPMG verifier (auto-healed on devnet reset)
  │
  │  xrpl-py SDK (JsonRpcClient)
  ▼
XRPL Devnet  (s.devnet.rippletest.net:51234)
  ├── MPTokenIssuanceCreate  → bond minted
  ├── MPTokenAuthorize       → buyer opts in
  ├── CredentialCreate       → verifier grants credential
  ├── CredentialAccept       → investor accepts credential
  ├── EscrowCreate           → XRP locked under crypto-condition
  ├── EscrowFinish           → XRP released on fulfillment reveal
  └── Payment(MPTAmount)     → bond tokens delivered to buyer
```

**Request lifetime for a bond purchase:**

```
Browser: POST /api/bond/buy  { buyer_seed, mpt_issuance_id, quantity }
  │
  ├── check_has_credential(buyer, verifier)   → AccountObjects query
  ├── MPTokenAuthorize(buyer)                 → submit_and_wait ~2s
  ├── Ledger(validated)                       → get live close_time
  ├── EscrowCreate(buyer → issuer)            → submit_and_wait ~2s
  ├── sleep(4)                                → wait for ledger to close
  ├── EscrowFinish(issuer reveals preimage)   → submit_and_wait ~2s
  └── Payment(MPTAmount, issuer → buyer)      → submit_and_wait ~2s
        │
        └── return 4 tx hashes + explorer links
```

Total time: ~12–15 seconds end-to-end on devnet.

---

## 5. Where the Project Stands Now

### What Works

- Full green bond issuance as MPT on XRPL devnet, with on-chain metadata
- XLS-70 credential issuance (KPMG verifier → investor) with dual-signature acceptance
- On-chain credential gate enforced server-side before any transaction is submitted
- 4-step atomic settlement: MPTokenAuthorize → EscrowCreate → EscrowFinish → Payment
- Live portfolio: XRP balance, bond holdings, drain bar showing XRP spent
- Per-bond detail page with live supply data pulled from ledger
- Green investment certificate with all four on-chain transaction hashes
- Credential rejection demo: Wallet B gets a clear "ACCESS DENIED" with XLS-70 enforcement explained
- Verifier wallet auto-heals after devnet resets (checks on-chain balance, re-funds if gone)

### Known Limitations

- **Devnet only** — no mainnet deployment, no real assets
- **Issuer seed stored in bonds_db.json** — necessary for the demo but not acceptable in production
- **Buyer seed stored in browser localStorage** — convenient for demo, not a custody solution
- **Escrow is settlement-only** — the ~5-minute escrow window is just for T+0 settlement; there is no long-term escrow holding funds through the bond's lifetime
- **Verify score is self-reported** — computed locally at issuance from form fields; no external verification data is pulled
- **No stale bond detection** — if the XRPL devnet resets, bonds in bonds_db.json become orphaned (issuer wallet gone, MPT gone); user must re-issue manually
- **Single verifier** — one KPMG wallet issues all credentials; no multi-verifier or credential revocation

---

## 6. Technical Gaps

### Gap 1 — Escrow Scope

The current escrow exists only for atomic settlement (seconds). It plays no role in the ongoing bond lifecycle. A real green bond runs for 5–10 years; the XRP escrow should be long-term, with the CancelAfter set to maturity. The investor's capital should stay locked until either the issuer proves continued compliance (EscrowFinish) or the bond fails and the investor gets a refund (EscrowCancel). This is the primary structural gap between the MVP and a production system.

### Gap 2 — Compliance Verification

The verify score is computed once at issuance and never updated. In reality, a bond's green status changes: proceeds can be misallocated, issuers can be downgraded, EU Taxonomy rules can shift. There is no mechanism in v1 to mark a bond as non-compliant after it has been issued, and no consequence on the escrow if compliance fails.

### Gap 3 — Credential Lifecycle

Credentials are issued but never revoked. A real system needs `CredentialDelete` capability — if KPMG removes an investor's green credential (e.g., sanctions, identity change), that investor should immediately lose the ability to buy new bonds. The credential check in `buy_bond` already reads on-chain state, so revocation would work automatically once deletion is wired up.

### Gap 4 — Key Management

Issuer seeds are stored in a local JSON file. Buyer seeds are in browser localStorage. Neither is acceptable beyond a demo. Production would need hardware wallet integration, a custody API (Fireblocks, Copper), or at minimum server-side encrypted seed storage with HSM-backed signing.

### Gap 5 — No Secondary Market

Bonds can be issued and bought once. There is no resale mechanism, no order book, and no price discovery. The `TF_MPT_CAN_TRADE` flag is set on every bond (enabling DEX trading on XRPL) but the UI doesn't surface it. A secondary market tab using XRPL's native DEX (OfferCreate/OfferCancel) would complete the trading lifecycle.

### Gap 6 — Off-chain/On-chain Data Bridge

The bond metadata is embedded in the MPT at issuance but the local `bonds_db.json` is the source of truth for the application. If the JSON file is deleted or the server changes, the UI loses context even though the on-chain data is permanent. A proper implementation would be able to reconstruct the full application state purely from ledger data.

---

## 7. Version 2 — Compliance Escrow Oracle + AI Agents

V2 closes Gap 1 and Gap 2 with a compliance oracle architecture. The core insight is that XRPL escrow can hold funds through the full bond lifetime, but it needs an off-chain oracle to make the release/refund decision. AI agents make that oracle intelligent.

### 7.1 The Compliance Escrow Design

When an investor buys a bond in v2, the escrow is long-term — locked until bond maturity:

```
Investor creates EscrowCreate:
  ├── amount:       XRP (bond price)
  ├── destination:  issuer wallet
  ├── cancel_after: bond maturity date (e.g. 2033)      ← investor can claim back after this
  └── condition:    SHA-256 crypto-condition             ← fulfillment held by ORACLE, not issuer

Compliance check (runs periodically via AI agent):
  ├── Bond GREEN & compliant at maturity
  │     → Oracle sends fulfillment to issuer
  │     → Issuer submits EscrowFinish → receives XRP
  │
  └── Bond NON-COMPLIANT before/at maturity
        → Oracle withholds fulfillment
        → After cancel_after, investor submits EscrowCancel → receives XRP back
```

**The key difference from v1:** the oracle holds the fulfillment, not the issuer. The issuer cannot cash out without the oracle's cryptographic approval. This enforces ongoing green compliance as a precondition for payment — not just a flag at issuance.

### 7.2 The Oracle Wallet

A new server-side wallet acts as the compliance oracle. It is the only entity that holds the escrow fulfillments.

```python
# oracle_service.py (new in v2)

class ComplianceOracle:
    wallet: Wallet           # oracle XRPL wallet
    fulfillments: dict       # mpt_id → { buyer, fulfillment_hex, escrow_sequence }

    def release(self, mpt_id):
        """Oracle approves payout. Sends fulfillment to issuer."""
        ...

    def withhold(self, mpt_id):
        """Oracle blocks payout. Records non-compliance on-chain (memo)."""
        ...
```

The oracle wallet address is public and on-chain — anyone can verify which oracle is gating a given escrow. This gives investors confidence that the gate is operated by a neutral party (e.g., KPMG, a DAO, a regulator).

### 7.3 AI Agent Architecture

The compliance check is not a simple rule. Green bond compliance involves unstructured documents, ESG ratings from multiple agencies, regulatory grey areas, and multi-framework interpretation. AI agents handle this better than hand-coded rules.

```
[Data Gatherer Agents]               [Orchestrator Agent]       [Oracle Action]
                                      (Claude Opus)
  ┌─ ESG Rating Agent ──────────────┐
  │  Calls Sustainalytics/MSCI API  │
  │  Extracts: rating, outlook,     ├──▶  Reasons over all      ──▶  GREEN
  │  downgrade flags                │      inputs against            → release fulfillment
  │                                 │      EU Taxonomy Art.3,
  ├─ News Sentiment Agent ──────────┤      ICMA GBP, CBS         ──▶  NON-COMPLIANT
  │  Searches recent news           │      Produces structured        → withhold, record memo
  │  Flags: greenwashing, scandal,  │      decision + rationale
  │  regulatory action              │
  │                                 │                             ──▶  FLAGGED
  ├─ Regulatory Filing Agent ───────┤      Confidence < 0.7          → escalate to human
  │  Parses EU taxonomy reports     │      → escalate to human
  │  Reads annual impact reports    │
  │  Checks allocation vs proceeds  │
  │                                 │
  └─ KPMG Verification Agent ───────┘
     Reads latest third-party
     verification report PDF
```

**Why Opus for the orchestrator:** Compliance decisions require multi-source synthesis, understanding of regulatory nuance (EU Taxonomy "do no significant harm" criteria, ICMA second-party opinion requirements), and the ability to produce an auditable written rationale. Haiku handles the fast, structured data-extraction sub-tasks (ESG API calls, news filtering); Opus does the heavyweight reasoning.

**The decision output is structured:**

```json
{
  "mpt_issuance_id": "0115D93E...",
  "bond_name": "EIB Climate Awareness Bond",
  "assessment_date": "2027-06-14",
  "status": "NON_COMPLIANT",
  "confidence": 0.89,
  "action": "WITHHOLD_FULFILLMENT",
  "reason": "EIB Q1 2027 allocation report shows 18% of proceeds directed to natural gas bridging infrastructure (LNG terminals, DE/PL pipeline). This fails EU Taxonomy Art.3 substantial contribution to climate change mitigation. ICMA GBP self-certification was not updated to reflect the reallocation.",
  "sources": [
    "EIB Green Bond Allocation Report Q1-2027.pdf",
    "Sustainalytics ESG Risk Rating — downgraded from 'Low' to 'Medium' (2027-03-15)",
    "Reuters: 'EIB faces backlash over gas infrastructure...' (2027-04-02)"
  ]
}
```

This decision is stored as a memo on the `EscrowFinish` or `EscrowCancel` transaction — giving regulators, auditors, and investors a complete, verifiable audit trail of *why* the oracle acted.

### 7.4 New Components in V2

| Component | Description |
|---|---|
| `oracle_service.py` | Compliance oracle wallet, fulfillment store, EscrowFinish/Cancel submission |
| `compliance_agent.py` | Multi-agent orchestration using Claude API (Opus orchestrator + Haiku sub-agents) |
| `POST /api/compliance/check/<mpt_id>` | Trigger compliance assessment, returns structured decision |
| `POST /api/escrow/release/<mpt_id>` | Oracle releases fulfillment → issuer submits EscrowFinish |
| `POST /api/escrow/refund/<mpt_id>` | After maturity, investor submits EscrowCancel |
| `GET /api/compliance/history/<mpt_id>` | Full compliance assessment history for a bond |
| Compliance timeline (UI) | Per-bond page shows compliance check history, agent rationale, status changes |
| Scheduled runner | `/schedule` cron job runs compliance checks weekly per bond |

### 7.5 V2 Architecture

```
Browser
  │
  ▼
Flask (app.py)
  ├── bonds_db.json
  ├── verifier_wallet.json      ← investor credentialing (unchanged)
  ├── oracle_wallet.json        ← NEW: compliance oracle wallet + fulfillment store
  │
  ├── compliance_agent.py       ← NEW: AI agent pipeline
  │     ├── ESG rating fetcher  (Haiku)
  │     ├── News sentiment      (Haiku)
  │     ├── Filing parser       (Haiku)
  │     └── Orchestrator        (Opus) → structured compliance decision
  │
  └── xrpl_service.py  (extended)
        ├── create_long_term_escrow(buyer, issuer, mpt_id, maturity_date)
        ├── oracle_release_escrow(mpt_id)
        └── investor_cancel_escrow(mpt_id)
```

### 7.6 V2 User Flows

**Bond purchase (long-term escrow):**
1. Investor buys bond → escrow locks XRP until 2033
2. Oracle wallet receives and stores the fulfillment
3. MPT tokens transfer immediately — investor holds the bond
4. XRP stays locked until compliance is confirmed at maturity

**Quarterly compliance check:**
1. Scheduled agent runs for each active bond
2. Haiku agents gather ESG data, news, filings
3. Opus orchestrator synthesises and produces a decision
4. If GREEN: status recorded, no escrow action yet
5. If NON-COMPLIANT: oracle withholds fulfillment, bond flagged on dashboard
6. At maturity: compliant bonds → EscrowFinish; non-compliant → EscrowCancel refund

**Investor refund flow:**
1. Bond is flagged non-compliant (oracle withholds)
2. `CancelAfter` (maturity date) passes
3. Investor clicks "Claim Refund" on the bond detail page
4. `EscrowCancel` submitted → XRP returned to investor

---

## Summary

GreenTrace demonstrates that XRPL's MPT, credential, and escrow primitives are mature enough to support a real green bond platform today — issuance, credentialing, and atomic settlement all work on devnet. The MVP proves the on-chain mechanics. Version 2 completes the picture: the compliance escrow oracle closes the gap between issuance-time verification and lifetime compliance, and AI agents make the oracle capable of reasoning over the real-world data that determines whether a bond deserves its green label.

---

*GreenTrace · Ripple Treasury Hackathon 2026 · XRPL Devnet*
*Built with xrpl-py, Flask, and Claude*
