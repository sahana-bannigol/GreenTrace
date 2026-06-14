"""
GreenTrace - XRPL Service Layer
Handles all XRPL operations: wallets, MPTs (green bonds), escrow, credentials
Network: XRPL Devnet
"""

import os
import json
import time
import hashlib
import secrets
from datetime import datetime

# The XRPL testnet server (s.altnet.rippletest.net:51234) serves an incomplete
# TLS chain which fails even with certifi's CA bundle. For testnet/demo purposes
# we disable SSL verification by patching httpx.AsyncClient globally.
# Production would use proper certificate pinning.
import httpx as _httpx

_orig_async_init = _httpx.AsyncClient.__init__

def _no_verify_async_init(self, *args, verify=True, **kwargs):
    _orig_async_init(self, *args, verify=False, **kwargs)

_httpx.AsyncClient.__init__ = _no_verify_async_init

from xrpl.clients import JsonRpcClient
from xrpl.wallet import generate_faucet_wallet, Wallet
from xrpl.models.transactions import (
    MPTokenIssuanceCreate,
    MPTokenIssuanceCreateFlag,
    MPTokenAuthorize,
    EscrowCreate,
    EscrowFinish,
    Payment,
    CredentialCreate,
    CredentialAccept,
)
from xrpl.models.amounts import MPTAmount
from xrpl.models.requests import AccountInfo, AccountObjects, LedgerEntry, Ledger, Tx
from xrpl.utils import xrp_to_drops, drops_to_xrp
from xrpl.transaction import submit_and_wait

TESTNET_URL = "https://s.devnet.rippletest.net:51234"
TESTNET_EXPLORER = "https://devnet.xrpl.org"


def get_client():
    return JsonRpcClient(TESTNET_URL)


def text_to_hex(text: str) -> str:
    return text.encode("utf-8").hex().upper()


def hex_to_text(hex_str: str) -> str:
    try:
        return bytes.fromhex(hex_str).decode("utf-8")
    except Exception:
        return hex_str


# XLS-70 credential constants (defined after text_to_hex)
CREDENTIAL_TYPE_HEX = text_to_hex("GREENTRACE_GREEN_BOND")
CREDENTIAL_URI_META = {
    "Bond_Status": "Green_Verified",
    "Standard": "EU_Green_Bond_Standard",
    "Verified_By": "KPMG",
    "EU_Taxonomy": "Pass",
    "ICMA": "Pass",
}
CREDENTIAL_URI_HEX = text_to_hex(json.dumps(CREDENTIAL_URI_META, separators=(",", ":")))


# ─────────────────────────────────────────────────────────────────
# WALLETS
# ─────────────────────────────────────────────────────────────────

def create_wallet():
    """Fund a fresh testnet wallet from the faucet. Returns dict with address + seed."""
    client = get_client()
    wallet = generate_faucet_wallet(client, debug=False)

    # Fetch balance
    try:
        resp = client.request(AccountInfo(
            account=wallet.classic_address,
            ledger_index="validated"
        ))
        balance_drops = resp.result["account_data"]["Balance"]
        balance_xrp = float(drops_to_xrp(balance_drops))
    except Exception:
        balance_xrp = 100.0

    return {
        "address": wallet.classic_address,
        "seed": wallet.seed,
        "public_key": wallet.public_key,
        "balance_xrp": balance_xrp,
        "explorer_url": f"{TESTNET_EXPLORER}/accounts/{wallet.classic_address}",
    }


def get_wallet_balance(address: str) -> float:
    try:
        client = get_client()
        resp = client.request(AccountInfo(account=address, ledger_index="validated"))
        return float(drops_to_xrp(resp.result["account_data"]["Balance"]))
    except Exception:
        return 0.0


def get_wallet_mpt_holdings(address: str):
    """Return list of MPT objects held by this wallet."""
    try:
        client = get_client()
        resp = client.request(AccountObjects(
            account=address,
            ledger_index="validated",
            type="mptoken"
        ))
        return resp.result.get("account_objects", [])
    except Exception:
        return []


