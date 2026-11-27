"""Closed Pocket TTS V2 registry with verified asset pins.

Config Git blob IDs identify the upstream YAML at SDK tag v4.0.1;
config_sha256 is the SHA256 of that YAML file. weights_sha256 is the
Hugging Face LFS SHA256 of model.safetensors at the pinned revision;
tokenizer_sha256 is the SHA256 of the downloaded tokenizer.json at its
pinned revision. Preset embedding pins live in lite/pocket_pins.py.
Production qualification is still NOT asserted by this registry.
"""
from dataclasses import dataclass
from types import MappingProxyType

PROVIDER = "pocket-tts"
CONTRACT = "pocket_cpu_api_v1"
SDK_VERSION = "4.0.1"
SDK_WHEEL_SHA256 = "a6baf5fd9195399a632d7ef1cb75d4ee772b8d3534c85b90164b39c687a69961"
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
    config_sha256: str
    weights_sha256: str
    weights_size: int
    tokenizer_sha256: str
    tokenizer_size: int
    sdk_replace_characters: tuple

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

    @property
    def preset_names(self):
        from .pocket_pins import PRESET_SHA256
        return tuple(PRESET_SHA256[self.model_variant])

    def preset_sha256(self, name: str) -> str:
        from .pocket_pins import PRESET_SHA256
        try:
            return PRESET_SHA256[self.model_variant][name]
        except KeyError:
            raise PocketError("VOICE_NOT_FOUND") from None

