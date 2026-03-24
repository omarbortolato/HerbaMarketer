"""
tests/test_agents.py

Unit tests for content_agent and validator_agent.
All Claude API calls are mocked — no real API calls made.

Run with:
    pytest tests/test_agents.py -v
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from agents.content_agent import (
    EmailContent,
    EmailPairOutput,
    ArticleOutput,
    generate_email_pair,
    generate_article,
)
from agents.validator_agent import (
    ValidationResult,
    validate_content,
    _check_illegal_claims,
    _check_cta_present,
    _check_html_structure_article,
    _check_word_count,
    _check_no_conclusion_heading,
    _word_count,
)
from config import SiteConfig


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def herbago_it_config() -> SiteConfig:
    return SiteConfig(
        slug="herbago_it",
        url="https://herbago.it",
        language="it",
        locale="it-IT",
        platform="mautic",
        mautic_campaign_id=1,
        email_prefix="ITA",
        wp_api_url="https://herbago.it/wp-json/wp/v2",
        active=True,
    )


@pytest.fixture
def herbago_fr_config() -> SiteConfig:
    return SiteConfig(
        slug="herbago_fr",
        url="https://herbago.fr",
        language="fr",
        locale="fr-FR",
        platform="mautic",
        mautic_campaign_id=2,
        email_prefix="FR",
        wp_api_url="https://herbago.fr/wp-json/wp/v2",
        active=True,
    )


@pytest.fixture
def mock_email_1_json() -> dict:
    """Valid Email 1 JSON response from Claude."""
    return {
        "subject": "Ti svegli stanco ogni mattina?",
        "preheader": "Scopri perché la colazione proteica cambia tutto",
        "body_html": (
            "<p>Iniziare la giornata senza energia è un problema molto diffuso.</p>"
            "<p>Le conseguenze si sentono subito: difficoltà di concentrazione, "
            "cali di energia a metà mattina, voglia di zuccheri. "
            "Non è una questione di volontà ma di nutrizione.</p>"
            "<p>La soluzione potrebbe essere più semplice di quanto pensi: "
            "una colazione bilanciata con le giuste proteine può fare la differenza.</p>"
            "<p>Scopri come nel nostro articolo dedicato: "
            "<a href='https://herbago.it/colazione-proteica'>Leggi qui →</a></p>"
            "<p>Cliente Privilegiato: <a href='https://herbago.it/cp'>Registrati</a> | "
            "Distributore: <a href='https://herbago.it/dist'>Info</a></p>"
        ) * 5,  # repeat to reach 300+ words
        "body_text": (
            "Iniziare la giornata senza energia è un problema molto diffuso. "
            "Le conseguenze si sentono subito. Scopri di più su herbago.it"
        ),
    }


@pytest.fixture
def mock_email_2_json() -> dict:
    """Valid Email 2 JSON response from Claude."""
    return {
        "subject": "Formula 1: la colazione che cercavi",
        "preheader": "Proteine, vitamine e gusto in un solo pasto",
        "body_html": (
            "<p>Ricordi quella sensazione di stanchezza al mattino? "
            "Formula 1 Herbalife è stato progettato per risolvere esattamente questo.</p>"
            "<p>Con 17g di proteine per porzione, vitamine e minerali essenziali, "
            "Formula 1 ti garantisce energia duratura fino all'ora di pranzo.</p>"
            "<p>Come usarlo: mescola 2 misurini in 250ml di latte o bevanda vegetale.</p>"
            "<p><a href='https://herbago.it/formula-1'>Acquista Formula 1 →</a></p>"
            "<p>Cliente Privilegiato: <a href='https://herbago.it/cp'>Registrati</a></p>"
        ) * 6,
        "body_text": (
            "Formula 1 Herbalife risolve il problema della stanchezza mattutina. "
            "Acquista su herbago.it/formula-1"
        ),
    }


@pytest.fixture
def mock_article_json() -> dict:
    """Valid article JSON response from Claude."""
    body = (
        "<h3>Il problema della stanchezza mattutina</h3>"
        "<p>La colazione proteica è fondamentale per iniziare la giornata. "
        "Molte persone si svegliano già stanche, con poca voglia di affrontare la giornata. "
        "La ragione spesso risiede in una colazione inadeguata dal punto di vista nutrizionale.</p>"
        "<h3>Cosa succede al tuo corpo senza proteine</h3>"
        "<p>Senza un adeguato apporto proteico al mattino, il corpo entra velocemente in carenza "
        "di aminoacidi essenziali. Questo provoca cali di energia, difficoltà di concentrazione "
        "e una costante ricerca di zuccheri rapidi durante la mattinata.</p>"
        "<h3>Le migliori soluzioni per una colazione proteica</h3>"
        "<p>Esistono diverse opzioni: uova, yogurt greco, frullati proteici. "
        "Ognuna ha i suoi vantaggi in termini di praticità e apporto nutrizionale.</p>"
        "<h4>Formula 1 Herbalife come soluzione ideale</h4>"
        "<p>Formula 1 Herbalife offre una soluzione pratica e completa: "
        "17g di proteine, vitamine e minerali in un unico pasto da preparare in pochi secondi.</p>"
        "<h3>In sintesi</h3>"
        "<p>Investire nella colazione proteica è uno dei cambiamenti più efficaci "
        "per migliorare energia e benessere quotidiano. 🌱</p>"
    )
    return {
        "title": "Colazione Proteica: Guida Completa per Iniziare la Giornata con Energia",
        "slug": "colazione-proteica-guida-completa",
        "content_html": body * 8,  # repeat to reach ~1500 words
        "meta_title": "Colazione Proteica: Come Iniziare la Giornata",
        "meta_description": (
            "Scopri come la colazione proteica può trasformare la tua mattina. "
            "Consigli pratici e il ruolo di Formula 1 Herbalife."
        ),
        "image_prompt": (
            "Hyperrealistic morning scene: wooden table with a glass of green smoothie, "
            "fresh fruits, sunlight streaming through a window, no people, no text, "
            "no products visible, soft warm tones, wellness lifestyle."
        ),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_claude_response(content: dict) -> MagicMock:
    """Build a mock anthropic.Message-like object."""
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=json.dumps(content))]
    return mock_response


# ---------------------------------------------------------------------------
# content_agent tests
# ---------------------------------------------------------------------------


class TestGenerateEmailPair:

    def test_returns_email_pair_output(
        self,
        herbago_it_config: SiteConfig,
        mock_email_1_json: dict,
        mock_email_2_json: dict,
    ):
        """generate_email_pair returns an EmailPairOutput with both emails."""
        responses = [
            _make_claude_response(mock_email_1_json),
            _make_claude_response(mock_email_2_json),
        ]

        with patch("agents.content_agent.anthropic.Anthropic") as mock_anthropic:
            mock_client = MagicMock()
            mock_anthropic.return_value = mock_client
            mock_client.messages.create.side_effect = responses

            result = generate_email_pair(
                topic="Colazione proteica: come iniziare la giornata con energia",
                site_config=herbago_it_config,
            )

        assert isinstance(result, EmailPairOutput)
        assert isinstance(result.email_1, EmailContent)
        assert isinstance(result.email_2, EmailContent)
        assert result.language == "it"
        assert result.site_slug == "herbago_it"

    def test_email_1_fields_populated(
        self,
        herbago_it_config: SiteConfig,
        mock_email_1_json: dict,
        mock_email_2_json: dict,
    ):
        """Email 1 has non-empty subject, preheader, body_html, body_text."""
        responses = [
            _make_claude_response(mock_email_1_json),
            _make_claude_response(mock_email_2_json),
        ]

        with patch("agents.content_agent.anthropic.Anthropic") as mock_anthropic:
            mock_client = MagicMock()
            mock_anthropic.return_value = mock_client
            mock_client.messages.create.side_effect = responses
            result = generate_email_pair("test topic", herbago_it_config)

        assert result.email_1.subject == mock_email_1_json["subject"]
        assert result.email_1.preheader == mock_email_1_json["preheader"]
        assert len(result.email_1.body_html) > 0
        assert len(result.email_1.body_text) > 0

    def test_email_2_fields_populated(
        self,
        herbago_it_config: SiteConfig,
        mock_email_1_json: dict,
        mock_email_2_json: dict,
    ):
        """Email 2 has non-empty subject, preheader, body_html, body_text."""
        responses = [
            _make_claude_response(mock_email_1_json),
            _make_claude_response(mock_email_2_json),
        ]

        with patch("agents.content_agent.anthropic.Anthropic") as mock_anthropic:
            mock_client = MagicMock()
            mock_anthropic.return_value = mock_client
            mock_client.messages.create.side_effect = responses
            result = generate_email_pair("test topic", herbago_it_config)

        assert result.email_2.subject == mock_email_2_json["subject"]
        assert result.email_2.preheader == mock_email_2_json["preheader"]
        assert len(result.email_2.body_html) > 0

    def test_raises_if_api_key_missing(self, herbago_it_config: SiteConfig):
        """Raises EnvironmentError when ANTHROPIC_API_KEY is not set."""
        with patch.dict("os.environ", {}, clear=True):
            # Remove the key if present
            import os
            os.environ.pop("ANTHROPIC_API_KEY", None)
            with pytest.raises(EnvironmentError, match="ANTHROPIC_API_KEY"):
                generate_email_pair("test topic", herbago_it_config)

    def test_raises_on_invalid_json(
        self,
        herbago_it_config: SiteConfig,
    ):
        """Raises ValueError when Claude returns non-JSON text."""
        bad_response = MagicMock()
        bad_response.content = [MagicMock(text="Sorry, I cannot generate that.")]

        with patch("agents.content_agent.anthropic.Anthropic") as mock_anthropic:
            mock_client = MagicMock()
            mock_anthropic.return_value = mock_client
            mock_client.messages.create.return_value = bad_response

            with pytest.raises(ValueError, match="invalid JSON"):
                generate_email_pair("test topic", herbago_it_config)

    def test_strips_markdown_code_fences(
        self,
        herbago_it_config: SiteConfig,
        mock_email_1_json: dict,
        mock_email_2_json: dict,
    ):
        """Claude sometimes wraps JSON in ```json ... ``` — should be stripped."""
        wrapped_json = f"```json\n{json.dumps(mock_email_1_json)}\n```"
        response_1 = MagicMock()
        response_1.content = [MagicMock(text=wrapped_json)]
        response_2 = _make_claude_response(mock_email_2_json)

        with patch("agents.content_agent.anthropic.Anthropic") as mock_anthropic:
            mock_client = MagicMock()
            mock_anthropic.return_value = mock_client
            mock_client.messages.create.side_effect = [response_1, response_2]
            result = generate_email_pair("test topic", herbago_it_config)

        assert result.email_1.subject == mock_email_1_json["subject"]

    def test_claude_called_twice(
        self,
        herbago_it_config: SiteConfig,
        mock_email_1_json: dict,
        mock_email_2_json: dict,
    ):
        """Claude API is called exactly twice per email pair (once per email)."""
        responses = [
            _make_claude_response(mock_email_1_json),
            _make_claude_response(mock_email_2_json),
        ]

        with patch("agents.content_agent.anthropic.Anthropic") as mock_anthropic:
            mock_client = MagicMock()
            mock_anthropic.return_value = mock_client
            mock_client.messages.create.side_effect = responses
            generate_email_pair("test topic", herbago_it_config)

        assert mock_client.messages.create.call_count == 2

    def test_model_is_claude_sonnet_45(
        self,
        herbago_it_config: SiteConfig,
        mock_email_1_json: dict,
        mock_email_2_json: dict,
    ):
        """Confirms the correct model ID is used."""
        responses = [
            _make_claude_response(mock_email_1_json),
            _make_claude_response(mock_email_2_json),
        ]

        with patch("agents.content_agent.anthropic.Anthropic") as mock_anthropic:
            mock_client = MagicMock()
            mock_anthropic.return_value = mock_client
            mock_client.messages.create.side_effect = responses
            generate_email_pair("test topic", herbago_it_config)

        calls = mock_client.messages.create.call_args_list
        for call in calls:
            assert call.kwargs.get("model") == "claude-sonnet-4-5"

    def test_french_site_uses_correct_language(
        self,
        herbago_fr_config: SiteConfig,
        mock_email_1_json: dict,
        mock_email_2_json: dict,
    ):
        """Language in output matches site config language."""
        responses = [
            _make_claude_response(mock_email_1_json),
            _make_claude_response(mock_email_2_json),
        ]

        with patch("agents.content_agent.anthropic.Anthropic") as mock_anthropic:
            mock_client = MagicMock()
            mock_anthropic.return_value = mock_client
            mock_client.messages.create.side_effect = responses
            result = generate_email_pair("test topic", herbago_fr_config)

        assert result.language == "fr"
        assert result.site_slug == "herbago_fr"


class TestGenerateArticle:

    def test_returns_article_output(
        self,
        herbago_it_config: SiteConfig,
        mock_article_json: dict,
    ):
        """generate_article returns an ArticleOutput with all fields."""
        with patch("agents.content_agent.anthropic.Anthropic") as mock_anthropic:
            mock_client = MagicMock()
            mock_anthropic.return_value = mock_client
            mock_client.messages.create.return_value = _make_claude_response(mock_article_json)

            result = generate_article(
                topic="Colazione proteica",
                keyword="colazione proteica",
                site_config=herbago_it_config,
            )

        assert isinstance(result, ArticleOutput)
        assert result.title == mock_article_json["title"]
        assert result.slug == mock_article_json["slug"]
        assert len(result.content_html) > 0
        assert len(result.meta_title) <= 60
        assert len(result.meta_description) <= 155
        assert len(result.image_prompt) > 0
        assert result.language == "it"

    def test_raises_on_missing_fields(self, herbago_it_config: SiteConfig):
        """Raises ValueError if article response is missing required fields."""
        incomplete = {"title": "Test", "slug": "test"}  # missing most fields
        with patch("agents.content_agent.anthropic.Anthropic") as mock_anthropic:
            mock_client = MagicMock()
            mock_anthropic.return_value = mock_client
            mock_client.messages.create.return_value = _make_claude_response(incomplete)

            with pytest.raises(ValueError, match="Missing fields"):
                generate_article("test topic", "test keyword", herbago_it_config)


class TestWordCount:

    def test_counts_words_in_plain_text(self):
        assert _word_count("hello world foo bar") == 4

    def test_strips_html_tags(self):
        assert _word_count("<p>hello <b>world</b></p>") == 2

    def test_empty_string(self):
        assert _word_count("") == 0

    def test_html_only(self):
        assert _word_count("<br/><hr/>") == 0


# ---------------------------------------------------------------------------
# validator_agent tests
# ---------------------------------------------------------------------------


class TestCheckWordCount:

    def test_email_1_within_range(self):
        # Build ~330 word HTML body
        words = " ".join(["parola"] * 330)
        penalty, issues, _ = _check_word_count(f"<p>{words}</p>", "email_1")
        assert penalty == 0
        assert issues == []

    def test_email_1_too_short(self):
        words = " ".join(["parola"] * 200)
        penalty, issues, _ = _check_word_count(f"<p>{words}</p>", "email_1")
        assert penalty > 0
        assert any("too short" in i for i in issues)

    def test_email_1_too_long(self):
        words = " ".join(["parola"] * 500)
        penalty, issues, _ = _check_word_count(f"<p>{words}</p>", "email_1")
        assert penalty > 0
        assert any("too long" in i for i in issues)

    def test_article_within_range(self):
        words = " ".join(["parola"] * 1600)
        penalty, issues, _ = _check_word_count(f"<p>{words}</p>", "article")
        assert penalty == 0

    def test_article_too_short(self):
        words = " ".join(["parola"] * 1000)
        penalty, issues, _ = _check_word_count(f"<p>{words}</p>", "article")
        assert penalty > 0


class TestCheckIllegalClaims:

    def test_no_illegal_claims(self):
        penalty, issues, _ = _check_illegal_claims(
            "<p>Questo prodotto supporta il benessere generale.</p>"
        )
        assert penalty == 0
        assert issues == []

    def test_detects_cura(self):
        penalty, issues, _ = _check_illegal_claims(
            "<p>Questo prodotto cura il diabete.</p>"
        )
        assert penalty > 0
        assert len(issues) > 0

    def test_detects_guarisce(self):
        penalty, issues, _ = _check_illegal_claims(
            "<p>Guarisce i problemi di peso.</p>"
        )
        assert penalty > 0

    def test_detects_clinically_proven_english(self):
        penalty, issues, _ = _check_illegal_claims(
            "<p>This product is clinically proven to reduce weight.</p>"
        )
        assert penalty > 0

    def test_case_insensitive(self):
        penalty, issues, _ = _check_illegal_claims("<p>CURA il diabete.</p>")
        assert penalty > 0


class TestCheckCtaPresent:

    def test_url_counts_as_cta(self):
        penalty, issues, _ = _check_cta_present(
            "<p>Visit <a href='https://herbago.it/shop'>our shop</a></p>"
        )
        assert penalty == 0

    def test_acquista_counts_as_cta(self):
        penalty, issues, _ = _check_cta_present("<p>Acquista ora!</p>")
        assert penalty == 0

    def test_no_cta_penalized(self):
        penalty, issues, _ = _check_cta_present(
            "<p>Questo è un articolo senza call to action.</p>"
        )
        assert penalty > 0
        assert len(issues) > 0

    def test_scopri_counts_as_cta(self):
        penalty, issues, _ = _check_cta_present("<p>Scopri di più sul nostro sito!</p>")
        assert penalty == 0


class TestCheckHtmlStructureArticle:

    def test_valid_h3_only(self):
        content = "<h3>Titolo</h3><p>Testo</p><h3>Altro</h3><p>Altro testo</p>"
        penalty, issues, _ = _check_html_structure_article(content)
        assert penalty == 0

    def test_h2_detected(self):
        content = "<h2>Titolo proibito</h2><p>Testo</p><h3>OK</h3>"
        penalty, issues, _ = _check_html_structure_article(content)
        assert penalty > 0
        assert any("h2" in i.lower() for i in issues)

    def test_h1_detected(self):
        content = "<h1>Titolo proibito</h1><p>Testo</p><h3>OK</h3>"
        penalty, issues, _ = _check_html_structure_article(content)
        assert penalty > 0
        assert any("h1" in i.lower() for i in issues)

    def test_missing_h3_penalized(self):
        content = "<p>Solo paragrafi senza heading h3.</p>"
        penalty, issues, _ = _check_html_structure_article(content)
        assert penalty > 0


class TestCheckNoConclusionHeading:

    def test_no_conclusion(self):
        content = "<h3>In sintesi</h3><p>Testo finale.</p>"
        penalty, issues, _ = _check_no_conclusion_heading(content)
        assert penalty == 0

    def test_conclusione_detected(self):
        content = "<h3>Conclusione</h3><p>Testo finale.</p>"
        penalty, issues, _ = _check_no_conclusion_heading(content)
        assert penalty > 0

    def test_case_insensitive(self):
        content = "<h3>CONCLUSIONI</h3><p>Testo finale.</p>"
        penalty, issues, _ = _check_no_conclusion_heading(content)
        assert penalty > 0


class TestValidateContent:

    def test_valid_email_1_passes(self):
        words = " ".join(["parola"] * 350)
        body = f"<p>{words}</p><p><a href='https://herbago.it/articolo'>Scopri di più</a></p>"
        result = validate_content(body, "email_1", language="it")

        assert isinstance(result, ValidationResult)
        assert result.passed is True
        assert result.score >= 70

    def test_short_email_fails(self):
        body = "<p>Troppo corta.</p><a href='https://herbago.it'>CTA</a>"
        result = validate_content(body, "email_1", language="it")

        assert result.passed is False
        assert result.score < 70
        assert len(result.issues) > 0

    def test_illegal_claim_fails(self):
        words = " ".join(["parola"] * 350)
        body = f"<p>{words} Questo prodotto cura il diabete.</p><a href='https://x.it'>CTA</a>"
        result = validate_content(body, "email_1", language="it")

        assert result.passed is False
        assert any("claim" in issue.lower() for issue in result.issues)

    def test_valid_article_passes(self):
        words = " ".join(["parola"] * 1600)
        body = (
            f"<h3>Sezione principale</h3><p>{words}</p>"
            "<h3>In sintesi</h3><p>Riassunto finale.</p>"
            "<p><a href='https://herbago.it/prodotto'>Acquista</a></p>"
        )
        result = validate_content(body, "article", language="it")

        assert result.passed is True
        assert result.score >= 70

    def test_article_with_h2_fails(self):
        words = " ".join(["parola"] * 1600)
        body = (
            f"<h2>Sezione proibita</h2><p>{words}</p>"
            "<h3>In sintesi</h3><p>Fine.</p>"
            "<p><a href='https://herbago.it'>Acquista</a></p>"
        )
        result = validate_content(body, "article", language="it")

        assert result.passed is False
        assert any("h2" in issue.lower() for issue in result.issues)

    def test_result_has_suggestions_on_failure(self):
        body = "<p>Email troppo corta senza CTA.</p>"
        result = validate_content(body, "email_1", language="it")

        assert result.passed is False
        assert len(result.suggestions) > 0

    def test_score_is_int_in_range(self):
        words = " ".join(["parola"] * 350)
        body = f"<p>{words}</p><a href='https://herbago.it'>CTA</a>"
        result = validate_content(body, "email_1")

        assert isinstance(result.score, int)
        assert 0 <= result.score <= 100
