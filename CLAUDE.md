# HerbaMarketer — CLAUDE.md

## Panoramica del progetto

HerbaMarketer è un sistema di marketing automation per la rete di siti e-commerce Herbalife di Omar.
Automatizza la creazione, traduzione e pubblicazione di email di nurturing e articoli SEO su 6+ siti
multilingua, con supervisione umana via Telegram e dashboard web di monitoraggio.

**Obiettivo business:** generare contenuti di qualità professionale per tutti i siti in modo scalabile,
controllato e completamente automatizzato, con human-in-the-loop solo per decisioni strategiche.

---

## Siti gestiti

| Sito | Nazione | Lingua | Prefisso Mautic | Stato |
|------|---------|--------|-----------------|-------|
| herbago.it | Italia | it | ITA | attivo |
| herbago.fr | Francia | fr | FR | attivo |
| herbago.de | Germania | de | DE | attivo |
| herbago.net | Irlanda | en | EN_IE | attivo |
| herbago.co.uk | UK | en | EN_UK | attivo |
| hlifeus.com | USA | en | EN_US | attivo |
| herbashop.it | Italia | it | - | gestito separatamente via Brevo |

Architettura progettata per aggiungere nuovi siti modificando solo `config/sites.yaml`.

---

## Stack tecnologico