def get_mpt_balance(address: str, mpt_issuance_id: str) -> float:
    """Return MPT balance scaled to human units (AssetScale=2)."""
    holdings = get_wallet_mpt_holdings(address)
    for h in holdings:
        if h.get("MPTokenIssuanceID") == mpt_issuance_id:
            return int(h.get("MPTAmount", "0")) / 100
    return 0.0


# ─────────────────────────────────────────────────────────────────
# GREEN BOND ISSUANCE  (MPT – XLS-33)
# ─────────────────────────────────────────────────────────────────

def issue_green_bond(issuer_seed: str, bond_data: dict) -> dict:
    """
    Mint a green bond as a Multi-Purpose Token on XRPL testnet.

    bond_data keys:
        ticker, name, issuer_name, isin, coupon, maturity,
        total_bonds, use_of_proceeds, frameworks
    """
    client = get_client()
    issuer_wallet = Wallet.from_seed(issuer_seed)

    # XLS-89d standard: max 9 top-level metadata fields for discoverability
    frameworks = bond_data.get("frameworks", ["ICMA Green Bond Principles"])
    verify_score = _compute_verify_score(bond_data)
    # on-chain metadata — max 9 top-level fields (XLS-89d standard)
    green_obj = {
        "isin":               bond_data.get("isin", ""),
        "coupon":             bond_data.get("coupon", "3.25"),
        "maturity":           bond_data.get("maturity", "2034"),
        "use_of_proceeds":    bond_data.get("use_of_proceeds", "Renewable Energy"),
        "frameworks":         frameworks,
        "icma_pass":          "ICMA Green Bond Principles" in frameworks,
        "eu_taxonomy_pass":   "EU Taxonomy" in frameworks,
        "climate_bonds_pass": "Climate Bonds Standard" in frameworks,
        "verify_score":       verify_score,
        "issued_date":        datetime.utcnow().isoformat(),
    }
    onchain_meta = {                           # exactly 6 top-level fields
        "ticker":      bond_data.get("ticker", "GRN"),
        "name":        bond_data.get("name", "Green Bond"),
        "asset_class": "rwa",
        "icon":        "https://greentrace.finance/icon.png",
        "issuer_name": bond_data.get("issuer_name", ""),
        "green":       green_obj,
    }
    # local metadata = flat dict with all fields for template convenience
    metadata = {**onchain_meta, **green_obj}

    metadata_hex = text_to_hex(json.dumps(onchain_meta, separators=(",", ":")))

    # total_bonds × 100 base units (AssetScale = 2, so 1 bond = 100 units)
    total_bonds = int(bond_data.get("total_bonds", 1000))
    maximum_amount = str(total_bonds * 100)

    tx = MPTokenIssuanceCreate(
        account=issuer_wallet.classic_address,
        asset_scale=2,
        maximum_amount=maximum_amount,
        transfer_fee=0,
        mptoken_metadata=metadata_hex,
        flags=(
            MPTokenIssuanceCreateFlag.TF_MPT_CAN_ESCROW
            | MPTokenIssuanceCreateFlag.TF_MPT_CAN_TRADE
            | MPTokenIssuanceCreateFlag.TF_MPT_CAN_TRANSFER
        ),
    )

    resp = submit_and_wait(tx, client, issuer_wallet)
    result = resp.result

    if result.get("meta", {}).get("TransactionResult") != "tesSUCCESS":
        raise Exception(f"MPT issuance failed: {result.get('meta', {}).get('TransactionResult')}")

    tx_hash = result.get("hash")
    mpt_issuance_id = result.get("meta", {}).get("mpt_issuance_id")

    return {
        "mpt_issuance_id": mpt_issuance_id,
        "tx_hash": tx_hash,
        "issuer": issuer_wallet.classic_address,
        "metadata": metadata,
        "total_bonds": total_bonds,
        "explorer_url": f"{TESTNET_EXPLORER}/transactions/{tx_hash}",
    }


