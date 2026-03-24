# HerbaMarketer — Stato del Progetto
> Aggiornato: 24 marzo 2026 — dopo test end-to-end completati

---

## Overview

HerbaMarketer è un sistema di marketing automation che genera, traduce e pubblica autonomamente contenuti (email di nurturing + articoli SEO) su 7 siti Herbalife multilingua. Supervisione umana via Telegram e dashboard web.

**Stato attuale: sistema testato end-to-end e funzionante. Pronto per il deploy su Coolify.**

| Metrica | Valore |
|---------|--------|
| Siti gestiti | 7 (6 Mautic + 1 Brevo) |
| Email pubblicate (test) | 14 (2 per sito × 7 siti) |
| Test automatici | 93 (tutti verdi) |
| Test end-to-end | ✅ completati |

---

## Siti gestiti

| Slug | Dominio | Lingua | Piattaforma | WP API |
|------|---------|--------|-------------|--------|
| herbago_it | www.herbago.it | it | Mautic (campaign 4) | www.herbago.it |
| herbago_fr | herbago.fr | fr | Mautic (campaign 5) | herbago.fr |
| herbago_de | www.herbago.de | de | Mautic (campaign 6) | www.herbago.de |
| herbago_net | www.herbago.net | en-IE | Mautic (campaign 7) | www.herbago.net |
| herbago_co_uk | www.herbago.co.uk | en-GB | Mautic (campaign 8) | www.herbago.co.uk |
| hlifeus_com | hlifeus.com | en-US | Mautic (campaign 9) | hlifeus.com |
| herbashop_it | herbashop.it | it | Brevo (lista 9) | herbashop.it |

**Nota**: herbago.it, .de, .net, .co.uk reindirizzano a www — i wp_api_url sono già corretti con www.

---

## Flusso Email — ogni 15 giorni per tutti i siti

### Trigger
- **Automatico**: APScheduler lancia `email_job()` ogni 15 giorni
- **Manuale**: `python3 test_scheduler_email.py` (o chiamata diretta da codice)

### Flusso dettagliato
```
1. email_job() si avvia
2. Seleziona il prossimo topic con status="approved" dal DB
   → Se nessun topic approved: log warning, job termina
3. Per il sito master (herbago_it):
   a. Genera email_1 (problema) in italiano via Claude API
   b. Genera email_2 (prodotto + soluzione) in italiano
   c. Validator controlla: lunghezza, tono, claim illegali, CTA
      → score < 70: rigenera (max 3 tentativi)
4. Per ogni sito attivo (Mautic + Brevo):
   a. Traduce la coppia email nella lingua del sito
   b. Validator ricontrolla la traduzione
   c. Publisher crea le email sulla piattaforma:
      - Mautic: POST /api/emails + aggiunge alla campagna del sito
      - Brevo: POST /v3/smtp/templates (due template)
   d. Salva EmailPair nel DB con status="published"
   e. Scrive PublishLog
5. Notifiche Telegram:
   - Mautic: "📧 Nuova coppia email pronta" con bottoni Approva/Rifiuta
   - Brevo: "📧 Nuovi template Brevo pronti" con istruzioni manuali
6. Topic status → "done"
```

### Naming convention email
| Piattaforma | Formato | Esempio |
|------------|---------|---------|
| Mautic | `{PREFIX}_{NNN}_{slug}` | `ITA_027_colazione_proteica` |
| Brevo | `HS_IT_{NNN}_{slug}` | `HS_IT_001_colazione_proteica` |

### Placeholder personalizzazione
| Piattaforma | Variabile |
|------------|----------|
| Mautic | `{contactfield=firstname}` |
| Brevo | `{{ contact.NOME }}` |

