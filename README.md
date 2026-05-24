# RM Watcher

Vigilancia 24/7 **ON-CHAIN ONLY** para RM apuestas inversas.

## Filosofía: ZERO-FOOTPRINT operacional

- **NO autentica al backend RM**
- **NO scrapea web RM** (ningún `m1.rmXXXX.buzz`)
- **NO toca grupo Telegram** con session de Fernando
- **NO hace DNS lookups frecuentes** a dominios RM
- **NO emite huella vinculable** a la cuenta de Fernando

Toda visibilidad viene de **TronGrid API pública** (rate limit 100K/día free, sin key).

Razón: Fernando es usuario activo de la plataforma RM. Cualquier scraping/login/Telegram-bot activity podría ser detectado como bot-like y disparar baneo. Ese baneo perdería su capital depositado.

**Escaneos profundos de web RM se hacen desde buscador aislado de su cuenta** (manual, no automatizado).

## Watchers operativos

| ID | Watcher | Trigger |
|---|---|---|
| RM1 | Hot wallet `TQZv...DDkUJ` USDT + TRX + activity | TRX <10 (sin gas) · 12h sin activity |
| RM2 | Treasury `TGT...8888888` USDT balance | drain >$30K en 6h · vaciado total |
| RM3 | Treasury outflows 6h | tx individual >$50K = cash-out anómalo |
| RM4 | Hot wallet deposits inbound 12h | 0 deposits = captación muerta |
| RM5 | Cash-out wallet `TH8fsKYV...LXbFm` | cambio balance >$100K |
| RMP1 | Wallet Fernando TRON recibe USDT desde sistema RM | settlement confirmado |

## Cron schedule
- **Cada 6h:** full sweep (cron `41 */6 * * *` UTC)
- **Cada 1h:** light (solo RM1 + RM2) — drain detection rápido

Offset cron `:41` y `:47` evita choque con bitnest-watcher (`:23` y `:11`).

## Setup

### Secretos GitHub Actions requeridos

| Secret | Valor / fuente |
|---|---|
| `TELEGRAM_BOT_TOKEN` | bot `@CyrusWatcherFTRbot` (compartido cyrus/bitradex/bitnest) |
| `TELEGRAM_CHAT_ID` | `-1003894658753` (supergroup) |
| `TELEGRAM_RM_THREAD_ID` | (opcional) thread_id topic RM dedicado · fallback usa bitradex_thread |
| `TELEGRAM_BITRADEX_THREAD_ID` | reuso del bitradex-watcher (`764`) |
| `FERNANDO_TRON_WALLET` | (opcional) tu wallet TRON personal para RMP1 settlement tracking |

### Setup secrets via gh CLI

```bash
cd rm-watcher
gh secret set TELEGRAM_BOT_TOKEN -b "<token>"
gh secret set TELEGRAM_CHAT_ID -b "-1003894658753"
gh secret set TELEGRAM_BITRADEX_THREAD_ID -b "764"
# Opcionales:
gh secret set TELEGRAM_RM_THREAD_ID -b "<thread_id si creas topic dedicado>"
gh secret set FERNANDO_TRON_WALLET -b "<tu wallet TRON personal>"
```

## Operación

**Cero mantenimiento.** State persiste vía commits `state.json`.

- **Pausar:** Settings → Actions → Disable workflows.
- **Re-correr manual:** `gh workflow run "RM Watcher" --repo pctechnologyxela-tech/rm-watcher`.

## Costo total: $0/mes

GH Actions free + TronGrid API público sin key + Telegram bot compartido. Sin claves pagas.

## Skill hermana

Activar `experto-rm` para análisis profundo cuando watcher alerta o se decide cambio de sizing.