- **Runtime:** Python 3.11+
- **LLM:** Anthropic Claude API (`claude-sonnet-4-6`)
- **SEO data:** DataForSEO API (keyword research)
- **Email platform herbago:** Mautic API (https://broadcast.herbago.info/)
- **Email platform herbashop.it:** Brevo API
- **CMS:** WordPress REST API (tutti i siti)
- **Immagini:** DALL-E 3 HD (`quality="hd"`, `style="natural"`) → Ideogram fallback
- **Notifiche:** Telegram Bot API (supervisione human-in-the-loop)
- **Database:** SQLite (sviluppo Mac) / PostgreSQL (produzione Coolify)
- **Web dashboard:** FastAPI + Jinja2 + TailwindCSS CDN
- **Scheduler:** APScheduler (in-process, processo separato `run_worker.py`)
- **Config:** YAML per sito + .env per secrets
- **Deploy:** Docker Compose (Mac per dev, Coolify per produzione — auto-deploy su push main)

---

## Struttura directory

```
herbamarketer/
├── CLAUDE.md                     # questo file
├── README.md
├── docker-compose.yml
├── .env.example
├── requirements.txt
│
├── config/
│   ├── sites.yaml                # config per ogni sito
│   ├── email_topics.yaml         # backlog argomenti email
│   └── settings.yaml             # configurazioni globali
│
├── core/
│   ├── __init__.py
│   ├── database.py               # SQLAlchemy models + migrations
│   ├── scheduler.py              # APScheduler jobs
│   └── telegram_bot.py           # Telegram bot per supervisione
│
├── agents/
│   ├── __init__.py
│   ├── content_agent.py          # genera email e articoli via Claude API
│   ├── seo_agent.py              # keyword research + proposta argomenti
│   ├── validator_agent.py        # controllo qualità contenuti
│   └── translator_agent.py       # traduzione IT → altre lingue
│
├── publishers/
│   ├── __init__.py
│   ├── wordpress.py              # WordPress REST API client
│   ├── mautic.py                 # Mautic API client
│   └── brevo.py                  # Brevo API client
│
├── dashboard/
│   ├── app.py                    # FastAPI app
│   ├── templates/                # Jinja2 templates
│   └── static/                   # CSS, JS
│
├── inputs/
│   ├── email_ingestor.py         # processa email inoltrate (Gmail API o IMAP)
│   └── url_ingestor.py           # scraping URL per input manuale
│
└── tests/
    ├── test_agents.py
    ├── test_publishers.py
    └── fixtures/
```

---

## Database schema

```sql
-- Siti configurati
CREATE TABLE sites (
    id INTEGER PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,         -- es. "herbago_it"
    url TEXT NOT NULL,
    language TEXT NOT NULL,            -- es. "it"
    locale TEXT NOT NULL,              -- es. "it-IT"
    mautic_campaign_id INTEGER,
    email_prefix TEXT,                 -- es. "ITA"
    platform TEXT DEFAULT 'mautic',    -- "mautic" | "brevo"
    active BOOLEAN DEFAULT true,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Argomenti contenuto (backlog)
CREATE TABLE content_topics (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,               -- descrizione argomento
    source TEXT NOT NULL,              -- "seo_agent" | "email_input" | "manual" | "url_input"
    source_detail TEXT,                -- URL, testo email, query keyword
    product_sku TEXT,                  -- SKU prodotto associato (se applicabile)
    status TEXT DEFAULT 'pending',     -- "pending" | "approved" | "rejected" | "in_progress" | "done"
    priority INTEGER DEFAULT 5,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Coppie email generate
CREATE TABLE email_pairs (
    id INTEGER PRIMARY KEY,
    topic_id INTEGER REFERENCES content_topics(id),
    site_id INTEGER REFERENCES sites(id),
    language TEXT NOT NULL,
    email_1_subject TEXT,              -- email "problema"
    email_1_body TEXT,
    email_2_subject TEXT,              -- email "prodotto"
    email_2_body TEXT,
    mautic_email_1_id INTEGER,         -- ID su Mautic dopo pubblicazione
    mautic_email_2_id INTEGER,
    status TEXT DEFAULT 'draft',       -- "draft" | "published" | "failed"
    published_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Articoli generati
CREATE TABLE articles (
    id INTEGER PRIMARY KEY,
    topic_id INTEGER REFERENCES content_topics(id),
    site_id INTEGER REFERENCES sites(id),
    language TEXT NOT NULL,
    title TEXT,
    slug TEXT,
    content TEXT,
    meta_title TEXT,
    meta_description TEXT,
    image_prompt TEXT,
    image_url TEXT,
    wp_post_id INTEGER,                -- ID su WordPress dopo pubblicazione
    status TEXT DEFAULT 'draft',       -- "draft" | "pending_approval" | "published" | "failed"
    published_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Log pubblicazioni
CREATE TABLE publish_log (
    id INTEGER PRIMARY KEY,
    entity_type TEXT NOT NULL,         -- "email_pair" | "article"
    entity_id INTEGER NOT NULL,
    site_id INTEGER REFERENCES sites(id),
    action TEXT NOT NULL,              -- "published" | "failed" | "rejected"
    detail TEXT,                       -- messaggio errore o dettaglio
    created_at TIMESTAMP DEFAULT NOW()
);

-- Snapshot keyword research
CREATE TABLE keyword_snapshots (
    id INTEGER PRIMARY KEY,
    site_id INTEGER REFERENCES sites(id),
    keyword TEXT NOT NULL,
    search_volume INTEGER,
    difficulty INTEGER,
    trend_score FLOAT,
    snapshot_date DATE NOT NULL,
    raw_data JSON
);
```

---

## Config sites.yaml (esempio)

```yaml
sites:
  herbago_it:
    url: "https://herbago.it"
    language: "it"
    locale: "it-IT"
    platform: "mautic"
    mautic_campaign_id: 1
    email_prefix: "ITA"
    wp_api_url: "https://herbago.it/wp-json/wp/v2"
    active: true

  herbago_fr:
    url: "https://herbago.fr"
    language: "fr"
    locale: "fr-FR"
    platform: "mautic"
    mautic_campaign_id: 2
    email_prefix: "FR"
    wp_api_url: "https://herbago.fr/wp-json/wp/v2"
    active: true

  herbashop_it:
    url: "https://herbashop.it"
    language: "it"
    locale: "it-IT"
    platform: "brevo"
    brevo_list_id: 1
    wp_api_url: "https://herbashop.it/wp-json/wp/v2"
    active: true

  # Aggiungere nuovi siti qui — nessun cambio al codice necessario
```

---

## Flusso Email (ogni 15 giorni per sito)

```
1. Scheduler triggers email_job()
2. content_agent.py seleziona prossimo topic approvato dal backlog
3. Genera email_1 (problema) in italiano
4. Genera email_2 (prodotto + soluzione) in italiano
5. validator_agent.py controlla: lunghezza, tono, assenza claim illegali, CTA presente
6. translator_agent.py traduce per ogni lingua attiva
7. validator_agent.py ri-controlla ogni traduzione
8. publishers/mautic.py crea le email con prefisso corretto (es. ITA_001_xxx)
9. Associa alla campagna Mautic del sito
10. Telegram notifica Omar con preview email + link Mautic per review
11. publish_log registra esito
```

**Struttura email:**
- Email 1: Problema/bisogno (300-400 parole) + link articolo blog o sito
- Email 2: Prodotto Herbalife come soluzione (350-450 parole) + CTA acquisto + footer Cliente Privilegiato / Distributore

---

## Flusso Articoli (ogni 15 giorni, human-in-the-loop)

```
INPUT FONTI (asincrono, qualsiasi momento):
  A. Email inoltrata a indirizzo dedicato → email_ingestor.py estrae testo e crea topic
  B. Messaggio Telegram diretto al bot → crea topic manuale
  C. URL linkato su Telegram → url_ingestor.py scrapa e crea topic
  D. seo_agent.py ogni mese fa keyword research → propone 2 topic automatici

SELEZIONE ARGOMENTO (ogni 15 giorni):
  1. Bot Telegram invia a Omar: "Scegli argomento quindicina [data]"
     con lista topic pending come bottoni inline
  2. Omar seleziona (o scrive argomento libero)
  3. Topic status → "approved"

GENERAZIONE E PUBBLICAZIONE:
  4. content_agent.py genera articolo IT (~1700 parole, H3/H4, no H2, no conclusione)
  5. validator_agent.py controlla: SEO, lunghezza, struttura, coerenza prodotto
  6. Genera prompt immagine iper-realistica (benessere, natura, no prodotto, no testo)
  7. Chiama image API → ottiene immagine
  8. translator_agent.py traduce articolo per ogni lingua attiva
     - Controlla che il prodotto esista nel sito di destinazione (via sitemap/PIM)
     - Se prodotto assente in un paese → salta quel sito e notifica
  9. validator_agent.py controlla ogni traduzione
  10. publishers/wordpress.py pubblica come bozza su ogni sito
  11. Telegram: "Articoli pronti in bozza — [lista siti con link preview]"
      Bottoni: [Approva tutto] [Rigetta] [Approva singolo sito]
  12. Se approvato → pubblica (status: published)
  13. publish_log registra tutto
```

---

## Agenti AI: prompt engineering

### content_agent — Email 1 (problema)

```
System: Sei il marketing manager di [sito] che vende prodotti Herbalife.
Il tuo mercato è [paese], lingua [lingua]. Scrivi in modo professionale ma caldo,
mai aggressivo commercialmente. Non nominare mai concorrenti.
Non fare claim medici non verificabili (es. "cura il diabete").
Usa emoji con parsimonia.

Task: Scrivi un'email di nurturing sul tema: [argomento]
- Oggetto: max 50 caratteri, curiosità o problema riconoscibile
- Preheader: max 80 caratteri
- Corpo: 300-400 parole in [lingua]
- Struttura: problema → conseguenze → accenno soluzione → CTA link articolo
- Footer standard: link Cliente Privilegiato, link Distributore
- NO menzione prodotto specifico
- Output JSON: {subject, preheader, body_html, body_text}
```

### content_agent — Email 2 (prodotto)

```
Task: Scrivi un'email che presenta il prodotto [prodotto] come soluzione al problema [argomento]
- Oggetto: focus sul beneficio del prodotto
- Corpo: 350-450 parole in [lingua]
- Struttura: richiama problema → presenta prodotto → benefici specifici → come usarlo → CTA acquisto
- Link prodotto: [url prodotto su sito]
- Footer standard
- Output JSON: {subject, preheader, body_html, body_text}
```

### content_agent — Articolo

```
Task: Scrivi un articolo blog SEO in [lingua] su: [argomento]
- Lunghezza: 1600-1800 parole (MAI meno di 1500)
- Keyword primaria: [keyword] — usala nel titolo, primo paragrafo, 2-3 volte nel testo
- Struttura: intro problema → sviluppo approfondito → soluzioni generali → prodotto Herbalife come soluzione → CTA
- Tag: solo H3 e H4, mai H2 e mai H1
- Ultimo paragrafo: chiamalo "In sintesi" o simile (MAI "Conclusione")
- Emoji: max 3-4 nell'intero articolo
- NO linee separatrici
- Dopo articolo: meta_title (max 60 char) e meta_description (max 155 char)
- Output JSON: {title, slug, content_html, meta_title, meta_description, image_prompt}
- image_prompt: scena iper-realistica, benessere e natura, NO prodotti, NO testo, NO persone riconoscibili
```

### validator_agent

```
Controlla il contenuto generato rispetto a questi criteri:
1. Lunghezza nella fascia target (email: 300-450 parole, articolo: 1500-1800)
2. Nessun claim medico non verificabile ("cura", "guarisce", "clinicamente provato" se non è vero)
3. CTA presente e link placeholder corretto
4. Tono coerente con brand (professionale, caldo, non aggressivo)
5. Struttura corretta (H3/H4 per articoli, niente H2)
6. Keyword presente nel titolo e nel testo (per articoli)
7. Output JSON: {passed: bool, score: 0-100, issues: [lista problemi], suggestions: [lista suggerimenti]}
Se score < 70: rigetta e richiede rigenerazione.
```

---

## Telegram Bot — Comandi

```
/status          — stato sistema (scheduler, ultima run, errori)
/topics          — lista topic pending
/addtopic [testo] — aggiunge topic manuale
/approve [id]    — approva topic per prossima run
/preview [id]    — mostra anteprima contenuto generato
/publish [id]    — forza pubblicazione contenuto approvato
/sites           — stato di ogni sito (ultimi articoli, email)
/report          — report settimanale sintetico
```

---

## Dashboard Web (FastAPI)

Pagine:
- `/` — overview: tutti i siti, semaforo stato (verde/giallo/rosso), contatori, bottoni Genera Email/Articolo con modal selezione siti
- `/sites/{slug}` — dettaglio sito: email pubblicate, articoli, log recenti
- `/topics` — backlog argomenti: filtri per stato/fonte, approva con modal product_url, cestino, ordinamento colonne
- `/content/email/{id}` — preview HTML email_1 e email_2
- `/content/article/{id}` — preview articolo: immagine, meta SEO, contenuto
- `/logs` — publish log con filtri per sito/azione/tipo
- `/config` — config siti editabile + form aggiungi nuovo sito
- `/login`, `/logout` — autenticazione

**Componenti JS notevoli:**
- `job_modal.html` (partial) — modal selezione siti per Genera Email/Articolo, incluso in index.html e topics.html
- Click-to-toggle tooltips sui semafori (non hover, evita bug di sparizione)
- Modal approve topic con data-* attributes (evita bug di escaping caratteri speciali nei titoli)

---

## Priorità di sviluppo (fasi)

### Fase 1 — MVP Email ✅ COMPLETATA
- [x] Setup progetto Python, Docker, database SQLite/PostgreSQL
- [x] Config loader (sites.yaml, .env)
- [x] content_agent: generazione coppia email per tutti i siti
- [x] validator_agent: controllo base (score 0-100, soglia 70)
- [x] translator_agent: IT → FR, DE, EN
- [x] Mautic publisher: crea email con prefisso, associa campagna
- [x] Brevo publisher: crea template per herbashop.it
- [x] Telegram bot: notifica + bottoni approva/rigetta
- [x] Scheduler: job ogni 15 giorni per sito
- [x] publish_log
- [x] Deploy Coolify con auto-deploy su push GitHub

### Fase 2 — Articoli ✅ COMPLETATA
- [x] seo_agent: keyword research via DataForSEO
- [x] email_ingestor: Gmail IMAP (herbamarketerg@gmail.com)
- [x] url_ingestor: scraping URL via BeautifulSoup
- [x] Telegram topic selection flow
- [x] WordPress publisher: bozza con immagine + Yoast meta + autore WP
- [x] DALL-E 3 HD image generation (quality="hd", style="natural")
- [x] Cross-site product URL matching via sitemap (find_equivalent_product_url)
- [x] product_url per topic (impostabile da dashboard al momento dell'approvazione)

### Fase 3 — Dashboard ✅ COMPLETATA
- [x] FastAPI app + login (omar/emiliano)
- [x] Overview con semafori stato sito (click-to-toggle tooltip + "Segna come risolto")
- [x] Pagina dettaglio sito
- [x] Backlog topic: filtri, approva con modal product_url, cestino, ordinamento
- [x] Modal selezione siti per Genera Email / Genera Articolo
- [x] Log viewer con filtri
- [x] Config siti editabile + form aggiungi nuovo sito
- [x] Auto-migrazione DB a startup (nuove colonne senza Alembic)
- [x] Deploy su Coolify con Docker Compose

### Fase 4 — herbashop.it ✅ COMPLETATA
- [x] Brevo publisher integrato nel flusso principale
- [x] WordPress publisher per herbashop.it
- [x] Integrazione nel flusso principale come sito aggiuntivo

### Fase 5 — Miglioramenti in corso
- [x] **Sblocco topic "in_progress"**: bottone "⚠ Reset" visibile solo per topic con status=in_progress, POST /topics/{id}/reset → riporta a "approved". Con confirm dialog. Utile quando il worker crasha a metà job.
- [ ] **URL detection email ingestor**: se il corpo email contiene un URL, chiama url_ingestor automaticamente
- [x] **Importazione articoli esistenti da WP**: `publishers/wp_importer.py` — paginazione automatica, upsert per (wp_post_id, site_id), word_count calcolato strippando HTML, bottone "Sync articoli da WP" in /sites/{slug}, tabella importati ordinabile/filtrabile separata da articoli generati
- [x] **Deduplicazione topic**: `GET /topics/{id}/duplicates` — estrae le prime 4 parole significative (>3 char) del topic, cerca con AND di LIKE su articles.title, mostra warning nel modal Approva con link WP e sito. Non blocca l'approvazione.
- [ ] **Piano editoriale Notion**: sync automatico pubblicazioni → database Notion con vista calendario
- [ ] **Force publish dalla dashboard**: pubblica contenuti senza passare per Telegram
- [ ] **SEO Health Check**: audit mensile articoli + ranking check per sito
- [ ] **Analisi competitor**: gap analysis automatica → topic (source="competitor_gap")
- [ ] **Content Refresh Agent**: identifica articoli datati, propone aggiornamento
- [ ] **Google Ads Integration**: KPI campagne + correlazione con contenuti
- [ ] **Business Report Agent**: ordini da Google Sheet → report Telegram ogni lunedì

---

## Regole per Claude Code

1. **Ogni agente è stateless** — riceve input, restituisce output JSON, nessuno stato interno
2. **Config first** — nessun valore hardcodato nel codice, tutto in YAML o .env
3. **Fail gracefully** — ogni publisher ha retry logic (3 tentativi) e log dettagliato su failure
4. **Idempotenza** — se un contenuto è già pubblicato (check su DB), non ripubblicare
5. **Test per ogni agente** — almeno un test unitario con fixture per ogni agent
6. **Logging strutturato** — usa `structlog` con JSON output per compatibilità Coolify
7. **Secrets mai nel codice** — API keys solo da .env, mai committate
8. **Un file per responsabilità** — nessun file supera 400 righe, splitta se necessario
9. **Lingua del codice** — Python in inglese, commenti e log in inglese, contenuti generati in lingua del sito
10. **Backward compatible** — aggiungere un sito NON richiede modifiche al codice

---

## Variabili d'ambiente richieste (.env.example)

```bash
# Anthropic
ANTHROPIC_API_KEY=

# Mautic
MAUTIC_URL=https://broadcast.herbago.info
MAUTIC_CLIENT_ID=
MAUTIC_CLIENT_SECRET=

# Brevo
BREVO_API_KEY=

# WordPress (una per sito — lette da sites.yaml con reference al nome env)
WP_HERBAGO_IT_USER=
WP_HERBAGO_IT_APP_PASSWORD=
WP_HERBAGO_FR_USER=
WP_HERBAGO_FR_APP_PASSWORD=
# ... ecc per ogni sito

# Telegram
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID_OMAR=
TELEGRAM_CHAT_ID_EMILIANO=  # opzionale per co-supervisione

# DataForSEO
DATAFORSEO_LOGIN=
DATAFORSEO_PASSWORD=

# Image generation (scegli uno)
IDEOGRAM_API_KEY=
# oppure:
OPENAI_API_KEY=

# Email ingestor (IMAP)
INGESTOR_EMAIL=herbamarketerg@gmail.com
INGESTOR_PASSWORD=     # Google App Password 16 char (spazi rimossi automaticamente)
INGESTOR_IMAP_HOST=imap.gmail.com
INGESTOR_IMAP_PORT=993

# Database
DATABASE_URL=sqlite:///./herbamarketer.db
# su Coolify: postgresql://user:pass@host/dbname

# Scheduler
EMAIL_JOB_INTERVAL_DAYS=15
ARTICLE_JOB_INTERVAL_DAYS=15
KEYWORD_RESEARCH_INTERVAL_DAYS=30

# Dashboard auth
SESSION_SECRET_KEY=    # segreto per cookie di sessione (generare con: python3 -c "import secrets; print(secrets.token_hex(32))")
```

---

## Note operative

- **Mautic naming convention:** `{PREFIX}_{NNN}_{argomento_breve}` es. `ITA_001_colazione_proteica`
- **WordPress:** pubblica sempre come bozza (`status: draft`) finché non approvato da Telegram
- **Prodotto non disponibile in un paese:** usa URL sito come fallback (NON salta il sito), logga warning su Telegram
- **Articolo in italiano:** è sempre il master. Le traduzioni referenziano l'ID dell'articolo IT padre
- **Rate limits:** Mautic e WordPress hanno rate limit — delay 1s tra chiamate consecutive
- **Product URL:** impostabile per topic (campo product_url in ContentTopic). Se presente, find_equivalent_product_url() lo cerca in tutte le sitemap degli altri siti. Se assente, fallback su Formula 1 Herbalife.
- **WP author:** configurabile per sito con wp_author_name in sites.yaml. La dashboard lo mostra in /config. Il publisher risolve l'ID WP via GET /wp/v2/users?search= e lo cachea per sessione.
- **Migrazioni DB:** a ogni avvio della dashboard FastAPI, il lifespan handler esegue `create_tables()` + `ALTER TABLE IF NOT EXISTS` per nuove colonne. Non serve Alembic per aggiunte semplici.
- **Article.source:** `"generated"` (default, articoli HerbaMarketer) vs `"wordpress_import"` (importati da WP). Le due sezioni nella site_detail sono separate. Gli articoli importati NON hanno topic_id.
- **Article nuove colonne:** `excerpt`, `wp_url`, `wp_published_at`, `word_count`, `source` — aggiunte via migration automatica al startup.
- **wp_importer status mapping:** WP `publish` → `published`, tutti gli altri valori (draft, private, ecc.) vengono mantenuti as-is. Il sync importa tutti gli status (publish, draft, private) per dare visibilità completa.
- **Email ingestor App Password:** Google App Passwords hanno spazi visuali (\xa0). Il codice li rimuove automaticamente: `re.sub(r"[\s\xa0]", "", password)`.
- **Tooltip semafori:** usare JS click-to-toggle (non CSS hover) — con hover il tooltip sparisce prima che si possa cliccare il bottone "Segna come risolto".
- **Modal e attributi HTML:** per passare dati variabili a funzioni JS inline, usare data-* attributes + `| e` filter (Jinja2 escape), NON interpolazione inline — evita bug con titoli contenenti virgolette o apostrofi.
- **Brevo automazione:** i template vanno aggiunti manualmente a Scenario #9 su Brevo — non automatizzabile senza rischiare di corrompere la sequenza esistente di 26 email.