### Note operative Brevo
Le campagne Brevo NON vengono aggiunte automaticamente all'automazione.
Dopo ogni notifica Telegram, aggiungere manualmente in **Brevo → Automazioni → Scenario #9**:
1. Nodo "Attendi 14 giorni"
2. Nodo "Invia email" → seleziona template `HS_IT_XXX`
3. Nodo "Attendi 14 giorni"
4. Nodo "Invia email" → seleziona template `HS_IT_XXX+1`

---

## Flusso Articoli — ogni 15 giorni, con approvazione Telegram

### Trigger
- **Automatico**: APScheduler lancia `article_job()` ogni 15 giorni
- **Manuale**: `python3 test_article_job.py`

### Flusso dettagliato
```
1. article_job() si avvia
2. Controlla se c'è un topic approved nel DB
   → Nessun topic: invia su Telegram lista topic pending
     con bottoni inline per selezionare → Omar sceglie → topic → "approved"
3. Genera articolo IT master (~1700 parole, H3/H4):
   - Cerca URL prodotto nella sitemap del sito
   - Genera articolo con CTA bottone verde con URL reale
   - meta_title (max 60 char) + meta_description (max 155 char)
4. Validator SEO: lunghezza, struttura, keyword, claim
5. Genera immagine con DALL-E 3 (fallback Ideogram):
   - Scena lifestyle/natura, no prodotto, no testo
6. Per ogni sito attivo con wp_api_url:
   a. Traduce articolo nella lingua del sito
   b. Controlla disponibilità prodotto via sitemap del sito
      → Prodotto non trovato: skip sito + notifica Telegram
   c. Carica immagine su WP media library
   d. Pubblica articolo come bozza (status="draft")
   e. Salva record Article nel DB
7. Telegram: "Articoli pronti in bozza" con link preview per ogni sito
   + bottoni [Pubblica tutto] [Rigetta tutto]
8. Se approvato: pubblica tutti i WP draft → status="published"
```

### Struttura articolo generato
- Titolo + slug ottimizzato SEO
- Corpo ~1700 parole con H3/H4 (mai H2/H1)
- Penultimo paragrafo: "In sintesi" (mai "Conclusione")
- CTA finale: bottone verde con URL prodotto reale
- Featured image generata da AI
- Yoast SEO: meta_title e meta_description popolati via REST API

---

## Flusso Input Topic — asincrono, qualsiasi momento

### Metodo 1: URL ingestor
```
URL inviato (script o Telegram)
→ url_ingestor.py scrapa la pagina (BeautifulSoup4)
→ Estrae testo dall'articolo/main
→ Claude genera topic + keyword
→ Salva ContentTopic (source="url_input", status="pending", priority=5)
```

### Metodo 2: Email ingestor (IMAP)
```
Email inoltrata a INGESTOR_EMAIL
→ email_ingestor.py legge casella IMAP (UNSEEN)
→ Estrae subject + body
→ Claude genera topic
→ Salva ContentTopic (source="email_input", status="pending", priority=6)
→ Marca email come letta
```

### Metodo 3: SEO Agent (mensile)
```
keyword_research_job() ogni 30 giorni
→ DataForSEO API: related keywords per seed keyword
→ Propone 2 topic per ogni sito
→ Salva ContentTopic (source="seo_agent", status="pending")
```

### Metodo 4: Manuale da dashboard o Telegram
```
Dashboard /topics → form "Aggiungi topic"
oppure: /addtopic <testo> su Telegram
→ ContentTopic (source="manual", status="pending", priority=5)
```

---

## Dashboard Web — `http://localhost:8001`

### Pagine disponibili
| URL | Contenuto |
|-----|-----------|
| `/` | Overview tutti i siti: semaforo stato, contatori email/articoli |
| `/sites/{slug}` | Dettaglio sito: ultime 20 email, articoli, log |
| `/topics` | Backlog topic: filtri per status/source, approva/rigetta inline |
| `/topics/add` | Aggiunge topic manuale (form POST) |
| `/content/email/{id}` | Preview HTML email_1 e email_2 |
| `/content/article/{id}` | Preview articolo: immagine, meta SEO, contenuto |
| `/logs` | Publish log: filtri per sito/azione/tipo |
| `/config` | Config siti read-only |

