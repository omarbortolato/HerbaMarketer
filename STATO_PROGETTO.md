# HerbaMarketer — Stato del Progetto
> Aggiornato: 25 marzo 2026 — sistema live su Coolify, login dashboard aggiunto

---

## Overview

HerbaMarketer è un sistema di marketing automation che genera, traduce e pubblica autonomamente contenuti (email di nurturing + articoli SEO) su 7 siti Herbalife multilingua. Supervisione umana via Telegram e dashboard web.

**Stato attuale: sistema deployato in produzione su Coolify. Dashboard protetta da login. In attesa di test E2E live.**

| Metrica | Valore |
|---------|--------|
| Siti gestiti | 7 (6 Mautic + 1 Brevo) |
| Email pubblicate (test locale) | 14 (2 per sito × 7 siti) |
| Test automatici | 93 (tutti verdi) |
| Deploy produzione | ✅ Coolify — dashboard.herbago.info |
| Login dashboard | ✅ omar / emiliano |
| API keys | ⚠️ Ruotate il 25/03 (esposizione accidentale su GitHub) |

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

## Architettura produzione

```
GitHub (omarbortolato/HerbaMarketer)
  └── push → Coolify auto-deploy
        ├── herbamarketer_db   (PostgreSQL 16)
        ├── herbamarketer_app  (FastAPI + Uvicorn — dashboard.herbago.info)
        └── herbamarketer_worker (APScheduler + Telegram bot polling)
```

- **Deploy**: push su `main` → Coolify auto-deploy
- **Database**: PostgreSQL su Coolify (migrato da SQLite locale con `migrate_to_postgres.py`)
- **Dashboard**: `http://dashboard.herbago.info` — login richiesto (omar / emiliano)

---

## Flusso Email — ogni 15 giorni per tutti i siti

### Trigger
- **Automatico**: APScheduler lancia `email_job()` ogni 15 giorni
- **Manuale**: chiamata diretta da script

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
- **Manuale**: chiamata diretta da script

### Flusso dettagliato
```
1. article_job() si avvia
2. Controlla se c'è un topic approved nel DB
   → Nessun topic: invia su Telegram lista topic pending
     con bottoni inline per selezionare → Omar sceglie → topic → "approved"
3. Genera articolo IT master (~1700 parole, H3/H4):
   - Cerca URL prodotto nella sitemap del sito (fallback: URL sito root)
   - Genera articolo con CTA bottone verde con URL reale
   - meta_title (max 60 char) + meta_description (max 155 char)
4. Validator SEO: lunghezza, struttura, keyword, claim
5. Genera immagine con DALL-E 3:
   - Scena lifestyle/natura, no prodotto, no testo
6. Per ogni sito attivo con wp_api_url:
   a. Traduce articolo nella lingua del sito
   b. Cerca URL prodotto nella sitemap del sito
      → Non trovato: usa URL sito come fallback (NON salta più il sito)
   c. Carica immagine su WP media library
   d. Pubblica articolo come bozza (status="draft" su WP)
   e. Salva record Article nel DB (status="pending_approval")
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

## Dashboard Web — `https://dashboard.herbago.info`

### Accesso
- **Login richiesto** — utenti: `omar`, `emiliano` (password condivisa)
- Cookie di sessione firmato (SessionMiddleware)

### Pagine disponibili
| URL | Contenuto |
|-----|-----------|
| `/login` | Pagina di login |
| `/` | Overview tutti i siti: semaforo stato, contatori email/articoli |
| `/sites/{slug}` | Dettaglio sito: ultime 20 email, articoli, log |
| `/topics` | Backlog topic: filtri per status/source, approva/rigetta inline |
| `/topics/add` | Aggiunge topic manuale (form POST) |
| `/content/email/{id}` | Preview HTML email_1 e email_2 |
| `/content/article/{id}` | Preview articolo: immagine, meta SEO, contenuto |
| `/logs` | Publish log: filtri per sito/azione/tipo |
| `/config` | Config siti read-only |
| `/logout` | Disconnessione |

### Semaforo stato sito
| Colore | Significato |
|--------|------------|
| 🟢 Verde | Contenuto pubblicato negli ultimi 30 giorni |
| 🟡 Giallo | Ultimo contenuto tra 30 e 60 giorni fa |
| 🔴 Rosso | Nessun contenuto da 60+ giorni O failure recenti (ultimi 7gg) |

### Contatori articoli
Il counter articoli include sia `pending_approval` (bozze WP, in attesa di approvazione Telegram) che `published` (live su WP). Così il dato è visibile subito dopo la generazione.

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
| `/report` | Report settimanale |

### Notifiche automatiche
| Evento | Messaggio |
|--------|----------|
| Email pronta (Mautic) | Preview soggetti + bottoni Approva/Rifiuta/Anteprima |
| Template pronto (Brevo) | Nomi template + istruzioni per Automazione #9 |
| Articoli in bozza | Link preview per sito + bottoni Pubblica/Rigetta |
| Selezione topic articolo | Lista topic pending con bottoni inline |
| Errore | Contesto + messaggio errore |

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

