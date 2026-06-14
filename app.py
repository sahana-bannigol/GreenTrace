"""
GreenTrace – Flask backend
Tokenized green bonds on the XRP Ledger
"""

import json
import os
from datetime import datetime
from flask import Flask, render_template, request, jsonify, abort

from xrpl_service import (
    create_wallet,
    get_wallet_balance,
    get_wallet_mpt_holdings,
    get_mpt_balance,
    issue_green_bond,
    get_mpt_info,
    buy_bond,
    issue_green_credential,
    issue_and_accept_credential,
    check_has_credential,
    TESTNET_EXPLORER,
    CREDENTIAL_URI_META,
)

app = Flask(__name__)
app.secret_key = "greentrace-xrpl-2026"

BONDS_DB = os.path.join(os.path.dirname(__file__), "bonds_db.json")
VERIFIER_FILE = os.path.join(os.path.dirname(__file__), "verifier_wallet.json")


def get_verifier_wallet() -> dict:
    """Load the KPMG verifier wallet; create and fund it if missing, incomplete, or gone from devnet."""
    if os.path.exists(VERIFIER_FILE):
        try:
            with open(VERIFIER_FILE) as f:
                data = json.load(f)
            if data.get("seed") and data.get("address"):
                # Confirm the account still exists on the devnet ledger (devnet resets wipe it)
                if get_wallet_balance(data["address"]) > 0:
                    return data
        except Exception:
            pass
    # First run, stale file, or devnet reset — fund a fresh testnet wallet
    w = create_wallet()
    data = {"seed": w["seed"], "address": w["address"]}
    with open(VERIFIER_FILE, "w") as f:
        json.dump(data, f)
    return data


# ── Database helpers ──────────────────────────────────────────────

def load_bonds():
    try:
        with open(BONDS_DB, "r") as f:
            return json.load(f)
    except Exception:
        return []


def save_bonds(bonds):
    with open(BONDS_DB, "w") as f:
        json.dump(bonds, f, indent=2)


def find_bond(mpt_id):
    return next((b for b in load_bonds() if b.get("mpt_issuance_id") == mpt_id), None)


# ── Page routes ───────────────────────────────────────────────────

@app.route("/")
def dashboard():
    bonds = load_bonds()
    # Attach live XRPL data to each bond (scrape from ledger)
    for bond in bonds:
        if bond.get("mpt_issuance_id"):
            live = get_mpt_info(bond["mpt_issuance_id"])
            bond["outstanding_bonds"] = live.get("outstanding_bonds", 0)
            bond["maximum_bonds"] = live.get("maximum_bonds", bond.get("total_bonds", 0))
    return render_template("dashboard.html", bonds=bonds, explorer=TESTNET_EXPLORER)


@app.route("/issuer")
def issuer():
    return render_template("issuer.html", explorer=TESTNET_EXPLORER)


@app.route("/marketplace")
def marketplace():
    bonds = load_bonds()
    return render_template("marketplace.html", bonds=bonds, explorer=TESTNET_EXPLORER)


@app.route("/bond/<mpt_id>")
def bond_detail(mpt_id):
    bond = find_bond(mpt_id)
    if not bond:
        abort(404)
    live = get_mpt_info(mpt_id)
    return render_template("bond_detail.html", bond=bond, live=live, explorer=TESTNET_EXPLORER)


@app.route("/certificate/<tx_hash>")
def certificate(tx_hash):
    bonds = load_bonds()
    bond = None
    trade = None
    for b in bonds:
        for t in b.get("trades", []):
            if t.get("payment_tx_hash") == tx_hash:
                bond = b
                trade = t
                break
    if not bond:
        abort(404)

    credential = issue_green_credential(bond)
    return render_template(
        "certificate.html",
        bond=bond,
        trade=trade,
        credential=credential,
        tx_hash=tx_hash,
        explorer=TESTNET_EXPLORER,
    )


# ── API: Wallets ──────────────────────────────────────────────────