_MODELS = (
    ModelSpec("es", "spanish_24l", 24, "Español", "La supervivencia nunca fue fácil.", "7622362fd6073c32b7088702d4647455139a68a0", "hf://kyutai/pocket-tts/languages/spanish_24l/model.safetensors@2dd944b099d06bb9edbd221fef138685711d9bf0", "hf://kyutai/pocket-tts-without-voice-cloning/languages/spanish_24l/tokenizer.json@4e1e0a3e611c51c0b4ed8174fc10f32a54644303", "2fb7fc04acc8fb750c8ce47ff99fc73e26102266e40ffd50be7021a873121508", "c5ce7e8b8fa3cf0e59d4dc2befa301ed73a49e2506dcc73a6470fc298d034840", 672178676, "9ade09bbdc64ab2b59952b90773fa0890b780ece4fd5aef0d36a00c6ad5f0531", 246610, tuple({"\"": "", "“": "", "”": "", "„": "", "«": "", "»": "", "’": "'", "‘": "'", "(": "", ")": "", "[": "", "]": "", "¡": "", "¿": "", "–": ","}.items())),
    ModelSpec("fr", "french_24l", 24, "Français", "La survie n’a jamais été facile.", "1da8ef0d71937dd3055301f5513335ed47572e16", "hf://kyutai/pocket-tts/languages/french_24l/model.safetensors@3e82814a68665eec246ff649b14c71331f955c06", "hf://kyutai/pocket-tts-without-voice-cloning/languages/french_24l/tokenizer.json@8843db76457a91db32077edf8dfcd1c0e3e755fd", "987aee4794b00a0667ade78520c5625017f7e08084ed34f8e2351ca990306a29", "deaee4da0688c157932219dcaa62e6e7584f2b15805314ad088a6e2ae1d103f8", 672178676, "6211acffe9cb95172f71f0051f18e26ea305b2b32ea00b331ee976551c70887f", 246403, tuple({"\"": "", "“": "", "”": "", "„": "", "«": "", "»": "", "’": "'", "‘": "'", ":": ",", "(": "", ")": "", "[": "", "]": "", "–": ","}.items())),
    ModelSpec("de", "german_24l", 24, "Deutsch", "Das Überleben war nie einfach.", "a46b2f9d040aa682c0845d56fab11910d3c0812a", "hf://kyutai/pocket-tts/languages/german_24l/model.safetensors@2dd944b099d06bb9edbd221fef138685711d9bf0", "hf://kyutai/pocket-tts-without-voice-cloning/languages/german_24l/tokenizer.json@4e1e0a3e611c51c0b4ed8174fc10f32a54644303", "086a0acd5010e4475cde8224ec3d73b147dd5d6eb36a0ddbdcab64fe8346d000", "580b9077400c48a801e130d972e5e603f29d4263a65df1a5fa115b185d89aeff", 672178676, "2d77849811e3d1b6ae80e5b11b203f57e1847901104d8a0573b34f46f8dddb53", 245966, tuple({"\"": "", "“": "", "”": "", "„": "", "«": "", "»": "", "’": "'", "‘": "'", "(": "", ")": "", "[": "", "]": "", "–": ",", "—": ","}.items())),
    ModelSpec("pt", "portuguese_24l", 24, "Português", "Sobreviver nunca foi fácil.", "7a5d12103f1204289dfcbb5fbcf35cb6484b87a2", "hf://kyutai/pocket-tts/languages/portuguese_24l/model.safetensors@2dd944b099d06bb9edbd221fef138685711d9bf0", "hf://kyutai/pocket-tts-without-voice-cloning/languages/portuguese_24l/tokenizer.json@4e1e0a3e611c51c0b4ed8174fc10f32a54644303", "fdaec3c1de6c64c5a57618275cc068d5fc14bd78bf7bb06af47e5cd0a74c122f", "d7c4af1b7afe4e15b8d976a939cc9158eb793a4ee0a7754ce6ee47f10c05bc7f", 672178676, "b33e269f7f042a763080d71ba087d74d2f6759674b238a723c028e436b1a25ab", 247014, tuple({"\"": "", "“": "", "”": "", "„": "", "«": "", "»": "", "’": "'", "‘": "'", "(": "", ")": "", "[": "", "]": "", "–": ","}.items())),
    ModelSpec("it", "italian_24l", 24, "Italiano", "Sopravvivere non è mai stato facile.", "cf609c3177603628e4185e4a7658c96198bee81e", "hf://kyutai/pocket-tts/languages/italian_24l/model.safetensors@3e82814a68665eec246ff649b14c71331f955c06", "hf://kyutai/pocket-tts-without-voice-cloning/languages/italian_24l/tokenizer.json@4e1e0a3e611c51c0b4ed8174fc10f32a54644303", "7c1c1ff13335a33b660275e47ad812a866590d14132fcdabc79eba83a80a65c0", "b318edeec49ad67da6c3e6a78c667648ed893a51850c71e482b5f9e0c087525a", 672178676, "4bc1c0d4d54f9480183e94c94cfabf74fd89b0912537d4ef607d1982c4a381ec", 246586, tuple({"\"": "", "“": "", "”": "", "„": "", "«": "", "»": "", "’": "'", "‘": "'", "(": "", ")": "", "[": "", "]": "", "–": ","}.items())),
    ModelSpec("nl", "dutch_24l", 24, "Nederlands", "Overleven was nooit gemakkelijk.", "dc583a9e2c2e97af051111a28455caf584893138", "hf://kyutai/pocket-tts/languages/dutch_24l/model.safetensors@2dd944b099d06bb9edbd221fef138685711d9bf0", "hf://kyutai/pocket-tts-without-voice-cloning/languages/dutch_24l/tokenizer.json@4e1e0a3e611c51c0b4ed8174fc10f32a54644303", "a62d007001f732f18c8ef7ca9b3ae479dfc571f343bc62a1df68ec474afcc12a", "fad28cbfed470bbb44f52d9c2d2f8f42e5e03620bfd147ef315ecc7c0fc73e93", 672178676, "e6f610b98c427641b4ab0e1024dca7843e3d6b620871a14b204932e42c5a1736", 245206, tuple({"\"": "", "“": "", "”": "", "„": "", "«": "", "»": "", "’": "'", "‘": "'", "(": "", ")": "", "[": "", "]": "", "–": ","}.items())),
    ModelSpec("en", "english_2026-10", 6, "English", "Survival was never easy.", "2ca27455413aa5ae3dd5313bd1ac42782db58f73", "hf://kyutai/pocket-tts/languages/english_2026-10/model.safetensors@db04f0e653473e5c7333c69686ac92b7460b0c1d", "hf://kyutai/pocket-tts-without-voice-cloning/languages/english_2026-09/tokenizer.json@00eac05ed3d16bdc3f6b5d598874019c34a89214", "dd92bc4b65b4721dc4f25b9d7664500cc90f7130526c4f0bdb832f686c54b3be", "bc96b82436c5ec00d3118ef3b01bc3358423e1766ecae3dca0aeea84789e7d74", 219029196, "f498428e1eafee50492f7be13dc9bfafcfc12e508cd0eb1b01c92ecd5d8c6687", 245020, tuple({"’": "'", "‘": "'"}.items())),
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
