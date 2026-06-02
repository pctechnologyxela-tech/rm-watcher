"""RM Domain Tracker — rastreo PROACTIVO del dominio rotatorio de RM.

Corre en GH Actions (IP neutral de GitHub, NO la de Fernando → seguro hacer
DNS / CT / fetch de favicon sin exponer la cuenta de Fernando).

Detecta el próximo dominio RM (patrón m1.rm{5díg}{2let}.{tld}) ANTES de que
Fernando pierda acceso. NO deposita, NO firma nada, NO autentica al backend.
Solo OSINT pasivo desde IP de GitHub:

  D1: Liveness de dominios conocidos (¿el activo murió? → rotación en curso)
  D2: CT logs (crt.sh) → certificados nuevos que matchean el patrón RM
  D3: Fingerprint (favicon mmh3 + strings HTML) → confirma mismo template
  D4: Hosting/edad (RDAP + ipinfo) → consistencia con histórico

VERIFICACIÓN DEFINITIVA on-chain (depósito→treasury) la hace Fernando manual
antes de depositar — este módulo solo emite candidatos con score de confianza.

Salida: actualiza domains.json + alerta a Telegram cuando aparece candidato
con score >= ALERT_THRESHOLD.
"""
import os
import re
import sys
import json
import time
import base64
import socket
import io
import requests
import mmh3
from pathlib import Path
from datetime import datetime, timezone

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ============= CONFIG =============

# Patrón de naming verificado empíricamente (6 dominios histórico, 2026-05).
TLDS = ["space", "shop", "life", "store", "pw", "xyz", "top", "cc", "vip", "club", "online", "site"]
RE_FULL = re.compile(r"^(?:m1\.)?rm\d{5}[a-z]{2}\.(?:" + "|".join(TLDS) + r")$", re.I)
RE_APEX = re.compile(r"^rm\d{5}[a-z]{2}\.(?:" + "|".join(TLDS) + r")$", re.I)

# Dominios conocidos (histórico). Se siembran en domains.json en primera corrida.
KNOWN_SEED = [
    "m1.rm10482qc.space", "m1.rm36285fc.shop", "m1.rm18974zu.shop",
    "m1.rm86940fc.life", "m1.rm15984ct.shop", "m1.rm68123pw.store",
]

# Dominio PERMANENTE oficial de RM (RM lo anuncia como "permanent VIP domain";
# verificado vivo 2026-06-01, resuelve al mismo destino que el dominio rotativo).
# Mientras este resuelva NO hay "rotación sin solución" — es el ancla estable.
PERMANENT_DOMAIN = "m1.rm888.club"
# Dominios que SIEMPRE se chequean (se inyectan en hosts cada corrida, no solo en seed).
# El activo se actualiza al detectar uno nuevo; el permanente es fijo.
ALWAYS_CHECK = ["m1.rm888.club", "m1.rm14578ku.shop"]

# Strings de huella del template RM (confirmados en vivo 2026-05-30 sobre
# m1.rm25108gb.store · title "RM新时代 - 引领时代, 投盈未来"). El bare "rm" se
# excluye a propósito (demasiado laxo). "rm新时代"+"saba" son los discriminantes.
FINGERPRINT_STRINGS = ["rm新时代", "引领时代", "投盈未来", "saba", "sabasports", "rmgs", "反波", "注册", "体育"]

# Cluster on-chain (idéntico a watcher.py — para nota en alerta de verificación).
TREASURY = "TGTdkJTwFALEmj889t95uosoxSv8888888"
HOT_WALLETS = [
    "TQZvYW7Am7rm7R2tbCVq84135zUXADDkUJ",
    "THavRzXk7Tt43GeU4Sxbig5VJakZYnN39e",
    "TLaGjwhvA8XQYSxFAcAXy7Dvuue9eGYitv",
]

ALERT_THRESHOLD = 55  # score mínimo para alertar a Fernando

# Telegram (mismos secrets que watcher.py)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_THREAD_ID = os.environ.get("TELEGRAM_RM_THREAD_ID", os.environ.get("TELEGRAM_BITRADEX_THREAD_ID", ""))

