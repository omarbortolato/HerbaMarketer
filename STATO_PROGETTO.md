# HerbaMarketer — Stato del Progetto
> Aggiornato: 5 aprile 2026 — sistema live e testato in produzione

---

## Overview

HerbaMarketer è un sistema di marketing automation che genera, traduce e pubblica autonomamente contenuti (email di nurturing + articoli SEO) su 7 siti Herbalife multilingua. L'obiettivo è azzerare il lavoro manuale di produzione contenuti, lasciando a Omar solo le decisioni strategiche (approvazione topic, review bozze) via Telegram.

**Stato attuale: sistema pienamente operativo in produzione su Coolify. Email testate e funzionanti su tutti i siti. Articoli generati e pubblicati come bozze WP. Email ingestor attivo su herbamarketerg@gmail.com.**

| Metrica | Valore |
|---------|--------|
| Siti gestiti | 7 (6 Mautic + 1 Brevo) |
| Email pubblicate | 16+ (testate in produzione) |
| Articoli generati | funzionanti con immagine DALL-E HD |
| Deploy produzione | ✅ Coolify — dashboard.herbago.info |
| Login dashboard | ✅ omar / emiliano |
| Email ingestor | ✅ herbamarketerg@gmail.com (Gmail IMAP) |

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

- **Deploy**: push su `main` → Coolify auto-deploy entro ~2 minuti
- **Database**: PostgreSQL su Coolify (migrato da SQLite locale con `migrate_to_postgres.py`)
- **Migrazioni DB**: gestite automaticamente a ogni avvio della dashboard — `ALTER TABLE IF NOT EXISTS` per nuove colonne, `create_all()` per nuove tabelle
- **Dashboard**: `https://dashboard.herbago.info` — login richiesto (omar / emiliano)

---

## Flusso Email — ogni 15 giorni per tutti i siti

### Trigger
- **Automatico**: APScheduler lancia `email_job()` ogni 15 giorni
- **Manuale dashboard**: bottone "Genera Email" → modal selezione siti → avvia in background
- **Manuale Telegram**: approvare un topic e attendere la run

### Flusso dettagliato
```
1. email_job() si avvia (opzionalmente con lista siti selezionati)
2. Seleziona il prossimo topic con status="approved" o "article_done" dal DB
   → Se nessun topic approved: log warning, notifica Telegram, job termina
3. Per il sito master (herbago_it):
   a. Genera email_1 (problema) in italiano via Claude API
   b. Genera email_2 (prodotto + soluzione) in italiano
      → URL prodotto: usa topic.product_url se impostato, altrimenti cerca in sitemap
   c. Validator controlla: lunghezza, tono, claim illegali, CTA
      → score < 70: rigenera (max 3 tentativi)
4. Per ogni sito attivo selezionato (Mautic + Brevo):
   a. Traduce la coppia email nella lingua del sito
   b. Sostituisce URL prodotto con equivalente per il sito di destinazione
      (via find_equivalent_product_url() in core/sitemap.py)
   c. Validator ricontrolla la traduzione
   d. Publisher crea le email sulla piattaforma:
      - Mautic: POST /api/emails + aggiunge alla campagna del sito
      - Brevo: POST /v3/smtp/templates (due template)
   e. Salva EmailPair nel DB con status="published"
   f. Scrive PublishLog
5. Notifiche Telegram:
   - Mautic: "📧 Nuova coppia email pronta" con bottoni Approva/Rifiuta
   - Brevo: "📧 Nuovi template Brevo pronti" con istruzioni manuali
6. Topic status → "email_done" (o "done" se articolo già fatto)
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
- **Manuale dashboard**: bottone "Genera Articolo" → modal selezione siti → avvia in background

### Flusso dettagliato
```
1. article_job() si avvia (opzionalmente con lista siti selezionati)
2. Controlla se c'è un topic approved o "email_done" nel DB
   → Nessun topic: invia su Telegram lista topic pending
     con bottoni inline per selezionare → Omar sceglie → topic → "approved"
3. Genera articolo IT master (~1700 parole, H3/H4):
   - Se topic.product_url è impostato → usa quello come URL prodotto per herbago.it
   - Altrimenti cerca in sitemap herbago.it (fallback: URL sito root)
   - Genera articolo con CTA bottone verde con URL reale
   - meta_title (max 60 char) + meta_description (max 155 char)