@app.route("/api/wallet/create", methods=["POST"])
def api_create_wallet():
    try:
        wallet = create_wallet()
        return jsonify({"success": True, "wallet": wallet})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/wallet/info", methods=["POST"])
def api_wallet_info():
    try:
        data = request.json or {}
        address = data.get("address")
        balance = get_wallet_balance(address)
        holdings = get_wallet_mpt_holdings(address)
        return jsonify({"success": True, "balance_xrp": balance, "mpt_holdings": holdings})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ── API: Bonds ────────────────────────────────────────────────────

@app.route("/api/bond/issue", methods=["POST"])
def api_issue_bond():
    try:
        data = request.json or {}
        issuer_seed = data.get("issuer_seed")
        # Accept fields at top level (from the UI form) or nested under bond_data
        bond_data = data.get("bond_data") or {k: v for k, v in data.items() if k != "issuer_seed"}

        if not issuer_seed:
            return jsonify({"success": False, "error": "issuer_seed required"}), 400

        result = issue_green_bond(issuer_seed, bond_data)

        # Persist to local DB
        bonds = load_bonds()
        entry = {
            "mpt_issuance_id": result["mpt_issuance_id"],
            "issuance_tx_hash": result["tx_hash"],
            "issuer": result["issuer"],
            "issuer_seed": issuer_seed,   # stored for demo only
            "metadata": result["metadata"],
            "verify_score": result["metadata"].get("verify_score", 0),
            "status": "verified",
            "total_bonds": result["total_bonds"],
            "issued_at": datetime.utcnow().isoformat(),
            "trades": [],
        }
        bonds.append(entry)
        save_bonds(bonds)

        return jsonify({
            "success": True,
            "mpt_issuance_id": result["mpt_issuance_id"],
            "tx_hash": result["tx_hash"],
            "issuer": result["issuer"],
            "metadata": result["metadata"],
            "total_bonds": result["total_bonds"],
            "explorer_url": result["explorer_url"],
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/bonds", methods=["GET"])
def api_list_bonds():
    bonds = load_bonds()
    # Scrape live data from ledger
    for bond in bonds:
        if bond.get("mpt_issuance_id"):
            live = get_mpt_info(bond["mpt_issuance_id"])
            bond["outstanding_bonds"] = live.get("outstanding_bonds", 0)
    return jsonify({"success": True, "bonds": bonds})


@app.route("/api/bond/<mpt_id>", methods=["GET"])
def api_get_bond(mpt_id):
    bond = find_bond(mpt_id)
    if not bond:
        return jsonify({"success": False, "error": "Bond not found"}), 404
    live = get_mpt_info(mpt_id)
    bond["live"] = live
    return jsonify({"success": True, "bond": bond})


@app.route("/api/bond/buy", methods=["POST"])
def api_buy_bond():
    try:
        data = request.json or {}
        buyer_seed = data.get("buyer_seed")
        mpt_issuance_id = data.get("mpt_issuance_id")
        quantity = int(data.get("quantity", 1))

        if not buyer_seed or not mpt_issuance_id:
            return jsonify({"success": False, "error": "buyer_seed and mpt_issuance_id required"}), 400

        bond = find_bond(mpt_issuance_id)
        if not bond:
            return jsonify({"success": False, "error": "Bond not found in database"}), 404

        issuer_seed = bond.get("issuer_seed")
        if not issuer_seed:
            return jsonify({"success": False, "error": "Issuer seed not available"}), 400

        verifier = get_verifier_wallet()
        result = buy_bond(buyer_seed, issuer_seed, mpt_issuance_id, quantity,
                          verifier_address=verifier["address"])

        # Record trade
        trade = {
            "payment_tx_hash": result["payment_tx_hash"],
            "auth_tx_hash": result["auth_tx_hash"],
            "escrow_create_tx_hash": result["escrow_create_tx_hash"],
            "escrow_finish_tx_hash": result["escrow_finish_tx_hash"],
            "buyer": result["buyer"],
            "quantity": quantity,
            "timestamp": datetime.utcnow().isoformat(),
            "explorer_url": result["explorer_url"],
        }
        bonds = load_bonds()
        for b in bonds:
            if b.get("mpt_issuance_id") == mpt_issuance_id:
                b.setdefault("trades", []).append(trade)
                break
        save_bonds(bonds)

        return jsonify({
            "success": True,
            "payment_tx_hash": result["payment_tx_hash"],
            "auth_tx_hash": result["auth_tx_hash"],
            "escrow_create_tx_hash": result["escrow_create_tx_hash"],
            "escrow_finish_tx_hash": result["escrow_finish_tx_hash"],
            "buyer": result["buyer"],
            "quantity": quantity,
            "mpt_issuance_id": mpt_issuance_id,
            "explorer_url": result["explorer_url"],
            "trade": trade,
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/wallet/<address>/portfolio", methods=["GET"])
def api_wallet_portfolio(address):
    """Return XRP balance + enriched MPT holdings for a wallet address."""
    try:
        balance = get_wallet_balance(address)
        holdings_raw = get_wallet_mpt_holdings(address)
        bonds_by_id = {b["mpt_issuance_id"]: b for b in load_bonds()}

        holdings = []
        for h in holdings_raw:
            mpt_id = h.get("MPTokenIssuanceID")
            quantity = int(h.get("MPTAmount", "0")) / 100
            bond = bonds_by_id.get(mpt_id, {})
            meta = bond.get("metadata", {})
            holdings.append({
                "mpt_issuance_id": mpt_id,
                "quantity": quantity,
                "bond_name": meta.get("name", "Unknown Bond"),
                "ticker": meta.get("ticker", "???"),
                "coupon": meta.get("coupon", "—"),
                "maturity": meta.get("maturity", "—"),
                "issuer_name": meta.get("issuer_name", "—"),
                "use_of_proceeds": meta.get("use_of_proceeds", "—"),
                "verify_score": bond.get("verify_score", 0),
                "bond_url": f"/bond/{mpt_id}",
            })

        return jsonify({
            "success": True,
            "address": address,
            "balance_xrp": balance,
            "holdings": holdings,
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/bond/verify/<mpt_id>", methods=["GET"])
def api_verify_bond(mpt_id):
    """Return live verification data scraped from XRPL."""
    live = get_mpt_info(mpt_id)
    bond = find_bond(mpt_id)
    if bond:
        live["local_metadata"] = bond.get("metadata", {})
        live["verify_score"] = bond.get("verify_score", 0)
        live["trades"] = len(bond.get("trades", []))
    return jsonify({"success": True, "verification": live})


# ── API: Credentials (XLS-70) ─────────────────────────────────────

@app.route("/api/credential/verifier", methods=["GET"])
def api_get_verifier():
    """Return the KPMG verifier wallet address."""
    try:
        v = get_verifier_wallet()
        return jsonify({"success": True, "address": v["address"],
                        "credential_meta": CREDENTIAL_URI_META})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/credential/issue", methods=["POST"])
def api_issue_credential():
    """Issue an XLS-70 green investor credential to a buyer wallet (Wallet A)."""
    try:
        data = request.json or {}
        buyer_seed = data.get("buyer_seed")
        if not buyer_seed:
            return jsonify({"success": False, "error": "buyer_seed required"}), 400
        verifier = get_verifier_wallet()
        result = issue_and_accept_credential(verifier["seed"], buyer_seed)
        return jsonify({"success": True, **result})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/credential/check", methods=["POST"])
def api_check_credential():
    """Check if a wallet address holds a valid green investor credential."""
    try:
        data = request.json or {}
        address = data.get("address")
        if not address:
            return jsonify({"success": False, "error": "address required"}), 400
        verifier = get_verifier_wallet()
        has_cred = check_has_credential(address, verifier["address"])
        return jsonify({
            "success": True,
            "address": address,
            "has_credential": has_cred,
            "verifier": verifier["address"],
            "credential_type": "GREENTRACE_GREEN_BOND",
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("  GREENTRACE – Tokenized Green Bonds on XRPL")
    print("  http://localhost:5000")
    print("=" * 60 + "\n")
    app.run(debug=True, port=5000)