### Semaforo stato sito
| Colore | Significato |
|--------|------------|
| 🟢 Verde | Contenuto pubblicato negli ultimi 30 giorni |
| 🟡 Giallo | Ultimo contenuto tra 30 e 60 giorni fa |
| 🔴 Rosso | Nessun contenuto da 60+ giorni O failure recenti (ultimi 7gg) |

### Come avviare
```bash
uvicorn dashboard.app:app --reload --port 8001
```

---

## Telegram Bot — comandi e notifiche

### Comandi disponibili
| Comando | Funzione |
|---------|---------|
| `/status` | Stato sistema, scheduler, ultima run |
| `/topics` | Lista topic pending |
| `/addtopic <testo>` | Aggiunge topic manuale |
| `/approve <id>` | Approva topic |
| `/preview <id>` | Anteprima contenuto |
| `/publish <article_db_id>` | Forza pubblicazione bozza WP |
| `/sites` | Stato di ogni sito |

### Notifiche automatiche
| Evento | Messaggio |
|--------|----------|
| Email pronta (Mautic) | Preview soggetti + bottoni Approva/Rifiuta/Anteprima |
| Template pronto (Brevo) | Nomi template + istruzioni per Automazione #9 |
| Articoli in bozza | Link preview per sito + bottoni Pubblica/Rigetta |
| Selezione topic articolo | Lista topic pending con bottoni inline |
| Errore | Contesto + messaggio errore |

### Avvio bot
```bash
python3 run_bot.py
# oppure in produzione: parte automaticamente con lo scheduler
```

---

## Scheduler — frequenze e job

| Job | Frequenza | Funzione |
|-----|-----------|---------|
| `email_job` | Ogni 15 giorni | Genera + pubblica email su tutti i siti |
| `article_job` | Ogni 15 giorni | Genera + pubblica articolo su tutti i siti WP |
| `keyword_research_job` | Ogni 30 giorni | DataForSEO → propone topic |

Configurabile via `.env`:
```
EMAIL_JOB_INTERVAL_DAYS=15
ARTICLE_JOB_INTERVAL_DAYS=15
KEYWORD_RESEARCH_INTERVAL_DAYS=30
```

---

## Cosa manca / Feature da aggiungere

### Bug noti
- [ ] **Articolo non salvato nel DB**: `test_article_job.py` pubblica su WP ma non crea il record `Article` nel DB → il counter dashboard rimane a 0. Da sistemare nello `article_job()` dello scheduler.

### Feature richieste (priorità alta)

#### 1. Force publish dalla dashboard
Attualmente non è possibile forzare la pubblicazione di email o articoli dalla dashboard — si può solo approvare/rifiutare topic. Da aggiungere:
- **Email**: bottone "Pubblica ora" su un EmailPair in bozza → lancia `email_job()` per quel sito
- **Articolo**: bottone "Pubblica su WP" su un Article in bozza → cambia status WP da draft a publish

#### 2. Piano editoriale integrato
Il backlog `/topics` mostra solo i topic pianificati. Da aggiungere una vista che integra:
- Topic pending/approved (cosa è in coda)
- Email già pubblicate (con soggetti e date)
- Articoli già pubblicati (con titoli e date)
- Deduplicazione: warning se un topic simile è già stato trattato
- Vista calendario/timeline per avere una visione completa di ciò che è già uscito e cosa uscirà

#### 3. Gestione IP Brevo
Brevo richiede approvazione IP per le chiamate API. In produzione su Coolify, approvare l'IP fisso del server una sola volta. Attualmente ogni cambio di rete richiede una nuova approvazione.