4. Validator SEO: lunghezza, struttura, keyword, claim
   → Validazione non bloccante: warning Telegram ma si procede anche se score < 70
5. Genera immagine con DALL-E 3 HD (quality="hd", style="natural"):
   - Prompt prefissato con "Hyperrealistic professional photography, Canon EOS 5D Mark IV..."
   - Scena lifestyle/natura correlata all'argomento, no prodotto, no testo
   - Fallback: Ideogram API
6. Per ogni sito attivo selezionato con wp_api_url:
   a. Traduce articolo nella lingua del sito
   b. Cerca URL prodotto equivalente nella sitemap del sito
      → Non trovato: usa URL sito come fallback
   c. Se wp_author_name configurato: risolve l'author ID via WP API /users?search=
   d. Carica immagine su WP media library
   e. Pubblica articolo come bozza (status="draft" su WP)
   f. Salva record Article nel DB (status="pending_approval")
7. Telegram: "Articoli pronti in bozza" con link preview per ogni sito
   + bottoni [Pubblica tutto] [Rigetta tutto]
8. Se approvato: pubblica tutti i WP draft → status="published"
```

### Struttura articolo generato
- Titolo + slug ottimizzato SEO
- Corpo ~1700 parole con H3/H4 (mai H2/H1)
- Penultimo paragrafo: "In sintesi" (mai "Conclusione")
- CTA finale: bottone verde con URL prodotto reale per ogni sito
- Featured image HD generata da AI
- Yoast SEO: meta_title e meta_description popolati via REST API
- Autore WP configurabile per sito tramite `wp_author_name` in sites.yaml

---

## Flusso Input Topic — asincrono, qualsiasi momento

### Metodo 1: Email ingestor (IMAP) — ATTIVO
```
Email inoltrata/inviata a herbamarketerg@gmail.com
→ email_ingestor.py legge casella IMAP (UNSEEN)
→ Estrae subject + body (max 3000 char)
→ Claude genera topic + keyword SEO
→ Salva ContentTopic (source="email_input", status="pending", priority=6)
→ Marca email come letta
```
**Note tecniche**:
- App Password Gmail: tutti gli spazi (inclusi \xa0 da copia-incolla Google) vengono rimossi automaticamente
- Supporta email inoltrate, email dirette, email con contenuto articolo
- TODO: rilevamento URL nel corpo email → chiama url_ingestor automaticamente

### Metodo 2: URL ingestor
```
URL inviato (script o Telegram)
→ url_ingestor.py scrapa la pagina (BeautifulSoup4)
→ Estrae testo dall'articolo/main
→ Claude genera topic + keyword
→ Salva ContentTopic (source="url_input", status="pending", priority=5)
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
Dashboard /topics → form "Aggiungi topic" (con campo URL prodotto opzionale)
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
| `/` | Overview tutti i siti: semaforo stato, contatori, bottoni genera |
| `/sites/{slug}` | Dettaglio sito: ultime 20 email, articoli, log |
| `/topics` | Backlog topic: filtri per status/source, approva con modal, cestino |
| `/content/email/{id}` | Preview HTML email_1 e email_2 |
| `/content/article/{id}` | Preview articolo: immagine, meta SEO, contenuto |
| `/logs` | Publish log: filtri per sito/azione/tipo |
| `/config` | Config siti editabile + aggiunta nuovi siti |
| `/logout` | Disconnessione |

### Funzionalità dashboard notevoli
- **Modal "Approva" topic**: cliccando Approva si apre un modal dove inserire l'URL prodotto su herbago.it (opzionale). Il sistema poi cerca automaticamente l'URL equivalente su tutti gli altri siti. Se lasciato vuoto usa Formula 1 Herbalife come default.
- **Cestino topic**: icona 🗑 su ogni riga per eliminare topic definitivamente (con confirm dialog).
- **Modal selezione siti**: cliccando "Genera Email" o "Genera Articolo" si apre un modal con checkbox per ogni sito attivo. Default: tutti selezionati. Deselezionare per fare un test su un singolo sito.
- **Semafori stato con tooltip click**: cliccando sul pallino colorato compare un tooltip con il dettaglio. Per i siti in rosso c'è un bottone "Segna come risolto" che azzera il semaforo (SiteStatusAck nel DB).
- **Aggiungi sito**: nella pagina /config, form completo per aggiungere un nuovo sito (scrive in sites.yaml).

