"""Gold-set regression of the extraction pipeline (real LLM calls, cost incurred): `make golden`.

Needs `packages/domino_eval/gold/extract_030.jsonl` (labeled by a person, task T15) and the compose Redis for the
`budget:golden:<date>` counter. The metrics table is printed before the thresholds are checked, so a miss is still a
measurement (decision W2-13).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from domino_eval.regression import Metrics, assert_thresholds, run_extract_regression
from domino_extract.schema import PROMPT_VERSION

pytestmark = pytest.mark.golden

GOLD_PATH = Path(__file__).resolve().parents[2] / "packages" / "domino_eval" / "gold" / "extract_030.jsonl"


def _table(m: Metrics, model: str) -> str:
    return "\n".join(
        [
            f"gold regression: model={model} prompt_version={PROMPT_VERSION} chunks={m.n_chunks}",
            f"  grounding_rate         {m.grounding_rate:.3f}   (>= 0.95)",
            f"  rk_macro_f1            {m.rk_macro_f1:.3f}   (>= 0.80)",
            f"  dir_acc                {m.dir_acc:.3f}   (>= 0.85)",
            f"  task1_f1               {m.task1_f1:.3f}   (>= 0.85)",
            f"  triples_per_chunk      {m.triples_per_chunk:.3f}   (gold {m.gold_triples_per_chunk:.3f} +/-30%)",
            f"  conf_over_08_ratio     {m.conf_over_08_ratio:.3f}   (<= 0.60)",
            f"  causal_type_acc        {m.causal_type_acc:.3f}   (info)",
            f"  triples: gold {m.n_gold_triples}, predicted {m.n_pred_triples}, matched {m.n_matched}",
            f"  extract cost (API-equivalent) ${m.extract_cost_usd:.4f}",
        ]
    )


def test_extract_regression() -> None:
    if not GOLD_PATH.is_file():
        pytest.fail(
            f"make golden: gold file not found: {GOLD_PATH}\n"
            "It is labeled by a person (task T15): export candidates with "
            "`uv run python -m domino_eval.gold_export --lang ko --n 15 --out <file>`, label them, "
            "and save the 30 labeled lines as the path above. No LLM call was made.",
            pytrace=False,
        )
    from domino_core.settings import get_settings  # after the file check: a missing gold file must fail first

    model = get_settings().model_extract
    metrics = run_extract_regression(GOLD_PATH, model, PROMPT_VERSION)
    print("\n" + _table(metrics, model))
    assert_thresholds(metrics)
