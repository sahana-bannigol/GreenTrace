# GreenTrace — Architecture & Flow Diagrams

---

## System Architecture

```mermaid
flowchart TD
    subgraph Browser["Browser"]
        LS["localStorage\nWallet seeds · addresses · initial XRP"]
        JS["Vanilla JS\napi() · toast() · portfolio refresh"]
    end

    subgraph Server["Flask Server — app.py"]
        Routes["Route Handlers\n/api/bond/buy\n/api/credential/issue\n/api/wallet/create  …"]
        BondsDB[("bonds_db.json\nBond registry\nTrade history\nIssuer seeds")]
        VerifierFile[("verifier_wallet.json\nKPMG verifier wallet\nAuto-heals on devnet reset")]
    end

    subgraph Service["XRPL Service Layer — xrpl_service.py"]
        Fns["issue_green_bond()\nbuy_bond()\nissue_and_accept_credential()\ncheck_has_credential()\nget_mpt_info()"]
    end

    subgraph Devnet["XRPL Devnet  ·  s.devnet.rippletest.net:51234"]
        Issuance["MPTokenIssuanceCreate\nMPTokenAuthorize\nPayment · MPTAmount"]
        Cred["CredentialCreate\nCredentialAccept"]
        Esc["EscrowCreate\nEscrowFinish"]
        Query["AccountInfo\nAccountObjects\nLedgerEntry · Ledger"]
    end

    Browser -- "HTTP REST / JSON" --> Server
    Routes --> BondsDB
    Routes --> VerifierFile
    Routes --> Service
    Service -- "xrpl-py  JsonRpcClient" --> Devnet
```

---

## Bond Issuance Flow

```mermaid
sequenceDiagram
    actor Issuer as Issuer (Browser)
    participant F as Flask
    participant X as xrpl_service.py
    participant L as XRPL Devnet

    Issuer->>F: POST /api/wallet/create
    F->>X: create_wallet()
    X->>L: generate_faucet_wallet()
    L-->>X: wallet { address, seed, 100 XRP }
    X-->>F: wallet details
    F-->>Issuer: address · seed · balance

    Issuer->>F: POST /api/bond/issue { issuer_seed, name, coupon, maturity, frameworks … }
    F->>X: issue_green_bond(issuer_seed, bond_data)
    Note over X: Build on-chain metadata JSON<br/>(ticker, ISIN, coupon, maturity,<br/>frameworks, verify_score)<br/>Hex-encode → MPTokenMetadata
    X->>L: MPTokenIssuanceCreate(AssetScale=2, metadata_hex, flags)
    L-->>X: tesSUCCESS · mpt_issuance_id
    X-->>F: mpt_issuance_id · tx_hash · metadata
    F->>F: Append bond to bonds_db.json
    F-->>Issuer: mpt_issuance_id · explorer_url
```

---

## Credential Issuance Flow

```mermaid
sequenceDiagram
    actor Inv as Investor / Wallet A (Browser)
    participant F as Flask
    participant X as xrpl_service.py
    participant L as XRPL Devnet

    Inv->>F: POST /api/wallet/create
    F->>X: create_wallet()
    X->>L: generate_faucet_wallet()
    L-->>X: wallet { address, seed, 100 XRP }
    F-->>Inv: address · seed · balance

    Inv->>F: POST /api/credential/issue { buyer_seed }
    F->>F: get_verifier_wallet()<br/>→ read verifier_wallet.json<br/>→ check on-chain balance<br/>→ auto-fund new wallet if stale
    F->>X: issue_and_accept_credential(verifier_seed, buyer_seed)

    Note over X,L: Step 1 — Verifier grants credential
    X->>L: CredentialCreate(verifier→buyer,<br/>type=GREENTRACE_GREEN_BOND,<br/>uri=EU_Taxonomy+ICMA JSON)
    L-->>X: tesSUCCESS · create_tx_hash

    Note over X,L: Step 2 — Investor accepts
    X->>L: CredentialAccept(buyer, issuer=verifier)
    L-->>X: tesSUCCESS · accept_tx_hash<br/>(sets lsfAccepted flag 0x00010000)

    X-->>F: create_tx_hash · accept_tx_hash
    F-->>Inv: ✓ KPMG VERIFIED · both tx hashes
```

---

## Bond Purchase Flow — 4-Step Atomic Settlement

