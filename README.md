# Phish Setlist Predictor

A machine learning project to predict Phish setlists based on phish.net API data.

## Setup

1. Copy `.env.example` to `.env` and add your phish.net API key from https://phish.net/api/keys
2. Install dependencies: `python -m uv sync`

## Usage

Run CLI commands via:

```bash
python -m uv run phishpred <command>
```

Available commands:
- `ingest` - Ingest data from phish.net API
- `refresh` - Refresh data
- `build-features` - Build feature engineering
- `backtest` - Backtest the model
- `predict` - Make predictions
