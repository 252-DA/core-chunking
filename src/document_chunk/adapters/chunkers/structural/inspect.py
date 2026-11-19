from math import ceil
from .tokens import TokenCounter


def inspection_metrics(chunks, chunker, config):
    result = dict(getattr(chunker, "stats", {}))
    try:
        counter = getattr(chunker, "counter", None) or TokenCounter(config)
        values = sorted(counter.count_many([c.content for c in chunks]))
    except ValueError:
        # Legacy inspection remains available without a local tokenizer.
        return result
    if values:
        result.update(
            token_p50=values[ceil(len(values) * 0.5) - 1],
            token_p95=values[ceil(len(values) * 0.95) - 1],
            token_max=values[-1],
            chunks_over_max=sum(v > config.max_tokens for v in values),
            chunks_under_min=sum(v < config.min_tokens for v in values),
        )
    return result
