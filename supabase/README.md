# Supabase setup for v0.04

1. Create a Supabase project.
2. Open **SQL Editor** and run [`schema.sql`](schema.sql). The script is safe to re-run and recreates the user-scoped policies consistently.
3. In **Authentication → Providers**, enable Email.
4. Keep email confirmation enabled for public use.
5. Add the deployed Streamlit URL to **Authentication → URL Configuration → Redirect URLs**. Use the exact origin and path shown by Streamlit Community Cloud.
6. In Streamlit Community Cloud, add:

```toml
SUPABASE_URL = "https://YOUR_PROJECT.supabase.co"
SUPABASE_ANON_KEY = "YOUR_ANON_KEY"
```

Only use the public `anon` key. Never put the `service_role` key in the app or GitHub.

The application keeps the Supabase access and refresh session in browser local storage through a Streamlit Custom Component v2. The tokens are not written to application tables or query parameters. When an order is submitted, the `record_paper_order` RPC updates the authenticated user's cash and inserts the order in one database transaction.

After deployment, test with two separate email accounts. Account A must not be able to read or change Account B's watchlist, history, alerts, paper account, orders, plans, or reviews. If a table or policy is changed later, keep the `user_id = auth.uid()` condition in both the read and write policies.
