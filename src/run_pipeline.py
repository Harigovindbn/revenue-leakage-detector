"""
Run the full Revenue Leakage Detector pipeline.

    python -m src.run_pipeline                       # regex extractor (free, default)
    python -m src.run_pipeline --extractor ollama    # local LLM (free, slower)
    python -m src.run_pipeline --extractor anthropic # Claude API (needs ANTHROPIC_API_KEY in .env)

Steps: 1 extract contract terms -> 2 match customers & contracts -> 3 detect leaks
       -> 4 evaluate against the answer key -> 5 export Power BI tables
"""

import argparse
import time

from src.common import ensure_output_dirs


def main() -> None:
    ap = argparse.ArgumentParser(description="Revenue Leakage Detector pipeline")
    ap.add_argument("--extractor", choices=["regex", "anthropic", "ollama"], default="regex",
                    help="which contract extractor feeds detection (default: regex)")
    ap.add_argument("--limit", type=int, default=None,
                    help="only extract the first N contracts (useful for trying an LLM)")
    ap.add_argument("--skip-eval", action="store_true", help="skip the evaluation step")
    args = ap.parse_args()

    ensure_output_dirs()
    t0 = time.time()

    # Imports are inside main() so each step stays an independent module.
    from src import detect_leaks, export_powerbi, extract_terms, match_customers

    print("\n=== 1. Extract contract terms (FR-01 to FR-03) ===")
    extract_terms.run(args.extractor, limit=args.limit)

    print("\n=== 2. Match invoice customers to contracts (FR-05, FR-12) ===")
    match_customers.run()

    print("\n=== 3. Detect leaks L1-L5 (FR-06 to FR-08) ===")
    detect_leaks.run()

    if not args.skip_eval:
        from src import evaluate
        print("\n=== 4. Evaluate against the answer key (NFR-01) ===")
        evaluate.run()

    print("\n=== 5. Export Power BI tables (FR-10, FR-11) ===")
    export_powerbi.run()

    print(f"\nPipeline finished in {time.time() - t0:.1f}s. Outputs are in ./output")


if __name__ == "__main__":
    main()
