"""cazador_reverse_ip_watch.py (rm-watcher cloud edition) — detector proactivo dominio RM nuevo vía reverse-IP.

Vigila los servidores HK del operador (donde co-viven todas sus marcas hermanas) y alerta apenas
aparezca un host `m1.rm<5díg><2let>.<tld>` VIVO = dominio RM nuevo provisionado, normalmente ANTES
del anuncio en Telegram. Mejor que crt.sh (que suele estar caído).

Corre en GH Actions junto a watcher.py + domain_tracker.py. Estado commiteado en cazador_ip_state.json.
NO deposita, NO firma. Verificación on-chain final = rm_onchain_verify_deposit (local).
"""
import sys, io, re, json, socket, os
from datetime import datetime, timezone
from pathlib import Path
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import requests

STATE = Path("cazador_ip_state.json")
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# IPs ancla del operador (actualizar si migra; ver resultados/cazador_infra_operador_*.md)
ANCHOR_IPS = ["103.42.146.28", "103.183.198.67"]
RE_M1 = re.compile(r"m1\.([a-z]{2})\d{5}[a-z]{2}\.[a-z]{2,6}", re.I)
RM_PREFIX = "rm"

def harvest():
    hosts = {}
    for ip in ANCHOR_IPS:
        for url in (f"https://rapiddns.io/sameip/{ip}?full=1",
                    f"https://api.hackertarget.com/reverseiplookup/?q={ip}"):
            try:
                r = requests.get(url, timeout=25, headers=H)
                for m in RE_M1.finditer(r.text):
                    hosts[m.group(0).lower()] = m.group(1).lower()
            except Exception as e:
                print(f"  [{ip}] {url.split('/')[2]} {type(e).__name__}")
    return hosts

def alive(host):
    try: return socket.gethostbyname(host)
    except Exception: return None

RE_BACKEND = re.compile(r"(frontend-api\.[a-z0-9\-]+\.[a-z]{2,6}|api[a-z0-9\-]*\.[a-z0-9\-]+\.[a-z]{2,6})", re.I)
def extract_backend(host):
    """Al cazar m1.rm* vivo: bajar su HTML/JS y sacar el frontend-api.<x>.shop (backend estable de RM).
    Eso permite, en la PRÓXIMA rotación, resolver el dominio autoritativamente vía site_status."""
    found = set()
    try:
        r = requests.get(f"https://{host}/", timeout=20, headers=H); html = r.text
        for m in RE_BACKEND.finditer(html): found.add(m.group(1).lower())
        for js in set(re.findall(r'src=["\']([^"\']+\.js[^"\']*)["\']', html))[:6]:
            ju = js if js.startswith("http") else f"https://{host}/" + js.lstrip("/")
            try:
                for m in RE_BACKEND.finditer(requests.get(ju, timeout=15, headers=H).text): found.add(m.group(1).lower())
            except Exception: pass
    except Exception as e:
        print(f"  extract_backend {type(e).__name__}")
    return sorted(found)

def telegram_alert(text):
    tok = os.environ.get("TELEGRAM_BOT_TOKEN"); chat = os.environ.get("TELEGRAM_CHAT_ID")
    topic = os.environ.get("TELEGRAM_RM_THREAD_ID")
    if not (tok and chat):
        print("  (sin secrets Telegram — alerta no enviada)"); return
    payload = {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if topic: payload["message_thread_id"] = int(topic)
    try:
        r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", json=payload, timeout=15)
        print(f"  alerta TG -> {r.status_code}")
    except Exception as e: print(f"  alerta TG {type(e).__name__}")

def main():
    print(f"=== REVERSE-IP WATCH {datetime.now(timezone.utc).isoformat()[:19]}Z ===")
    hosts = harvest()
    prev = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {"seen": [], "rm_alerted": []}
    seen = set(prev.get("seen", [])); rm_alerted = set(prev.get("rm_alerted", []))
    rm_live, brands, fresh = [], {}, []
    for host, brand in sorted(hosts.items()):
        ip = alive(host)
        brands.setdefault(brand, {"live": 0, "dead": 0})["live" if ip else "dead"] += 1
        if brand == RM_PREFIX and ip:
            rm_live.append((host, ip))
            if host not in rm_alerted: fresh.append((host, ip))
    print("Marcas operador:", ", ".join(f"{b}({v['live']}v/{v['dead']}m)" for b, v in sorted(brands.items())) or "(ninguna)")
    backends = []
    if fresh:
        backends = extract_backend(fresh[0][0])  # capturar backend RM para la PRÓXIMA rotación
        msg = ("🚨 <b>POSIBLE dominio RM nuevo (reverse-IP)</b>\n" +
               "\n".join(f"• https://{h} ({ip})" for h, ip in fresh) +
               (f"\n🔑 Backend RM capturado: {', '.join(backends)}" if backends else "") +
               "\n⚠️ NO confirmado. Verificar 100% antes de operar:\n"
               "1) Abrir y confirmar por VISIÓN que es RM新時代 (no marca hermana bw/jc/jp/pn).\n"
               "2) Copiar dirección depósito TRC20 → debe hacer sweep a treasury TGT…888888.")
        print("\n" + re.sub(r"</?b>", "", msg))
        telegram_alert(msg)
        rm_alerted |= {h for h, _ in fresh}
    elif rm_live:
        print(f">>> RM vivo ya alertado antes: {[h for h,_ in rm_live]}")
    else:
        print(">>> Sin m1.rm* vivo en la infra todavía. El operador no ha provisionado el RM nuevo.")
    prev_backends = prev.get("rm_backend", [])
    STATE.write_text(json.dumps({"ran": datetime.now(timezone.utc).isoformat(),
        "seen": sorted(set(list(seen) + list(hosts.keys()))),
        "rm_live": rm_live, "rm_alerted": sorted(rm_alerted), "brands": brands,
        "rm_backend": sorted(set(prev_backends + backends))},
        indent=2, ensure_ascii=False), encoding="utf-8")

if __name__ == "__main__":
    main()
