# FinPilot v0.5 — AI Financial Copilot

FinPilot v0.5 is a deployable ChatGPT-style financial decision product built around a deterministic financial safety engine. The AI can explain results, but it is never the source of truth for financial math.

## v0.5 features

- ChatGPT-style conversational UI.
- Explicit AI-agent tool registry: snapshot, safe-spending capacity, affordability, expenses, income, upcoming payments, and what-if simulation.
- Natural-language intent routing with short follow-up context.
- Deterministic 90-day forecast and affordability engine from the supplied demo dataset.
- What-if simulation for eligible recurring expenses.
- Dashboard with balance, safe-to-spend, minimum-balance and forecast cards.
- Expense bars and upcoming-event view.
- Optional OpenAI Responses API explanation layer.
- Supabase Auth for email/password accounts.
- Supabase RLS-backed persistent conversations/messages for signed-in users.
- Guest/demo mode still works without signing in.
- Vercel deployment configuration included.
- Optional local hostname `http://finpilot` setup script for Windows.
- No API key is stored in the repository.

## Run locally

```bash
python -m pip install -r requirements.txt
python run.py
```

Open `http://127.0.0.1:8000`.

### Optional local name: http://finpilot

Run PowerShell **as Administrator** and execute:

```powershell
.\tools\setup_finpilot_host.ps1
```

Then use `http://finpilot` while the local server is running.

## Supabase

The frontend is configured with a Supabase **publishable** key. Publishable/anon keys are designed to be used in browser apps; never place a Supabase service-role key in frontend code.

The required persistence tables and RLS policies are in `supabase.sql`. The schema was applied to the connected Supabase project when this package was prepared.

Signed-in users get persistent chat conversations. Financial demo profiles still come from `dataset/` and are not presented as live bank data.

## OpenAI

Without `OPENAI_API_KEY`, FinPilot works fully in deterministic mode.

To enable natural-language AI explanations locally, create `.env` from `.env.example` and add your server-side key:

```env
OPENAI_API_KEY=your_key_here
OPENAI_MODEL=gpt-5.6-luna
```

Never commit `.env` or an API key.

For Vercel, add `OPENAI_API_KEY` as a server-side environment variable in the Vercel project settings. `OPENAI_MODEL` is optional.

## Deploy to Vercel

This repository includes `api/index.py` and `vercel.json` for deployment. From the project directory, use the Vercel CLI or import the project into Vercel.

The generated Vercel URL will look like:

`https://finpilot-<deployment>.vercel.app`

A custom domain such as `https://finpilot...` can be connected later if you own an available domain. Common obvious names like `finpilot.ai` and `finpilot.app` were checked during preparation and were not available for purchase.

## Important safety boundary

This package uses the supplied challenge/demo financial dataset. It is not a live bank connection. Do not treat the demo outputs as personalized regulated financial advice.