DOMAINS_FILE = Path(__file__).parent / "domains.json"

# ============= HELPERS =============

def utc_now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def load_domains():
    if DOMAINS_FILE.exists():
        try:
            return json.loads(DOMAINS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"baseline": {}, "hosts": {}}

def save_domains(d):
    DOMAINS_FILE.write_text(json.dumps(d, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")

SESS = requests.Session()
SESS.headers["User-Agent"] = "Mozilla/5.0 (compatible; domain-monitor/1.0)"

# ============= TELEGRAM ALERT =============

def send_alert(message, severity="INFO"):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[NO TG] {severity}: {message[:200]}")
        return False
    prefix = {"CRIT": "🚨 CRITICAL", "WARN": "⚠️ WARN", "INFO": "ℹ️ INFO", "NEW": "🆕 NUEVO DOMINIO"}.get(severity, severity)
    text = f"{prefix} · RM Domain Tracker\n\n{message}\n\n_{utc_now_iso()}_"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown", "disable_web_page_preview": True}
    if TELEGRAM_THREAD_ID:
        payload["message_thread_id"] = int(TELEGRAM_THREAD_ID)
    try:
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json=payload, timeout=20)
        return r.status_code == 200
    except Exception as e:
        print(f"[TG EXC] {e}", file=sys.stderr)
        return False

# ============= D1: LIVENESS =============

def dns_resolve(host):
    try:
        return socket.gethostbyname(host)
    except Exception:
        return None

# ============= D2: CT LOGS (crt.sh) =============

CT_TLDS = ["store", "space", "shop", "life", "pw", "xyz"]  # los realmente usados (menos carga)
CT_TIME_BUDGET = 150  # segundos máximos totales para no colgar el job

def ct_search():
    """Consulta crt.sh por cada TLD; devuelve {host: not_before_iso}.
    crt.sh es INESTABLE (502/timeout frecuentes) → mejor-esfuerzo con presupuesto
    de tiempo global. Falla parcial/total != error: Telegram+on-chain son el respaldo."""
    found = {}
    t0 = time.time()
    for tld in CT_TLDS:
        if time.time() - t0 > CT_TIME_BUDGET:
            print("  [crt.sh] presupuesto agotado, corte mejor-esfuerzo", file=sys.stderr)
            break
        q = f"rm%.{tld}"
        for attempt in range(2):
            try:
                r = SESS.get("https://crt.sh/", params={"q": q, "output": "json", "exclude": "expired"}, timeout=30)
                txt = r.text.strip()
                if r.status_code == 200 and txt.startswith("["):
                    for row in r.json():
                        for nm in str(row.get("name_value", "")).split("\n"):
                            nm = nm.strip().lower().lstrip("*.")
                            if RE_FULL.match(nm) or RE_APEX.match(nm):
                                nb = row.get("not_before", "")
                                if nb > found.get(nm, ""):
                                    found[nm] = nb
                    break
                else:
                    time.sleep(3)
            except Exception as e:
                print(f"  [crt.sh {q}] {repr(e)[:60]}", file=sys.stderr)
                time.sleep(3)
    return found

# ============= D3: FINGERPRINT =============

def favicon_hash(host):
    """mmh3 del favicon (método Shodan: base64 con saltos de línea)."""
    for scheme in ("https", "http"):
        try:
            r = SESS.get(f"{scheme}://{host}/favicon.ico", timeout=15)
            if r.status_code == 200 and r.content:
                b64 = base64.encodebytes(r.content)
                return mmh3.hash(b64)
        except Exception:
            continue
    return None

def fetch_fingerprint(host):
    """Devuelve dict {favicon, title, strings_hits, status} del candidato vivo."""
    fp = {"favicon": None, "title": None, "strings": [], "ok": False}
    fp["favicon"] = favicon_hash(host)
    for scheme in ("https", "http"):
        try:
            r = SESS.get(f"{scheme}://{host}/", timeout=15)
            if r.status_code < 400 and r.text:
                html = r.text.lower()
                m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S)
                fp["title"] = (m.group(1).strip()[:120] if m else None)
                fp["strings"] = [s for s in FINGERPRINT_STRINGS if s.lower() in html]
                fp["ok"] = True
                break
        except Exception:
            continue
    return fp

