"""RM Watcher — vigilancia 24/7 ON-CHAIN ONLY para RM apuestas inversas.

CRÍTICO: este watcher es 100% PASIVO ON-CHAIN.
- NO autentica al backend RM
- NO scrapea web RM (ningún m1.rmXXXX.buzz)
- NO toca grupo Telegram con session de Fernando
- NO hace DNS lookups frecuentes a dominios RM
- NO emite ninguna huella vinculable a la cuenta de Fernando

Solo consulta TronGrid API pública (rate limit 100K/día free, sin key).

Watchers operativos (Capa 1 — on-chain TRON only):
  RM1: Hot wallet operador `TQZv...DDkUJ` USDT balance + activity rate
  RM2: Treasury `TGT...8888888` USDT balance (drain detection >$30K en 6h = exit signal)
  RM3: Cash-out wallet `TH8fsKYV...LXbFm` outflows (where treasury goes final)
  RM4: Hot wallet inbound rate (deposits/hour) — cero deposits 12h = pre-rug
  RM5: Treasury outflow grandes (>$50K) = cash-out anómalo
  RM6: TRX balance hot wallet (operador queda sin gas = preview to abandon)

Capa 2 — Wallet Fernando (settlement tracking):
  RMP1: Wallet Fernando TRON recibe USDT desde sistema RM = settlement confirmado

Run schedule:
  Cada 6h: full sweep (cron 41 */6 * * *)
  Cada 1h: light (RM1 + RM2 balance check only) — para drain rápido detection
"""
import os
import sys
import json
import time
import io
import requests
from pathlib import Path
from datetime import datetime, timezone

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ============= CONFIG =============

# TRON addresses RM cluster (verificadas 2026-05-23/24)
HOT_WALLET     = "TQZvYW7Am7rm7R2tbCVq84135zUXADDkUJ"   # hot wallet operador (depósito Fernando)
HOT_WALLET_2   = "THavRzXk7Tt43GeU4Sxbig5VJakZYnN39e"   # 2nd hot wallet del cluster (descubierta 2026-05-24, 1430 tx, ~$15K/día IN, sweep a treasury vanity)
HOT_WALLET_3   = "TLaGjwhvA8XQYSxFAcAXy7Dvuue9eGYitv"   # 3rd hot wallet recurrente (bridge interno, sender frecuente a HOT_WALLET_2 — confirmada 2026-05-24)
TREASURY       = "TGTdkJTwFALEmj889t95uosoxSv8888888"   # treasury vanity chino (recibe sweeps)
CASH_OUT       = "TH8fsKYVTQh8tmEyUr6PgzbMjoD54LXbFm"   # wallet probable cash-out final
PAYOUT_HUB     = "TCciSR7N3mpCPKkXdbkHU3xmdNit6nHUwF"   # hub de payout RM (paga a usuarios; 2026-06-01)
HUB_FEEDER     = "TPUtZUmCcYm2gWhTeD9VXYiVUepQRdPauw"   # alimenta el payout hub
USDT_TRC20     = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

# Colectoras de depósito que RM le asignó a Fernando por período (todas barren 100% al treasury,
# verificado on-chain 2026-06-01). Son COMPARTIDAS (pooled, no exclusivas) — sirven como anclas
# adicionales de salud del canal de captación de RM.
DEPOSIT_COLLECTORS = {
    "TQZvYW7Am7rm7R2tbCVq84135zUXADDkUJ": "RM inv mayo26 (=HOT1)",
    "THavRzXk7Tt43GeU4Sxbig5VJakZYnN39e": "RM mayo26 B (=HOT2)",
    "TCrXsS2ZwXHtAQFavpHbuYgzScaJXGEvyk": "RM dic25",
    "TS9ZYS5y17GuyNpRqC2ZK4C85R45qZaVeu": "RM 20feb",
    "TXc73hqEzuwcQswKPF4HFYbms3EYyWH6PK": "RM inversas 3",
    "TJy6JgH2oZQXxNAmVhj667Gd8D5DMYFBR1": "RM inversas 2",
}

# Cluster RM completo (para iteración en checks)
RM_CLUSTER_HOT = [HOT_WALLET, HOT_WALLET_2, HOT_WALLET_3]
# Wallets que pagan a usuarios (fuente de settlements) — usado por RMP1
RM_PAYERS = [HOT_WALLET, TREASURY, CASH_OUT, PAYOUT_HUB, HUB_FEEDER]

# Fernando wallet TRON (la misma EVM-format que BSC pero TRON usa base58)
# IMPORTANT: Fernando NO me dio su wallet TRON personal. Por ahora tracking
# del depósito sale por Binance hot wallet. Cuando Fernando me confirme su
# wallet TRON personal, actualizar este valor.
FERNANDO_TRON = os.environ.get("FERNANDO_TRON_WALLET", "TLtozDSkukon7AF8Uk2J14h9uTkFqT44ra")

