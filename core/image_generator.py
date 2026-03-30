"""
core/image_generator.py

Generates hyper-realistic lifestyle/wellness images for blog articles.

Provider selection (in order of priority):
  1. DALL-E 3 via OpenAI API — if OPENAI_API_KEY is set
  2. Ideogram v2 via HTTP API — if IDEOGRAM_API_KEY is set

Public API:
    generate_image(prompt) -> str   # returns public image URL
"""

import os

import httpx
import structlog

log = structlog.get_logger(__name__)

# Prepended to every prompt to enforce photorealism regardless of the article topic.
_PHOTO_PREFIX = (
    "Hyperrealistic professional photography, Canon EOS 5D Mark IV, 85mm lens, "
    "natural soft light, shallow depth of field, 8K ultra-high resolution, "
    "photojournalistic wellness style, no text overlays, no logos, "
    "no recognizable people: "
)


# ---------------------------------------------------------------------------
# DALL-E 3
# ---------------------------------------------------------------------------


def _generate_dalle3(prompt: str) -> str:
    """Generate image with DALL-E 3 HD. Returns the image URL."""
    try:
        import openai
    except ImportError:
        raise ImportError(
            "openai package not installed — run: pip install openai"
        )

    client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    full_prompt = _PHOTO_PREFIX + prompt

    response = client.images.generate(
        model="dall-e-3",
        prompt=full_prompt,
        size="1792x1024",
        quality="hd",
        style="natural",
        n=1,
    )

    url = response.data[0].url
    log.info("dalle3_image_generated", url=url[:80] if url else "")
    return url


# ---------------------------------------------------------------------------
# Ideogram v2
# ---------------------------------------------------------------------------


def _generate_ideogram(prompt: str) -> str:
    """Generate image with Ideogram v2 API. Returns the image URL."""
    api_key = os.getenv("IDEOGRAM_API_KEY", "")
    if not api_key:
        raise EnvironmentError("IDEOGRAM_API_KEY is not set")

    full_prompt = _PHOTO_PREFIX + prompt

    resp = httpx.post(
        "https://api.ideogram.ai/generate",
        headers={"Api-Key": api_key, "Content-Type": "application/json"},
        json={
            "image_request": {
                "prompt": full_prompt,
                "aspect_ratio": "ASPECT_16_9",
                "model": "V_2",
                "magic_prompt_option": "OFF",  # Prefix already sets style; disable rewrite
            }
        },
        timeout=60,
    )
    resp.raise_for_status()

    url = resp.json()["data"][0]["url"]
    log.info("ideogram_image_generated", url=url[:80] if url else "")
    return url


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_image(prompt: str) -> str:
    """
    Generate a lifestyle/wellness image from a descriptive prompt.

    Tries DALL-E 3 first (if OPENAI_API_KEY is set), then Ideogram.

    Args:
        prompt: Image description from content_agent. Should describe a
                wellness/nature scene related to the article topic.
                A photorealism prefix is added automatically.

    Returns:
        Public URL of the generated image (valid for ~1 hour for DALL-E 3,
        longer for Ideogram).

    Raises:
        RuntimeError: if no image generation provider is configured.
    """
    openai_key = os.getenv("OPENAI_API_KEY", "")
    ideogram_key = os.getenv("IDEOGRAM_API_KEY", "")

    if openai_key:
        log.info("image_generator_provider", provider="dalle3")
        return _generate_dalle3(prompt)

    if ideogram_key:
        log.info("image_generator_provider", provider="ideogram")
        return _generate_ideogram(prompt)

    raise RuntimeError(
        "No image generation provider configured. "
        "Set OPENAI_API_KEY (DALL-E 3) or IDEOGRAM_API_KEY (Ideogram)."
    )
