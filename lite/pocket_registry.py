"""Closed Pocket TTS V2 registry; upstream Git blob IDs are not asset SHA256s.

Asset integrity/qualification is deliberately NOT asserted by this registry.
"""
from dataclasses import dataclass
from types import MappingProxyType

PROVIDER = "pocket-tts"
CONTRACT = "pocket_cpu_api_v1"
SDK_VERSION = "4.0.1"
SDK_REVISION = "a922b912dfa366975294afb568d815b305acde5d"
DEFAULT_UI_LANGUAGE = "es"
MAX_INPUT_CHARS = 24000
MAX_CHUNK_CHARS = 320
SAMPLE_RATE = 24000
MAX_OUTPUT_SECONDS = 900
MAX_OUTPUT_BYTES = 48 * 1024 * 1024
SPEED_MIN = 0.80
SPEED_MAX = 1.25

class PocketError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)

@dataclass(frozen=True)
class ModelSpec:
    language: str
    model_variant: str
    layers: int
    label: str
    preview_text: str
    config_git_blob: str
    weights_uri: str
    tokenizer_uri: str

    @property
    def config_path(self):
        return f"pocket_tts/config/{self.model_variant}.yaml"

    @property
    def model_revision(self):
        return self.weights_uri.rsplit("@", 1)[1]

    @property
    def sample_rate(self):
        return SAMPLE_RATE

    @property
    def precision_policy(self):
        return ("fp32",)

_MODELS = (
    ModelSpec("es", "spanish_24l", 24, "Español", "La supervivencia nunca fue fácil.", "7622362fd6073c32b7088702d4647455139a68a0", "hf://kyutai/pocket-tts/languages/spanish_24l/model.safetensors@2dd944b099d06bb9edbd221fef138685711d9bf0", "hf://kyutai/pocket-tts-without-voice-cloning/languages/spanish_24l/tokenizer.json@4e1e0a3e611c51c0b4ed8174fc10f32a54644303"),
    ModelSpec("fr", "french_24l", 24, "Français", "La survie n’a jamais été facile.", "1da8ef0d71937dd3055301f5513335ed47572e16", "hf://kyutai/pocket-tts/languages/french_24l/model.safetensors@3e82814a68665eec246ff649b14c71331f955c06", "hf://kyutai/pocket-tts-without-voice-cloning/languages/french_24l/tokenizer.json@8843db76457a91db32077edf8dfcd1c0e3e755fd"),
    ModelSpec("de", "german_24l", 24, "Deutsch", "Das Überleben war nie einfach.", "a46b2f9d040aa682c0845d56fab11910d3c0812a", "hf://kyutai/pocket-tts/languages/german_24l/model.safetensors@2dd944b099d06bb9edbd221fef138685711d9bf0", "hf://kyutai/pocket-tts-without-voice-cloning/languages/german_24l/tokenizer.json@4e1e0a3e611c51c0b4ed8174fc10f32a54644303"),
    ModelSpec("pt", "portuguese_24l", 24, "Português", "Sobreviver nunca foi fácil.", "7a5d12103f1204289dfcbb5fbcf35cb6484b87a2", "hf://kyutai/pocket-tts/languages/portuguese_24l/model.safetensors@2dd944b099d06bb9edbd221fef138685711d9bf0", "hf://kyutai/pocket-tts-without-voice-cloning/languages/portuguese_24l/tokenizer.json@4e1e0a3e611c51c0b4ed8174fc10f32a54644303"),
    ModelSpec("it", "italian_24l", 24, "Italiano", "Sopravvivere non è mai stato facile.", "cf609c3177603628e4185e4a7658c96198bee81e", "hf://kyutai/pocket-tts/languages/italian_24l/model.safetensors@3e82814a68665eec246ff649b14c71331f955c06", "hf://kyutai/pocket-tts-without-voice-cloning/languages/italian_24l/tokenizer.json@4e1e0a3e611c51c0b4ed8174fc10f32a54644303"),
    ModelSpec("nl", "dutch_24l", 24, "Nederlands", "Overleven was nooit gemakkelijk.", "dc583a9e2c2e97af051111a28455caf584893138", "hf://kyutai/pocket-tts/languages/dutch_24l/model.safetensors@2dd944b099d06bb9edbd221fef138685711d9bf0", "hf://kyutai/pocket-tts-without-voice-cloning/languages/dutch_24l/tokenizer.json@4e1e0a3e611c51c0b4ed8174fc10f32a54644303"),
    ModelSpec("en", "english_2026-10", 6, "English", "Survival was never easy.", "2ca27455413aa5ae3dd5313bd1ac42782db58f73", "hf://kyutai/pocket-tts/languages/english_2026-10/model.safetensors@db04f0e653473e5c7333c69686ac92b7460b0c1d", "hf://kyutai/pocket-tts-without-voice-cloning/languages/english_2026-09/tokenizer.json@00eac05ed3d16bdc3f6b5d598874019c34a89214"),
)
REGISTRY = MappingProxyType({m.language: m for m in _MODELS})
VARIANTS = frozenset(m.model_variant for m in _MODELS)

def resolve_model(language: str, model_variant: str) -> ModelSpec:
    if language not in REGISTRY:
        raise PocketError("LANGUAGE_UNSUPPORTED")
    if model_variant not in VARIANTS:
        raise PocketError("MODEL_VARIANT_INVALID")
    spec = REGISTRY[language]
    if model_variant != spec.model_variant:
        raise PocketError("MODEL_LANGUAGE_MISMATCH")
    return spec

def discovery_entry():
    return {"id": PROVIDER, "languages": list(REGISTRY),
            "default_language": DEFAULT_UI_LANGUAGE, "backend": "CPU",
            "default_endpoint": "/pocket/v1/audio/speech"}