def _compute_verify_score(bond_data: dict) -> int:
    """Compute a continuous green verification score (0-100)."""
    score = 50
    frameworks = bond_data.get("frameworks", [])
    if "ICMA Green Bond Principles" in frameworks:
        score += 15
    if "EU Taxonomy" in frameworks:
        score += 15
    if "Climate Bonds Standard" in frameworks:
        score += 10
    if bond_data.get("use_of_proceeds"):
        score += 5
    if bond_data.get("isin"):
        score += 5
    return min(score, 100)


def get_mpt_info(mpt_issuance_id: str) -> dict:
    """Fetch live MPT issuance data from the XRPL ledger."""
    try:
        client = get_client()
        resp = client.request(LedgerEntry(
            mpt_issuance=mpt_issuance_id,
            ledger_index="validated"
        ))
        node = resp.result.get("node", {})

        # Decode metadata
        raw_meta = node.get("MPTokenMetadata", "")
        try:
            meta = json.loads(hex_to_text(raw_meta))
        except Exception:
            meta = {}

        outstanding_raw = int(node.get("OutstandingAmount", "0"))
        maximum_raw = int(node.get("MaximumAmount", "0"))
        asset_scale = node.get("AssetScale", 2)
        divisor = 10 ** asset_scale

        return {
            "mpt_issuance_id": mpt_issuance_id,
            "issuer": node.get("Issuer"),
            "outstanding_bonds": outstanding_raw / divisor,
            "maximum_bonds": maximum_raw / divisor,
            "asset_scale": asset_scale,
            "metadata": meta,
            "verify_score": meta.get("verify_score", 0),
        }
    except Exception as e:
        return {"error": str(e)}


# ─────────────────────────────────────────────────────────────────
# XLS-70 CREDENTIALS  (Green investor verification)
# ─────────────────────────────────────────────────────────────────

def issue_and_accept_credential(verifier_seed: str, buyer_seed: str) -> dict:
    """
    KPMG verifier issues a green bond credential to buyer, buyer auto-accepts.
    Uses XLS-70 CredentialCreate + CredentialAccept on XRPL devnet.
    """
    client = get_client()
    verifier_wallet = Wallet.from_seed(verifier_seed)
    buyer_wallet = Wallet.from_seed(buyer_seed)

    # CredentialCreate: verifier grants credential to buyer
    create_tx = CredentialCreate(
        account=verifier_wallet.classic_address,
        subject=buyer_wallet.classic_address,
        credential_type=CREDENTIAL_TYPE_HEX,
        uri=CREDENTIAL_URI_HEX,
    )
    create_resp = submit_and_wait(create_tx, client, verifier_wallet)
    if create_resp.result.get("meta", {}).get("TransactionResult") != "tesSUCCESS":
        raise Exception(f"CredentialCreate failed: {create_resp.result.get('meta',{}).get('TransactionResult')}")
    create_tx_hash = create_resp.result.get("hash")

    # CredentialAccept: buyer accepts the credential
    accept_tx = CredentialAccept(
        account=buyer_wallet.classic_address,
        issuer=verifier_wallet.classic_address,
        credential_type=CREDENTIAL_TYPE_HEX,
    )
    accept_resp = submit_and_wait(accept_tx, client, buyer_wallet)
    if accept_resp.result.get("meta", {}).get("TransactionResult") != "tesSUCCESS":
        raise Exception(f"CredentialAccept failed: {accept_resp.result.get('meta',{}).get('TransactionResult')}")
    accept_tx_hash = accept_resp.result.get("hash")

    return {
        "create_tx_hash": create_tx_hash,
        "accept_tx_hash": accept_tx_hash,
        "subject": buyer_wallet.classic_address,
        "issuer": verifier_wallet.classic_address,
        "credential_type": "GREENTRACE_GREEN_BOND",
        "metadata": CREDENTIAL_URI_META,
        "explorer_create": f"{TESTNET_EXPLORER}/transactions/{create_tx_hash}",
        "explorer_accept": f"{TESTNET_EXPLORER}/transactions/{accept_tx_hash}",
    }