### Deployment (prossimo step)
- [ ] Push su GitHub (repository privato)
- [ ] Deploy su Coolify con Docker Compose
- [ ] Configurare variabili d'ambiente come secrets su Coolify
- [ ] Switchare `DATABASE_URL` da SQLite a PostgreSQL
- [ ] Approvare IP Coolify su Brevo (una tantum)
- [ ] Verificare che Mautic sia raggiungibile dall'IP del server Coolify

---

## Setup locale (da zero)

```bash
# 1. Dipendenze
pip3 install -r requirements.txt

# 2. Variabili d'ambiente
cp .env.example .env
# → compila tutte le chiavi API

# 3. Database
python3 -c "from core.database import create_tables; create_tables()"

# 4. Dashboard (terminale 1)
uvicorn dashboard.app:app --reload --port 8001

# 5. Bot Telegram (terminale 2)
python3 run_bot.py

# 6. Scheduler (terminale 3 — o integrato in produzione)
python3 -c "from core.scheduler import start_scheduler; import time; start_scheduler(); time.sleep(99999)"

# 7. Test unitari
python3 -m pytest tests/ -q
```

---

## Struttura file

```
herbamarketer/
├── CLAUDE.md                        # istruzioni per Claude Code
├── STATO_PROGETTO.md                # questo file
├── requirements.txt
├── .env / .env.example
├── docker-compose.yml + Dockerfile
│
├── config/
│   ├── __init__.py                  # SiteConfig, get_all_active_sites()
│   ├── sites.yaml                   # 7 siti configurati
│   ├── settings.yaml                # delay, retry, intervalli
│   └── email_topics.yaml            # backlog iniziale
│
├── agents/
│   ├── content_agent.py             # genera email (4096 tok) + articoli (8192 tok)
│   ├── seo_agent.py                 # keyword research DataForSEO
│   ├── translator_agent.py          # IT → FR/DE/EN
│   └── validator_agent.py           # quality check (score 0-100, soglia 70)
│
├── publishers/
│   ├── mautic.py                    # Mautic API: crea email + aggiunge a campagna
│   ├── brevo.py                     # Brevo API: crea template (NO campagne)
│   └── wordpress.py                 # WP REST API: bozza + immagine + Yoast meta
│
├── core/
│   ├── database.py                  # SQLAlchemy: Site, EmailPair, Article, Topic, Log
│   ├── scheduler.py                 # APScheduler: email_job, article_job, keyword_job
│   ├── telegram_bot.py              # notifiche + comandi + callback bottoni
│   ├── image_generator.py           # DALL-E 3 → Ideogram fallback
│   └── sitemap.py                   # lookup URL prodotto da sitemap XML
│
├── inputs/
│   ├── email_ingestor.py            # IMAP Gmail → topic
│   └── url_ingestor.py             # scraping URL → topic
│
├── dashboard/
│   ├── app.py                       # FastAPI: 10 route
│   └── templates/                   # 8 template Jinja2 + TailwindCSS CDN
│
└── tests/                           # 93 test unitari (tutti verdi)
```

---

## Variabili d'ambiente — riepilogo

```bash
ANTHROPIC_API_KEY          # Claude API
TELEGRAM_BOT_TOKEN         # Bot Telegram
TELEGRAM_CHAT_ID_OMAR      # Chat ID Omar
MAUTIC_URL                 # https://broadcast.herbago.info
MAUTIC_CLIENT_ID / SECRET  # OAuth2 Mautic
BREVO_API_KEY              # API key Brevo
BREVO_SENDER_NAME          # HerbaShop
BREVO_SENDER_EMAIL         # info@herbashop.it
WP_*_USER / APP_PASSWORD   # herba-api + Application Password per ogni sito
DATAFORSEO_LOGIN / PASSWORD # API DataForSEO
OPENAI_API_KEY             # DALL-E 3
DATABASE_URL               # SQLite (dev) / PostgreSQL (produzione)
EMAIL_JOB_INTERVAL_DAYS    # 15
ARTICLE_JOB_INTERVAL_DAYS  # 15
KEYWORD_RESEARCH_INTERVAL_DAYS # 30
```
