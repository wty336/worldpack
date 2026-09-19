"""⑤ 训练脚本的守卫（不需要 GPU、不需要模型权重）。

重点钉三件事：
1. **关 thinking 的守卫真的会响** —— 用 Qwen3 的**实测渲染形态**做夹具；
2. **加权采样真的是 4:3:2 且跨任务打乱**（HF 默认采样器会让模块成块出现，计划 §5 明令禁止）；
3. DDP 分片不重不漏（两卡各拿一半，合起来正好一份）。
"""
from __future__ import annotations

import json
from collections import Counter

import pytest

from scripts.train_sidechannel import (MIX, MixedSampler, guard_render, load_split,
                                       render_pair, run_name)

# Qwen3 实测形态（2026-09-18 在真 tokenizer 上量出来的）：
#   enable_thinking=False ⇒ 生成前缀以「空 think 块」结尾，答案段干净
QWEN3_PROMPT = ("<|im_start|>system\n你是判定校验员<|im_end|>\n"
                "<|im_start|>user\n材料<|im_end|>\n"
                "<|im_start|>assistant\n<think>\n\n</think>\n\n")


class _StubTok:
    """**忠实**模拟 Qwen3 的 chat template（这一点很要紧）。

    初版桩忽略了 `messages`、只会吐固定串 —— 于是训练脚本里"两次渲染都喂含答案的
    messages"这个**真 bug** 照样全绿（它在真实数据上才被守卫抓到）。忠实模拟的代价很小：
    按 messages 逐条拼、`enable_thinking=False` 时给 assistant 加**空 think 块**、
    `add_generation_prompt=True` 时补一个 assistant 前缀。
    """

    def __init__(self, answer="通过"):
        self.answer = answer

    def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True,
                            enable_thinking=False):
        think = "" if enable_thinking else "<think>\n\n</think>\n\n"
        out = ""
        for m in messages:
            pre = think if m["role"] == "assistant" else ""
            out += f"<|im_start|>{m['role']}\n{pre}{m['content']}<|im_end|>\n"
        if add_generation_prompt:
            out += f"<|im_start|>assistant\n{think}"
        return out


def _msgs(answer="通过"):
    return [{"role": "system", "content": "你是判定校验员"},
            {"role": "user", "content": "材料"},
            {"role": "assistant", "content": answer}]


# --- 1. 渲染与关 thinking 守卫 ---------------------------------------------

def test_render_pair_splits_at_the_generation_boundary():
    prompt, completion = render_pair(_StubTok(), _msgs())
    # 前缀只到 user 为止 + assistant 的空 think 块；答案段**只有**答案（不含前缀内容）
    assert prompt.endswith("<|im_start|>assistant\n<think>\n\n</think>\n\n"), repr(prompt[-40:])
    assert "通过" not in prompt, "生成前缀里不该出现答案（否则等于把答案喂给模型）"
    assert completion == "通过<|im_end|>\n", repr(completion)
    guard_render(prompt, completion)          # 实测形态必须过守卫


def test_render_pair_rejects_messages_without_assistant_turn():
    """messages 末条必须是 assistant —— 否则 prompt 渲染会喂进"半截对话"。"""
    with pytest.raises(RuntimeError, match="末条必须是 assistant"):
        render_pair(_StubTok(), _msgs()[:2])


def test_guard_catches_thinking_leak_in_the_answer():
    """答案段里出现 `<think>` ⇒ 关 thinking 失败，必须报错。

    （踩过的坑：断言写成 `" thinking"` **带空格**，而真实标记是 `<think>` ⇒ 假阴性。）
    """
    with pytest.raises(RuntimeError, match="think 标记"):
        guard_render(QWEN3_PROMPT, "<think>\n用户想让我判断…\n</think>\n\n通过<|im_end|>\n")
    with pytest.raises(RuntimeError, match="think 标记"):
        guard_render(QWEN3_PROMPT, "通过</think>\n")


def test_guard_catches_template_change():
    """生成前缀结尾不再是空 think 块 ⇒ 模板/kwargs 变了，训练与推理分布会不一致。"""
    with pytest.raises(RuntimeError, match="空 think 块"):
        guard_render("<|im_start|>assistant\n", "通过<|im_end|>\n")