## Prossimi passi immediati

### Test E2E in produzione (priorità alta)
Il sistema è live su Coolify. Prima di considerarlo pienamente operativo:

- [ ] **Aggiornare API keys su Coolify** — le chiavi sono state ruotate il 25/03 a causa di esposizione accidentale su GitHub. Aggiornare tutti i secrets in Coolify (Anthropic, Mautic, Brevo, WP, Telegram, DataForSEO, OpenAI)
- [ ] **Approvare IP Coolify su Brevo** — al primo test, Brevo invierà notifica IP sconosciuto. Approvare una volta sola dall'interfaccia Brevo. L'IP Coolify è fisso, non servirà rifarlo
- [ ] **Test email job** — aggiungere un topic approvato in produzione e lanciare manualmente `email_job()` (via Telegram `/approve <id>` + attesa run, o trigger diretto)
- [ ] **Verifica Mautic** — controllare che le email create appaiano nelle campagne corrette su `broadcast.herbago.info`
- [ ] **Test article job** — approvare topic → verificare che gli articoli vengano pubblicati come bozze WP su tutti e 7 i siti
- [ ] **Verifica Telegram** — notifiche di bozze ricevute → cliccare "Pubblica tutto" → verificare che gli articoli vadano live su WP

### Procedura test E2E step by step
```
1. Verifica login dashboard: https://dashboard.herbago.info
2. Aggiorna API keys su Coolify → Redeploy
3. Su Telegram: /addtopic "colazione proteica e shake herbalife"
4. Su dashboard /topics: Approva topic appena creato
5. Attendi run scheduler (o triggera manualmente)
6. Telegram: ricevi notifica IP Brevo → approva su Brevo → ritesta
7. Verifica:
   - Dashboard /logs: voci "published" per tutti i siti
   - Mautic: email nelle campagne
   - Brevo: template in Marketing → Modelli
   - WordPress bozze: link preview funzionanti
8. Telegram: clicca "Pubblica tutto" sugli articoli
9. Verifica articoli live su ogni sito
```

---

## Roadmap feature

---

### FASE 2 — Centro di controllo contenuti (prossima)

#### Feature 2.1 — Importazione contenuti esistenti (PRIORITÀ ALTA)
**Obiettivo**: scaricare da WP e Mautic tutti gli articoli e le email già esistenti e mostrarli in dashboard. La dashboard diventa l'unico centro di controllo per tutti i contenuti dei 7 siti, non solo quelli generati da HerbaMarketer.

**Implementazione**:
- `sync/wp_importer.py` — `GET /wp-json/wp/v2/posts?per_page=100` per ogni sito → crea/aggiorna record `Article` nel DB con `source="imported"`
- `sync/mautic_importer.py` — `GET /api/emails` su Mautic → crea/aggiorna record `EmailPair` con `source="imported"`
- Deduplicazione per `wp_post_id` / `mautic_email_id` (idempotente)
- Trigger: comando Telegram `/sync` + job mensile automatico
- Dashboard: badge "importato" vs "generato da AI", filtro per source

#### Feature 2.2 — Check deduplicazione topic (IMPORTANTE)
**Obiettivo**: prima di generare un nuovo articolo, verificare che non esista già un contenuto sullo stesso argomento (sia tra i generati che tra gli importati).

**Implementazione**:
- Confronto semantico via Claude tra il topic in coda e i titoli degli articoli esistenti nel DB
- Fallback: keyword match semplice se Claude non è necessario
- Warning su Telegram e dashboard: "⚠️ Argomento simile già trattato: [titolo] ([data])"
- Omar decide se procedere ugualmente o scartare il topic

#### Feature 2.3 — Piano editoriale su Notion
**Obiettivo**: calendario editoriale integrato su Notion dove vedere tutto sotto controllo — cosa è uscito, cosa è in coda, cosa è pianificato.

**Implementazione**:
- Database Notion con colonne: Titolo, Tipo (email/articolo), Sito, Data pubblicazione, Status, Source (AI/importato), Link
- Sync automatico: ogni volta che HerbaMarketer pubblica un contenuto → aggiunge riga su Notion via API
- Importazione: sync da WP/Mautic aggiunge anche i contenuti esistenti su Notion
- Vista calendario Notion per avere la timeline visiva
- Notion come piano editoriale condiviso (Omar + Emiliano)

#### Feature 2.4 — Force publish dalla dashboard
**Obiettivo**: pubblicare contenuti direttamente dalla dashboard senza passare per Telegram.
- Bottone "Pubblica ora" su EmailPair → chiama Mautic/Brevo API
- Bottone "Approva e pubblica" su Article → chiama WP API `status: publish`

---

### FASE 3 — SEO Health & Competitor Intelligence

**Obiettivo**: monitoraggio continuo della salute SEO di ogni sito e identificazione opportunità per nuovi articoli. Analisi competitor per capire cosa pubblicano e dove siamo posizionati.