def fingerprint_score(fp, baseline):
    """0..1 de similitud contra baseline guardado."""
    if not baseline:
        return 0.0
    score = 0.0
    if fp.get("favicon") and baseline.get("favicon") and fp["favicon"] == baseline["favicon"]:
        score += 0.6  # favicon idéntico = señal fortísima
    bset = set(baseline.get("strings", []))
    fset = set(fp.get("strings", []))
    if bset:
        score += 0.4 * (len(bset & fset) / len(bset))
    return round(score, 2)

# ============= D4: HOSTING / EDAD =============

def domain_age_days(apex):
    """RDAP → días desde creación. None si no disponible."""
    try:
        r = SESS.get(f"https://rdap.org/domain/{apex}", timeout=20)
        if r.status_code == 200:
            for ev in r.json().get("events", []):
                if ev.get("eventAction") == "registration":
                    d = datetime.fromisoformat(ev["eventDate"].replace("Z", "+00:00"))
                    return (datetime.now(timezone.utc) - d).days
    except Exception:
        pass
    return None

def ip_asn(ip):
    try:
        r = SESS.get(f"https://ipinfo.io/{ip}/json", timeout=15)
        if r.status_code == 200:
            j = r.json()
            return f"{j.get('org','?')} / {j.get('country','?')}"
    except Exception:
        pass
    return None

# ============= SCORING =============

def score_candidate(host, alive_ip, fp, fp_sim, age):
    """Score 0..100 de probabilidad de que el host sea el RM real."""
    s = 0
    notes = []
    if RE_FULL.match(host):
        s += 40; notes.append("naming match +40")
    if alive_ip:
        s += 20; notes.append(f"resuelve {alive_ip} +20")
    if fp and fp.get("favicon") is not None:
        s += 5; notes.append("favicon presente +5")
    if fp_sim >= 0.8:
        s += 25; notes.append(f"fingerprint {fp_sim} +25")
    elif fp_sim >= 0.4:
        s += 12; notes.append(f"fingerprint {fp_sim} +12")
    if fp and fp.get("strings"):
        s += min(10, 3 * len(fp["strings"])); notes.append(f"strings {fp['strings']}")
    if age is not None and age <= 60:
        s += 10; notes.append(f"dominio joven {age}d +10")
    return s, notes

# ============= MAIN =============

def apex_of(host):
    return host[3:] if host.startswith("m1.") else host

