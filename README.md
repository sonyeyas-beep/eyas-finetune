# order pipeline

`bot.py` takes orders, `orders.py` is the payment gate, `.github/workflows/finetune.yml` runs a job.

Setup (repository secrets of the pipeline repo): `GH_PAT` (read access to the private tools repo), `TOOLS_REPO`, `TOOLS_ENTRY`, `TELEGRAM_BOT_TOKEN`.
Bot host environment: see `.env.example`. Payment links and wallet come from the environment only.

Payment (v1): the client sends a screenshot or transaction id, the operator confirms with `/confirm <order>`. Nothing starts before that.
Run `python -m unittest discover -s tests`.