#### Feature 3.1 — SEO Health Check (mensile per sito)
- **Audit articoli esistenti**: controllare articoli pubblicati per link rotti, contenuto datato (>12 mesi), mancanza meta SEO
- **Ranking check**: via DataForSEO, monitorare posizioni delle keyword target per ogni sito
- **Opportunità keyword**: keyword con volume > 500, difficulty < 40, per cui non abbiamo ancora un articolo
- **Report Telegram**: ogni mese, riepilogo per sito con semaforo (verde = sano, rosso = intervento necessario)
- Dashboard: nuova sezione `/seo` con health score per sito

#### Feature 3.2 — Analisi Competitor
- Identificare i principali 3-5 competitor per ogni mercato (IT, FR, DE, EN)
- Monitorare i loro articoli di punta via DataForSEO competitor research
- Identificare gap: argomenti che i competitor trattano e noi no
- Proporre automaticamente topic basati sui gap → ContentTopic (source="competitor_gap")
- Report mensile su Telegram con top opportunità

#### Feature 3.3 — Content Refresh Agent
- Identificare articoli pubblicati da >12 mesi con calo di posizione
- Generare versione aggiornata con Claude (stessa struttura, dati aggiornati)
- Proporre su Telegram: "Articolo da aggiornare: [titolo] — posizione calata da X a Y"

---

### FASE 4 — Google Ads & Business Intelligence

**Obiettivo**: integrare i dati di performance pubblicitaria e di business per avere un quadro completo del ROI e suggerire ottimizzazioni.

#### Feature 4.1 — Google Ads Integration
- Connessione Google Ads API per ogni account (IT, FR, DE, EN, US)
- Dati: impression, click, costo, conversioni per campagna e keyword
- Correlazione con contenuti pubblicati: articoli e email → impatto sulle conversioni
- Report settimanale automatico su Telegram ogni lunedì

#### Feature 4.2 — Business Report Agent
- Lettura ordini da Google Sheet (già usato da Omar) via Google Sheets API
- KPI settimanali: ordini per sito, revenue, AOV (Average Order Value)
- Trend vs settimana precedente e vs stesso periodo anno scorso
- Correlazione contenuti → ordini: "questa settimana articolo su [topic] → +X ordini su herbago.it"
- Report ogni lunedì mattina su Telegram + aggiornamento Notion

#### Feature 4.3 — Suggerimenti ottimizzazione campagne
- Agente Claude che analizza i dati Google Ads + contenuti pubblicati
- Suggerisce: keyword da aggiungere/togliere, budget da riallocare, annunci da riscrivere
- Output: report mensile con priorità di intervento
- Non automatizza le modifiche — solo suggerisce, Omar approva

---

## Bug noti / Note tecniche

- **IP Brevo**: ogni nuovo IP richiede approvazione manuale su Brevo. Su Coolify (IP fisso) basta farlo una volta.
- **WP redirect**: herbago.it, .de, .net, .co.uk reindirizzano a www — già gestito con `follow_redirects=True` e wp_api_url corretti in sites.yaml.
- **Token Claude per articoli**: `max_tokens=8192` (vs 4096 per email) — necessario per articoli da ~1700 parole in HTML.
- **Brevo automazione**: i template vanno aggiunti manualmente a Scenario #9 — non è automatizzabile senza rischiare di corrompere la sequenza esistente di 26 email.
- **SESSION_SECRET_KEY**: aggiungere questa variabile d'ambiente su Coolify per la sicurezza della sessione dashboard (altrimenti usa il default hardcoded).

---

## Setup locale (da zero)

```bash
# 1. Dipendenze
pip3 install -r requirements.txt

# 2. Variabili d'ambiente
cp .env.example .env
# → compila tutte le chiavi API

# 3. Database
alembic upgrade head

# 4. Dashboard (terminale 1)
uvicorn dashboard.app:app --reload --port 8001

# 5. Worker (terminale 2 — scheduler + bot)
python3 run_worker.py
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
├── run_worker.py                    # entry point produzione: scheduler + bot
├── migrate_to_postgres.py           # migrazione SQLite → PostgreSQL
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
│   └── url_ingestor.py              # scraping URL → topic
│
├── dashboard/
│   ├── app.py                       # FastAPI: login + 10 route protette
│   ├── static/logo.jpg              # favicon
│   └── templates/                   # 9 template Jinja2 + TailwindCSS CDN
│
└── tests/                           # 93 test unitari (tutti verdi)
```

---

## Variabili d'ambiente — riepilogo

```bash
# ⚠️ RUOTATE IL 25/03/2026 — aggiornare su Coolify
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
DATABASE_URL               # PostgreSQL su Coolify
EMAIL_JOB_INTERVAL_DAYS    # 15
ARTICLE_JOB_INTERVAL_DAYS  # 15
KEYWORD_RESEARCH_INTERVAL_DAYS # 30
SESSION_SECRET_KEY         # segreto per cookie sessione dashboard (aggiungere su Coolify)
```