def main():
    print(f"=== RM Domain Tracker @ {utc_now_iso()} ===")
    data = load_domains()
    hosts = data.setdefault("hosts", {})
    baseline = data.get("baseline", {})

    # Sembrar histórico en primera corrida
    if not hosts:
        for h in KNOWN_SEED:
            hosts[h] = {"first_seen": utc_now_iso(), "source": "seed", "alive": None, "ip": None}
        print(f"  Seed: {len(KNOWN_SEED)} dominios históricos")

    # Inyectar SIEMPRE el permanente + activo (no solo en primera corrida)
    for h in ALWAYS_CHECK:
        if h not in hosts:
            hosts[h] = {"first_seen": utc_now_iso(), "source": "always-check", "alive": None, "ip": None}

    # ---- D1: liveness de conocidos ----
    any_alive_known = False
    permanent_alive = False
    for h in list(hosts.keys()):
        ip = dns_resolve(h)
        hosts[h]["alive"] = bool(ip)
        hosts[h]["ip"] = ip
        hosts[h]["last_check"] = utc_now_iso()
        if ip:
            any_alive_known = True
            if h == PERMANENT_DOMAIN:
                permanent_alive = True
            print(f"  D1 ALIVE {h} -> {ip}")
    if not any_alive_known:
        print("  D1: NINGÚN dominio conocido vivo → rotación en curso, buscando nuevo")

    # ---- D2: CT logs ----
    print("  D2: consultando crt.sh ...")
    ct = ct_search()
    print(f"  D2: {len(ct)} hosts en CT logs (certs activos)")

    # ---- D3+D4: evaluar candidatos NUEVOS ----
    candidates = []
    for host, nb in sorted(ct.items(), key=lambda x: x[1], reverse=True):
        is_new = host not in hosts
        if host not in hosts:
            hosts[host] = {"first_seen": utc_now_iso(), "source": "crt.sh", "cert_not_before": nb}
        ip = dns_resolve(host)
        hosts[host]["alive"] = bool(ip)
        hosts[host]["ip"] = ip
        hosts[host]["last_check"] = utc_now_iso()
        if not ip:
            continue  # cert existe pero no resuelve aún (registrado, no desplegado)
        fp = fetch_fingerprint(host)
        sim = fingerprint_score(fp, baseline)
        age = domain_age_days(apex_of(host))
        sc, notes = score_candidate(host, ip, fp, sim, age)
        hosts[host].update({"score": sc, "fingerprint": fp, "fp_sim": sim, "age_days": age, "asn": ip_asn(ip)})
        # Capturar baseline si aún no hay y este es claramente RM (naming + strings)
        if not baseline and RE_FULL.match(host) and fp.get("ok") and fp.get("favicon"):
            data["baseline"] = {"favicon": fp["favicon"], "strings": fp["strings"], "host": host, "captured": utc_now_iso()}
            baseline = data["baseline"]
            print(f"  BASELINE capturado de {host}: favicon={fp['favicon']} strings={fp['strings']}")
        candidates.append((sc, host, notes, fp, age))
        print(f"  CAND {host} score={sc} ip={ip} {notes}")

    save_domains(data)

    # ---- ALERTAS ----
    candidates.sort(reverse=True)
    top = [c for c in candidates if c[0] >= ALERT_THRESHOLD and hosts[c[1]].get("alerted") is None]
    for sc, host, notes, fp, age in top:
        hosts[host]["alerted"] = utc_now_iso()
        send_alert(
            f"*Posible NUEVO dominio RM detectado*\n\n"
            f"`{host}`  (score {sc}/100)\n"
            f"Título: {fp.get('title')}\n"
            f"Edad: {age}d · {notes}\n\n"
            f"⚠️ *NO DEPOSITES sin verificar on-chain.* Pasos:\n"
            f"1) Abre el sitio, copia tu dirección de depósito TRC20.\n"
            f"2) En Tronscan, confirma que hace sweep a la treasury conocida:\n"
            f"   `{TREASURY}`\n"
            f"3) Si NO cae ahí = clon/phishing, NO deposites.\n"
            f"[Abrir](https://{host}) · [Treasury](https://tronscan.org/#/address/{TREASURY})",
            "NEW",
        )
    save_domains(data)

    # Alerta de rotación SOLO si TAMBIÉN murió el permanente (ancla estable).
    # Mientras m1.rm888.club resuelva, RM es accesible → NO alertar "sin solución".
    if not any_alive_known and not permanent_alive and not top:
        prev = data.get("_rotation_alerted", "")
        if prev != utc_now_iso()[:10]:  # 1 vez/día
            data["_rotation_alerted"] = utc_now_iso()[:10]
            save_domains(data)
            send_alert(
                "*Rotación RM en curso*\n\n"
                f"Ni el dominio rotativo ni el permanente (`{PERMANENT_DOMAIN}`) resuelven, "
                "y aún no hay candidato confiable en CT logs.\n"
                "Acción: revisa el grupo Telegram de RM por el dominio nuevo y verifícalo on-chain "
                "antes de operar. Corre `scripts/rm_domain_telegram_scan.py` local.",
                "WARN",
            )
    elif permanent_alive:
        print(f"  Permanente {PERMANENT_DOMAIN} VIVO → RM accesible, sin alerta de rotación.")

    print(f"=== Domain Tracker COMPLETE · {len(candidates)} candidatos · {len(top)} alertas ===")

if __name__ == "__main__":
    main()