def check_has_credential(buyer_address: str, verifier_address: str) -> bool:
    """Return True if buyer has an accepted GREENTRACE_GREEN_BOND credential from verifier."""
    try:
        client = get_client()
        resp = client.request(AccountObjects(
            account=buyer_address,
            ledger_index="validated",
            type="credential",
        ))
        for obj in resp.result.get("account_objects", []):
            if (obj.get("Issuer") == verifier_address
                    and obj.get("CredentialType", "").upper() == CREDENTIAL_TYPE_HEX.upper()
                    and obj.get("Flags", 0) & 0x00010000):  # lsfAccepted
                return True
        return False
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────
# BOND PURCHASE  (MPTokenAuthorize + Escrow + MPT Payment)
# ─────────────────────────────────────────────────────────────────

def buy_bond(buyer_seed: str, issuer_seed: str, mpt_issuance_id: str, quantity: int,
             verifier_address: str = None) -> dict:
    """
    Full bond purchase flow:
      1. Buyer authorizes MPT receipt (MPTokenAuthorize)
      2. Buyer creates XRP escrow → issuer  (crypto-condition only, no time lock)
      3. Issuer finishes escrow by revealing fulfillment (next ledger)
      4. Issuer sends MPT tokens to buyer (Payment with MPTAmount)
      5. Returns TX hashes + certificate data
    """
    client = get_client()
    buyer_wallet = Wallet.from_seed(buyer_seed)
    issuer_wallet = Wallet.from_seed(issuer_seed)

    # ── Credential gate ───────────────────────────────────────────
    if verifier_address:
        if not check_has_credential(buyer_wallet.classic_address, verifier_address):
            raise Exception(
                "CREDENTIAL_REQUIRED: Wallet has no KPMG green investor credential. "
                "Only verified investors may purchase green bonds on GreenTrace."
            )

    # ── Step 1: MPTokenAuthorize ──────────────────────────────────
    auth_tx = MPTokenAuthorize(
        account=buyer_wallet.classic_address,
        mptoken_issuance_id=mpt_issuance_id,
    )
    auth_resp = submit_and_wait(auth_tx, client, buyer_wallet)
    if auth_resp.result.get("meta", {}).get("TransactionResult") != "tesSUCCESS":
        raise Exception("MPTokenAuthorize failed")
    auth_tx_hash = auth_resp.result.get("hash")

    # ── Step 2: Escrow Create ──────────────────────────────────────
    # Crypto-condition (SHA-256 preimage). No FinishAfter — condition alone
    # is sufficient and avoids tecNO_PERMISSION from ledger clock skew.
    preimage = secrets.token_bytes(32)
    condition_hash = hashlib.sha256(preimage).digest()
    condition = bytes([0xA0, 0x25, 0x80, 0x20]) + condition_hash + bytes([0x81, 0x01, 0x20])
    fulfillment = bytes([0xA0, 0x22, 0x80, 0x20]) + preimage
    condition_hex = condition.hex().upper()
    fulfillment_hex = fulfillment.hex().upper()

    # XRP "price": 1 XRP per bond (symbolic on devnet)
    xrp_amount = xrp_to_drops(max(1, quantity))

    # Devnet ledger clock can be offset from local UTC (observed: +3600s).
    # Always anchor CancelAfter to the live ledger close_time to avoid
    # tecNO_PERMISSION from submitting a past-dated expiry.
    ledger_resp = client.request(Ledger(ledger_index="validated"))
    ledger_close = ledger_resp.result["ledger"]["close_time"]
    cancel_after_ripple = ledger_close + 300  # 5 min window from current ledger time

    escrow_create_tx = EscrowCreate(
        account=buyer_wallet.classic_address,
        amount=xrp_amount,
        destination=issuer_wallet.classic_address,
        cancel_after=cancel_after_ripple,
        condition=condition_hex,
    )
    escrow_resp = submit_and_wait(escrow_create_tx, client, buyer_wallet)
    if escrow_resp.result.get("meta", {}).get("TransactionResult") != "tesSUCCESS":
        raise Exception("EscrowCreate failed")

    # Sequence lives in tx_json; fall back to top-level Sequence field
    escrow_sequence = (
        escrow_resp.result.get("tx_json", {}).get("Sequence")
        or escrow_resp.result.get("Sequence")
    )
    escrow_tx_hash = escrow_resp.result.get("hash")

    # ── Step 3: Finish escrow (next ledger, ~4s) ───────────────────
    time.sleep(4)

    escrow_finish_tx = EscrowFinish(
        account=issuer_wallet.classic_address,
        owner=buyer_wallet.classic_address,
        offer_sequence=escrow_sequence,
        condition=condition_hex,
        fulfillment=fulfillment_hex,
    )
    finish_resp = submit_and_wait(escrow_finish_tx, client, issuer_wallet)
    if finish_resp.result.get("meta", {}).get("TransactionResult") != "tesSUCCESS":
        raise Exception("EscrowFinish failed")
    escrow_finish_tx_hash = finish_resp.result.get("hash")

    # ── Step 4: Transfer MPT to buyer ─────────────────────────────
    # quantity bonds = quantity * 100 base units (AssetScale 2)
    mpt_units = str(quantity * 100)

    payment_tx = Payment(
        account=issuer_wallet.classic_address,
        destination=buyer_wallet.classic_address,
        amount=MPTAmount(mpt_issuance_id=mpt_issuance_id, value=mpt_units),
    )
    payment_resp = submit_and_wait(payment_tx, client, issuer_wallet)
    if payment_resp.result.get("meta", {}).get("TransactionResult") != "tesSUCCESS":
        raise Exception("MPT Payment failed")
    payment_tx_hash = payment_resp.result.get("hash")

    return {
        "payment_tx_hash": payment_tx_hash,
        "auth_tx_hash": auth_tx_hash,
        "escrow_create_tx_hash": escrow_tx_hash,
        "escrow_finish_tx_hash": escrow_finish_tx_hash,
        "buyer": buyer_wallet.classic_address,
        "quantity": quantity,
        "mpt_issuance_id": mpt_issuance_id,
        "explorer_url": f"{TESTNET_EXPLORER}/transactions/{payment_tx_hash}",
    }