### Semaforo stato sito
| Colore | Significato |
|--------|------------|
| 🟢 Verde | Contenuto pubblicato negli ultimi 30 giorni |
| 🟡 Giallo | Ultimo contenuto tra 30 e 60 giorni fa |
| 🔴 Rosso | Nessun contenuto da 60+ giorni O failure recenti (ultimi 7gg) non risolti |

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
| `/syncemail` | Esegue email_ingestor manualmente (legge IMAP) |
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
| `email_job` | Ogni 15 giorni | Genera + pubblica email (tutti o siti selezionati) |
| `article_job` | Ogni 15 giorni | Genera + pubblica articolo (tutti o siti selezionati) |
| `keyword_research_job` | Ogni 30 giorni | DataForSEO → propone topic |

Configurabile via `.env` o dalla dashboard `/config`:
```
EMAIL_JOB_INTERVAL_DAYS=15
ARTICLE_JOB_INTERVAL_DAYS=15
KEYWORD_RESEARCH_INTERVAL_DAYS=30
```

---

## Roadmap feature

### FASE 2 — Centro di controllo contenuti (prossima)

#### Feature 2.1 — Importazione contenuti esistenti (PRIORITÀ ALTA)
**Obiettivo**: scaricare da WP e Mautic tutti gli articoli e le email già esistenti e mostrarli in dashboard. La dashboard diventa l'unico centro di controllo per tutti i contenuti dei 7 siti, non solo quelli generati da HerbaMarketer.

**Implementazione**:
- `sync/wp_importer.py` — `GET /wp-json/wp/v2/posts?per_page=100` per ogni sito → crea/aggiorna record `Article` nel DB con `source="imported"`
- `sync/mautic_importer.py` — `GET /api/emails` su Mautic → crea/aggiorna record `EmailPair` con `source="imported"`
- Deduplicazione per `wp_post_id` / `mautic_email_id` (idempotente)
- Trigger: comando Telegram `/sync` + job mensile automatico
- Dashboard: badge "importato" vs "generato da AI", filtro per source

#### Feature 2.2 — Check deduplicazione topic
**Obiettivo**: prima di generare un nuovo articolo, verificare che non esista già un contenuto sullo stesso argomento.

**Implementazione**:
- Confronto semantico via Claude tra il topic in coda e i titoli degli articoli esistenti nel DB
- Warning su Telegram e dashboard: "⚠️ Argomento simile già trattato: [titolo] ([data])"
- Omar decide se procedere ugualmente o scartare il topic

#### Feature 2.3 — Piano editoriale su Notion
**Obiettivo**: calendario editoriale integrato su Notion — cosa è uscito, cosa è in coda, cosa è pianificato.

**Implementazione**:
- Database Notion con colonne: Titolo, Tipo, Sito, Data, Status, Source, Link
- Sync automatico: ogni pubblicazione → aggiunge riga su Notion via API
- Vista calendario per la timeline visiva

#### Feature 2.4 — Force publish dalla dashboard
**Obiettivo**: pubblicare contenuti direttamente dalla dashboard senza passare per Telegram.
- Bottone "Pubblica ora" su EmailPair → chiama Mautic/Brevo API
- Bottone "Approva e pubblica" su Article → chiama WP API `status: publish`

#### Feature 2.5 — Sblocca topic "in_progress"
**Obiettivo**: se un job si blocca a metà, il topic rimane `in_progress` per sempre. Aggiungere un bottone "Sblocca" nella dashboard (ripristina a `approved`) e/o un timeout automatico.

---

### FASE 3 — SEO Health & Competitor Intelligence

#### Feature 3.1 — SEO Health Check (mensile per sito)
- Audit articoli esistenti: link rotti, contenuto datato (>12 mesi), mancanza meta SEO
- Ranking check via DataForSEO: posizioni delle keyword target per ogni sito
- Opportunità keyword: volume > 500, difficulty < 40, senza articolo esistente
- Report Telegram mensile con semaforo per sito

