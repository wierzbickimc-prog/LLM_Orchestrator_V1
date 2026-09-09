# MTP speculative-decode depth: per-model measured results

Source: `mtplx tune --depths 1,2,3` results in `~/.mtplx/tuning.json` (this
file is the raw source of truth and will accumulate more entries over time
-- what follows is a snapshot/distillation, not a copy, so re-check the raw
file if a model gets re-tuned). Suite: `cold-long-code-192`. Hardware:
Apple M1 Max, 64GB.

## Why this matters

`depth` controls how many tokens ahead mtplx speculatively drafts per step
before verification. Every drafted token costs compute whether or not it's
accepted -- so the right depth isn't "as deep as possible," it's wherever
acceptance probability stops paying for the extra draft cost. That point is
architecture-dependent, and the two families in this project sit on
opposite sides of it.

## Results

| Model | Profile | Best depth | tok/s @ best | vs autoregressive | Depth-3 acceptance (position 3) |
|---|---|---|---|---|---|
| Ornith-1.5-35B-A3B (MoE) | sustained | **1** | 60.0 | 1.16x | 1% |
| Qwen3.6-35B-A3B Speed (MoE) | sustained | **1** | 64.4 | 1.11x | 24% |
| Qwen3.6-35B-A3B Balance (MoE) | sustained | **1** | 46.4 | 1.05x | 23% |
| Qwen3.8-27B Speed (dense) | turbo | **3** | 28.6 | 1.91x | 81% |
| Qwen3.8-27B Quality (dense) | turbo | **3** | 29.5 | 2.79x | 92% |

## Why the MoE models want shallow depth

Acceptance probability at each speculative position, depth-3 run:

- Ornith: 95% → 30% → **1%** (position 3 is almost pure wasted compute)
- Qwen3.6 Speed: 84% → 46% → 24%
- Qwen3.6 Balance: 82% → 50% → 23%
- Qwen3.8 Speed (dense): 97% → 90% → **81%**
- Qwen3.8 Quality (dense): 99% → 97% → **92%**

The dense models' acceptance barely degrades with depth; the MoE models'
collapses sharply after position 1. Depth beyond where acceptance holds up
is pure overhead -- draft, verify, reject, repeat -- which is exactly why
Ornith at depth 3 is *slower* than plain autoregressive decode (42.2 vs
51.8 tok/s), not just slower than depth 1.

One thing this data does NOT explain: all five models here have identical
`mtp_num_hidden_layers: 1` (a single trained MTP head), so the shallow-depth
behavior isn't about having fewer MTP layers -- it's specifically about how
each architecture's single MTP head degrades when asked to predict further
from its own uncertain prior prediction, not the model type per se. Worth
re-testing if a model with `mtp_num_hidden_layers > 1` ever enters the mix.

## Applied config (Builder benchmark, `docs/IMPROVEMENTS_TODO.md`)

```
Ornith-1.5-35B-A3B       -- sustained -- kv off -- depth 1
Qwen3.6-35B-A3B Balance  -- sustained -- kv off -- depth 1
Qwen3.6-35B-A3B Speed    -- sustained -- kv off -- depth 1
Qwen3.8-27B Speed        -- turbo     -- kv q8  -- depth 3
Qwen3.8-27B Quality      -- turbo     -- kv q8  -- depth 3
```

kv off is safe RAM-wise for the MoE models specifically: their hybrid
linear-attention architecture (`full_attention_interval: 4`) means only
1-in-4 layers carry a real, growing KV cache, and both have only 2 KV
heads -- full-precision KV cache at 131K context is ~2.5GB, not the tens of
GB a classic dense-attention model would need at that window.