# ─────────────────────────────────────────────────────────────────
# CREDENTIAL SIMULATION (Green verification badge)
# Production: would use XLS-70 on devnet/mainnet
# ─────────────────────────────────────────────────────────────────

def issue_green_credential(bond: dict) -> dict:
    """
    Simulates issuing an on-chain green credential for a bond.
    In production, this would use XLS-70 CredentialCreate on devnet.
    """
    credential_data = {
        "credential_type": "GREENTRACE_GREEN_VERIFIED",
        "bond_name": bond.get("metadata", {}).get("name"),
        "mpt_issuance_id": bond.get("mpt_issuance_id"),
        "issuer": bond.get("issuer"),
        "verify_score": bond.get("verify_score", 0),
        "frameworks_passed": [
            f for f in [
                "ICMA Green Bond Principles" if bond.get("metadata", {}).get("icma_pass") else None,
                "EU Taxonomy" if bond.get("metadata", {}).get("eu_taxonomy_pass") else None,
                "Climate Bonds Standard" if bond.get("metadata", {}).get("climate_bonds_pass") else None,
            ] if f
        ],
        "issued_at": datetime.utcnow().isoformat(),
        "status": "VERIFIED",
    }

    # Generate a deterministic credential hash from bond data
    credential_hash = hashlib.sha256(
        json.dumps(credential_data, sort_keys=True).encode()
    ).hexdigest().upper()

    credential_data["credential_hash"] = credential_hash
    return credential_data