# Telegram supergroup
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
# Usa thread temporal compartido con Bitradex hasta que Fernando cree topic RM dedicado
TELEGRAM_THREAD_ID = os.environ.get("TELEGRAM_RM_THREAD_ID", os.environ.get("TELEGRAM_BITRADEX_THREAD_ID", ""))

STATE_FILE = Path(__file__).parent / "state.json"

# ============= HELPERS =============

def utc_now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}
    return {}

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))

# ============= TRON API (TronGrid pública, sin key) =============

def trongrid(endpoint, retry=3):
    """Llama TronGrid API con retry vocal."""
    last_err = None
    url = f"https://api.trongrid.io{endpoint}"
    for i in range(retry):
        try:
            r = requests.get(url, headers={"Accept": "application/json", "User-Agent": "rm-watcher/1.0"}, timeout=25)
            if r.status_code == 200:
                return r.json()
            last_err = f"HTTP {r.status_code}"
        except Exception as e:
            last_err = str(e)
        time.sleep(0.5 * (i + 1))
    print(f"  [TronGrid FAIL] {endpoint} after {retry} retries: {last_err}", file=sys.stderr, flush=True)
    return None

def get_account_info(addr):
    """Account TRX balance + TRC20 holdings + activity timestamps."""
    d = trongrid(f"/v1/accounts/{addr}")
    if not d or not d.get("data"):
        return None
    a = d["data"][0]
    trc20 = {}
    for t in a.get("trc20", []):
        for k, v in t.items():
            trc20[k] = int(v) / 1e6
    return {
        "trx": a.get("balance", 0) / 1e6,
        "created_ts": a.get("create_time", 0) / 1000,
        "last_op_ts": a.get("latest_opration_time", 0) / 1000,
        "usdt": trc20.get(USDT_TRC20, 0),
    }

def get_trc20_tx(addr, limit=50):
    """Últimas N tx USDT TRC20 de la address."""
    d = trongrid(f"/v1/accounts/{addr}/transactions/trc20?limit={limit}&contract_address={USDT_TRC20}")
    if not d:
        return None
    return d.get("data", [])

# ============= TELEGRAM ALERT =============

def send_alert(message, severity="INFO"):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[NO TG] {severity}: {message[:200]}")
        return False
    prefix = {
        "CRIT": "🚨 CRITICAL",
        "WARN": "⚠️ WARN",
        "INFO": "ℹ️ INFO",
        "OPP": "🎯 SETTLEMENT",
    }.get(severity, severity)
    text = f"{prefix} · RM Watcher\n\n{message}\n\n_{utc_now_iso()}_"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    if TELEGRAM_THREAD_ID:
        payload["message_thread_id"] = int(TELEGRAM_THREAD_ID)
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json=payload,
            timeout=20,
        )
        return r.status_code == 200
    except Exception as e:
        print(f"[TG EXC] {e}", file=sys.stderr)
        return False

# ============= WATCHERS =============

def check_rm1_hot_wallet(state):
    """RM1: Hot wallet operador balance + activity."""
    info = get_account_info(HOT_WALLET)
    if not info:
        print(f"  RM1 SKIP: cannot fetch", file=sys.stderr)
        return
    prev_usdt = state.get("rm1_hot_usdt")
    prev_last_op = state.get("rm1_hot_last_op")
    state["rm1_hot_usdt"] = info["usdt"]
    state["rm1_hot_trx"] = info["trx"]
    state["rm1_hot_last_op"] = info["last_op_ts"]
    print(f"  RM1: Hot wallet USDT=${info['usdt']:,.2f} TRX={info['trx']:,.2f} last_op={time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(info['last_op_ts']))}")

    # Alerta gas crítico (operador deja sin gas = abandono)
    if info["trx"] < 10:
        send_alert(
            f"*RM1 — Hot wallet TRX bajo ({info['trx']:.2f})*\n"
            f"Posible: operador dejando de operar/abandono\n"
            f"USDT: ${info['usdt']:,.2f}\n"
            f"[Tronscan](https://tronscan.org/#/address/{HOT_WALLET})",
            "WARN",
        )

    # Alerta sin actividad 12h+ (silencio operacional)
    if prev_last_op and info["last_op_ts"] == prev_last_op:
        hours_quiet = (time.time() - info["last_op_ts"]) / 3600
        if hours_quiet > 12:
            send_alert(
                f"*RM1 — Hot wallet SIN actividad {hours_quiet:.0f}h*\n"
                f"Posible: pre-rug / abandono operador",
                "WARN",
            )