#### Feature 3.2 — Analisi Competitor
- Monitoraggio top 3-5 competitor per mercato (IT, FR, DE, EN)
- Identificazione gap: argomenti che i competitor trattano e noi no
- Proposta automatica topic (source="competitor_gap")

#### Feature 3.3 — Content Refresh Agent
- Identificare articoli da >12 mesi con calo di posizione
- Generare versione aggiornata con Claude
- Proposta su Telegram: "Articolo da aggiornare: [titolo]"

---

### FASE 4 — Google Ads & Business Intelligence

#### Feature 4.1 — Google Ads Integration
- Connessione Google Ads API per ogni account (IT, FR, DE, EN, US)
- KPI: impression, click, costo, conversioni per campagna e keyword
- Correlazione con contenuti pubblicati

#### Feature 4.2 — Business Report Agent
- Lettura ordini da Google Sheet (già usato da Omar)
- KPI settimanali: ordini per sito, revenue, AOV
- Report ogni lunedì mattina su Telegram + aggiornamento Notion

#### Feature 4.3 — Suggerimenti ottimizzazione campagne
- Analisi Claude su dati Google Ads + contenuti pubblicati
- Suggerisce keyword da aggiungere/togliere, budget da riallocare
- Solo suggerimenti — Omar approva le modifiche

---

## Bug noti / Note tecniche

- **Brevo automazione**: i template vanno aggiunti manualmente a Scenario #9 — non è automatizzabile senza rischiare di corrompere la sequenza esistente.
- **WP redirect**: herbago.it, .de, .net, .co.uk reindirizzano a www — gestito con `follow_redirects=True` e wp_api_url corretti in sites.yaml.
- **Token Claude per articoli**: `max_tokens=8192` (vs 4096 per email) — necessario per articoli da ~1700 parole in HTML.
- **Topic "in_progress" bloccati**: se un job si interrompe a metà, il topic rimane `in_progress`. Reset manuale via DB o da aggiungere come feature nella dashboard.
- **Email ingestor App Password**: Google App Passwords vengono copiate con spazi visuali (inclusi \xa0). Il codice li rimuove automaticamente con `re.sub(r"[\s\xa0]", "", password)`.
- **Migrazioni DB**: gestite automaticamente a startup con `ALTER TABLE IF NOT EXISTS`. Non serve Alembic per nuove colonne su tabelle esistenti.

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
│   ├── __init__.py                  # SiteConfig, get_all_active_sites(), add_site()
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
│   └── wordpress.py                 # WP REST API: bozza + immagine + Yoast + autore
│
├── core/
│   ├── database.py                  # SQLAlchemy: Site, EmailPair, Article, Topic, Log, SiteStatusAck
│   ├── scheduler.py                 # APScheduler: email_job, article_job, keyword_job
│   │                                #   → entrambi accettano site_slugs opzionale
│   ├── telegram_bot.py              # notifiche + comandi + callback bottoni
│   ├── image_generator.py           # DALL-E 3 HD → Ideogram fallback
│   └── sitemap.py                   # lookup + cross-site match URL prodotto
│
├── inputs/
│   ├── email_ingestor.py            # IMAP Gmail → topic (herbamarketerg@gmail.com)
│   └── url_ingestor.py              # scraping URL → topic
│
├── dashboard/
│   ├── app.py                       # FastAPI: login + route protette + lifespan migrations
│   ├── static/logo.jpg
│   └── templates/
│       ├── base.html
│       ├── index.html               # overview + modal selezione siti
│       ├── topics.html              # backlog + modal approva + cestino
│       ├── config.html              # config siti + aggiungi sito
│       ├── partials/
│       │   └── job_modal.html       # modal selezione siti (incluso in index + topics)
│       └── ...altri template
│
└── tests/                           # test unitari
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
INGESTOR_EMAIL             # herbamarketerg@gmail.com
INGESTOR_PASSWORD          # Google App Password (16 caratteri, spazi rimossi auto)
DATABASE_URL               # PostgreSQL su Coolify
EMAIL_JOB_INTERVAL_DAYS    # 15
ARTICLE_JOB_INTERVAL_DAYS  # 15
KEYWORD_RESEARCH_INTERVAL_DAYS # 30
SESSION_SECRET_KEY         # segreto per cookie sessione dashboard
```
