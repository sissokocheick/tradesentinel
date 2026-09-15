# TradeSentinel — Task List

## Phase 1 : Infrastructure ✅
- [x] Plan approuvé
- [x] Structure projet créée
- [x] `mcp/binance_mcp_client.py` — client MCP complet avec HMAC signing
- [x] `agents/scout_agent.py` — collecte + indicateurs techniques
- [x] `agents/strategist_agent.py` — LLM + mémoire de session
- [x] `agents/executor_agent.py` — pipeline complet avec audit trail
- [x] `skills/risk_manager.py` — 7 guardrails configurables
- [x] `wallet/agentic_wallet.py` — solde + valorisation USDC
- [x] `wallet/x402_payments.py` — micropaiements HTTP 402
- [x] `orchestrator.py` — point d'entrée + mode démo
- [x] Syntaxe validée (13/13 fichiers OK)

## Phase 2 : Dashboard ✅
- [x] `dashboard/app.py` — FastAPI + WebSocket
- [x] `dashboard/frontend/index.html` — dashboard live (pas de build React requis)

## Phase 3 : Documentation & Démo
- [x] `README.md` — complet avec badges, quick start, architecture
- [x] `.env.example` — toutes les variables configurables
- [x] `requirements.txt` + `Dockerfile` + `docker-compose.yml`
- [x] Intégration Google Gemini 2.5 Flash opérationnelle (testée end-to-end)
- [x] Test complet en mode `--demo --dashboard` validé avec succès
- [x] **Repo GitHub PUBLIC créé** — https://github.com/sissokocheick/tradesentinel
- [x] **Hardening de la rentabilité (session du 2026-09-15)**
  - [x] Stop-loss de secours : une position n'est **jamais** laissée nue
  - [x] P&L réel mark-to-market (suppression de la simulation MD5)
  - [x] Sharpe / drawdown sur capital réel (suppression des constantes 397460.99 / 2.42)
  - [x] Funding rate corrigé (`BTCUSDT_PERP` → `BTCUSDT`)
  - [x] Test de régression `test_protection.py` — 4/4 PASS
- [ ] Remplir le `.env` avec les vraies clés Binance Agent OS
- [ ] **Révoquer la clé API en clair dans `api test .txt`** (toujours en attente)
- [ ] Enregistrer la vidéo démo (5–7 min)
- [ ] Poster sur Twitter/X avec la vidéo + GitHub
- [ ] Compléter le survey Binance

## Checklist finale soumission
- [ ] Follow @Binance sur X
- [ ] Repost l'annonce officielle
- [ ] Reply/quote-repost avec vidéo + GitHub
- [ ] Survey complété : https://www.binance.com/en/survey/2913aa200aac462c89a737779393f3d4