def check_rm2_treasury(state):
    """RM2: Treasury balance + drain detection."""
    info = get_account_info(TREASURY)
    if not info:
        print(f"  RM2 SKIP: cannot fetch", file=sys.stderr)
        return
    prev = state.get("rm2_treasury_usdt")
    state["rm2_treasury_usdt"] = info["usdt"]
    print(f"  RM2: Treasury USDT=${info['usdt']:,.2f}")
    if prev is not None:
        delta = info["usdt"] - prev
        # Drain >$30K en 6h = exit signal
        if delta < -30_000:
            send_alert(
                f"*RM2 — TREASURY DRAIN {delta:+,.0f}*\n"
                f"Anterior: ${prev:,.2f}\n"
                f"Actual:   ${info['usdt']:,.2f}\n"
                f"🚨 Posible exit del operador. Verificar destinos OUT.\n"
                f"[Tronscan](https://tronscan.org/#/address/{TREASURY})",
                "CRIT",
            )
        # Treasury vacío total
        if info["usdt"] < 1_000 and prev > 30_000:
            send_alert(
                f"🚨🚨 *RM2 — TREASURY VACIADA*\n"
                f"USDT: ${info['usdt']:,.2f} (era ${prev:,.2f})\n"
                f"EXIT CONFIRMADO. Retirar cualquier capital pendiente YA.",
                "CRIT",
            )

def check_rm3_treasury_flows(state, blocks_window_min=360):
    """RM3: Treasury outflows en últimas 6h — detectar cash-outs grandes."""
    txs = get_trc20_tx(TREASURY, limit=50)
    if not txs:
        print(f"  RM3 SKIP: no tx data", file=sys.stderr)
        return
    cutoff_ts = time.time() - (blocks_window_min * 60)  # últimas 6h
    big_outs = []
    total_out = 0
    for t in txs:
        ts = t.get("block_timestamp", 0) / 1000
        if ts < cutoff_ts:
            continue
        if t.get("from") != TREASURY:
            continue
        v = int(t.get("value", 0)) / 1e6
        total_out += v
        if v > 50_000:
            big_outs.append((v, t.get("to"), t.get("transaction_id"), ts))

    state["rm3_treasury_6h_out"] = total_out
    print(f"  RM3: Treasury OUT 6h = ${total_out:,.2f} · {len(big_outs)} txs >$50K")

    if big_outs:
        msg = f"*RM3 — Treasury cash-outs grandes 6h*\n\n"
        for v, to, h, ts in big_outs[:5]:
            dt = time.strftime('%Y-%m-%d %H:%M', time.gmtime(ts))
            msg += f"-${v:,.0f} → `{to}` ({dt})\n[tx](https://tronscan.org/#/transaction/{h})\n\n"
        send_alert(msg, "WARN")

def check_rm4_hot_inbound_rate(state, blocks_window_min=720):
    """RM4: Hot wallet deposits inbound últimas 12h — cero = pre-rug."""
    txs = get_trc20_tx(HOT_WALLET, limit=50)
    if not txs:
        print(f"  RM4 SKIP", file=sys.stderr)
        return
    cutoff_ts = time.time() - (blocks_window_min * 60)
    inbound = 0
    inbound_count = 0
    for t in txs:
        ts = t.get("block_timestamp", 0) / 1000
        if ts < cutoff_ts:
            continue
        if t.get("to") != HOT_WALLET:
            continue
        inbound += int(t.get("value", 0)) / 1e6
        inbound_count += 1
    state["rm4_hot_inbound_12h_count"] = inbound_count
    state["rm4_hot_inbound_12h_usdt"] = inbound
    print(f"  RM4: Hot wallet deposits 12h = {inbound_count} tx · ${inbound:,.2f}")
    if inbound_count == 0:
        send_alert(
            f"*RM4 — Hot wallet RM SIN deposits 12h*\n"
            f"Posible: captación muerta / operador desactivó canal",
            "WARN",
        )

def check_rm5_cashout_wallet(state):
    """RM5: Wallet cash-out final TH8fsKYV — balance + activity."""
    info = get_account_info(CASH_OUT)
    if not info:
        return
    prev = state.get("rm5_cashout_usdt")
    state["rm5_cashout_usdt"] = info["usdt"]
    state["rm5_cashout_trx"] = info["trx"]
    print(f"  RM5: Cash-out wallet USDT=${info['usdt']:,.2f} TRX={info['trx']:,.2f}")
    if prev is not None:
        delta = info["usdt"] - prev
        if abs(delta) > 100_000:
            send_alert(
                f"*RM5 — Cash-out wallet balance cambió*\n"
                f"${prev:,.0f} → ${info['usdt']:,.0f} (Δ${delta:+,.0f})",
                "WARN",
            )