def test_render_pair_rejects_inconsistent_rendering():
    """两次渲染若不自洽（完整串不以生成前缀开头）⇒ 报错 —— 这是挡"喂错 messages"的兜底。"""
    class _Bad(_StubTok):
        def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True,
                                enable_thinking=False):
            if add_generation_prompt:
                return "别的前缀"
            return super().apply_chat_template(messages, tokenize, False, enable_thinking)

    with pytest.raises(RuntimeError, match="渲染不自洽"):
        render_pair(_Bad(), _msgs())


# --- 2. 加权采样（4:3:2 + 跨任务打乱）--------------------------------------

def _modules(n_ex=400, n_ju=300, n_co=100):
    return ["extract"] * n_ex + ["judge"] * n_ju + ["compress"] * n_co


def test_mix_follows_the_spec():
    assert MIX == {"extract": 4, "judge": 3, "compress": 2}    # 计划 §5 的比值绊线


def test_sampler_draws_by_weight_and_shuffles_across_tasks():
    mods = _modules()
    s = MixedSampler(mods, MIX, seed=1)
    drawn = [mods[i] for i in s]
    share = Counter(drawn)
    total = len(drawn)
    # 样本量 800，4:3:2 ⇒ 期望 44%/33%/22%，给 ±4pp 容差（固定 seed，结果确定）
    assert abs(share["extract"] / total - 4 / 9) < 0.04, share
    assert abs(share["judge"] / total - 3 / 9) < 0.04, share
    assert abs(share["compress"] / total - 2 / 9) < 0.04, share
    # 跨任务打乱：不该出现"前 200 条全是 extract"这种块状
    first_half = set(drawn[:total // 2])
    assert len(first_half) == 3, f"前半段只有 {first_half} —— 模块成块出现了"


def test_sampler_epoch_changes_order_and_is_deterministic():
    mods = _modules()
    s = MixedSampler(mods, MIX, seed=7)
    a = list(s)
    s.set_epoch(1)
    b = list(s)
    assert a != b, "换 epoch 应换一批（否则每轮看同一顺序）"
    s.set_epoch(0)
    assert list(s) == a, "同 seed 同 epoch 必须完全可复现"


def test_sampler_shards_across_ranks_without_overlap():
    """DDP：两卡各取一半，**跨卡不重**、长度相同（不重不漏，且不会一张卡先跑完）。"""
    mods = _modules()
    r0 = list(MixedSampler(mods, MIX, seed=3, num_replicas=2, rank=0))
    r1 = list(MixedSampler(mods, MIX, seed=3, num_replicas=2, rank=1))
    assert len(r0) == len(r1) == len(mods) // 2, "两张卡步数必须相同（否则 DDP 挂起）"
    assert not (set(r0) & set(r1)), "两张卡拿到同一条 = 重复训练同一份数据"


def test_sampler_oversamples_the_small_module():
    """compress 只占 10.9%，但要按 2/9 抽 ⇒ **重复采样**它（4:3:2 是权重不是配额）。"""
    mods = _modules()
    s = MixedSampler(mods, MIX, seed=5)
    drawn = Counter(mods[i] for i in s)
    compress_pool = mods.count("compress")
    assert drawn["compress"] > compress_pool * 0.9, (
        f"compress 被抽 {drawn['compress']} 次（池子 {compress_pool} 条），"
        f"应接近按权重所需的量")


def test_sampler_refuses_a_missing_module():
    """混比里声明了但没有数据的模块 ⇒ 报错（数据没导出全时必须响亮地失败）。"""
    with pytest.raises(RuntimeError, match="没有数据"):
        MixedSampler(["extract"], MIX, seed=1)


# --- 3. 数据读取与训练名 -----------------------------------------------------

def test_load_split_tags_module_and_tolerates_missing(tmp_path):
    (tmp_path / "judge.train.jsonl").write_text(
        json.dumps({"id": "j1", "messages": []}, ensure_ascii=False) + "\n", encoding="utf-8")
    rows = load_split(tmp_path, "train")
    assert [r["id"] for r in rows] == ["j1"] and rows[0]["module"] == "judge"
    assert load_split(tmp_path, "dev") == []          # 缺文件 = 空，不崩


def test_run_name_carries_the_key_hyperparams(tmp_path):
    class A:
        model = "/home/ubuntu/models/Qwen3-14B"
        lora_r, epochs, run_name = 16, 5, None
    assert run_name(A()) == "qwen3-14b-lora-r16-ep5-4-3-2"
    A.run_name = "自定义"
    assert run_name(A()) == "自定义", "显式指定的名字优先（便于多组对照）"
