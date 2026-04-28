from eth_account import Account
from eth_account.messages import encode_defunct
from requests import HTTPError
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

# Collateral Token (Conditional Tokens) address - unchanged
CT_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

# V2 Exchange contract addresses (migrated from V1)
# Source: CLOB_V2.md
CTF_EXCHANGE = "0xE111180000d2663C0091e4f400237545B87B996B"  # V2 Exchange
CTF_EXCHANGE_NEG_RISK = "0xe2222d279d744050d28e00520010520000310F59"  # V2 NegRisk Exchange
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
CT_APPROVAL_OPERATORS = [CTF_EXCHANGE, CTF_EXCHANGE_NEG_RISK, NEG_RISK_ADAPTER]

CT_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "account", "type": "address"}, {"name": "operator", "type": "address"}],
        "name": "isApprovedForAll",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "constant": False,
        "inputs": [{"name": "operator", "type": "address"}, {"name": "approved", "type": "bool"}],
        "name": "setApprovalForAll",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]

SAFE_ABI = [
    {
        "inputs": [],
        "name": "nonce",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "to", "type": "address"},
            {"internalType": "uint256", "name": "value", "type": "uint256"},
            {"internalType": "bytes", "name": "data", "type": "bytes"},
            {"internalType": "uint8", "name": "operation", "type": "uint8"},
            {"internalType": "uint256", "name": "safeTxGas", "type": "uint256"},
            {"internalType": "uint256", "name": "baseGas", "type": "uint256"},
            {"internalType": "uint256", "name": "gasPrice", "type": "uint256"},
            {"internalType": "address", "name": "gasToken", "type": "address"},
            {"internalType": "address", "name": "refundReceiver", "type": "address"},
            {"internalType": "uint256", "name": "_nonce", "type": "uint256"},
        ],
        "name": "getTransactionHash",
        "outputs": [{"internalType": "bytes32", "name": "", "type": "bytes32"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "to", "type": "address"},
            {"internalType": "uint256", "name": "value", "type": "uint256"},
            {"internalType": "bytes", "name": "data", "type": "bytes"},
            {"internalType": "uint8", "name": "operation", "type": "uint8"},
            {"internalType": "uint256", "name": "safeTxGas", "type": "uint256"},
            {"internalType": "uint256", "name": "baseGas", "type": "uint256"},
            {"internalType": "uint256", "name": "gasPrice", "type": "uint256"},
            {"internalType": "address", "name": "gasToken", "type": "address"},
            {"internalType": "address", "name": "refundReceiver", "type": "address"},
            {"internalType": "bytes", "name": "signatures", "type": "bytes"},
        ],
        "name": "execTransaction",
        "outputs": [{"internalType": "bool", "name": "success", "type": "bool"}],
        "stateMutability": "payable",
        "type": "function",
    },
]


def ensure_conditional_token_approvals(
    private_key: str,
    proxy_address: str,
    chain_id: int,
    rpc_url: str | None = None,
) -> int:
    rpc = (rpc_url or "").strip()
    if not rpc:
        raise ValueError("POLYGON_RPC_URL is required for proxy-wallet approval bootstrap")

    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 30}))
    try:
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    except ValueError:
        pass

    signer = Account.from_key(private_key)
    proxy = Web3.to_checksum_address(proxy_address)
    ct_address = Web3.to_checksum_address(CT_ADDRESS)
    ct = w3.eth.contract(address=ct_address, abi=CT_ABI)
    safe = w3.eth.contract(address=proxy, abi=SAFE_ABI)

    approvals_set = 0
    for operator in CT_APPROVAL_OPERATORS:
        checksum_operator = Web3.to_checksum_address(operator)
        try:
            approved = bool(ct.functions.isApprovedForAll(proxy, checksum_operator).call())
        except HTTPError as exc:
            raise RuntimeError(f"POLYGON_RPC_URL rejected the approval check request: {exc}") from exc
        if approved:
            continue

        _approve_operator(
            w3=w3,
            signer_address=signer.address,
            private_key=private_key,
            safe=safe,
            ct=ct,
            ct_address=ct_address,
            operator=checksum_operator,
            chain_id=chain_id,
        )
        approvals_set += 1

    return approvals_set


def _approve_operator(
    *,
    w3: Web3,
    signer_address: str,
    private_key: str,
    safe,
    ct,
    ct_address: str,
    operator: str,
    chain_id: int,
) -> None:
    call_data = ct.functions.setApprovalForAll(operator, True)._encode_transaction_data()
    safe_nonce = int(safe.functions.nonce().call())
    safe_tx_hash = safe.functions.getTransactionHash(
        ct_address,
        0,
        call_data,
        0,
        0,
        0,
        0,
        ZERO_ADDRESS,
        ZERO_ADDRESS,
        safe_nonce,
    ).call()

    signed_message = Account.sign_message(
        encode_defunct(hexstr=Web3.to_hex(safe_tx_hash)),
        private_key=private_key,
    )
    safe_signature = (
        signed_message.r.to_bytes(32, "big")
        + signed_message.s.to_bytes(32, "big")
        + bytes([signed_message.v + 4])
    )

    exec_tx = safe.functions.execTransaction(
        ct_address,
        0,
        call_data,
        0,
        0,
        0,
        0,
        ZERO_ADDRESS,
        ZERO_ADDRESS,
        safe_signature,
    )

    tx_params = {
        "from": signer_address,
        "nonce": w3.eth.get_transaction_count(signer_address, "pending"),
        "chainId": chain_id,
        "gasPrice": int(w3.eth.gas_price),
    }
    try:
        tx_params["gas"] = int(exec_tx.estimate_gas({"from": signer_address}) * 1.2) + 50_000
    except Exception:
        tx_params["gas"] = 500_000

    signed_tx = w3.eth.account.sign_transaction(exec_tx.build_transaction(tx_params), private_key=private_key)
    tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    if receipt.status != 1:
        raise RuntimeError(f"Conditional token approval tx failed: {Web3.to_hex(tx_hash)}")
