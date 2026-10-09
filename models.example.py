# models.py — the Python model catalog for copilot-muze.
#
# Copy to <data_dir>/models.py (e.g. data/models.py) and edit. This file is
# a TEMPLATE: it is committed, your copy is gitignored (keys never live
# here anyway — they merge in from config.yaml llm.models.<name>.api_key
# or the MUZE_KEY_<NAME> environment variable).
#
# Why Python instead of YAML: pricing is logic. Peak/off-peak windows,
# per-modality rates, subscription tiers — functions, not static numbers.
#
# After editing, /restart reloads it. A broken file is logged and ignored
# (the pure-yaml registry keeps working). Entries appear in /balance and
# /cost automatically; usage pricing shows up in /cost once recorded.

from muze.model_catalog import ModelCaps, ModelSpec, PeakOffPeak, Pricing

MODELS = {
    # --- DeepSeek: text-only, tools, 谷时半价 (00:30-08:30 Beijing) ---
    "dpsk-chat": ModelSpec(
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        caps=ModelCaps(tools=True),
        pricing=Pricing(
            input_per_mtok=2.0, output_per_mtok=8.0,
            peak=PeakOffPeak(off_start="00:30", off_end="08:30", off_factor=0.5),
        ),
    ),
    "dpsk-reasoner": ModelSpec(
        base_url="https://api.deepseek.com",
        model="deepseek-reasoner",
        caps=ModelCaps(tools=False),  # reasoner has no tool_calls
        pricing=Pricing(
            input_per_mtok=4.0, output_per_mtok=16.0,
            peak=PeakOffPeak(off_start="00:30", off_end="08:30", off_factor=0.5),
        ),
    ),
    # --- MiMo (Xiaomi): the SERIES is split by capability —
    #     some ids take images, others do ASR. Declared per entry: ---
    "mimo": ModelSpec(
        base_url="https://api.xiaomimilu.com/v1",
        model="mimo-7b",
        caps=ModelCaps(tools=True),
        rpm=60,
        pricing=Pricing(input_per_mtok=0.5, output_per_mtok=1.5),
        note="纯文本主力",
    ),
    "mimo-vl": ModelSpec(
        base_url="https://api.xiaomimilu.com/v1",
        model="mimo-vl-7b",
        caps=ModelCaps(tools=True, vision=True),
        pricing=Pricing(input_per_mtok=0.5, output_per_mtok=1.5, image_per_call=0.01),
        note="图文模态",
    ),
    "mimo-audio": ModelSpec(
        base_url="https://api.xiaomimilu.com/v1",
        model="mimo-audio",
        caps=ModelCaps(asr=True),  # 语音转写
        pricing=Pricing(audio_per_minute=0.05),
        note="ASR 专用，roles.asr 指到这里",
    ),
    # --- GLM: multimodal chat on the same endpoint ---
    "glm": ModelSpec(
        base_url="https://open.bigmodel.cn/api/paas/v4/",
        model="glm-4.7",
        caps=ModelCaps(tools=True, vision=True),
        pricing=Pricing(input_per_mtok=2.0, output_per_mtok=8.0),
    ),
    # --- whisper-compatible ASR (SiliconFlow etc.) ---
    "sensevoice": ModelSpec(
        base_url="https://api.siliconflow.cn/v1",
        model="FunAudioLLM/SenseVoiceSmall",
        caps=ModelCaps(asr=True, tools=False),
        pricing=Pricing(audio_per_minute=0.1),
    ),
}

# Wire the roles in config.yaml:
#   llm:
#     roles:
#       chat:   [mimo, dpsk-chat, glm]
#       vision: [mimo-vl, glm]
#       asr:    [mimo-audio, sensevoice]
#       value:  [mimo, dpsk-chat]
#     models:                      # keys only, per entry
#       mimo:      {api_key: "..."}
#       mimo-vl:   {api_key: "..."}
#       glm:       {api_key: "..."}