def check_rmp1_fernando_settlement(state):
    """RMP1: Detecta USDT entrante a wallet Fernando TRON (settlement RM)."""
    if not FERNANDO_TRON:
        return
    txs = get_trc20_tx(FERNANDO_TRON, limit=50)
    if not txs:
        return
    seen = set(state.get("rmp1_seen_txs", []))
    new_settles = []
    for t in txs:
        h = t.get("transaction_id")
        if h in seen:
            continue
        if t.get("to") != FERNANDO_TRON:
            continue
        v = int(t.get("value", 0)) / 1e6
        fr = t.get("from", "")
        # Solo alertar si viene del sistema RM (hot wallet, treasury, cash-out, hub, feeder)
        if fr in RM_PAYERS:
            new_settles.append((v, fr, h, t.get("block_timestamp", 0) / 1000))
            seen.add(h)
    state["rmp1_seen_txs"] = list(seen)[-50:]
    if new_settles:
        msg = f"🎯 *RMP1 — SETTLEMENT RM recibido*\n\n"
        for v, fr, h, ts in new_settles:
            dt = time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(ts))
            msg += f"+${v:.4f} USDT desde {'HOT_WALLET' if fr == HOT_WALLET else 'TREASURY' if fr == TREASURY else 'CASH_OUT'}\n[tx](https://tronscan.org/#/transaction/{h}) · {dt}\n\n"
        send_alert(msg, "OPP")

def check_rm7_collector_fleet(state):
    """RM7: Salud del canal de captación — flota de colectoras de depósito.
    Suma actividad reciente; si TODA la flota queda en silencio = captación muerta
    (señal temprana, independiente del dominio)."""
    cutoff = time.time() - 24 * 3600
    active = 0
    total_in_24h = 0.0
    lines = []
    for addr, label in DEPOSIT_COLLECTORS.items():
        txs = get_trc20_tx(addr, limit=30)
        if txs is None:
            continue
        recent_in = 0.0
        last_ts = 0
        for t in txs:
            ts = t.get("block_timestamp", 0) / 1000
            last_ts = max(last_ts, ts)
            if ts >= cutoff and t.get("to") == addr:
                recent_in += int(t.get("value", 0)) / 1e6
        if last_ts >= cutoff:
            active += 1
        total_in_24h += recent_in
        lines.append(f"{label}: ${recent_in:,.0f} 24h")
        time.sleep(0.2)
    state["rm7_collectors_active"] = active
    state["rm7_collectors_in_24h"] = total_in_24h
    print(f"  RM7: Flota colectoras activas {active}/{len(DEPOSIT_COLLECTORS)} · IN 24h ${total_in_24h:,.0f}")
    # Si TODA la flota silenciosa 24h = canal de depósito muerto (pre-rug fuerte)
    if active == 0:
        send_alert(
            "*RM7 — FLOTA DE COLECTORAS EN SILENCIO 24h*\n"
            "Ninguna de las direcciones de depósito de RM recibió fondos en 24h.\n"
            "🚨 Canal de captación posiblemente muerto. Verificar treasury/cash-out.",
            "CRIT",
        )


# ============= MAIN =============

def main():
    print(f"=== RM Watcher run @ {utc_now_iso()} ===")
    state = load_state()
    is_first_run = "rm1_hot_usdt" not in state

    cadence = os.environ.get("WATCHER_CADENCE", "6h")
    print(f"Cadence: {cadence} · first_run={is_first_run}")

    # Always (cada 1h y cada 6h)
    check_rm1_hot_wallet(state)
    check_rm2_treasury(state)

    if cadence == "6h":
        check_rm3_treasury_flows(state)
        check_rm4_hot_inbound_rate(state)
        check_rm5_cashout_wallet(state)
        check_rm7_collector_fleet(state)
        check_rmp1_fernando_settlement(state)

    save_state(state)

    if is_first_run:
        send_alert(
            f"✅ *RM Watcher INICIADO (on-chain only)*\n\n"
            f"Baseline establecido. Modo PASIVO — sin web RM auth, sin Telegram session.\n\n"
            f"Hot wallet USDT: ${state.get('rm1_hot_usdt', 0):,.2f}\n"
            f"Treasury USDT:   ${state.get('rm2_treasury_usdt', 0):,.2f}\n"
            f"Cash-out USDT:   ${state.get('rm5_cashout_usdt', 0):,.2f}\n"
            f"Capital Fernando expuesto: $98.70\n"
            f"CAP absoluto: $300",
            "INFO",
        )

    print(f"=== RM Watcher run COMPLETE ===")

if __name__ == "__main__":
    main()