```mermaid
sequenceDiagram
    actor Inv as Investor / Wallet A (Browser)
    participant F as Flask
    participant X as xrpl_service.py
    participant L as XRPL Devnet

    Inv->>F: POST /api/bond/buy { buyer_seed, mpt_issuance_id, quantity }
    F->>F: find_bond(mpt_id) → get issuer_seed from bonds_db.json
    F->>F: get_verifier_wallet() → get verifier address

    Note over F,L: Credential Gate — on-chain check before any tx
    F->>X: check_has_credential(buyer, verifier)
    X->>L: AccountObjects(buyer, type="credential")
    L-->>X: credential objects
    X-->>F: lsfAccepted = true ✓

    F->>X: buy_bond(buyer_seed, issuer_seed, mpt_id, qty, verifier_addr)

    Note over X,L: Step 1 — Buyer opts in to receive this MPT
    X->>L: MPTokenAuthorize(buyer, mpt_issuance_id)
    L-->>X: tesSUCCESS · auth_tx_hash

    Note over X,L: Step 2 — Buyer locks XRP under SHA-256 condition
    X->>L: Ledger(validated) → close_time
    L-->>X: close_time (devnet epoch)
    X->>X: generate preimage · condition · fulfillment
    X->>L: EscrowCreate(buyer→issuer, amount, condition,<br/>cancel_after = close_time + 300s)
    L-->>X: tesSUCCESS · escrow_sequence · escrow_tx_hash

    Note over X,L: Step 3 — Issuer reveals preimage, XRP releases
    X->>X: sleep(4s) — wait for EscrowCreate ledger to close
    X->>L: EscrowFinish(issuer, owner=buyer,<br/>offer_sequence, fulfillment_hex)
    L-->>X: tesSUCCESS · finish_tx_hash

    Note over X,L: Step 4 — Issuer delivers bond tokens to buyer
    X->>L: Payment(issuer→buyer, MPTAmount(mpt_id, qty×100))
    L-->>X: tesSUCCESS · payment_tx_hash

    X-->>F: 4 tx hashes · buyer · quantity
    F->>F: Append trade to bonds_db.json
    F-->>Inv: auth_tx_hash · escrow_create_tx_hash<br/>escrow_finish_tx_hash · payment_tx_hash<br/>+ certificate link

    Note over Inv: Total time: ~12–15 seconds on devnet
```

---

## Credential Rejection Flow (Wallet B)

```mermaid
sequenceDiagram
    actor Inv as Unverified / Wallet B (Browser)
    participant F as Flask
    participant X as xrpl_service.py
    participant L as XRPL Devnet

    Inv->>F: POST /api/credential/check { address: walletB }
    F->>X: check_has_credential(walletB, verifier)
    X->>L: AccountObjects(walletB, type="credential")
    L-->>X: [] empty — no credential objects
    X-->>F: has_credential = false
    F-->>Inv: has_credential: false → ACCESS DENIED banner

    Note over Inv,F: Even if buy is attempted directly:
    Inv->>F: POST /api/bond/buy { buyer_seed=walletB … }
    F->>X: check_has_credential(walletB, verifier)
    X->>L: AccountObjects(walletB, type="credential")
    L-->>X: [] empty
    X-->>F: false
    F-->>Inv: 500 CREDENTIAL_REQUIRED<br/>No transactions submitted
```

---

## V2 — Compliance Oracle + AI Agent Flow

```mermaid
sequenceDiagram
    participant S as Scheduler (cron)
    participant A as AI Agent Pipeline
    participant O as Oracle Service
    participant L as XRPL Devnet
    actor Inv as Investor (Browser)

    Note over S,A: Quarterly compliance check
    S->>A: run_compliance_check(mpt_issuance_id)

    Note over A: Haiku sub-agents gather data in parallel
    A->>A: ESG Rating Agent → Sustainalytics/MSCI API
    A->>A: News Sentiment Agent → recent articles
    A->>A: Filing Parser Agent → allocation reports PDF
    A->>A: KPMG Report Agent → third-party verification

    Note over A: Opus orchestrator synthesises
    A->>A: Reason against EU Taxonomy Art.3,<br/>ICMA GBP, Climate Bonds Standard
    A->>A: Output structured decision:<br/>{ status, confidence, reason, sources }

    alt Bond GREEN and compliant
        A->>O: release_fulfillment(mpt_id)
        O->>L: EscrowFinish(fulfillment_hex, memo=decision_json)
        L-->>O: tesSUCCESS — issuer receives XRP
    else Bond NON-COMPLIANT
        A->>O: withhold_fulfillment(mpt_id)
        O->>L: Record non-compliance memo on-chain
        Note over L,Inv: cancel_after (maturity date) passes
        Inv->>L: EscrowCancel(mpt_id)
        L-->>Inv: tesSUCCESS — investor receives XRP refund
    else Confidence too low
        A->>A: Escalate to human reviewer
    end
```
